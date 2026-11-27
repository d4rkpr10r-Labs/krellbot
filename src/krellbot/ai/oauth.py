"""Sign in with OpenRouter: OAuth PKCE (S256) through a single-use loopback callback.

The flow, per OpenRouter's OAuth PKCE guide:

1. The browser opens ``https://openrouter.ai/auth?callback_url=...&code_challenge=...
   &code_challenge_method=S256``.
2. After the user approves, OpenRouter redirects to ``callback_url`` with ``?code=``.
3. The engine POSTs ``{code, code_verifier, code_challenge_method}`` to
   ``https://openrouter.ai/api/v1/auth/keys`` and reads the key from ``key``.

The key is stored through :class:`ConnectionStore` as an ``oauth`` connection,
so it lands only in the keychain namespace ``krellbot-ai:<id>``.

Binding a callback to its flow: OpenRouter's docs disagree on whether a
``state`` parameter comes back on the redirect, so the flow does not rely
on one. Each flow's callback path carries an unguessable token
(``/callback/<state>``) and the listener serves exactly one request on that
path. Any other path, method or ``Host`` is a 404 that leaves the flow
waiting. The listener binds ``127.0.0.1`` on an ephemeral port, is a
separate socket from the workstation API, and closes after one callback or
300 seconds.

Nothing here logs or returns the code, the verifier or the key. Flows live
in engine memory only.

Sign in with ChatGPT (M38.23) follows the owner's M38.22 record and lives in
the second half of this module. It registers a per-account OAuth client
with OpenAI: the first sign-in sends ``client_id=dynamic_agent_client``,
``agent_name_hint=Krellbot`` and this install's ``ext_agent_host_id`` (a
``urn:uuid:`` UUIDv4 persisted in ``<home>/ai/chatgpt.json`` before the
first sign-in); the callback returns the issued client id, which every later
sign-in, refresh and revocation uses. The callback is
``http://127.0.0.1:<port>/auth/callback`` and is bound to its attempt by
``state``. The ID token is verified against the JWKS that OpenAI's discovery
document names (RS256 with ``cryptography``; issuer, audience = issued
client id, expiry, nonce). Plan use needs ``chatgpt.tokens.use.direct`` in
the granted scopes. One credential record per issued client id goes to the
keychain at ``krellbot-ai:<connection_id>``; ``chatgpt.json`` holds only
non-secret state (host id, account-to-client mapping, plan and welcome
flags). The browser is given a loopback launch address that redirects to
OpenAI, so an authorize URL carrying ``id_token_hint`` never passes through
the workstation's API or scripts.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import http.server
import json
import math
import os
import re
import secrets as stdlib_secrets  # the standard library module, not krellbot.secrets
import socket
import socketserver
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import keyring
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from krellbot import paths
from krellbot.ai.connections import (
    _MAX_MODEL,
    Connection,
    ConnectionRefused,
    ConnectionStore,
    _clean_text,
    is_chatgpt,
    keychain_is_safe,
)
from krellbot.ai.http import FormTransport, HttpTransport, StreamFailure, TransportError
from krellbot.ai.openai_compat import OpenAICompatAdapter
from krellbot.ai.providers import PRESETS
from krellbot.ai.types import ChatAdapter, ProviderError

OPENROUTER_AUTHORIZE_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_PRESET = "openrouter"
CODE_CHALLENGE_METHOD = "S256"
SIGNIN_LIFETIME_SECONDS = 300
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PREFIX = "/callback/"
FLOW_ID_RE = re.compile(r"[0-9a-f]{32}")

_VERIFIER_BYTES = 64  # 86 base64url characters, inside RFC 7636's 43..128
_STATE_BYTES = 24
_EXCHANGE_TIMEOUT = 30.0
_MAX_EXCHANGE_BYTES = 64 * 1024
_MAX_CODE = 2048
_MAX_QUERY_FIELDS = 16
_KEEP_FINISHED_FLOWS = 16
_POLL_SECONDS = 0.25
_CONNECTION_BUDGET = 5.0  # seconds in all for one connection, so one slow client cannot hold the listener

SigninState = Literal["waiting", "connected", "failed", "expired"]

SIGNIN_MESSAGES: dict[str, str] = {
    "ai_signin_not_found": "There is no sign-in with that id. Start the sign-in again.",
    "ai_signin_listener_failed": "Krellbot could not open its sign-in address on this computer.",
    "ai_signin_expired": "The sign-in was not finished within 5 minutes. Start it again.",
    "ai_signin_superseded": "A newer sign-in replaced this one.",
    "ai_signin_cancelled": "The sign-in was cancelled, so nothing was stored.",
    "ai_signin_local_only": (
        "Sign in with OpenRouter from Krellbot on this computer. OpenRouter returns to this computer's "
        "own address, which a remote browser cannot reach."
    ),
    "ai_signin_no_code": "OpenRouter returned without a sign-in code, so nothing was stored. Start the sign-in again.",
    "ai_signin_rejected": "OpenRouter did not accept the sign-in, so nothing was stored. Start the sign-in again.",
    "ai_signin_unavailable": "OpenRouter could not be reached to finish the sign-in, so nothing was stored.",
    "ai_signin_bad_response": "OpenRouter sent a reply Krellbot could not read, so nothing was stored.",
    "ai_signin_failed": "The sign-in failed unexpectedly, so nothing was stored.",
    # Sign in with ChatGPT (M38.23).
    "ai_chatgpt_local_only": (
        "Continue with ChatGPT from Krellbot on this computer. OpenAI returns to this computer's own address, "
        "which a remote browser cannot reach."
    ),
    "ai_chatgpt_access_denied": (
        "The ChatGPT sign-in was declined, so nothing was stored. To use your ChatGPT plan, choose Continue with "
        "ChatGPT again and allow it, or connect an OpenAI API key instead."
    ),
    "ai_chatgpt_signin_error": "OpenAI ended the sign-in with an error, so nothing was stored. Start it again.",
    "ai_chatgpt_registration_incomplete": (
        "OpenAI did not return Krellbot's registration, so nothing was stored. Start the sign-in again."
    ),
    "ai_chatgpt_client_mismatch": (
        "OpenAI answered for a different registration than this connection's, so nothing was changed."
    ),
    "ai_chatgpt_account_mismatch": (
        "That sign-in was for a different ChatGPT account, so this connection was not changed."
    ),
    "ai_chatgpt_id_token_invalid": "OpenAI's sign-in answer could not be verified, so nothing was stored.",
    "ai_chatgpt_no_code": "OpenAI returned without a sign-in code, so nothing was stored. Start the sign-in again.",
    "ai_chatgpt_rejected": "OpenAI did not accept the sign-in, so nothing was stored. Start the sign-in again.",
    "ai_chatgpt_unavailable": "OpenAI could not be reached to finish the sign-in, so nothing was stored.",
    "ai_chatgpt_bad_response": "OpenAI sent a reply Krellbot could not read, so nothing was stored.",
}

_CLOSE_PAGE = (
    b'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>Krellbot</title></head>'
    b"<body><p>You can close this tab and return to Krellbot.</p></body></html>\n"
)
_NOT_FOUND_PAGE = b"Not found\n"
_BAD_REQUEST_PAGE = b"Bad request\n"


class SigninRefused(Exception):
    """A typed sign-in refusal. ``message`` is a fixed string and never echoes input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = SIGNIN_MESSAGES[code]


@dataclass(frozen=True)
class SigninFlow:
    id: str
    authorize_url: str
    # The per-flow callback path token. Not a credential: the PKCE verifier never leaves the engine.
    state: str


@dataclass(frozen=True)
class CallbackReply:
    status: int
    body: bytes
    # Stop listening once this reply is sent.
    close: bool = False
    # Run after the listener has closed (the key exchange), off the request path.
    after: Callable[[], None] | None = None
    # A redirect target (ChatGPT's launch address only). Never logged.
    location: str | None = field(default=None, repr=False)


# ``(method, request_target, host_header) -> reply``. Called on the listener's thread.
Dispatch = Callable[[str, str, str], CallbackReply]


class CallbackListener(Protocol):
    port: int

    def close(self) -> None: ...


# ``(dispatch, lifetime_seconds) -> listener``. Tests pass a socket-free fake.
ListenerFactory = Callable[[Dispatch, float], CallbackListener]


# ---- the loopback listener -----------------------------------------------------------


class _Server(socketserver.TCPServer):
    """One connection at a time, each with a total time budget, and a hard abort.

    The socket of the connection being served is tracked so another thread
    can hang it up: the per-connection budget timer, and :meth:`abort` (from
    ``close()`` or the lifetime timer). A socket is only ever shut down while
    it is still the active one, under ``_guard``, so a closed descriptor is
    never touched.
    """

    # TCPServer, not HTTPServer: HTTPServer.server_bind does a reverse DNS lookup.
    allow_reuse_address = False
    request_queue_size = 4

    def __init__(self, address: tuple[str, int], handler: type[socketserver.BaseRequestHandler]) -> None:
        self._guard = threading.Lock()
        self._active: socket.socket | None = None
        self._aborted = False
        self._closed = False
        super().__init__(address, handler)

    def process_request(self, request, client_address) -> None:
        with self._guard:
            admitted = not self._aborted
            if admitted:
                self._active = request
        if not admitted:
            self.shutdown_request(request)
            return
        # A total budget, not a per-read timeout: a client dribbling bytes cannot hold the listener.
        budget = threading.Timer(_CONNECTION_BUDGET, self._hang_up, (request,))
        budget.daemon = True
        budget.start()
        try:
            self.finish_request(request, client_address)
        finally:
            budget.cancel()
            with self._guard:
                self._active = None
            self.shutdown_request(request)

    def _hang_up(self, request: socket.socket) -> None:
        with self._guard:
            if self._active is request:
                with contextlib.suppress(OSError):
                    request.shutdown(socket.SHUT_RDWR)

    def abort(self) -> None:
        """Hang up the active connection and stop accepting, from any thread."""

        with self._guard:
            self._aborted = True
            if self._active is not None:
                with contextlib.suppress(OSError):
                    self._active.shutdown(socket.SHUT_RDWR)
            if not self._closed:
                # Wakes the serving thread and refuses new connections where the OS supports it
                # (Linux); elsewhere the serving thread closes the socket within one poll.
                with contextlib.suppress(OSError):
                    self.socket.shutdown(socket.SHUT_RDWR)

    def server_close(self) -> None:
        with self._guard:
            self._closed = True
            super().server_close()

    def handle_error(self, request, client_address) -> None:
        """Silent: a traceback must never reach a log."""


class LoopbackListener:
    """A one-shot HTTP listener on ``127.0.0.1:<ephemeral>`` in a daemon thread.

    Each request goes to ``dispatch``. A reply with ``close`` set stops the
    listener once that reply is sent; its ``after`` step runs on this thread
    once the socket is closed. ``close()`` and the ``lifetime`` timer stop it
    absolutely: the connection being served is hung up and the listening
    socket shut, so a slow or dribbling client cannot keep it open. One
    connection gets ``_CONNECTION_BUDGET`` seconds in all. Nothing is
    logged: the request line would carry the code.
    """

    def __init__(self, dispatch: Dispatch, lifetime: float) -> None:
        self._dispatch = dispatch
        self._stop = threading.Event()
        self._after: Callable[[], None] | None = None
        self._deadline = time.monotonic() + lifetime
        self._server = _Server((CALLBACK_HOST, 0), _handler_for(self))
        self.port: int = self._server.server_address[1]
        self._lifetime = threading.Timer(lifetime, self.close)
        self._lifetime.daemon = True
        self._thread = threading.Thread(target=self._run, name="krellbot-ai-signin", daemon=True)
        self._thread.start()
        self._lifetime.start()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def close(self) -> None:
        """Stop now, from any thread: the active connection is hung up and the port stops accepting."""

        self._stop.set()
        self._server.abort()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _answer(self, method: str, target: str, host: str) -> CallbackReply:
        try:
            reply = self._dispatch(method, target, host)
        except Exception:  # noqa: BLE001 — a dispatch bug must not crash or log; refuse quietly
            return CallbackReply(404, _NOT_FOUND_PAGE)
        if reply.close:
            # Not close(): the reply to this request must still be written. The loop ends after it.
            self._after = reply.after
            self._stop.set()
        return reply

    def _run(self) -> None:
        # Never surface socket errors (or request text) in a log.
        with contextlib.suppress(Exception):
            try:
                while not self._stop.is_set():
                    remaining = self._deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._server.timeout = min(remaining, _POLL_SECONDS)
                    self._server.handle_request()
            finally:
                self._lifetime.cancel()
                self._server.server_close()
        after, self._after = self._after, None
        if after is not None:
            with contextlib.suppress(Exception):  # the flow records its own failures
                after()


def _handler_for(listener: LoopbackListener) -> type[http.server.BaseHTTPRequestHandler]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        timeout = _CONNECTION_BUDGET  # per read; the server also caps the connection's total time
        server_version = "krellbot"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            """Silent: the request line carries the authorization code."""

        def do_GET(self) -> None:
            if self.request_version == "HTTP/0.9":
                # No browser sends it; a request line cut short by a hang-up parses as one.
                self._send(CallbackReply(404, _NOT_FOUND_PAGE))
                return
            self._send(listener._answer(self.command, self.path, self.headers.get("Host", "")))

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            # Any other method (501) is a plain 404. Bodies are fixed: nothing from the request is reflected.
            if code == 501:
                self._send(CallbackReply(404, _NOT_FOUND_PAGE))
            else:
                self._send(CallbackReply(code if 400 <= code < 600 else 400, _BAD_REQUEST_PAGE))

        def _send(self, reply: CallbackReply) -> None:
            self.close_connection = True
            self.send_response(reply.status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(reply.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Connection", "close")
            if reply.location is not None:
                self.send_header("Location", reply.location)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(reply.body)

    return _Handler


# ---- one flow ----------------------------------------------------------------------------


@dataclass(eq=False)
class _Flow:
    id: str
    state: str
    model: str
    started: float
    clock: Callable[[], float]
    transport: HttpTransport
    store: ConnectionStore
    verifier: str | None = field(repr=False)
    port: int | None = None
    listener: CallbackListener | None = field(default=None, repr=False)
    # "waiting" for the callback, "exchanging" once consumed, then a final state.
    phase: str = "waiting"
    connection_id: str | None = None
    # The typed refusal code once the flow failed or expired. Never the OAuth code.
    error_code: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def finished(self) -> bool:
        return self.phase in ("connected", "failed", "expired")

    def _expired_locked(self) -> bool:
        if self.phase == "waiting" and self.clock() - self.started >= SIGNIN_LIFETIME_SECONDS:
            self._finish_locked("expired", "ai_signin_expired")
            return True
        return False

    def _finish_locked(self, phase: str, error_code: str | None = None) -> None:
        self.phase = phase
        self.error_code = error_code
        self.verifier = None

    def dispatch(self, method: str, target: str, host: str) -> CallbackReply:
        """Serve one request. Only a GET on this flow's exact callback path, to its own host, consumes it."""

        with self.lock:
            if self._expired_locked():
                return CallbackReply(404, _NOT_FOUND_PAGE, close=True)
            if self.phase != "waiting":
                return CallbackReply(404, _NOT_FOUND_PAGE, close=True)
            if method != "GET" or self.port is None or host != f"{CALLBACK_HOST}:{self.port}":
                return CallbackReply(404, _NOT_FOUND_PAGE)
            path, _sep, query = target.partition("?")
            expected = CALLBACK_PREFIX + self.state
            if not hmac.compare_digest(path.encode("utf-8", "replace"), expected.encode("ascii")):
                return CallbackReply(404, _NOT_FOUND_PAGE)
            code = _code_from_query(query)
            if code is None:
                self._finish_locked("failed", "ai_signin_no_code")
                return CallbackReply(200, _CLOSE_PAGE, close=True)
            self.phase = "exchanging"
        return CallbackReply(200, _CLOSE_PAGE, close=True, after=lambda: self._exchange(code))

    def _exchange(self, code: str) -> None:
        with self.lock:
            verifier = self.verifier
            self.verifier = None
            if verifier is None:  # never expected: the flow was consumed exactly once
                self._finish_locked("failed", "ai_signin_failed")
                return
        try:
            key = _exchange_code(self.transport, code, verifier)
            conn = self.store.add(OPENROUTER_PRESET, model=self.model, credential=key, auth="oauth")
        except (SigninRefused, ConnectionRefused) as exc:
            outcome: tuple[str, str | None, str | None] = ("failed", exc.code, None)
        except Exception:  # noqa: BLE001 — exception text could carry the key; record a fixed code
            outcome = ("failed", "ai_signin_failed", None)
        else:
            outcome = ("connected", None, conn.id)
        with self.lock:
            self._finish_locked(outcome[0], outcome[1])
            self.connection_id = outcome[2]

    def status(self) -> dict:
        with self.lock:
            expired = self._expired_locked()
            phase, error_code, connection_id = self.phase, self.error_code, self.connection_id
        if expired:
            self.close_listener()
        if phase in ("waiting", "exchanging"):
            return {"state": "waiting"}
        if phase == "connected":
            return {"state": "connected", "connection_id": connection_id}
        error_code = error_code or "ai_signin_failed"
        message = SIGNIN_MESSAGES.get(error_code) or _refusal_message(error_code)
        return {"state": phase, "code": error_code, "message": message}

    def abandon(self, error_code: str) -> bool:
        """Fail a flow still waiting for its callback and close its listener.

        A flow whose callback already arrived (exchanging) or that finished
        is left as it is: returns False.
        """

        with self.lock:
            expired = self._expired_locked()
            abandoned = not expired and self.phase == "waiting"
            if abandoned:
                self._finish_locked("failed", error_code)
        if expired or abandoned:
            self.close_listener()
        return abandoned

    def close_listener(self) -> None:
        listener = self.listener
        if listener is not None:
            with contextlib.suppress(Exception):  # best effort: the listener also times out
                listener.close()


def _refusal_message(code: str) -> str:
    try:
        return ConnectionRefused(code).message
    except KeyError:
        return SIGNIN_MESSAGES["ai_signin_failed"]


def _code_from_query(query: str) -> str | None:
    """The one ``code`` parameter, if it is a plausible authorization code."""

    try:
        fields = parse_qs(query, keep_blank_values=True, max_num_fields=_MAX_QUERY_FIELDS)
    except ValueError:
        return None
    values = fields.get("code", [])
    if len(values) != 1:
        return None
    code = values[0]
    if not code or len(code) > _MAX_CODE or any(not ("!" <= ch <= "~") for ch in code):
        return None
    return code


def _exchange_code(transport: HttpTransport, code: str, verifier: str) -> str:
    """POST the code and verifier to OpenRouter; return the key. Errors are typed and never echo either."""

    body = {"code": code, "code_verifier": verifier, "code_challenge_method": CODE_CHALLENGE_METHOD}
    raw = bytearray()
    try:
        for chunk in transport.post_stream(OPENROUTER_KEYS_URL, body, {}, _EXCHANGE_TIMEOUT):
            raw += chunk
            if len(raw) > _MAX_EXCHANGE_BYTES:
                raise SigninRefused("ai_signin_bad_response")
    except TransportError as exc:
        status = exc.status
        rejected = status is not None and 400 <= status < 500 and status != 429
        raise SigninRefused("ai_signin_rejected" if rejected else "ai_signin_unavailable") from None
    try:
        parsed = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise SigninRefused("ai_signin_bad_response") from None
    key = parsed.get("key") if isinstance(parsed, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise SigninRefused("ai_signin_bad_response")
    return key


# ---- the registry -------------------------------------------------------------------------


class SigninRegistry:
    """Sign-in flows in engine memory. One waiting flow at a time; a new one replaces it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flows: dict[str, _Flow] = {}

    def add(self, flow: _Flow) -> None:
        with self._lock:
            older = list(self._flows.values())
            finished = [f for f in older if f.finished]
            for stale in finished[: max(0, len(finished) - _KEEP_FINISHED_FLOWS + 1)]:
                self._flows.pop(stale.id, None)
            self._flows[flow.id] = flow
        for other in older:
            other.abandon("ai_signin_superseded")

    def get(self, flow_id: object) -> _Flow:
        if not isinstance(flow_id, str) or FLOW_ID_RE.fullmatch(flow_id) is None:
            raise SigninRefused("ai_signin_not_found")
        with self._lock:
            flow = self._flows.get(flow_id)
        if flow is None:
            raise SigninRefused("ai_signin_not_found")
        return flow

    def close_all(self) -> None:
        with self._lock:
            flows = list(self._flows.values())
        for flow in flows:
            flow.abandon("ai_signin_cancelled")


_DEFAULT_REGISTRY = SigninRegistry()


# ---- entry points --------------------------------------------------------------------------


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def start_openrouter_signin(
    home: Path,
    *,
    model: str,
    clock: Callable[[], float],
    transport: HttpTransport,
    listener_factory: ListenerFactory | None = None,
    registry: SigninRegistry | None = None,
    allow_injected_fake_backend: bool = False,
) -> SigninFlow:
    """Open the loopback callback and return the URL the browser should visit.

    The model and the keychain are checked first, so a bad request never
    opens a listener. Blocks briefly on the keychain backend lookup: async
    callers use a worker thread.
    """

    clean_model = _clean_text(model, _MAX_MODEL)
    if clean_model is None:
        raise ConnectionRefused("ai_model_invalid")
    if not keychain_is_safe(keyring.get_keyring(), allow_injected_fake_backend=allow_injected_fake_backend):
        raise ConnectionRefused("ai_keychain_unavailable")

    verifier = stdlib_secrets.token_urlsafe(_VERIFIER_BYTES)
    flow = _Flow(
        id=stdlib_secrets.token_hex(16),
        state=stdlib_secrets.token_urlsafe(_STATE_BYTES),
        model=clean_model,
        started=clock(),
        clock=clock,
        transport=transport,
        store=ConnectionStore(home, allow_injected_fake_backend=allow_injected_fake_backend),
        verifier=verifier,
    )
    factory = listener_factory or LoopbackListener
    try:
        listener = factory(flow.dispatch, SIGNIN_LIFETIME_SECONDS)
    except OSError:
        raise SigninRefused("ai_signin_listener_failed") from None
    port = getattr(listener, "port", None)
    if not isinstance(port, int) or not 0 < port < 65536:
        listener.close()
        raise SigninRefused("ai_signin_listener_failed")
    with flow.lock:
        flow.port = port
        flow.listener = listener

    callback_url = f"http://{CALLBACK_HOST}:{port}{CALLBACK_PREFIX}{flow.state}"
    authorize_url = (
        OPENROUTER_AUTHORIZE_URL
        + "?"
        + urlencode(
            {
                "callback_url": callback_url,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": CODE_CHALLENGE_METHOD,
            }
        )
    )
    (registry or _DEFAULT_REGISTRY).add(flow)
    return SigninFlow(id=flow.id, authorize_url=authorize_url, state=flow.state)


def signin_status(flow_id: str, *, registry: SigninRegistry | None = None) -> dict:
    """``{state: waiting|connected|failed|expired}``, plus ``connection_id`` or a fixed ``code`` and ``message``."""

    return (registry or _DEFAULT_REGISTRY).get(flow_id).status()


def cancel_signin(flow_id: str, *, registry: SigninRegistry | None = None) -> tuple[bool, dict]:
    """Cancel a sign-in still waiting for its callback; its listener closes and it can never add a connection.

    A flow whose callback already arrived, or that finished, is left alone.
    Returns ``(cancelled, status)``.
    """

    flow = (registry or _DEFAULT_REGISTRY).get(flow_id)
    cancelled = flow.abandon("ai_signin_cancelled")
    return cancelled, flow.status()


# ==== Sign in with ChatGPT (M38.23) ===========================================================
#
# Built to the owner's M38.22 record. Endpoints and parameters are OpenAI's,
# from https://developers.openai.com/siwc/token-sharing-open-source and the
# pages under it.

CHATGPT_ISSUER = "https://auth.openai.com"
CHATGPT_AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
CHATGPT_TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
CHATGPT_DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
CHATGPT_RESOURCE = "https://api.openai.com/v1"
CHATGPT_SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
CHATGPT_PLAN_SCOPE = "chatgpt.tokens.use.direct"
CHATGPT_DYNAMIC_CLIENT = "dynamic_agent_client"
CHATGPT_AGENT_NAME = "Krellbot"
CHATGPT_PRESET = "openai"
CHATGPT_LABEL = "OpenAI (ChatGPT plan)"
CHATGPT_CALLBACK_PATH = "/auth/callback"
CHATGPT_LAUNCH_PREFIX = "/auth/start/"
# The only hosts a ChatGPT sign-in or plan request reaches: OpenAI's auth server and API.
CHATGPT_EGRESS_HOSTS = frozenset({"auth.openai.com", "api.openai.com"})
CHATGPT_STATE_FILE = "chatgpt.json"

if PRESETS[CHATGPT_PRESET].base_url != CHATGPT_RESOURCE:  # pragma: no cover — a preset edit must update both
    raise RuntimeError("the OpenAI preset and the ChatGPT resource disagree")

REVOCATION_NOT_CONFIRMED = (
    "Signed out on this computer, but remote revocation was not confirmed: OpenAI could not be reached. "
    "You can disconnect Krellbot in ChatGPT settings."
)

_HOST_ID_RE = re.compile(r"urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_STATE_SCHEMA = "1"
_MAX_TOKEN = 32 * 1024
_MAX_CLIENT_ID = 256
_MAX_SUBJECT = 256
_MAX_EMAIL = 320
_MAX_SCOPES = 32
_DEFAULT_EXPIRES_IN = 3600  # OpenAI's documented access-token lifetime
_REFRESH_MARGIN = 300.0  # refresh this long before the access token expires
_ID_TOKEN_LEEWAY = 60.0  # clock skew allowed on the ID token's expiry
_DISCOVERY_TIMEOUT = 15.0
_REVOKE_ATTEMPTS = 3
_REVOKE_DELAYS: tuple[float, ...] = (0.5, 1.5)  # between attempts; bounded
_REVOKE_TIMEOUT = 10.0  # per attempt, so an unreachable OpenAI delays a sign-out by tens of seconds at most
_MIN_RSA_BITS = 2048
_UNUSABLE_REFRESH_CODES = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)
_REDIRECT_PAGE = b"Continue to OpenAI\n"

PlanState = Literal["enabled", "disabled"]
Revocation = Literal["confirmed", "not_confirmed", "none"]


@dataclass(frozen=True)
class RevocationResult:
    """How a sign-out or removal ended at OpenAI. ``message`` is fixed text, empty when nothing needs saying."""

    revocation: Revocation
    message: str = ""


# ---- non-secret state: <home>/ai/chatgpt.json -------------------------------------------------

_STATE_LOCKS: dict[str, threading.RLock] = {}
_RECORD_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _state_lock(home: Path) -> threading.RLock:
    key = os.path.abspath(home)
    with _LOCKS_GUARD:
        return _STATE_LOCKS.setdefault(key, threading.RLock())


def _record_lock(home: Path, conn_id: str) -> threading.RLock:
    """One lock per connection: refreshes, sign-outs and reauthorizations of one record never interleave."""

    key = (os.path.abspath(home), conn_id)
    with _LOCKS_GUARD:
        return _RECORD_LOCKS.setdefault(key, threading.RLock())


def _state_path(home: Path) -> Path:
    return Path(home) / "ai" / CHATGPT_STATE_FILE


def _empty_state() -> dict:
    return {"schema_version": _STATE_SCHEMA, "ext_agent_host_id": None, "accounts": [], "connections": {}}


def _load_state(home: Path) -> dict:
    """The validated state, or a fresh one when the file does not exist. An unreadable file is refused, never replaced."""

    try:
        raw = _state_path(home).read_bytes()
    except FileNotFoundError:
        return _empty_state()
    except OSError:
        raise ConnectionRefused("ai_chatgpt_state_unreadable") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise ConnectionRefused("ai_chatgpt_state_unreadable") from None
    if not isinstance(data, dict):
        raise ConnectionRefused("ai_chatgpt_state_unreadable")
    host_id = data.get("ext_agent_host_id")
    if host_id is not None and (not isinstance(host_id, str) or _HOST_ID_RE.fullmatch(host_id) is None):
        raise ConnectionRefused("ai_chatgpt_state_unreadable")
    state = _empty_state()
    state["ext_agent_host_id"] = host_id
    for account in data.get("accounts") or []:
        if isinstance(account, dict) and all(
            isinstance(account.get(k), str) for k in ("client_id", "issuer", "subject")
        ):
            state["accounts"].append({k: account[k] for k in ("client_id", "issuer", "subject")})
    connections = data.get("connections")
    for conn_id, entry in connections.items() if isinstance(connections, dict) else []:
        if (
            isinstance(conn_id, str)
            and isinstance(entry, dict)
            and isinstance(entry.get("client_id"), str)
            and entry.get("plan") in ("enabled", "disabled")
            and isinstance(entry.get("signed_in"), bool)
            and isinstance(entry.get("welcome_shown"), bool)
            and (entry.get("account") is None or isinstance(entry.get("account"), str))
        ):
            state["connections"][conn_id] = {
                "client_id": entry["client_id"],
                "plan": entry["plan"],
                "signed_in": entry["signed_in"],
                "welcome_shown": entry["welcome_shown"],
                "account": entry.get("account"),
            }
    return state


def _save_state(home: Path, state: dict) -> None:
    folder = Path(home) / "ai"
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(folder, 0o700)
    paths.atomic_write(_state_path(home), (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"))


@contextlib.contextmanager
def _state_change(home: Path) -> Iterator[dict]:
    """Read, change and write the state under its lock."""

    with _state_lock(home):
        state = _load_state(home)
        yield state
        _save_state(home, state)


def chatgpt_host_id(home: Path) -> str:
    """This install's ``ext_agent_host_id``: a ``urn:uuid:`` UUIDv4 made once and reused for every sign-in.

    Random, so never derived from the account. Not a secret: it lives in
    ``chatgpt.json``, not the keychain.
    """

    with _state_lock(home):
        state = _load_state(home)
        host_id = state["ext_agent_host_id"]
        if host_id is None:
            host_id = f"urn:uuid:{uuid.uuid4()}"
            state["ext_agent_host_id"] = host_id
            _save_state(home, state)
        return host_id


def _set_connection_state(home: Path, conn_id: str, **changes: object) -> None:
    with _state_change(home) as state:
        entry = state["connections"].get(conn_id)
        if entry is not None:
            entry.update(changes)


def chatgpt_connection_view(home: Path, conn_id: str) -> dict | None:
    """What the workstation may know about a ChatGPT connection: no token, no client id.

    ``welcome`` is true until the "You're using your ChatGPT plan" welcome
    was shown for this connection's first plan-enabled sign-in. Reads
    ``chatgpt.json`` only; never the keychain.
    """

    try:
        entry = _load_state(home)["connections"].get(conn_id)
    except ConnectionRefused:
        return None
    if entry is None:
        return None
    return {
        "plan": entry["plan"],
        "signed_in": entry["signed_in"],
        "welcome": entry["plan"] == "enabled" and entry["signed_in"] and not entry["welcome_shown"],
        "account": entry["account"],
    }


def chatgpt_plan_in_use(home: Path, conn: Connection) -> bool:
    """True when requests on ``conn`` use the person's ChatGPT plan (signed in, plan use allowed)."""

    if not is_chatgpt(conn):
        return False
    view = chatgpt_connection_view(home, conn.id)
    return view is not None and view["plan"] == "enabled" and view["signed_in"]


def mark_chatgpt_welcome_shown(home: Path, conn_id: str) -> bool:
    """Remember that this connection's welcome was shown. Returns False when it already was."""

    with _state_lock(home):
        state = _load_state(home)
        entry = state["connections"].get(conn_id)
        if entry is None:
            raise ConnectionRefused("ai_chatgpt_not_chatgpt")
        if entry["welcome_shown"]:
            return False
        entry["welcome_shown"] = True
        _save_state(home, state)
        return True


def _upsert_account(state: dict, client_id: str, issuer: str, subject: str) -> None:
    """Keep the account-to-client mapping (it outlives sign-out and removal)."""

    for account in state["accounts"]:
        if account["client_id"] == client_id:
            account.update(issuer=issuer, subject=subject)
            return
    state["accounts"].append({"client_id": client_id, "issuer": issuer, "subject": subject})


# ---- the credential record (keychain) ---------------------------------------------------------


def _iso(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_value(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_TOKEN:
        return None
    if any(not ("!" <= ch <= "~") for ch in value):
        return None
    return value


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _timestamp(value: object) -> float | None:
    """``earliest_refresh_at``: OpenAI documents the field, not its form; Unix seconds or an ISO 8601 time."""

    number = _number(value)
    if number is not None:
        return number
    if isinstance(value, str) and 0 < len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _valid_record(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    for key in ("client_id", "issuer", "subject", "ext_agent_host_id"):
        if not isinstance(data.get(key), str) or not data[key]:
            return None
    for key in ("email", "id_token", "access_token", "refresh_token"):
        if data.get(key) is not None and not isinstance(data.get(key), str):
            return None
    scopes = data.get("scopes")
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        return None
    for key in ("expires_at", "earliest_refresh_at"):
        if data.get(key) is not None and _number(data.get(key)) is None:
            return None
    return data


def _read_record(store: ConnectionStore, conn: Connection) -> dict:
    """The connection's credential record. A missing or unreadable one means sign in again."""

    raw = store.credential(conn.id)
    if not raw:
        raise ConnectionRefused("ai_chatgpt_signin_required")
    try:
        record = _valid_record(json.loads(raw))
    except (ValueError, RecursionError):
        record = None
    if record is None:
        raise ConnectionRefused("ai_chatgpt_signin_required")
    return record


def _write_record(store: ConnectionStore, conn_id: str, record: dict) -> None:
    """The whole token set in one keychain write."""

    store.replace_record(conn_id, json.dumps(record, sort_keys=True))


def _cleared(record: dict, *, keep_id_token: bool) -> dict:
    out = dict(record, access_token=None, refresh_token=None, expires_at=None, earliest_refresh_at=None)
    if not keep_id_token:
        out["id_token"] = None
    return out


# ---- talking to OpenAI's auth server -------------------------------------------------------------


def _egress_allowed(url: object, *, host: str = "auth.openai.com") -> bool:
    """An https URL on one of OpenAI's two hosts, with nothing that could smuggle a credential elsewhere."""

    if not isinstance(url, str) or len(url) > 2048 or not url.isprintable():
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == host
        and host in CHATGPT_EGRESS_HOSTS
        and port is None
        and parts.username is None
        and parts.password is None
        and not parts.fragment
    )


def _post_form(
    transport: FormTransport, url: str, fields: dict[str, str], *, timeout: float = _EXCHANGE_TIMEOUT
) -> tuple[int, bytes]:
    if not _egress_allowed(url):
        raise TransportError(None, "host not allowed")
    return transport.post_form(url, fields, {"accept": "application/json"}, timeout)


def _discovery(transport: FormTransport) -> dict:
    """OpenAI's discovery document: its issuer, JWKS and revocation endpoint, each checked."""

    doc = transport.get_json(CHATGPT_DISCOVERY_URL, {"accept": "application/json"}, _DISCOVERY_TIMEOUT)
    if not isinstance(doc, dict) or doc.get("issuer") != CHATGPT_ISSUER or not _egress_allowed(doc.get("jwks_uri")):
        raise SigninRefused("ai_chatgpt_bad_response")
    revocation = doc.get("revocation_endpoint")
    return {
        "jwks_uri": doc["jwks_uri"],
        "revocation_endpoint": revocation if _egress_allowed(revocation) else None,
    }


def _signin_failure(exc: TransportError) -> SigninRefused:
    status = exc.status
    rejected = status is not None and 400 <= status < 500 and status != 429
    return SigninRefused("ai_chatgpt_rejected" if rejected else "ai_chatgpt_unavailable")


def _oauth_error_code(body: object) -> str | None:
    """The machine-readable code of an OAuth error body: ``error`` as a string, or ``error.code``."""

    try:
        parsed = json.loads(body) if isinstance(body, str) and body else None
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    err = parsed.get("error")
    code = err.get("code") if isinstance(err, dict) else err
    if code is None:
        code = parsed.get("code")
    return code if isinstance(code, str) and len(code) <= 128 else None


def _parse_token_set(raw: bytes, *, need_id_token: bool) -> dict:
    """A token-endpoint reply, validated. Raises SigninRefused("ai_chatgpt_bad_response")."""

    try:
        parsed = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise SigninRefused("ai_chatgpt_bad_response") from None
    if not isinstance(parsed, dict):
        raise SigninRefused("ai_chatgpt_bad_response")
    access = _token_value(parsed.get("access_token"))
    id_token = _token_value(parsed.get("id_token"))
    refresh = _token_value(parsed.get("refresh_token")) if parsed.get("refresh_token") is not None else None
    token_type = parsed.get("token_type", "Bearer")
    if access is None or (need_id_token and id_token is None) or not isinstance(token_type, str):
        raise SigninRefused("ai_chatgpt_bad_response")
    if token_type.lower() != "bearer":
        raise SigninRefused("ai_chatgpt_bad_response")
    expires_in = parsed.get("expires_in", _DEFAULT_EXPIRES_IN)
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        raise SigninRefused("ai_chatgpt_bad_response")
    scope = parsed.get("scope")
    scopes = tuple(sorted(set(scope.split()))) if isinstance(scope, str) else None
    if scopes is not None and len(scopes) > _MAX_SCOPES:
        raise SigninRefused("ai_chatgpt_bad_response")
    return {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "expires_in": expires_in,
        "scopes": scopes,
        "earliest_refresh_at": _timestamp(parsed.get("earliest_refresh_at")),
    }


# ---- ID-token validation (RS256 over the published JWKS) -------------------------------------------


def _b64url(text: object) -> bytes | None:
    if not isinstance(text, str) or len(text) > _MAX_TOKEN or not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        return None


def _b64url_json(text: str) -> dict | None:
    raw = _b64url(text)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _rsa_key(jwk: dict) -> rsa.RSAPublicKey | None:
    n_bytes, e_bytes = _b64url(jwk.get("n")), _b64url(jwk.get("e"))
    if not n_bytes or not e_bytes:
        return None
    n, e = int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")
    if n.bit_length() < _MIN_RSA_BITS or e < 3:
        return None
    try:
        return rsa.RSAPublicNumbers(e, n).public_key()
    except ValueError:
        return None


def verify_id_token(token: object, *, keys: object, client_id: str, nonce: str, now: float) -> dict:
    """Verify an OpenAI ID token and return its claims. Raises SigninRefused("ai_chatgpt_id_token_invalid").

    The signature is RS256 by a key in OpenAI's JWKS; the issuer is OpenAI's;
    the audience is the issued client id; it has not expired; and its nonce
    is this attempt's.
    """

    invalid = SigninRefused("ai_chatgpt_id_token_invalid")
    if not isinstance(token, str) or token.count(".") != 2:
        raise invalid
    head_b64, body_b64, sig_b64 = token.split(".")
    header, claims, signature = _b64url_json(head_b64), _b64url_json(body_b64), _b64url(sig_b64)
    if header is None or claims is None or not signature or header.get("alg") != "RS256" or "crit" in header:
        raise invalid
    kid = header.get("kid")
    candidates = [
        k
        for k in (keys if isinstance(keys, list) else [])
        if isinstance(k, dict)
        and k.get("kty") == "RSA"
        and k.get("use", "sig") == "sig"
        and k.get("alg", "RS256") == "RS256"
        and (kid is None or k.get("kid") == kid)
    ]
    signing_input = f"{head_b64}.{body_b64}".encode("ascii")
    verified = False
    for jwk in candidates:
        key = _rsa_key(jwk)
        if key is None:
            continue
        try:
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            continue
        verified = True
        break
    if not verified:
        raise invalid
    if claims.get("iss") != CHATGPT_ISSUER:
        raise invalid
    aud = claims.get("aud")
    if isinstance(aud, str):
        audience_ok = aud == client_id
    elif isinstance(aud, list) and aud and all(isinstance(a, str) for a in aud):
        audience_ok = client_id in aud and (len(aud) == 1 or claims.get("azp") == client_id)
    else:
        audience_ok = False
    if not audience_ok:
        raise invalid
    exp = _number(claims.get("exp"))
    if exp is None or now >= exp + _ID_TOKEN_LEEWAY:
        raise invalid
    got_nonce = claims.get("nonce")
    if not isinstance(got_nonce, str) or not hmac.compare_digest(got_nonce.encode("utf-8"), nonce.encode("utf-8")):
        raise invalid
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > _MAX_SUBJECT or not sub.isprintable():
        raise invalid
    return claims


def _plausible_client_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_CLIENT_ID
        and all("!" <= ch <= "~" for ch in value)
        and value != CHATGPT_DYNAMIC_CLIENT
    )


def _email(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= _MAX_EMAIL and value.isprintable() and "@" in value:
        return value
    return None


# ---- one ChatGPT sign-in -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reauth:
    """A sign-in again for one existing connection, with that account's issued client."""

    conn_id: str
    client_id: str
    issuer: str
    subject: str
    email: str | None = field(repr=False)
    id_token: str | None = field(repr=False)
    # Plan use was not granted before: ask for consent again (OpenAI's "enable plan usage" path).
    enable_plan: bool = False


@dataclass(eq=False)
class _ChatGPTFlow(_Flow):
    nonce: str = field(default="", repr=False)
    # The unguessable path segment of the loopback launch address.
    launch: str = field(default="", repr=False)
    host_id: str = ""
    target: _Reauth | None = None
    # May carry id_token_hint: kept in engine memory, given only to the browser's redirect, never logged.
    authorize_url: str | None = field(default=None, repr=False)
    plan: PlanState | None = None

    def _finish_locked(self, phase: str, error_code: str | None = None) -> None:
        super()._finish_locked(phase, error_code)
        self.authorize_url = None
        self.nonce = ""

    def _fail_locked(self, code: str) -> CallbackReply:
        self._finish_locked("failed", code)
        return CallbackReply(200, _CLOSE_PAGE, close=True)

    def dispatch(self, method: str, target: str, host: str) -> CallbackReply:
        """The launch redirect, then exactly one callback on ``/auth/callback`` carrying this attempt's ``state``."""

        with self.lock:
            if self._expired_locked() or self.phase != "waiting":
                return CallbackReply(404, _NOT_FOUND_PAGE, close=True)
            if method != "GET" or self.port is None or host != f"{CALLBACK_HOST}:{self.port}":
                return CallbackReply(404, _NOT_FOUND_PAGE)
            path, _sep, query = target.partition("?")
            launch = (CHATGPT_LAUNCH_PREFIX + self.launch).encode("ascii")
            if hmac.compare_digest(path.encode("utf-8", "replace"), launch):
                if query or self.authorize_url is None:
                    return CallbackReply(404, _NOT_FOUND_PAGE)
                return CallbackReply(302, _REDIRECT_PAGE, location=self.authorize_url)
            if path != CHATGPT_CALLBACK_PATH:
                return CallbackReply(404, _NOT_FOUND_PAGE)
            try:
                fields = parse_qs(query, keep_blank_values=True, max_num_fields=_MAX_QUERY_FIELDS)
            except ValueError:
                return CallbackReply(404, _NOT_FOUND_PAGE)
            states = fields.get("state", [])
            if len(states) != 1 or not hmac.compare_digest(
                states[0].encode("utf-8", "replace"), self.state.encode("ascii")
            ):
                return CallbackReply(404, _NOT_FOUND_PAGE)  # not this attempt's answer: keep waiting
            errors = fields.get("error", [])
            if errors:
                return self._fail_locked(
                    "ai_chatgpt_access_denied" if errors == ["access_denied"] else "ai_chatgpt_signin_error"
                )
            issued = fields.get("client_id", [])
            if self.target is None:
                client_id = issued[0] if len(issued) == 1 else None
                if not _plausible_client_id(client_id):
                    return self._fail_locked("ai_chatgpt_registration_incomplete")
            else:
                saved = self.target.client_id.encode("utf-8")
                if len(issued) > 1 or (issued and not hmac.compare_digest(issued[0].encode("utf-8", "replace"), saved)):
                    return self._fail_locked("ai_chatgpt_client_mismatch")
                client_id = self.target.client_id
            code = _code_from_query(query)
            if code is None:
                return self._fail_locked("ai_chatgpt_no_code")
            self.phase = "exchanging"
            chosen: str = client_id  # type: ignore[assignment]
        return CallbackReply(200, _CLOSE_PAGE, close=True, after=lambda: self._exchange(code, chosen))

    def _exchange(self, code: str, client_id: str = "") -> None:  # type: ignore[override]
        with self.lock:
            verifier, nonce = self.verifier, self.nonce
            self.verifier = None
            if verifier is None or not nonce:  # never expected: the flow was consumed exactly once
                self._finish_locked("failed", "ai_signin_failed")
                return
        try:
            conn_id, plan = self._complete(code, client_id, verifier, nonce)
        except (SigninRefused, ConnectionRefused) as exc:
            outcome: tuple[str, str | None, str | None, PlanState | None] = ("failed", exc.code, None, None)
        except Exception:  # noqa: BLE001 — exception text could carry a token; record a fixed code
            outcome = ("failed", "ai_signin_failed", None, None)
        else:
            outcome = ("connected", None, conn_id, plan)
        with self.lock:
            self._finish_locked(outcome[0], outcome[1])
            self.connection_id = outcome[2]
            self.plan = outcome[3]

    def _complete(self, code: str, client_id: str, verifier: str, nonce: str) -> tuple[str, PlanState]:
        transport: FormTransport = self.transport  # type: ignore[assignment]
        redirect_uri = f"http://{CALLBACK_HOST}:{self.port}{CHATGPT_CALLBACK_PATH}"
        try:
            _status, raw = _post_form(
                transport,
                CHATGPT_TOKEN_URL,
                {
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": redirect_uri,
                    "resource": CHATGPT_RESOURCE,
                },
            )
            tokens = _parse_token_set(raw, need_id_token=True)
            discovery = _discovery(transport)
            jwks = transport.get_json(discovery["jwks_uri"], {"accept": "application/json"}, _DISCOVERY_TIMEOUT)
        except TransportError as exc:
            raise _signin_failure(exc) from None
        now = self.clock()
        keys = jwks.get("keys") if isinstance(jwks, dict) else None
        claims = verify_id_token(tokens["id_token"], keys=keys, client_id=client_id, nonce=nonce, now=now)
        scopes = tokens["scopes"] or ()
        plan: PlanState = "enabled" if CHATGPT_PLAN_SCOPE in scopes else "disabled"
        email = _email(claims.get("email"))
        if self.target is not None:
            if claims["sub"] != self.target.subject or claims["iss"] != self.target.issuer:
                raise SigninRefused("ai_chatgpt_account_mismatch")
            email = email or self.target.email
        record = {
            "email": email,
            "issuer": claims["iss"],
            "subject": claims["sub"],
            "client_id": client_id,
            "ext_agent_host_id": self.host_id,
            "id_token": tokens["id_token"],
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "token_type": "Bearer",
            "expires_in": tokens["expires_in"],
            "expires_at": now + tokens["expires_in"],
            "earliest_refresh_at": tokens["earliest_refresh_at"],
            "scopes": list(scopes),
            "saved_at": _iso(now),
        }
        home = self.store.home
        if self.target is None:
            conn = self.store.add_record(
                CHATGPT_PRESET, model=self.model, record=json.dumps(record, sort_keys=True), label=CHATGPT_LABEL
            )
            try:
                with _state_change(home) as state:
                    _upsert_account(state, client_id, record["issuer"], record["subject"])
                    state["connections"][conn.id] = {
                        "client_id": client_id,
                        "plan": plan,
                        "signed_in": True,
                        "welcome_shown": False,
                        "account": email,
                    }
            except BaseException:
                with contextlib.suppress(Exception):
                    self.store.remove(conn.id)
                raise
            return conn.id, plan
        conn_id = self.target.conn_id
        with _record_lock(home, conn_id):
            if not is_chatgpt(self.store.get(conn_id)):
                raise ConnectionRefused("ai_chatgpt_not_chatgpt")
            _write_record(self.store, conn_id, record)
            with _state_change(home) as state:
                _upsert_account(state, client_id, record["issuer"], record["subject"])
                entry = state["connections"].setdefault(
                    conn_id, {"client_id": client_id, "welcome_shown": False, "account": None}
                )
                entry.update(client_id=client_id, plan=plan, signed_in=True, account=email)
        return conn_id, plan

    def status(self) -> dict:
        out = super().status()
        with self.lock:
            plan = self.plan
        if out["state"] == "connected" and plan is not None:
            out["plan"] = plan
        return out


def start_chatgpt_signin(
    home: Path,
    *,
    model: str | None,
    clock: Callable[[], float],
    transport: FormTransport,
    listener_factory: ListenerFactory | None = None,
    registry: SigninRegistry | None = None,
    connection_id: str | None = None,
    allow_injected_fake_backend: bool = False,
) -> SigninFlow:
    """Open the loopback callback and return the address the browser should open.

    Without ``connection_id`` this registers a new client for the account the
    person picks (``dynamic_agent_client``, ``agent_name_hint=Krellbot``).
    With it, it signs that connection in again with its saved issued client
    id, no ``agent_name_hint``, and ``id_token_hint`` / ``login_hint`` when
    retained. The returned ``authorize_url`` is the listener's launch address
    on ``127.0.0.1``, which redirects to OpenAI; the OpenAI URL itself stays in
    the engine. The model, the connection, the keychain and the host id are
    checked first, so a bad request never opens a listener. Blocks briefly on
    the keychain: async callers use a worker thread.
    """

    store = ConnectionStore(home, allow_injected_fake_backend=allow_injected_fake_backend)
    conn: Connection | None = None
    if connection_id is not None:
        conn = store.get(connection_id)
        if not is_chatgpt(conn):
            raise ConnectionRefused("ai_chatgpt_not_chatgpt")
        clean_model = conn.model
    else:
        clean = _clean_text(model, _MAX_MODEL)
        if clean is None:
            raise ConnectionRefused("ai_model_invalid")
        clean_model = clean
    if not keychain_is_safe(keyring.get_keyring(), allow_injected_fake_backend=allow_injected_fake_backend):
        raise ConnectionRefused("ai_keychain_unavailable")
    target: _Reauth | None = None
    if conn is not None:
        with _record_lock(store.home, conn.id):
            record = _read_record(store, conn)
        target = _Reauth(
            conn_id=conn.id,
            client_id=record["client_id"],
            issuer=record["issuer"],
            subject=record["subject"],
            email=_email(record.get("email")),
            id_token=_token_value(record.get("id_token")),
            enable_plan=CHATGPT_PLAN_SCOPE not in record["scopes"],
        )
    host_id = chatgpt_host_id(home)  # persisted before the first sign-in; an unreadable file refuses here

    verifier = stdlib_secrets.token_urlsafe(_VERIFIER_BYTES)
    flow = _ChatGPTFlow(
        id=stdlib_secrets.token_hex(16),
        state=stdlib_secrets.token_urlsafe(_STATE_BYTES),
        model=clean_model,
        started=clock(),
        clock=clock,
        transport=transport,  # type: ignore[arg-type]
        store=store,
        verifier=verifier,
        nonce=stdlib_secrets.token_urlsafe(_STATE_BYTES),
        launch=stdlib_secrets.token_urlsafe(_STATE_BYTES),
        host_id=host_id,
        target=target,
    )
    factory = listener_factory or LoopbackListener
    try:
        listener = factory(flow.dispatch, SIGNIN_LIFETIME_SECONDS)
    except OSError:
        raise SigninRefused("ai_signin_listener_failed") from None
    port = getattr(listener, "port", None)
    if not isinstance(port, int) or not 0 < port < 65536:
        listener.close()
        raise SigninRefused("ai_signin_listener_failed")

    params = {
        "response_type": "code",
        "client_id": CHATGPT_DYNAMIC_CLIENT if target is None else target.client_id,
        "redirect_uri": f"http://{CALLBACK_HOST}:{port}{CHATGPT_CALLBACK_PATH}",
        "scope": CHATGPT_SCOPE,
        "resource": CHATGPT_RESOURCE,
        "state": flow.state,
        "nonce": flow.nonce,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": CODE_CHALLENGE_METHOD,
    }
    if target is None:
        params["agent_name_hint"] = CHATGPT_AGENT_NAME
    else:
        if target.id_token is not None:
            params["id_token_hint"] = target.id_token
        if target.email is not None:
            params["login_hint"] = target.email
        if target.enable_plan:
            # Enabling plan use after it was declined repeats OAuth asking for consent (OpenAI's
            # documented prompt=consent, until force_reconsent is confirmed for this integration).
            params["prompt"] = "consent"
    params["ext_agent_host_id"] = host_id
    with flow.lock:
        flow.port = port
        flow.listener = listener
        flow.authorize_url = CHATGPT_AUTHORIZE_URL + "?" + urlencode(params, quote_via=quote)
    (registry or _DEFAULT_REGISTRY).add(flow)
    launch_url = f"http://{CALLBACK_HOST}:{port}{CHATGPT_LAUNCH_PREFIX}{flow.launch}"
    return SigninFlow(id=flow.id, authorize_url=launch_url, state=flow.state)


# ---- the token lifecycle ---------------------------------------------------------------------------


def _needs_refresh(record: dict, now: float) -> bool:
    """Near expiry, but never before ``earliest_refresh_at`` while the access token still works."""

    expires_at = _number(record.get("expires_at"))
    if expires_at is None:
        return True
    if now < expires_at - _REFRESH_MARGIN:
        return False
    earliest = _number(record.get("earliest_refresh_at"))
    return not (earliest is not None and now < earliest and now < expires_at)


def _refresh(store: ConnectionStore, conn: Connection, record: dict, transport: FormTransport, now: float) -> dict:
    """Rotate the token set with the issued client id and ``resource`` (no ``scope``). Caller holds the record lock."""

    refresh_token = record.get("refresh_token")
    expires_at = _number(record.get("expires_at"))
    if not refresh_token:
        if expires_at is not None and now < expires_at:
            return record  # nothing to refresh with yet; the access token still works
        _write_record(store, conn.id, _cleared(record, keep_id_token=True))
        _set_connection_state(store.home, conn.id, signed_in=False)
        raise ConnectionRefused("ai_chatgpt_signin_required")
    fields = {
        "grant_type": "refresh_token",
        "client_id": record["client_id"],
        "refresh_token": refresh_token,
        "resource": CHATGPT_RESOURCE,
    }
    try:
        _status, raw = _post_form(transport, CHATGPT_TOKEN_URL, fields)
    except TransportError as exc:
        code = _oauth_error_code(exc.body) if exc.status is not None and exc.status < 500 else None
        if code is not None and (code in _UNUSABLE_REFRESH_CODES or code.startswith("refresh_token_")):
            # Unusable: clear the tokens (the retained ID token stays, as the next sign-in's hint).
            _write_record(store, conn.id, _cleared(record, keep_id_token=True))
            _set_connection_state(store.home, conn.id, signed_in=False)
            raise ConnectionRefused("ai_chatgpt_signin_required") from None
        if code == "invalid_client":
            raise ConnectionRefused("ai_chatgpt_client_invalid") from None
        # A network or server failure never erases credentials.
        raise ConnectionRefused("ai_chatgpt_refresh_unavailable") from None
    try:
        tokens = _parse_token_set(raw, need_id_token=False)
    except SigninRefused:
        raise ConnectionRefused("ai_chatgpt_refresh_unavailable") from None
    updated = dict(record)
    updated.update(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"] or refresh_token,
        token_type="Bearer",
        expires_in=tokens["expires_in"],
        expires_at=now + tokens["expires_in"],
        earliest_refresh_at=tokens["earliest_refresh_at"],
        saved_at=_iso(now),
    )
    if tokens["scopes"] is not None:
        updated["scopes"] = list(tokens["scopes"])
    _write_record(store, conn.id, updated)  # the whole set together
    plan: PlanState = "enabled" if CHATGPT_PLAN_SCOPE in updated["scopes"] else "disabled"
    _set_connection_state(store.home, conn.id, plan=plan, signed_in=True)
    return updated


def chatgpt_access_token(
    store: ConnectionStore,
    conn: Connection,
    *,
    transport: FormTransport,
    clock: Callable[[], float] = time.time,
) -> str:
    """A usable access token for ``conn``, refreshed near expiry. Refreshes of one connection are serialized.

    Raises ConnectionRefused: ``ai_chatgpt_signin_required`` (signed out, or
    the refresh token is unusable), ``ai_chatgpt_plan_disabled`` (plan use
    was not granted: never usable for Krell), ``ai_chatgpt_client_invalid``
    or ``ai_chatgpt_refresh_unavailable``.
    """

    if not is_chatgpt(conn):
        raise ConnectionRefused("ai_chatgpt_not_chatgpt")
    with _record_lock(store.home, conn.id):
        record = _read_record(store, conn)  # re-read under the lock: another thread may have rotated it
        if not record.get("access_token"):
            raise ConnectionRefused("ai_chatgpt_signin_required")
        if CHATGPT_PLAN_SCOPE not in record["scopes"]:
            raise ConnectionRefused("ai_chatgpt_plan_disabled")
        now = clock()
        if _needs_refresh(record, now):
            record = _refresh(store, conn, record, transport, now)
            if CHATGPT_PLAN_SCOPE not in record["scopes"]:
                raise ConnectionRefused("ai_chatgpt_plan_disabled")
        token = record.get("access_token")
        if not isinstance(token, str) or not token:
            raise ConnectionRefused("ai_chatgpt_signin_required")
        return token


def chatgpt_adapter(
    store: ConnectionStore,
    conn: Connection,
    *,
    transport: HttpTransport,
    clock: Callable[[], float] = time.time,
) -> ChatAdapter:
    """The Responses adapter for a ChatGPT connection. Refuses (ConnectionRefused) before any provider call."""

    form: FormTransport = transport  # type: ignore[assignment]
    first = [chatgpt_access_token(store, conn, transport=form, clock=clock)]

    def token() -> str:
        if first:
            return first.pop()
        try:
            return chatgpt_access_token(store, conn, transport=form, clock=clock)
        except ConnectionRefused as exc:
            raise StreamFailure(ProviderError("provider_auth_failed", exc.message)) from None

    return OpenAICompatAdapter(conn.base_url, None, transport, api="responses", token_source=token)


def _revoke(transport: FormTransport, record: dict, sleep: Callable[[float], None]) -> bool:
    """Revoke the refresh token at the discovery ``revocation_endpoint``; retry network and 5xx failures."""

    fields = {
        "token": record["refresh_token"],
        "token_type_hint": "refresh_token",
        "client_id": record["client_id"],
    }
    endpoint: str | None = None
    for attempt in range(_REVOKE_ATTEMPTS):
        if attempt:
            sleep(_REVOKE_DELAYS[min(attempt - 1, len(_REVOKE_DELAYS) - 1)])
        try:
            if endpoint is None:
                endpoint = _discovery(transport)["revocation_endpoint"]
                if endpoint is None:
                    return False
            _post_form(transport, endpoint, fields, timeout=_REVOKE_TIMEOUT)
        except TransportError as exc:
            if exc.status is not None and exc.status < 500:
                return False  # a refusal will not change on retry
            continue
        except SigninRefused:
            return False
        return True  # an empty 200 (also for an already-invalid token)
    return False


def chatgpt_sign_out(
    store: ConnectionStore,
    conn: Connection,
    *,
    transport: FormTransport,
    sleep: Callable[[float], None] = time.sleep,
) -> RevocationResult:
    """Revoke the refresh token, then clear the tokens locally.

    The account-to-client mapping and the host id are kept, so a later
    sign-in reuses the issued client. When revocation is not confirmed, the
    tokens are still cleared and the result says so.
    """

    if not is_chatgpt(conn):
        raise ConnectionRefused("ai_chatgpt_not_chatgpt")
    with _record_lock(store.home, conn.id):
        try:
            record: dict | None = _read_record(store, conn)
        except ConnectionRefused as exc:
            if exc.code != "ai_chatgpt_signin_required":
                raise
            record = None
        revocation: Revocation = "none"
        if record is not None and record.get("refresh_token"):
            revocation = "confirmed" if _revoke(transport, record, sleep) else "not_confirmed"
        if record is not None:
            _write_record(store, conn.id, _cleared(record, keep_id_token=False))
        _set_connection_state(store.home, conn.id, signed_in=False)
    return RevocationResult(revocation, REVOCATION_NOT_CONFIRMED if revocation == "not_confirmed" else "")


def remove_chatgpt_connection(
    store: ConnectionStore,
    conn: Connection,
    *,
    transport: FormTransport,
    sleep: Callable[[float], None] = time.sleep,
) -> RevocationResult:
    """Sign out (revoking the refresh token), then remove the connection and its keychain entry.

    The account-to-client mapping and the host id stay in ``chatgpt.json``.
    """

    result = chatgpt_sign_out(store, conn, transport=transport, sleep=sleep)
    with _record_lock(store.home, conn.id):
        store.remove(conn.id)
        with _state_change(store.home) as state:
            state["connections"].pop(conn.id, None)
    return result
