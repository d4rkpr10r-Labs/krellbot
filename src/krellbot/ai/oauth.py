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
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import http.server
import json
import re
import secrets as stdlib_secrets  # the standard library module, not krellbot.secrets
import socket
import socketserver
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import parse_qs, urlencode

import keyring

from krellbot.ai.connections import (
    _MAX_MODEL,
    ConnectionRefused,
    ConnectionStore,
    _clean_text,
    keychain_is_safe,
)
from krellbot.ai.http import HttpTransport, TransportError

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
