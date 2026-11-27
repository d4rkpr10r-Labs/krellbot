"""Sign in with ChatGPT and the Responses API path, built to the M38.22 record.

The engine registers a per-account OAuth client with OpenAI through a
single-use loopback callback on ``127.0.0.1``: the first sign-in sends
``client_id=dynamic_agent_client`` with ``agent_name_hint=Krellbot`` and this
install's ``ext_agent_host_id``; the callback returns the issued client id,
which every later sign-in, refresh and revocation uses. The ID token is
checked against OpenAI's JWKS (RS256, issuer, audience, expiry, nonce), and
ChatGPT plan use is enabled only when ``chatgpt.tokens.use.direct`` was
granted. One credential record per issued client id lives in the keychain
under ``krellbot-ai:<connection_id>``.

Inference goes to ``POST /v1/responses`` with ``store: false`` and
``stream: true``; only ``response.completed`` is success, and plan-usage
errors stop inference with no fallback.

Fakes only: ``FakeListener`` for the loopback socket, ``FakeOpenAI`` for
OpenAI's auth server and API (discovery, a JWKS signed by an RSA key made
here, token, refresh, revocation, models and a Responses SSE stream), and
``FakeKeyring``. One test drives the real listener on ``127.0.0.1``; nothing
leaves the machine.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import http.client
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from krellbot.ai import connections, oauth
from krellbot.ai.connections import ConnectionRefused, ConnectionStore
from krellbot.ai.http import TransportError
from krellbot.ai.openai_compat import RESPONSES_UNSUPPORTED_FIELDS, OpenAICompatAdapter
from krellbot.ai.types import ChatMessage, Done, ProviderError, TextDelta, ToolCall, ToolCallDone, ToolDef, Usage
from tests.fakes.fake_ai_transport import sse
from tests.test_ns06_api import TEST_PORT, _bootstrap_and_get_csrf, _build_app, asgi_call

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
REVOKE_URL = "https://auth.openai.com/api/accounts/oauth/revoke"
RESPONSES_URL = "https://api.openai.com/v1/responses"
MODELS_URL = "https://api.openai.com/v1/models"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
GRANTED = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"
NO_PLAN = "email offline_access openid profile resource.invoke"

CLIENT = "oaiapp_ISSUED-client-7f3a"
OTHER_CLIENT = "oaiapp_OTHER-client-0b1c"
SUBJECT = "user-SUBJECT-91c2"
EMAIL = "trader@example.com"
CODE = "oai-auth-CODE-5e1d0c"
ACCESS = "oai-ACCESS-token-11aa"
REFRESH = "oai-REFRESH-token-22bb"
ACCESS_2 = "oai-ACCESS-token-33cc"
REFRESH_2 = "oai-REFRESH-token-44dd"
NOW = 1_800_000_000.0
TOKEN = "boot-ai-chatgpt"
HOST_ID_RE = re.compile(r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
FLOW_ID_RE = re.compile(r"^[0-9a-f]{32}$")


# ---- an RSA signer for ID tokens --------------------------------------------------------


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64u(value: int) -> str:
    return _b64u(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class Signer:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict:
        numbers = self.key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "kid": self.kid,
            "alg": "RS256",
            "use": "sig",
            "n": _int_b64u(numbers.n),
            "e": _int_b64u(numbers.e),
        }

    def sign(self, claims: dict, *, header: dict | None = None) -> str:
        head = {"alg": "RS256", "typ": "JWT", "kid": self.kid} if header is None else header
        signing_input = _b64u(json.dumps(head).encode()) + "." + _b64u(json.dumps(claims).encode())
        signature = self.key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return signing_input + "." + _b64u(signature)


@functools.cache
def signer() -> Signer:
    return Signer("key-1")


@functools.cache
def stranger() -> Signer:
    """A key OpenAI never published."""
    return Signer("key-1")


def claims(nonce: str, *, aud: object = CLIENT, now: float = NOW, **over: object) -> dict:
    out = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": aud,
        "email": EMAIL,
        "nonce": nonce,
        "iat": int(now),
        "exp": int(now) + 3600,
    }
    out.update(over)
    return out


# ---- the fake OpenAI ----------------------------------------------------------------------


class FakeOpenAI:
    """OpenAI's auth server and API, scripted. Every call is recorded."""

    def __init__(self) -> None:
        self.form_requests: list[dict] = []
        self.get_requests: list[dict] = []
        self.requests: list[dict] = []
        self.token_replies: list[object] = []
        self.revoke_replies: list[object] = []
        self.stream_replies: list[object] = []
        self.discovery = {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZE_URL,
            "token_endpoint": TOKEN_URL,
            "jwks_uri": JWKS_URL,
            "revocation_endpoint": REVOKE_URL,
        }
        self.jwks = {"keys": [signer().jwk()]}
        self.models = {
            "models": [
                {"slug": "gpt-plan-b", "display_name": "Plan B", "visibility": "list"},
                {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide"},
                {"slug": "gpt-plan-a", "display_name": "Plan A", "visibility": "list"},
            ]
        }

    # -- the transport seam
    def post_form(self, url: str, fields: dict, headers: dict, timeout: float) -> tuple[int, bytes]:
        self.form_requests.append({"url": url, "fields": dict(fields), "headers": dict(headers)})
        queue = self.token_replies if url == TOKEN_URL else self.revoke_replies if url == REVOKE_URL else None
        if queue is None:
            raise TransportError(404, "")
        if not queue:
            if url == REVOKE_URL:
                return 200, b""
            raise TransportError(None, "no scripted reply")
        item = queue.pop(0)
        if callable(item):
            item = item(fields)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            return item
        return 200, json.dumps(item).encode()

    def get_json(self, url: str, headers: dict, timeout: float) -> dict:
        self.get_requests.append({"url": url, "headers": dict(headers)})
        if url == DISCOVERY_URL:
            return self.discovery
        if url == JWKS_URL:
            return self.jwks
        if url == MODELS_URL:
            return self.models
        raise TransportError(404, "")

    def post_stream(self, url: str, body: dict, headers: dict, timeout: float):
        self.requests.append({"url": url, "body": body, "headers": dict(headers)})
        if not self.stream_replies:
            raise TransportError(None, "no scripted stream")
        item = self.stream_replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return self._iterate(item)

    @staticmethod
    def _iterate(lines):
        for line in lines:
            if isinstance(line, Exception):
                raise line
            yield line.encode("utf-8") if isinstance(line, str) else line

    # -- helpers
    def forms_to(self, url: str) -> list[dict]:
        return [r["fields"] for r in self.form_requests if r["url"] == url]

    def all_outbound(self) -> list[str]:
        return (
            [r["url"] for r in self.form_requests]
            + [r["url"] for r in self.get_requests]
            + [r["url"] for r in self.requests]
        )


def token_reply(nonce_box: dict, *, client: str = CLIENT, scope: str = GRANTED, id_claims: dict | None = None, **extra):
    """A token response built when the exchange arrives, signed for the flow's nonce."""

    def reply(fields: dict) -> dict:
        body = {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "id_token": signer().sign(id_claims if id_claims is not None else claims(nonce_box["nonce"], aud=client)),
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": scope,
        }
        body.update(extra)
        return body

    return reply


def refresh_reply(access: str = ACCESS_2, refresh: str = REFRESH_2, *, scope: str = GRANTED, **extra) -> dict:
    body = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": scope,
    }
    body.update(extra)
    return body


# ---- the loopback listener, socket-free ---------------------------------------------------


class FakeListener:
    port = 51455

    def __init__(self, dispatch, lifetime: float) -> None:
        self.dispatch = dispatch
        self.lifetime = lifetime
        self.closed = False

    def request(self, target: str, *, method: str = "GET", host: str | None = None):
        if self.closed:
            return None
        reply = self.dispatch(method, target, f"127.0.0.1:{self.port}" if host is None else host)
        if reply.close:
            self.close()
            if reply.after is not None:
                reply.after()
        return reply

    def close(self) -> None:
        self.closed = True


class ListenerFactory:
    def __init__(self) -> None:
        self.made: list[FakeListener] = []

    def __call__(self, dispatch, lifetime: float) -> FakeListener:
        listener = FakeListener(dispatch, lifetime)
        self.made.append(listener)
        return listener

    @property
    def last(self) -> FakeListener:
        return self.made[-1]


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Env:
    """One test's engine side: home, fake OpenAI, clock, listeners and registry."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.openai = FakeOpenAI()
        self.clock = Clock()
        self.factory = ListenerFactory()
        self.registry = oauth.SigninRegistry()
        self.nonce: dict = {}

    def store(self) -> ConnectionStore:
        return ConnectionStore(self.home, allow_injected_fake_backend=True)

    def start(self, *, model: str | None = "gpt-plan-a", connection_id: str | None = None, retry: bool = False):
        flow = oauth.start_chatgpt_signin(
            self.home,
            model=model,
            clock=self.clock,
            transport=self.openai,
            listener_factory=self.factory,
            registry=self.registry,
            connection_id=connection_id,
            retry=retry,
            allow_injected_fake_backend=True,
        )
        return flow, self.factory.last

    def authorize(self, flow, listener) -> dict:
        """Open the launch address as the browser would; return the authorize query (one value each)."""

        launch = urlsplit(flow.authorize_url)
        assert (launch.scheme, launch.hostname, launch.port) == ("http", "127.0.0.1", listener.port)
        reply = listener.request(launch.path)
        assert reply is not None and reply.status == 302 and not reply.close
        target = urlsplit(reply.location)
        assert f"{target.scheme}://{target.netloc}{target.path}" == AUTHORIZE_URL
        query = parse_qs(target.query, strict_parsing=True)
        assert all(len(v) == 1 for v in query.values()), query
        params = {k: v[0] for k, v in query.items()}
        self.nonce["nonce"] = params["nonce"]
        return params

    def status(self, flow) -> dict:
        return oauth.signin_status(flow.id, registry=self.registry)

    def sign_in(self, *, scope: str = GRANTED, model: str = "gpt-plan-a") -> str:
        flow, listener = self.start(model=model)
        params = self.authorize(flow, listener)
        self.openai.token_replies.append(token_reply(self.nonce, scope=scope))
        reply = listener.request(
            f"/auth/callback?code={CODE}&scope={scope.replace(' ', '+')}&state={params['state']}&client_id={CLIENT}"
        )
        assert reply.status == 200
        status = self.status(flow)
        assert status["state"] == "connected", status
        return status["connection_id"]


def keychain_record(fresh_keyring, conn_id: str) -> dict:
    """The credential record, reassembled from its chunks and checked against its digest."""

    raw = connections._read_secret(fresh_keyring, f"krellbot-ai:{conn_id}")
    assert raw is not None
    return json.loads(raw)


def keychain_entries(fresh_keyring, conn_id: str) -> dict[str, str]:
    """Every keychain entry under the connection's service, by username."""

    return {
        user: value for (service, user), value in fresh_keyring._store.items() if service == f"krellbot-ai:{conn_id}"
    }


def chatgpt_state(home: Path) -> dict:
    return json.loads((home / "ai" / "chatgpt.json").read_text())


def accounts(state: dict) -> list[dict]:
    """The saved accounts without their ids, after checking each id is random 16-hex, not the client id."""

    out = []
    for account in state["accounts"]:
        account = dict(account)
        account_id = account.pop("id")
        assert re.fullmatch(r"[0-9a-f]{16}", account_id) and account_id not in account["client_id"]
        out.append(account)
    return out


def everything_written(home: Path) -> str:
    return "".join(p.read_text(errors="replace") for p in home.rglob("*") if p.is_file())


SECRETS = (ACCESS, REFRESH, ACCESS_2, REFRESH_2, CODE)


# ---- 1. the host id -----------------------------------------------------------------------


def test_host_id_is_a_urn_uuid_v4_persisted_before_the_first_signin_and_reused(home, fresh_keyring):
    env = Env(home)
    assert not (home / "ai" / "chatgpt.json").exists()
    flow, listener = env.start()
    host_id = chatgpt_state(home)["ext_agent_host_id"]
    assert HOST_ID_RE.match(host_id)
    assert uuid.UUID(host_id.removeprefix("urn:uuid:")).version == 4
    assert env.authorize(flow, listener)["ext_agent_host_id"] == host_id

    # Every later sign-in, and a fresh engine, reuse it.
    flow2, listener2 = Env(home).start()
    assert Env(home).authorize(flow2, listener2)["ext_agent_host_id"] == host_id
    assert oauth.chatgpt_host_id(home) == host_id
    # Opaque: never derived from the account.
    assert EMAIL not in host_id and SUBJECT not in host_id

    # It is not a secret and never goes to the keychain.
    assert fresh_keyring._store == {}


def test_an_unreadable_chatgpt_state_file_refuses_rather_than_making_a_new_host(home, fresh_keyring):
    (home / "ai").mkdir()
    (home / "ai" / "chatgpt.json").write_text("{not json")
    env = Env(home)
    with pytest.raises(ConnectionRefused) as refused:
        env.start()
    assert refused.value.code == "ai_chatgpt_state_unreadable"
    assert env.factory.made == []
    assert (home / "ai" / "chatgpt.json").read_text() == "{not json"


# ---- 2. the first authorize request ---------------------------------------------------------


def test_first_signin_registers_a_dynamic_client_with_pkce_state_and_nonce(home, fresh_keyring):
    env = Env(home)
    flow, listener = env.start()
    assert FLOW_ID_RE.match(flow.id)
    assert listener.lifetime == 300
    # The browser gets only a loopback launch address; the authorize URL stays in the engine.
    assert urlsplit(flow.authorize_url).path.startswith("/auth/start/")
    assert "auth.openai.com" not in flow.authorize_url

    params = env.authorize(flow, listener)
    assert set(params) == {
        "response_type",
        "client_id",
        "redirect_uri",
        "scope",
        "resource",
        "state",
        "nonce",
        "code_challenge",
        "code_challenge_method",
        "agent_name_hint",
        "ext_agent_host_id",
    }
    assert params["response_type"] == "code"
    assert params["client_id"] == "dynamic_agent_client"
    assert params["agent_name_hint"] == "Krellbot"
    assert params["redirect_uri"] == f"http://127.0.0.1:{listener.port}/auth/callback"
    assert params["scope"] == SCOPE
    assert params["resource"] == RESOURCE
    assert params["code_challenge_method"] == "S256"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", params["code_challenge"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", params["state"]) and params["state"] == flow.state
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", params["nonce"]) and params["nonce"] != params["state"]
    assert env.status(flow) == {"state": "waiting"}

    # A second attempt gets fresh state, nonce and challenge.
    flow2, listener2 = env.start()
    params2 = env.authorize(flow2, listener2)
    for name in ("state", "nonce", "code_challenge"):
        assert params2[name] != params[name]


# ---- 3. the callback and the exchange ---------------------------------------------------------


def test_first_registration_saves_the_issued_client_id_not_dynamic_agent_client(home, fresh_keyring):
    env = Env(home)
    flow, listener = env.start(model="gpt-plan-a")
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(token_reply(env.nonce))
    reply = listener.request(
        f"/auth/callback?code={CODE}&scope={GRANTED.replace(' ', '+')}&state={params['state']}&client_id={CLIENT}"
    )
    assert reply.status == 200 and b"close this tab" in reply.body
    assert listener.closed

    [exchange] = env.openai.forms_to(TOKEN_URL)
    verifier = exchange.pop("code_verifier")
    assert exchange == {
        "grant_type": "authorization_code",
        "client_id": CLIENT,
        "code": CODE,
        "redirect_uri": params["redirect_uri"],
        "resource": RESOURCE,
    }
    digest = _b64u(hashlib.sha256(verifier.encode("ascii")).digest())
    assert digest == params["code_challenge"]
    # The ID token was checked against the JWKS that discovery names.
    assert [r["url"] for r in env.openai.get_requests] == [DISCOVERY_URL, JWKS_URL]

    status = env.status(flow)
    assert set(status) == {"state", "connection_id", "plan"}
    assert (status["state"], status["plan"]) == ("connected", "enabled")
    conn_id = status["connection_id"]

    record = keychain_record(fresh_keyring, conn_id)
    assert record["client_id"] == CLIENT
    assert record["issuer"] == ISSUER and record["subject"] == SUBJECT and record["email"] == EMAIL
    assert record["ext_agent_host_id"] == oauth.chatgpt_host_id(home)
    assert (record["access_token"], record["refresh_token"]) == (ACCESS, REFRESH)
    assert record["id_token"].count(".") == 2
    assert record["token_type"] == "Bearer" and record["expires_in"] == 3600
    assert record["expires_at"] == NOW + 3600
    assert record["scopes"] == sorted(GRANTED.split())
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record["saved_at"])
    # Only this connection's service: one header plus its chunks.
    assert {service for service, _user in fresh_keyring._store} == {f"krellbot-ai:{conn_id}"}

    [conn] = env.store().list()
    assert (conn.id, conn.preset, conn.auth, conn.model, conn.capability) == (
        conn_id,
        "openai",
        "oauth",
        "gpt-plan-a",
        "unprobed",
    )
    assert conn.base_url == RESOURCE

    # The non-secret state maps the account to its issued client; no token, no dynamic id.
    state = chatgpt_state(home)
    assert accounts(state) == [{"client_id": CLIENT, "issuer": ISSUER, "subject": SUBJECT, "email": EMAIL}]
    assert state["connections"][conn_id]["client_id"] == CLIENT
    written = everything_written(home)
    assert "dynamic_agent_client" not in written
    for secret in (*SECRETS, record["id_token"]):
        assert secret not in written


@pytest.mark.parametrize(
    "client_part",
    ["", "&client_id=", "&client_id=dynamic_agent_client", f"&client_id={CLIENT}&client_id={OTHER_CLIENT}"],
)
def test_a_new_registration_without_an_issued_client_id_is_incomplete(home, fresh_keyring, client_part):
    env = Env(home)
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}{client_part}")
    status = env.status(flow)
    assert (status["state"], status["code"]) == ("failed", "ai_chatgpt_registration_incomplete")
    assert env.openai.form_requests == []
    assert fresh_keyring._store == {}


def test_access_denied_validates_state_and_stops_without_an_exchange(home, fresh_keyring):
    env = Env(home)
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    # Without the right state it is not this attempt's answer.
    reply = listener.request("/auth/callback?error=access_denied&state=nope")
    assert reply.status == 404 and env.status(flow) == {"state": "waiting"}

    reply = listener.request(f"/auth/callback?error=access_denied&state={params['state']}")
    assert reply.status == 200 and listener.closed
    status = env.status(flow)
    assert (status["state"], status["code"]) == ("failed", "ai_chatgpt_access_denied")
    assert "API key" in status["message"]
    assert env.openai.form_requests == [] and fresh_keyring._store == {}


def test_a_callback_with_a_bad_state_is_rejected_and_leaves_the_flow_waiting(home, fresh_keyring):
    env = Env(home)
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    good = f"/auth/callback?code={CODE}&state={params['state']}&client_id={CLIENT}"
    port = listener.port
    probes = [
        ("GET", f"/auth/callback?code={CODE}&state={'A' * len(params['state'])}&client_id={CLIENT}", None),
        ("GET", f"/auth/callback?code={CODE}&client_id={CLIENT}", None),
        ("GET", f"{good}&state={params['state']}", None),  # two states
        ("GET", f"/callback?code={CODE}&state={params['state']}&client_id={CLIENT}", None),
        ("GET", f"/auth/callback/?code={CODE}&state={params['state']}&client_id={CLIENT}", None),
        ("GET", f"http://127.0.0.1:{port}{good}", None),
        ("POST", good, None),
        ("GET", good, f"localhost:{port}"),
        ("GET", good, f"evil.example:{port}"),
    ]
    for method, target, host in probes:
        reply = listener.request(target, method=method, host=host)
        assert reply.status == 404 and not reply.close and reply.after is None, (method, target, host)
        assert CODE.encode() not in reply.body
    assert not listener.closed
    assert env.openai.form_requests == []
    assert env.status(flow) == {"state": "waiting"}

    env.openai.token_replies.append(token_reply(env.nonce))
    assert listener.request(good).status == 200
    assert env.status(flow)["state"] == "connected"


@pytest.mark.parametrize(
    ("case", "build"),
    [
        ("signature", lambda nonce: stranger().sign(claims(nonce))),
        ("issuer", lambda nonce: signer().sign(claims(nonce, iss="https://auth.openai.com.evil"))),
        ("audience dynamic", lambda nonce: signer().sign(claims(nonce, aud="dynamic_agent_client"))),
        ("audience other", lambda nonce: signer().sign(claims(nonce, aud=OTHER_CLIENT))),
        ("audience list", lambda nonce: signer().sign(claims(nonce, aud=[OTHER_CLIENT, CLIENT]))),
        ("expired", lambda nonce: signer().sign(claims(nonce, exp=int(NOW) - 600))),
        ("nonce", lambda nonce: signer().sign(claims("not-the-flow-nonce"))),
        ("no nonce", lambda nonce: signer().sign({k: v for k, v in claims(nonce).items() if k != "nonce"})),
        ("no subject", lambda nonce: signer().sign(claims(nonce, sub=""))),
        ("alg none", lambda nonce: signer().sign(claims(nonce), header={"alg": "none", "kid": "key-1"})),
        ("alg hs256", lambda nonce: signer().sign(claims(nonce), header={"alg": "HS256", "kid": "key-1"})),
        ("unknown kid", lambda nonce: signer().sign(claims(nonce), header={"alg": "RS256", "kid": "key-9"})),
        ("not a jwt", lambda nonce: "not.a-jwt"),
    ],
)
def test_an_id_token_with_a_bad_signature_issuer_audience_expiry_or_nonce_is_rejected(home, fresh_keyring, case, build):
    env = Env(home)
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(
        lambda fields: {**token_reply(env.nonce)(fields), "id_token": build(env.nonce["nonce"])}
    )
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={CLIENT}")
    status = env.status(flow)
    assert (status["state"], status["code"]) == ("failed", "ai_chatgpt_id_token_invalid"), case
    assert fresh_keyring._store == {}
    assert env.store().list() == []
    assert ACCESS not in json.dumps(status)


def test_a_discovery_document_pointing_off_auth_openai_com_is_refused(home, fresh_keyring):
    env = Env(home)
    env.openai.discovery["jwks_uri"] = "https://evil.example/jwks.json"
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(token_reply(env.nonce))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={CLIENT}")
    status = env.status(flow)
    assert (status["state"], status["code"]) == ("failed", "ai_chatgpt_bad_response")
    assert "https://evil.example/jwks.json" not in [r["url"] for r in env.openai.get_requests]
    assert fresh_keyring._store == {}


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        # A first registration's refused code keeps its issued client and offers Try again.
        (TransportError(400, json.dumps({"error": "invalid_grant"})), "ai_chatgpt_code_rejected"),
        (TransportError(400, json.dumps({"error": "invalid_request"})), "ai_chatgpt_rejected"),
        (TransportError(None, "URLError"), "ai_chatgpt_unavailable"),
        (TransportError(503, ACCESS), "ai_chatgpt_unavailable"),
        ((200, b"not json"), "ai_chatgpt_bad_response"),
        ({"token_type": "Bearer"}, "ai_chatgpt_bad_response"),
    ],
)
def test_a_failed_code_exchange_is_typed_and_stores_nothing(home, fresh_keyring, answer, code):
    env = Env(home)
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(answer)
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={CLIENT}")
    status = env.status(flow)
    assert (status["state"], status["code"]) == ("failed", code)
    assert CODE not in json.dumps(status) and ACCESS not in json.dumps(status)
    assert fresh_keyring._store == {}


# ---- 4. the plan scope -------------------------------------------------------------------------


def test_a_missing_plan_scope_keeps_the_signin_but_disables_plan_use_and_krell(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in(scope=NO_PLAN)
    record = keychain_record(fresh_keyring, conn_id)
    assert record["scopes"] == sorted(NO_PLAN.split())
    assert chatgpt_state(home)["connections"][conn_id]["plan"] == "disabled"
    view = oauth.chatgpt_connection_view(home, conn_id)
    assert view == {"plan": "disabled", "signed_in": True, "welcome": False, "account": EMAIL}

    store = env.store()
    conn = store.get(conn_id)
    with pytest.raises(ConnectionRefused) as refused:
        store.adapter(conn, env.openai)
    assert refused.value.code == "ai_chatgpt_plan_disabled"
    assert "API key" in refused.value.message
    assert env.openai.requests == []


# ---- 5. reauthorization ------------------------------------------------------------------------


def test_reauthorization_uses_the_saved_client_id_without_agent_name_hint(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    host_id = oauth.chatgpt_host_id(home)
    first_id_token = keychain_record(fresh_keyring, conn_id)["id_token"]
    env.openai.form_requests.clear()

    flow, listener = env.start(model=None, connection_id=conn_id)
    params = env.authorize(flow, listener)
    assert params["client_id"] == CLIENT
    assert "agent_name_hint" not in params
    assert params["ext_agent_host_id"] == host_id
    assert params["id_token_hint"] == first_id_token
    assert params["login_hint"] == EMAIL
    assert params["scope"] == SCOPE and params["resource"] == RESOURCE

    env.openai.token_replies.append(
        lambda fields: {**token_reply(env.nonce)(fields), "access_token": ACCESS_2, "refresh_token": REFRESH_2}
    )
    # On reauthorization the callback may omit client_id.
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}")
    status = env.status(flow)
    assert (status["state"], status["connection_id"]) == ("connected", conn_id)
    [exchange] = env.openai.forms_to(TOKEN_URL)
    assert exchange["client_id"] == CLIENT
    assert [c.id for c in env.store().list()] == [conn_id]
    record = keychain_record(fresh_keyring, conn_id)
    assert (record["access_token"], record["refresh_token"], record["client_id"]) == (ACCESS_2, REFRESH_2, CLIENT)


def test_reauthorization_rejects_a_callback_with_a_different_client_id(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    before = keychain_record(fresh_keyring, conn_id)
    env.openai.form_requests.clear()

    flow, listener = env.start(model=None, connection_id=conn_id)
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(token_reply(env.nonce, client=OTHER_CLIENT))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={OTHER_CLIENT}")
    status = env.status(flow)
    assert (status["state"], status["code"]) == ("failed", "ai_chatgpt_client_mismatch")
    assert env.openai.form_requests == []
    assert keychain_record(fresh_keyring, conn_id) == before


def test_reauthorization_for_another_account_does_not_replace_the_credentials(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    before = keychain_record(fresh_keyring, conn_id)
    flow, listener = env.start(model=None, connection_id=conn_id)
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(
        lambda fields: {
            **token_reply(env.nonce)(fields),
            "id_token": signer().sign(claims(env.nonce["nonce"], sub="someone-else")),
        }
    )
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}")
    status = env.status(flow)
    assert (status["state"], status["code"]) == ("failed", "ai_chatgpt_account_mismatch")
    assert keychain_record(fresh_keyring, conn_id) == before


def test_reauthorizing_a_plan_disabled_connection_asks_for_consent_again(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in(scope=NO_PLAN)
    flow, listener = env.start(model=None, connection_id=conn_id)
    params = env.authorize(flow, listener)
    assert params["prompt"] == "consent"
    assert params["scope"] == SCOPE
    env.openai.token_replies.append(token_reply(env.nonce))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}")
    assert env.status(flow)["plan"] == "enabled"
    assert oauth.chatgpt_connection_view(home, conn_id)["plan"] == "enabled"


def test_reauthorization_needs_a_chatgpt_connection(home, fresh_keyring):
    env = Env(home)
    other = env.store().add("openai", model="gpt-x", credential="sk-test-key-0001")
    for conn_id in (other.id, "0" * 16, "../etc"):
        with pytest.raises(ConnectionRefused) as refused:
            env.start(model=None, connection_id=conn_id)
        assert refused.value.code in ("ai_chatgpt_not_chatgpt", "ai_connection_not_found")
    assert env.factory.made == []


# ---- 6. refresh ------------------------------------------------------------------------------


def test_a_refresh_near_expiry_rotates_and_stores_the_whole_token_set(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    conn = store.get(conn_id)
    original = keychain_record(fresh_keyring, conn_id)

    # Fresh: no refresh.
    assert oauth.chatgpt_access_token(store, conn, transport=env.openai, clock=env.clock) == ACCESS
    assert env.openai.forms_to(TOKEN_URL) == [env.openai.forms_to(TOKEN_URL)[0]]  # only the code exchange

    env.clock.now = NOW + 3600 - 60
    env.openai.token_replies.append(refresh_reply())
    assert oauth.chatgpt_access_token(store, conn, transport=env.openai, clock=env.clock) == ACCESS_2
    refresh = env.openai.forms_to(TOKEN_URL)[-1]
    assert refresh == {
        "grant_type": "refresh_token",
        "client_id": CLIENT,
        "refresh_token": REFRESH,
        "resource": RESOURCE,
    }
    assert "scope" not in refresh

    record = keychain_record(fresh_keyring, conn_id)
    assert (record["access_token"], record["refresh_token"]) == (ACCESS_2, REFRESH_2)
    assert record["expires_at"] == env.clock.now + 3600
    assert record["client_id"] == CLIENT and record["id_token"] == original["id_token"]
    assert REFRESH not in json.dumps(record)


def test_earliest_refresh_at_defers_a_refresh_until_it_passes(home, fresh_keyring):
    env = Env(home)
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(token_reply(env.nonce, earliest_refresh_at=int(NOW) + 3500))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={CLIENT}")
    conn_id = env.status(flow)["connection_id"]
    store = env.store()
    conn = store.get(conn_id)
    exchanges = len(env.openai.forms_to(TOKEN_URL))

    env.clock.now = NOW + 3360  # inside the refresh window, before earliest_refresh_at
    assert oauth.chatgpt_access_token(store, conn, transport=env.openai, clock=env.clock) == ACCESS
    assert len(env.openai.forms_to(TOKEN_URL)) == exchanges

    env.clock.now = NOW + 3500
    env.openai.token_replies.append(refresh_reply())
    assert oauth.chatgpt_access_token(store, conn, transport=env.openai, clock=env.clock) == ACCESS_2
    assert len(env.openai.forms_to(TOKEN_URL)) == exchanges + 1


def test_concurrent_refreshes_for_one_connection_are_serialized(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    env.clock.now = NOW + 3590
    entered = threading.Event()
    release = threading.Event()

    def slow_refresh(fields):
        entered.set()
        assert release.wait(5)
        return refresh_reply()

    env.openai.token_replies.append(slow_refresh)
    results: list[str] = []

    def worker() -> None:
        store = env.store()
        results.append(oauth.chatgpt_access_token(store, store.get(conn_id), transport=env.openai, clock=env.clock))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    threads[0].start()
    assert entered.wait(5)
    for t in threads[1:]:
        t.start()
    time.sleep(0.1)  # the others are now waiting on the connection's lock
    release.set()
    for t in threads:
        t.join(5)
    assert results == [ACCESS_2, ACCESS_2, ACCESS_2]
    refreshes = [f for f in env.openai.forms_to(TOKEN_URL) if f["grant_type"] == "refresh_token"]
    assert len(refreshes) == 1
    assert keychain_record(fresh_keyring, conn_id)["refresh_token"] == REFRESH_2


@pytest.mark.parametrize(
    "body",
    [
        {"error": "invalid_grant"},
        {"error": "invalid_refresh_token"},
        {"error": "token_expired"},
        {"error": {"code": "refresh_token_expired", "message": "expired"}},
        {"error": {"code": "refresh_token_invalidated"}},
        {"error": "refresh_token_reused"},
    ],
)
def test_an_unusable_refresh_token_clears_the_tokens_and_asks_for_signin_with_the_saved_client(
    home, fresh_keyring, body
):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    env.clock.now = NOW + 3600
    env.openai.token_replies.append(TransportError(400, json.dumps(body)))
    with pytest.raises(ConnectionRefused) as refused:
        oauth.chatgpt_access_token(store, store.get(conn_id), transport=env.openai, clock=env.clock)
    assert refused.value.code == "ai_chatgpt_signin_required"
    record = keychain_record(fresh_keyring, conn_id)
    assert record["access_token"] is None and record["refresh_token"] is None
    assert record["client_id"] == CLIENT and record["subject"] == SUBJECT
    assert oauth.chatgpt_connection_view(home, conn_id)["signed_in"] is False

    # Signing in again goes back to the same issued client.
    flow, listener = env.start(model=None, connection_id=conn_id)
    assert env.authorize(flow, listener)["client_id"] == CLIENT


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (TransportError(401, json.dumps({"error": "invalid_client"})), "ai_chatgpt_client_invalid"),
        (TransportError(None, "URLError"), "ai_chatgpt_refresh_unavailable"),
        (TransportError(502, ""), "ai_chatgpt_refresh_unavailable"),
    ],
)
def test_other_refresh_failures_keep_the_tokens(home, fresh_keyring, answer, code):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    env.clock.now = NOW + 3600
    env.openai.token_replies.append(answer)
    with pytest.raises(ConnectionRefused) as refused:
        oauth.chatgpt_access_token(store, store.get(conn_id), transport=env.openai, clock=env.clock)
    assert refused.value.code == code
    assert keychain_record(fresh_keyring, conn_id)["refresh_token"] == REFRESH


# ---- 7. sign-out and removal revoke the refresh token -------------------------------------------


def test_removing_a_chatgpt_connection_revokes_its_refresh_token(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    host_id = oauth.chatgpt_host_id(home)
    result = oauth.remove_chatgpt_connection(store, store.get(conn_id), transport=env.openai, sleep=lambda s: None)
    assert result.revocation == "confirmed"
    assert env.openai.forms_to(REVOKE_URL) == [
        {"token": REFRESH, "token_type_hint": "refresh_token", "client_id": CLIENT}
    ]
    assert store.list() == []
    assert fresh_keyring._store == {}
    state = chatgpt_state(home)
    assert state["ext_agent_host_id"] == host_id
    assert accounts(state) == [{"client_id": CLIENT, "issuer": ISSUER, "subject": SUBJECT, "email": EMAIL}]
    assert conn_id not in state.get("connections", {})


def test_sign_out_revokes_clears_tokens_and_keeps_the_account_and_host(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    result = oauth.chatgpt_sign_out(store, store.get(conn_id), transport=env.openai, sleep=lambda s: None)
    assert result.revocation == "confirmed"
    record = keychain_record(fresh_keyring, conn_id)
    assert (record["access_token"], record["refresh_token"], record["id_token"]) == (None, None, None)
    assert record["client_id"] == CLIENT and record["ext_agent_host_id"] == oauth.chatgpt_host_id(home)
    assert oauth.chatgpt_connection_view(home, conn_id)["signed_in"] is False
    with pytest.raises(ConnectionRefused) as refused:
        store.adapter(store.get(conn_id), env.openai)
    assert refused.value.code == "ai_chatgpt_signin_required"

    # A later sign-in reuses the client; with no retained ID token there is no id_token_hint.
    flow, listener = env.start(model=None, connection_id=conn_id)
    params = env.authorize(flow, listener)
    assert params["client_id"] == CLIENT and "id_token_hint" not in params and params["login_hint"] == EMAIL


def test_unconfirmed_revocation_retries_with_bounded_backoff_then_clears_locally(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    env.openai.revoke_replies.extend([TransportError(None, "reset"), TransportError(503, ""), TransportError(500, "")])
    slept: list[float] = []
    result = oauth.chatgpt_sign_out(store, store.get(conn_id), transport=env.openai, sleep=slept.append)
    assert result.revocation == "not_confirmed"
    assert "not confirmed" in result.message and "ChatGPT" in result.message
    assert len(env.openai.forms_to(REVOKE_URL)) == 3
    assert slept and all(0 < s <= 5 for s in slept) and slept == sorted(slept)
    record = keychain_record(fresh_keyring, conn_id)
    assert record["access_token"] is None and record["refresh_token"] is None


# ---- 8. the Responses request -------------------------------------------------------------------


TOOL = ToolDef("get_status", "Read the engine status.", {"type": "object", "properties": {}})
HISTORY = (
    ChatMessage("user", "What is running?"),
    ChatMessage(
        "assistant", "Let me look.", tool_calls=(ToolCall("call_1", "get_status", {}, {"namespace": "krellbot"}),)
    ),
    ChatMessage("tool", '<tool_result tool="get_status">{"ok": true}</tool_result>', tool_call_id="call_1"),
    ChatMessage("assistant", "Nothing is running."),
    ChatMessage("user", "Thanks. And now?"),
)


def completed(*output: dict, usage: tuple[int, int] = (12, 7)) -> dict:
    return {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "status": "completed",
            "output": list(output),
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1], "total_tokens": sum(usage)},
        },
    }


def text_stream(text: str = "Hello") -> list[str]:
    return sse(
        {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        completed(),
        named=True,
    )


def responses_adapter(openai: FakeOpenAI, token: str = ACCESS) -> OpenAICompatAdapter:
    return OpenAICompatAdapter(RESOURCE, token, openai, api="responses")


def test_the_responses_request_is_store_false_stream_true_with_instructions_and_full_history():
    openai = FakeOpenAI()
    openai.stream_replies.append(text_stream())
    events = list(
        responses_adapter(openai).stream(
            model="gpt-plan-a", system="You are Krell.", messages=HISTORY, tools=(TOOL,), max_tokens=4096
        )
    )
    assert isinstance(events[-1], Done)

    [request] = openai.requests
    assert request["url"] == RESPONSES_URL
    assert request["headers"]["authorization"] == f"Bearer {ACCESS}"
    body = request["body"]
    assert set(body) == {"model", "instructions", "input", "store", "stream", "tools"}
    assert body["store"] is False and body["stream"] is True
    assert body["model"] == "gpt-plan-a"
    assert body["instructions"] == "You are Krell."
    for field in (
        "previous_response_id",
        "temperature",
        "top_p",
        "max_output_tokens",
        "metadata",
        "user",
        "truncation",
        "prompt",
        "background",
        "conversation",
        "max_tool_calls",
        "safety_identifier",
    ):
        assert field in RESPONSES_UNSUPPORTED_FIELDS
        assert field not in body
    assert body["input"] == [
        {"role": "user", "content": "What is running?"},
        {"role": "assistant", "content": "Let me look."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_status",
            "arguments": "{}",
            "namespace": "krellbot",
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '<tool_result tool="get_status">{"ok": true}</tool_result>',
        },
        {"role": "assistant", "content": "Nothing is running."},
        {"role": "user", "content": "Thanks. And now?"},
    ]
    assert not any(item.get("role") == "system" for item in body["input"])
    [namespace] = body["tools"]
    assert namespace["type"] == "namespace" and namespace["name"] == "krellbot" and namespace["description"]
    assert namespace["tools"] == [
        {
            "type": "function",
            "name": "get_status",
            "description": "Read the engine status.",
            "parameters": {"type": "object", "properties": {}},
        }
    ]

    # No tools and no system prompt: neither key is sent.
    openai.stream_replies.append(text_stream())
    list(responses_adapter(openai).stream(model="m", system="", messages=HISTORY[:1], tools=(), max_tokens=10))
    assert set(openai.requests[-1]["body"]) == {"model", "input", "store", "stream"}


def test_a_stream_succeeds_only_on_response_completed():
    call_item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_9",
        "name": "get_status",
        "arguments": "",
        "namespace": "krellbot",
    }
    good = sse(
        {"type": "response.output_text.delta", "output_index": 0, "delta": "Checking"},
        {"type": "response.output_item.added", "output_index": 1, "item": call_item},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "fc_1", "delta": '{"a"'},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "fc_1", "delta": ": 1}"},
        {
            "type": "response.function_call_arguments.done",
            "output_index": 1,
            "item_id": "fc_1",
            "arguments": '{"a": 1}',
        },
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {**call_item, "arguments": '{"a": 1}', "status": "completed"},
        },
        completed(usage=(30, 9)),
        named=True,
    )
    openai = FakeOpenAI()
    openai.stream_replies.append(good)
    events = list(
        responses_adapter(openai).stream(model="m", system="s", messages=HISTORY[:1], tools=(TOOL,), max_tokens=10)
    )
    assert events[0] == TextDelta("Checking")
    assert ToolCallDone(ToolCall("call_9", "get_status", {"a": 1}, {"namespace": "krellbot"})) in events
    assert Usage(30, 9) in events
    assert isinstance(events[-1], Done)

    # The same events without response.completed: an error, and no tool call runs.
    for cut in (good[:-2], [*good[:-2], TransportError(None, "reset")]):
        openai.stream_replies.append(cut)
        events = list(
            responses_adapter(openai).stream(model="m", system="s", messages=HISTORY[:1], tools=(TOOL,), max_tokens=10)
        )
        assert not any(isinstance(e, (ToolCallDone, Done)) for e in events)
        assert isinstance(events[-1], ProviderError)

    failed = sse(
        {
            "type": "response.failed",
            "response": {"id": "r", "status": "failed", "error": {"code": "server_error", "message": "x"}},
        },
        named=True,
    )
    incomplete = sse(
        {
            "type": "response.incomplete",
            "response": {
                "id": "r",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 5, "output_tokens": 4},
            },
        },
        named=True,
    )
    errored = sse({"type": "error", "code": "server_error", "message": "boom", "param": None}, named=True)
    for stream in (failed, incomplete, errored):
        openai.stream_replies.append([*good[:-2], *stream])
        events = list(
            responses_adapter(openai).stream(model="m", system="s", messages=HISTORY[:1], tools=(TOOL,), max_tokens=10)
        )
        assert not any(isinstance(e, (ToolCallDone, Done)) for e in events)
        assert isinstance(events[-1], ProviderError)
    # Tokens spent on an incomplete answer still count.
    openai.stream_replies.append(incomplete)
    events = list(
        responses_adapter(openai).stream(model="m", system="s", messages=HISTORY[:1], tools=(), max_tokens=10)
    )
    assert events == [Usage(5, 4), events[-1]] and isinstance(events[-1], ProviderError)


# ---- 9. plan-usage errors ------------------------------------------------------------------------


def _error_body(code: str, message: str = "msg", param: str | None = None) -> str:
    return json.dumps({"error": {"code": code, "message": message, "param": param, "type": "invalid_request_error"}})


def test_a_usage_limit_error_maps_to_a_usage_limit_state_before_or_during_the_stream():
    openai = FakeOpenAI()
    exc = TransportError(429, _error_body("subscription_sharing_usage_limit_exceeded"))
    exc.request_id = "req_ABC123"
    openai.stream_replies.append(exc)
    [error] = list(
        responses_adapter(openai).stream(model="m", system="s", messages=HISTORY[:1], tools=(), max_tokens=10)
    )
    assert error.code == "provider_usage_limit"
    assert (error.status, error.provider_code, error.request_id) == (
        429,
        "subscription_sharing_usage_limit_exceeded",
        "req_ABC123",
    )
    assert "subscription_sharing_usage_limit_exceeded" in (error.detail or "")
    assert "msg" not in error.message  # provider text is diagnostic only
    assert len(openai.requests) == 1  # no retry, no fallback

    during = sse(
        {"type": "response.output_text.delta", "output_index": 0, "delta": "Partial"},
        {
            "type": "response.failed",
            "response": {
                "id": "resp_9",
                "status": "failed",
                "error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"},
            },
        },
        named=True,
    )
    openai.stream_replies.append(during)
    events = list(
        responses_adapter(openai).stream(model="m", system="s", messages=HISTORY[:1], tools=(), max_tokens=10)
    )
    assert events[0] == TextDelta("Partial")
    assert (
        events[-1].code == "provider_usage_limit"
        and events[-1].provider_code == "subscription_sharing_usage_limit_exceeded"
    )


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (429, _error_body("subscription_sharing_usage_limit_exceeded"), "provider_usage_limit"),
        (503, _error_body("subscription_sharing_usage_unavailable"), "provider_unavailable"),
        (403, _error_body("subscription_sharing_user_not_eligible"), "provider_plan_not_eligible"),
        (
            400,
            _error_body("subscription_sharing_unsupported_capability", param="tools[0]"),
            "provider_plan_unsupported",
        ),
        (403, _error_body("subscription_sharing_route_not_supported"), "provider_plan_unsupported"),
        (401, _error_body("subscription_sharing_invalid_user"), "provider_auth_failed"),
        (403, _error_body("chatpass_v2_scope_not_authorized"), "provider_plan_not_eligible"),
        (403, _error_body("chatpass_v2_invalid_authorization_context"), "provider_plan_not_eligible"),
        (503, _error_body("subscription_sharing_user_unavailable"), "provider_unavailable"),
        (401, json.dumps({"detail": "direct permission not accepted"}), "provider_auth_failed"),
        (403, json.dumps({"detail": "serving region not permitted"}), "provider_plan_not_eligible"),
        (503, json.dumps({"detail": "direct routing unavailable"}), "provider_unavailable"),
        (429, _error_body("rate_limit_exceeded"), "provider_rate_limited"),
    ],
)
def test_plan_usage_errors_keep_status_code_and_request_id_and_map_as_the_record_says(status, body, code):
    openai = FakeOpenAI()
    exc = TransportError(status, body)
    exc.request_id = "req_77"
    openai.stream_replies.append(exc)
    [error] = list(
        responses_adapter(openai).stream(model="m", system="s", messages=HISTORY[:1], tools=(), max_tokens=10)
    )
    assert error.code == code
    assert error.status == status and error.request_id == "req_77"
    assert error.detail == body
    parsed = json.loads(body)
    if "error" in parsed:
        assert error.provider_code == parsed["error"]["code"]
        assert error.param == parsed["error"]["param"]
    else:
        # A pre-stream {"detail": ...} body is diagnostic text, never a machine code or the message.
        assert error.provider_code is None
        assert parsed["detail"] not in error.message


# ---- 10. models --------------------------------------------------------------------------------


def test_the_model_list_shows_only_visibility_list_entries_in_server_order():
    from krellbot.ai.openai_compat import fetch_chatgpt_models

    openai = FakeOpenAI()
    assert fetch_chatgpt_models(RESOURCE, ACCESS, openai) == ["gpt-plan-b", "gpt-plan-a"]
    [request] = openai.get_requests
    assert request["url"] == MODELS_URL
    assert request["headers"]["authorization"] == f"Bearer {ACCESS}"


# ---- 11. the routes, end to end -------------------------------------------------------------------


class Client:
    def __init__(self, env: Env) -> None:
        self.env = env
        self.app = _build_app(env.home, bootstrap_token=TOKEN)
        state = self.app.state.krellbot
        state.ai_transport = env.openai
        state.ai_signin_listener_factory = env.factory
        state.ai_signins = env.registry
        state.clock = env.clock
        state.keys_allow_fake_backend = True
        self.csrf = _bootstrap_and_get_csrf(self.app, TOKEN)
        self.session = next(iter(state.sessions))
        self.raw: list[bytes] = []

    def call(self, method: str, path: str, payload: object = None, *, csrf: bool = True, cookie: bool = True):
        headers = [("Host", f"127.0.0.1:{TEST_PORT}"), ("Origin", f"http://127.0.0.1:{TEST_PORT}")]
        if cookie:
            headers.append(("Cookie", f"krellbot_session={self.session}"))
        if csrf:
            headers.append(("X-Krellbot-CSRF", self.csrf))
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers += [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
        status, _hdrs, raw, _cookies = asgi_call(self.app, method=method, path=path, body=body, headers=headers)
        self.raw.append(raw)
        return status, raw

    def json(self, method: str, path: str, payload: object = None, *, expect: int = 200) -> dict:
        status, raw = self.call(method, path, payload)
        assert status == expect, (status, raw)
        return json.loads(raw)

    def sign_in(self, *, scope: str = GRANTED) -> str:
        started = self.json("POST", "/api/v1/ai/signin/chatgpt", {"model": "gpt-plan-a"})
        flow = oauth.SigninFlow(id=started["flow_id"], authorize_url=started["authorize_url"], state="")
        params = self.env.authorize(flow, self.env.factory.last)
        self.env.openai.token_replies.append(token_reply(self.env.nonce, scope=scope))
        self.env.factory.last.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={CLIENT}")
        body = self.json("GET", f"/api/v1/ai/signin/{started['flow_id']}")
        assert body["state"] == "connected"
        return body["connection_id"]


def test_chatgpt_signin_routes_are_gated_local_only_and_report_plan_state(home, fresh_keyring):
    env = Env(home)
    client = Client(env)
    status, _raw = client.call("POST", "/api/v1/ai/signin/chatgpt", {"model": "gpt-plan-a"}, csrf=False)
    assert status == 403
    status, raw = client.call("POST", "/api/v1/ai/signin/chatgpt", {"model": " "})
    assert status == 400 and json.loads(raw)["code"] == "ai_model_invalid"
    status, raw = client.call("POST", "/api/v1/ai/signin/chatgpt", {"model": "m", "connection_id": 7})
    assert status == 400 and json.loads(raw)["code"] == "ai_body_invalid"
    assert env.factory.made == []

    started = client.json("POST", "/api/v1/ai/signin/chatgpt", {"model": "gpt-plan-a"})
    assert FLOW_ID_RE.match(started["flow_id"])
    assert started["authorize_url"].startswith(f"http://127.0.0.1:{env.factory.last.port}/auth/start/")

    conn_id = client.sign_in()
    listed = client.json("GET", "/api/v1/ai/connections")
    [view] = listed["connections"]
    assert (view["id"], view["preset"], view["auth"]) == (conn_id, "openai", "oauth")
    assert view["chatgpt"] == {"plan": "enabled", "signed_in": True, "welcome": True, "account": EMAIL}

    # The welcome shows once per connection: the engine remembers it.
    marked = client.json("POST", f"/api/v1/ai/connections/{conn_id}/welcome")
    assert marked["effect"] == "changed"
    assert client.json("GET", "/api/v1/ai/connections")["connections"][0]["chatgpt"]["welcome"] is False
    assert client.json("POST", f"/api/v1/ai/connections/{conn_id}/welcome")["effect"] == "unchanged"
    status, _raw = client.call("POST", f"/api/v1/ai/connections/{conn_id}/welcome", csrf=False)
    assert status == 403


def test_the_probe_lists_plan_models_and_runs_one_responses_turn(home, fresh_keyring):
    env = Env(home)
    client = Client(env)
    conn_id = client.sign_in()
    ping = {
        "type": "function_call",
        "id": "fc_p",
        "call_id": "call_p",
        "name": "ping",
        "arguments": '{"reply": "ok"}',
        "namespace": "krellbot",
    }
    env.openai.stream_replies.append(
        sse({"type": "response.output_item.done", "output_index": 0, "item": ping}, completed(ping), named=True)
    )
    probed = client.json("POST", f"/api/v1/ai/connections/{conn_id}/probe")
    assert probed["probe"]["capability"] == "tools"
    assert probed["probe"]["models"] == ["gpt-plan-b", "gpt-plan-a"]
    [request] = env.openai.requests
    assert request["url"] == RESPONSES_URL and request["body"]["store"] is False
    assert request["headers"]["authorization"] == f"Bearer {ACCESS}"


def test_a_plan_disabled_signin_is_never_usable_for_krell(home, fresh_keyring):
    env = Env(home)
    client = Client(env)
    conn_id = client.sign_in(scope=NO_PLAN)
    status, raw = client.call("POST", f"/api/v1/ai/connections/{conn_id}/probe")
    assert status == 409 and json.loads(raw)["code"] == "ai_chatgpt_plan_disabled"
    assert client.json("GET", "/api/v1/ai/connections")["connections"][0]["capability"] == "unprobed"
    assert env.openai.requests == []


def test_remove_and_sign_out_routes_revoke_and_report_unconfirmed_revocation(home, fresh_keyring, monkeypatch):
    monkeypatch.setattr(oauth, "_REVOKE_DELAYS", (0.0, 0.0))
    env = Env(home)
    client = Client(env)
    first = client.sign_in()
    signed_out = client.json("POST", f"/api/v1/ai/connections/{first}/signout")
    assert (signed_out["effect"], signed_out["revocation"]) == ("changed", "confirmed")
    view = client.json("GET", "/api/v1/ai/connections")["connections"][0]
    assert view["chatgpt"]["signed_in"] is False and view["capability"] == "unprobed"

    second = client.sign_in()
    env.openai.revoke_replies.extend([TransportError(503, "")] * 3)
    removed = client.json("DELETE", f"/api/v1/ai/connections/{second}")
    assert removed["removed"] == second and removed["revocation"] == "not_confirmed"
    assert "not confirmed" in removed["detail"]
    assert [c["id"] for c in client.json("GET", "/api/v1/ai/connections")["connections"]] == [first]

    other = env.store().add("openai", model="gpt-x", credential="sk-test-key-0001")
    status, raw = client.call("POST", f"/api/v1/ai/connections/{other.id}/signout")
    assert status == 404 and json.loads(raw)["code"] == "ai_chatgpt_not_chatgpt"


def test_a_usage_limit_reaches_krell_as_a_usage_limit_error_and_nothing_is_retried(home, fresh_keyring):
    from tests.test_krell_loop_api import Client as KrellClient
    from tests.test_krell_loop_api import _error

    env = Env(home)
    env.clock.now = time.time()  # Krell's adapter reads the wall clock for token expiry
    conn_id = env.sign_in()
    store = env.store()
    store.set_capability(conn_id, "tools")
    store.activate(conn_id)
    krell = KrellClient(home, env.openai)
    convo = krell.json("GET", "/api/v1/krell/conversation")
    assert convo["connection"]["plan"] == "chatgpt"

    exc = TransportError(429, _error_body("subscription_sharing_usage_limit_exceeded"))
    exc.request_id = "req_LIMIT"
    env.openai.stream_replies.append(exc)
    events = krell.stream("/api/v1/krell/messages", {"text": "hello"})
    error = _error(events)
    assert error["code"] == "provider_usage_limit"
    assert "Manage usage" in error["message"]
    assert error["provider"] == {
        "status": 429,
        "code": "subscription_sharing_usage_limit_exceeded",
        "request_id": "req_LIMIT",
    }
    assert len(env.openai.requests) == 1


def test_no_token_reaches_logs_the_journal_or_browser_facing_payloads(home, fresh_keyring, caplog, capfd):
    from tests.test_krell_loop_api import Client as KrellClient

    caplog.set_level(logging.DEBUG)
    env = Env(home)
    env.clock.now = time.time()
    client = Client(env)
    conn_id = client.sign_in()
    record = keychain_record(fresh_keyring, conn_id)
    id_token = record["id_token"]

    # Reauthorization builds an authorize URL carrying id_token_hint; it never leaves the engine but to the browser's navigation.
    started = client.json("POST", "/api/v1/ai/signin/chatgpt", {"connection_id": conn_id})
    assert "id_token_hint" not in started["authorize_url"]
    client.json("DELETE", f"/api/v1/ai/signin/{started['flow_id']}")

    store = env.store()
    store.set_capability(conn_id, "tools")
    store.activate(conn_id)
    call_item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_j",
        "name": "journal_recent",
        "arguments": "{}",
        "namespace": "krellbot",
    }
    env.openai.stream_replies.append(
        sse(
            {"type": "response.output_item.done", "output_index": 0, "item": call_item},
            completed(call_item),
            named=True,
        )
    )
    env.openai.stream_replies.append(text_stream("All quiet."))
    krell = KrellClient(home, env.openai)
    events = krell.stream("/api/v1/krell/messages", {"text": "Read the journal."})
    assert any(e["type"] == "tool_result" for e in events)
    env.openai.stream_replies.append(text_stream())
    client.json("POST", f"/api/v1/ai/connections/{conn_id}/probe")
    client.json("GET", "/api/v1/ai/connections")
    client.json("GET", "/api/v1/krell/conversation")
    client.json("POST", f"/api/v1/ai/connections/{conn_id}/signout")

    out, err = capfd.readouterr()
    browser = b"".join(client.raw).decode() + json.dumps(events)
    logged = out + err + caplog.text
    on_disk = everything_written(home)
    for secret in (ACCESS, REFRESH, CODE, id_token):
        assert secret not in browser
        assert secret not in logged
        assert secret not in on_disk  # connections.json, chatgpt.json, the journal and Krell's conversation store
    assert (home / "journal").exists()


def test_the_real_listener_binds_127_0_0_1_redirects_the_launch_and_serves_one_callback(home, fresh_keyring):
    env = Env(home)
    flow = oauth.start_chatgpt_signin(
        home,
        model="gpt-plan-a",
        clock=time.time,
        transport=env.openai,
        registry=env.registry,
        allow_injected_fake_backend=True,
    )
    launch = urlsplit(flow.authorize_url)
    assert launch.hostname == "127.0.0.1"

    def get(target: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", launch.port, timeout=5)
        try:
            conn.request("GET", target, headers={"Host": f"127.0.0.1:{launch.port}"})
            res = conn.getresponse()
            return res.status, {k.lower(): v for k, v in res.getheaders()}, res.read()
        finally:
            conn.close()

    status, headers, _body = get(launch.path)
    assert status == 302
    assert headers["location"].startswith(AUTHORIZE_URL + "?")
    assert headers["referrer-policy"] == "no-referrer" and headers["cache-control"] == "no-store"
    params = {k: v[0] for k, v in parse_qs(urlsplit(headers["location"]).query).items()}
    assert params["redirect_uri"] == f"http://127.0.0.1:{launch.port}/auth/callback"
    env.nonce["nonce"] = params["nonce"]
    env.openai.token_replies.append(token_reply(env.nonce, id_claims=claims(params["nonce"], now=time.time())))

    status, _headers, body = get(f"/auth/callback?code={CODE}&state={params['state']}&client_id={CLIENT}")
    assert status == 200 and b"close this tab" in body
    deadline = time.monotonic() + 5
    while oauth.signin_status(flow.id, registry=env.registry)["state"] == "waiting" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert oauth.signin_status(flow.id, registry=env.registry)["state"] == "connected"
    with pytest.raises(OSError):
        for _ in range(50):
            get(f"/auth/callback?code={CODE}&state={params['state']}")
            time.sleep(0.05)


def test_every_chatgpt_request_goes_only_to_openai_hosts(home, fresh_keyring):
    env = Env(home)
    client = Client(env)
    conn_id = client.sign_in()
    env.openai.stream_replies.append(text_stream())
    client.json("POST", f"/api/v1/ai/connections/{conn_id}/probe")
    client.json("DELETE", f"/api/v1/ai/connections/{conn_id}")
    hosts = {urlsplit(url).hostname for url in env.openai.all_outbound()}
    assert hosts == {"auth.openai.com", "api.openai.com"}
    assert all(url.startswith("https://") for url in env.openai.all_outbound())
    assert oauth.CHATGPT_EGRESS_HOSTS == frozenset({"auth.openai.com", "api.openai.com"})


# ---- 12. a keychain that holds 1280 characters per secret (Windows Credential Manager) -------------

WINDOWS_LIMIT = 1280  # 2560 bytes of UTF-16


@pytest.fixture
def windows_keyring(fresh_keyring):
    """The in-tree fake, refusing any value longer than Windows' CredWrite blob holds."""

    original = fresh_keyring.set_password

    def set_password(service, username, password):
        if len(password) > WINDOWS_LIMIT:
            raise RuntimeError("CredWrite: the stub received bad data")
        original(service, username, password)

    fresh_keyring.set_password = set_password
    return fresh_keyring


LONG_ACCESS = "oai-ACCESS-long-" + "a" * 2600
LONG_REFRESH = "oai-REFRESH-long-" + "r" * 1900


def _chunk_entries(entries: dict[str, str]) -> dict[str, str]:
    return {user: value for user, value in entries.items() if user != "credential"}


def _assert_one_generation(entries: dict[str, str]) -> None:
    """The header names exactly the chunks present: no stale piece of an older generation is left."""

    header = connections._parse_chunk_header(entries["credential"])
    assert header is not None
    assert set(_chunk_entries(entries)) == set(connections._chunk_names(header))


def test_a_chatgpt_record_is_stored_in_chunks_a_1280_character_keychain_accepts(home, windows_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    entries = keychain_entries(windows_keyring, conn_id)
    assert len(_chunk_entries(entries)) >= 2
    assert all(len(value) <= WINDOWS_LIMIT for value in entries.values())
    # The header: a random 32-hex generation, the piece count and the total length. No digest of the record.
    match = re.fullmatch(r"krellbot-chunked-v1:([0-9a-f]{32}):(\d+):(\d+)", entries["credential"])
    assert match is not None
    generation = match.group(1)
    assert all(re.fullmatch(rf"credential\.{generation}\.\d+", user) for user in _chunk_entries(entries))
    # Every piece starts with the same generation id.
    assert all(value.startswith(generation + ":") for value in _chunk_entries(entries).values())
    _assert_one_generation(entries)
    record = keychain_record(windows_keyring, conn_id)
    assert (record["client_id"], record["access_token"], record["refresh_token"]) == (CLIENT, ACCESS, REFRESH)
    assert int(match.group(3)) == len(connections._read_secret(windows_keyring, f"krellbot-ai:{conn_id}"))
    # The token set never sits in the header, and the generation is not derived from it.
    assert ACCESS not in entries["credential"] and REFRESH not in entries["credential"]
    oauth.chatgpt_sign_out(env.store(), env.store().get(conn_id), transport=env.openai, sleep=lambda s: None)
    again = env.sign_in()
    assert keychain_entries(windows_keyring, again)["credential"].split(":")[1] != generation


def test_a_refresh_to_a_token_set_of_another_size_replaces_every_chunk(home, windows_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    before = set(_chunk_entries(keychain_entries(windows_keyring, conn_id)))

    env.clock.now = NOW + 3590
    env.openai.token_replies.append(refresh_reply(LONG_ACCESS, LONG_REFRESH))
    assert oauth.chatgpt_access_token(store, store.get(conn_id), transport=env.openai, clock=env.clock) == LONG_ACCESS
    grown = keychain_entries(windows_keyring, conn_id)
    _assert_one_generation(grown)
    assert len(_chunk_entries(grown)) > len(before)
    assert not before & set(grown)  # the previous generation is gone
    assert all(len(value) <= WINDOWS_LIMIT for value in grown.values())

    env.clock.now += 3590
    env.openai.token_replies.append(refresh_reply())
    assert oauth.chatgpt_access_token(store, store.get(conn_id), transport=env.openai, clock=env.clock) == ACCESS_2
    shrunk = keychain_entries(windows_keyring, conn_id)
    _assert_one_generation(shrunk)
    assert len(_chunk_entries(shrunk)) < len(_chunk_entries(grown))
    record = keychain_record(windows_keyring, conn_id)
    assert (record["access_token"], record["refresh_token"]) == (ACCESS_2, REFRESH_2)


def test_sign_out_and_removal_leave_no_stale_chunks(home, windows_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    oauth.chatgpt_sign_out(store, store.get(conn_id), transport=env.openai, sleep=lambda s: None)
    entries = keychain_entries(windows_keyring, conn_id)
    # The cleared record is short: one plain entry, every old chunk deleted.
    assert set(entries) == {"credential"}
    assert keychain_record(windows_keyring, conn_id)["client_id"] == CLIENT

    other = env.sign_in()
    assert _chunk_entries(keychain_entries(windows_keyring, other))
    oauth.remove_chatgpt_connection(store, store.get(other), transport=env.openai, sleep=lambda s: None)
    assert keychain_entries(windows_keyring, other) == {}


def test_a_legacy_single_entry_api_key_still_loads_and_a_long_key_is_chunked(home, windows_keyring):
    store = ConnectionStore(home, allow_injected_fake_backend=True)
    short = store.add("openai", model="gpt-x", credential="sk-legacy-SINGLE-0001")
    assert keychain_entries(windows_keyring, short.id) == {"credential": "sk-legacy-SINGLE-0001"}
    assert store.credential(short.id) == "sk-legacy-SINGLE-0001"
    # An entry written before chunking existed reads exactly as before.
    windows_keyring._store[(f"krellbot-ai:{short.id}", "credential")] = "sk-written-by-an-older-krellbot"
    assert store.credential(short.id) == "sk-written-by-an-older-krellbot"

    long_key = "sk-long-" + "k" * 3000
    keyed = store.add("custom_openai", model="m", credential=long_key[:4096], base_url="https://llm.example/v1")
    assert len(_chunk_entries(keychain_entries(windows_keyring, keyed.id))) == 4
    assert store.credential(keyed.id) == long_key
    store.remove(keyed.id)
    assert keychain_entries(windows_keyring, keyed.id) == {}


def _torn(entries: dict[str, str], how: str) -> tuple[str, str | None]:
    """One chunk made torn or stale: ``(chunk name, new value or None to delete it)``."""

    name = min(_chunk_entries(entries))
    value = entries[name]
    generation, piece = value.split(":", 1)
    other = ("0" if generation[0] != "0" else "1") + generation[1:]
    return name, {
        "other_generation": f"{other}:{piece}",
        "missing": None,
        "short": f"{generation}:{piece[:-1]}",
        "no_prefix": piece,
    }[how]


@pytest.mark.parametrize("how", ["other_generation", "missing", "short", "no_prefix"])
def test_a_torn_or_stale_chunk_fails_closed_and_never_yields_a_partial_record(home, windows_keyring, how):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    name, value = _torn(keychain_entries(windows_keyring, conn_id), how)
    key = (f"krellbot-ai:{conn_id}", name)
    if value is None:
        del windows_keyring._store[key]
    else:
        windows_keyring._store[key] = value

    with pytest.raises(connections.TornSecret):
        connections._read_secret(windows_keyring, f"krellbot-ai:{conn_id}")
    with pytest.raises(ConnectionRefused) as refused:
        store.credential(conn_id)
    assert refused.value.code == "ai_keychain_unavailable"
    with pytest.raises(ConnectionRefused) as refused:
        oauth.chatgpt_access_token(store, store.get(conn_id), transport=env.openai, clock=env.clock)
    assert refused.value.code == "ai_keychain_unavailable"
    assert env.openai.forms_to(REVOKE_URL) == []

    # A torn record never blocks removal: every entry goes, and the person is told nothing was revoked.
    result = oauth.remove_chatgpt_connection(store, store.get(conn_id), transport=env.openai, sleep=lambda s: None)
    assert result.revocation == "not_confirmed" and "could not be read" in result.message
    assert keychain_entries(windows_keyring, conn_id) == {}
    assert store.list() == []


def test_a_replacement_that_fails_part_way_keeps_the_previous_record(home, windows_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    before = keychain_entries(windows_keyring, conn_id)
    written = windows_keyring.set_password
    calls = {"n": 0}

    def flaky(service, username, password):
        calls["n"] += 1
        if calls["n"] == 2:  # the second chunk of the new generation
            raise RuntimeError("keychain busy")
        written(service, username, password)

    windows_keyring.set_password = flaky
    with pytest.raises(ConnectionRefused) as refused:
        store.replace_record(
            conn_id, json.dumps({**keychain_record(windows_keyring, conn_id), "access_token": LONG_ACCESS})
        )
    assert refused.value.code == "ai_keychain_unavailable"
    assert keychain_entries(windows_keyring, conn_id) == before
    assert keychain_record(windows_keyring, conn_id)["access_token"] == ACCESS


# ---- 13. "Continue as": every later sign-in of an account reuses its issued client -----------------


def test_readding_a_removed_account_reuses_its_issued_client_id(home, fresh_keyring):
    env = Env(home)
    first = env.sign_in()
    store = env.store()
    oauth.remove_chatgpt_connection(store, store.get(first), transport=env.openai, sleep=lambda s: None)
    [saved] = oauth.chatgpt_saved_accounts(home)
    assert saved["email"] == EMAIL and re.fullmatch(r"[0-9a-f]{16}", saved["id"])
    assert CLIENT not in json.dumps(saved)

    env.openai.form_requests.clear()
    flow = oauth.start_chatgpt_signin(
        home,
        model="gpt-plan-a",
        clock=env.clock,
        transport=env.openai,
        listener_factory=env.factory,
        registry=env.registry,
        account=saved["id"],
        allow_injected_fake_backend=True,
    )
    listener = env.factory.last
    params = env.authorize(flow, listener)
    assert params["client_id"] == CLIENT
    assert "agent_name_hint" not in params
    assert params["login_hint"] == EMAIL
    assert "id_token_hint" not in params  # no ID token outlives the removed connection's record
    assert params["ext_agent_host_id"] == oauth.chatgpt_host_id(home)

    env.openai.token_replies.append(token_reply(env.nonce))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}")
    status = env.status(flow)
    assert status["state"] == "connected" and status["connection_id"] != first
    assert [f["client_id"] for f in env.openai.forms_to(TOKEN_URL)] == [CLIENT]
    assert keychain_record(fresh_keyring, status["connection_id"])["client_id"] == CLIENT
    assert accounts(chatgpt_state(home)) == [
        {"client_id": CLIENT, "issuer": ISSUER, "subject": SUBJECT, "email": EMAIL}
    ]
    assert oauth.chatgpt_saved_accounts(home) == []  # in use again


def test_continue_as_refuses_a_sign_in_to_another_account(home, fresh_keyring):
    env = Env(home)
    first = env.sign_in()
    store = env.store()
    oauth.remove_chatgpt_connection(store, store.get(first), transport=env.openai, sleep=lambda s: None)
    [saved] = oauth.chatgpt_saved_accounts(home)
    flow = oauth.start_chatgpt_signin(
        home,
        model="m",
        clock=env.clock,
        transport=env.openai,
        listener_factory=env.factory,
        registry=env.registry,
        account=saved["id"],
        allow_injected_fake_backend=True,
    )
    params = env.authorize(flow, env.factory.last)
    env.openai.token_replies.append(
        lambda fields: {
            **token_reply(env.nonce)(fields),
            "id_token": signer().sign(claims(env.nonce["nonce"], sub="someone-else")),
        }
    )
    env.factory.last.request(f"/auth/callback?code={CODE}&state={params['state']}")
    assert env.status(flow)["code"] == "ai_chatgpt_account_mismatch"
    assert env.store().list() == []

    with pytest.raises(ConnectionRefused) as refused:
        oauth.start_chatgpt_signin(
            home,
            model="m",
            clock=env.clock,
            transport=env.openai,
            listener_factory=env.factory,
            registry=env.registry,
            account="0" * 16,
            allow_injected_fake_backend=True,
        )
    assert refused.value.code == "ai_chatgpt_account_unknown"


def test_a_different_account_registers_a_new_client_and_a_mapped_account_updates_its_own(home, fresh_keyring):
    env = Env(home)
    env.sign_in()
    # "Use a different ChatGPT account": dynamic registration, another verified account.
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    assert params["client_id"] == "dynamic_agent_client" and params["agent_name_hint"] == "Krellbot"
    env.openai.token_replies.append(
        token_reply(
            env.nonce,
            client=OTHER_CLIENT,
            id_claims=claims(env.nonce["nonce"], aud=OTHER_CLIENT, sub="second-user", email="second@example.com"),
        )
    )
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={OTHER_CLIENT}")
    assert env.status(flow)["state"] == "connected"
    accounts = chatgpt_state(home)["accounts"]
    assert sorted((a["client_id"], a["subject"]) for a in accounts) == sorted(
        [(CLIENT, SUBJECT), (OTHER_CLIENT, "second-user")]
    )

    # A new registration for an account already mapped replaces its client id instead of duplicating it.
    third = "oaiapp_THIRD-client-9d9d"
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(token_reply(env.nonce, client=third))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={third}")
    assert env.status(flow)["state"] == "connected"
    accounts = chatgpt_state(home)["accounts"]
    assert sorted((a["client_id"], a["subject"]) for a in accounts) == sorted(
        [(third, SUBJECT), (OTHER_CLIENT, "second-user")]
    )


def test_the_providers_route_offers_saved_accounts_and_the_start_route_continues_as_one(home, fresh_keyring):
    env = Env(home)
    client = Client(env)
    conn_id = client.sign_in()
    openai = next(p for p in client.json("GET", "/api/v1/ai/providers")["providers"] if p["id"] == "openai")
    assert openai["chatgpt_accounts"] == []  # the account has a connection
    client.json("DELETE", f"/api/v1/ai/connections/{conn_id}")
    providers = client.json("GET", "/api/v1/ai/providers")["providers"]
    [saved] = next(p for p in providers if p["id"] == "openai")["chatgpt_accounts"]
    assert saved["email"] == EMAIL
    assert all("chatgpt_accounts" not in p for p in providers if p["id"] != "openai")
    assert CLIENT.encode() not in client.raw[-1]

    started = client.json("POST", "/api/v1/ai/signin/chatgpt", {"model": "gpt-plan-a", "account": saved["id"]})
    flow = oauth.SigninFlow(id=started["flow_id"], authorize_url=started["authorize_url"], state="")
    assert env.authorize(flow, env.factory.last)["client_id"] == CLIENT
    for bad in ({"model": "m", "account": 3}, {"connection_id": conn_id, "account": saved["id"]}):
        status, raw = client.call("POST", "/api/v1/ai/signin/chatgpt", bad)
        assert status == 400 and json.loads(raw)["code"] == "ai_body_invalid"
    status, raw = client.call("POST", "/api/v1/ai/signin/chatgpt", {"model": "m", "account": "f" * 16})
    assert status == 404 and json.loads(raw)["code"] == "ai_chatgpt_account_unknown"


def test_an_invalid_grant_first_exchange_keeps_the_issued_client_for_the_next_attempt(home, fresh_keyring):
    env = Env(home)
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(TransportError(400, json.dumps({"error": "invalid_grant"})))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={CLIENT}")
    status = env.status(flow)
    assert status["code"] == "ai_chatgpt_code_rejected" and "Try again" in status["message"]
    assert chatgpt_state(home)["pending_client"] == CLIENT
    assert fresh_keyring._store == {}

    flow, listener = env.start(retry=True)
    params = env.authorize(flow, listener)
    assert params["client_id"] == CLIENT and "agent_name_hint" not in params
    env.openai.token_replies.append(token_reply(env.nonce))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}")
    status = env.status(flow)
    assert status["state"] == "connected"
    assert env.openai.forms_to(TOKEN_URL)[-1]["client_id"] == CLIENT
    state = chatgpt_state(home)
    assert state["pending_client"] is None
    assert accounts(state) == [{"client_id": CLIENT, "issuer": ISSUER, "subject": SUBJECT, "email": EMAIL}]


def test_a_pending_client_that_openai_refuses_otherwise_is_dropped(home, fresh_keyring):
    env = Env(home)
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(TransportError(400, json.dumps({"error": "invalid_grant"})))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={CLIENT}")
    flow, listener = env.start(retry=True)
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(TransportError(401, json.dumps({"error": "invalid_client"})))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}")
    assert chatgpt_state(home)["pending_client"] is None
    flow, listener = env.start(retry=True)
    assert env.authorize(flow, listener)["client_id"] == "dynamic_agent_client"


def _pending_after_invalid_grant(env: Env) -> None:
    flow, listener = env.start()
    params = env.authorize(flow, listener)
    env.openai.token_replies.append(TransportError(400, json.dumps({"error": "invalid_grant"})))
    listener.request(f"/auth/callback?code={CODE}&state={params['state']}&client_id={CLIENT}")
    assert chatgpt_state(env.home)["pending_client"] == CLIENT


def test_use_a_different_account_registers_with_dynamic_agent_client_even_with_a_pending_client(home, fresh_keyring):
    env = Env(home)
    _pending_after_invalid_grant(env)
    flow, listener = env.start()  # no retry: "Use a different ChatGPT account" or a plain new sign-in
    params = env.authorize(flow, listener)
    assert params["client_id"] == "dynamic_agent_client" and params["agent_name_hint"] == "Krellbot"


@pytest.mark.parametrize("ending", ["cancel", "expiry", "error_callback"])
def test_a_retry_that_is_cancelled_expires_or_is_refused_at_openai_clears_the_pending_client(
    home, fresh_keyring, ending
):
    env = Env(home)
    _pending_after_invalid_grant(env)
    flow, listener = env.start(retry=True)
    params = env.authorize(flow, listener)
    assert params["client_id"] == CLIENT
    if ending == "cancel":
        cancelled, status = oauth.cancel_signin(flow.id, registry=env.registry)
        assert cancelled and status["code"] == "ai_signin_cancelled"
    elif ending == "expiry":
        env.clock.now += oauth.SIGNIN_LIFETIME_SECONDS
        assert env.status(flow)["code"] == "ai_signin_expired"
    else:
        listener.request(f"/auth/callback?error=invalid_client&state={params['state']}")
        assert env.status(flow)["code"] == "ai_chatgpt_signin_error"
    assert chatgpt_state(home)["pending_client"] is None
    # The next Try again has nothing to retry with, so it registers afresh.
    flow, listener = env.start(retry=True)
    assert env.authorize(flow, listener)["client_id"] == "dynamic_agent_client"


def test_the_start_route_takes_retry_as_a_boolean(home, fresh_keyring):
    env = Env(home)
    client = Client(env)
    _pending_after_invalid_grant(env)
    status, raw = client.call("POST", "/api/v1/ai/signin/chatgpt", {"model": "m", "retry": "yes"})
    assert status == 400 and json.loads(raw)["code"] == "ai_body_invalid"
    started = client.json("POST", "/api/v1/ai/signin/chatgpt", {"model": "m", "retry": True})
    flow = oauth.SigninFlow(id=started["flow_id"], authorize_url=started["authorize_url"], state="")
    assert env.authorize(flow, env.factory.last)["client_id"] == CLIENT


# ---- 14. mid-turn refresh failures, revocation causes, the lock during revocation --------------------


def test_a_mid_turn_refresh_failure_is_temporary_unless_the_signin_ended(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    adapter = oauth.chatgpt_adapter(store, store.get(conn_id), transport=env.openai, clock=env.clock)
    env.openai.stream_replies.append(text_stream())
    assert isinstance(
        list(adapter.stream(model="m", system="", messages=HISTORY[:1], tools=(), max_tokens=1))[-1], Done
    )

    env.clock.now = NOW + 3590
    env.openai.token_replies.append(TransportError(None, "URLError"))
    [blip] = list(adapter.stream(model="m", system="", messages=HISTORY[:1], tools=(), max_tokens=1))
    assert blip.code == "provider_unavailable"

    env.openai.token_replies.append(TransportError(400, json.dumps({"error": "invalid_grant"})))
    [ended] = list(adapter.stream(model="m", system="", messages=HISTORY[:1], tools=(), max_tokens=1))
    assert ended.code == "provider_auth_failed"
    assert len(env.openai.requests) == 1  # no inference ran without a token


@pytest.mark.parametrize(
    ("script", "cause"),
    [
        ({"revoke": [TransportError(400, json.dumps({"error": "unsupported_token_type"}))]}, "refused the revocation"),
        ({"revoke": [TransportError(None, "reset")] * 3}, "could not be reached"),
        ({"no_endpoint": True}, "no usable revocation address"),
    ],
)
def test_an_unconfirmed_revocation_says_why(home, fresh_keyring, script, cause):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    env.openai.revoke_replies.extend(script.get("revoke", []))
    if script.get("no_endpoint"):
        del env.openai.discovery["revocation_endpoint"]
    result = oauth.chatgpt_sign_out(store, store.get(conn_id), transport=env.openai, sleep=lambda s: None)
    assert result.revocation == "not_confirmed"
    assert cause in result.message and "ChatGPT settings" in result.message
    assert keychain_record(fresh_keyring, conn_id)["refresh_token"] is None


def test_revocation_runs_outside_the_record_lock_and_no_refresh_rotates_meanwhile(home, fresh_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    seen: dict = {}

    def revoke(fields):
        # Another thread asks for a token while OpenAI is answering the revocation.
        def ask() -> None:
            store = env.store()
            try:
                oauth.chatgpt_access_token(store, store.get(conn_id), transport=env.openai, clock=env.clock)
            except ConnectionRefused as exc:
                seen["code"] = exc.code

        worker = threading.Thread(target=ask)
        worker.start()
        worker.join(2)
        seen["blocked"] = worker.is_alive()
        return 200, b""

    env.openai.revoke_replies.append(revoke)
    store = env.store()
    result = oauth.chatgpt_sign_out(store, store.get(conn_id), transport=env.openai, sleep=lambda s: None)
    assert result.revocation == "confirmed"
    assert seen == {"blocked": False, "code": "ai_chatgpt_signin_required"}
    assert [f["grant_type"] for f in env.openai.forms_to(TOKEN_URL)] == ["authorization_code"]


def test_a_usage_limit_notice_keeps_its_diagnostics_across_a_reload(home, fresh_keyring):
    from tests.test_krell_loop_api import Client as KrellClient

    env = Env(home)
    env.clock.now = time.time()
    conn_id = env.sign_in()
    store = env.store()
    store.set_capability(conn_id, "tools")
    store.activate(conn_id)
    krell = KrellClient(home, env.openai)
    body = _error_body("subscription_sharing_usage_limit_exceeded", message="x" * 1000)
    exc = TransportError(429, body)
    exc.request_id = "req_KEEP"
    env.openai.stream_replies.append(exc)
    krell.stream("/api/v1/krell/messages", {"text": "hello"})

    convo = krell.json("GET", "/api/v1/krell/conversation")
    [notice] = [m["payload"] for m in convo["messages"] if m["payload"].get("kind") == "error"]
    assert notice["code"] == "provider_usage_limit"
    provider = notice["provider"]
    assert (provider["status"], provider["code"], provider["request_id"]) == (
        429,
        "subscription_sharing_usage_limit_exceeded",
        "req_KEEP",
    )
    assert provider["detail"] == body[:300]
    assert ACCESS not in json.dumps(convo) and REFRESH not in json.dumps(convo)


# ---- 15. a write that dies part way leaves no orphaned pieces; redacted diagnostics --------------------


class Crash(BaseException):
    """The process dying mid-write: nothing after it runs, not even best-effort cleanup."""


def _die_before_the_header(keyring_fake, home: Path, conn_id: str, record: dict) -> None:
    """Replace the record, dying after its pieces are written and before its header commits."""

    set_password, delete_password = keyring_fake.set_password, keyring_fake.delete_password

    def dying_set(service, username, password):
        if username == "credential":
            raise Crash()
        set_password(service, username, password)

    def dead_delete(service, username):
        raise RuntimeError("the process is gone")

    keyring_fake.set_password, keyring_fake.delete_password = dying_set, dead_delete
    try:
        with pytest.raises(Crash):
            ConnectionStore(home, allow_injected_fake_backend=True).replace_record(conn_id, json.dumps(record))
    finally:
        keyring_fake.set_password, keyring_fake.delete_password = set_password, delete_password


@pytest.mark.parametrize("cleanup", ["remove_chatgpt_connection", "store_remove", "next_write"])
def test_a_write_that_dies_before_its_header_leaves_no_orphaned_pieces(home, windows_keyring, cleanup):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    before = keychain_entries(windows_keyring, conn_id)
    _die_before_the_header(
        windows_keyring, home, conn_id, {**keychain_record(windows_keyring, conn_id), "access_token": LONG_ACCESS}
    )

    after = keychain_entries(windows_keyring, conn_id)
    orphans = set(after) - set(before)
    assert "credential.pending" in orphans and len(orphans) >= 3  # the new pieces and the marker
    assert ACCESS not in after["credential.pending"] and LONG_ACCESS[:40] not in after["credential.pending"]
    assert after["credential"] == before["credential"]
    # The old record is still whole and readable.
    assert keychain_record(windows_keyring, conn_id)["access_token"] == ACCESS

    if cleanup == "remove_chatgpt_connection":
        oauth.remove_chatgpt_connection(store, store.get(conn_id), transport=env.openai, sleep=lambda s: None)
        assert keychain_entries(windows_keyring, conn_id) == {}
    elif cleanup == "store_remove":
        store.remove(conn_id)
        assert keychain_entries(windows_keyring, conn_id) == {}
    else:
        env.clock.now = NOW + 3590
        env.openai.token_replies.append(refresh_reply())
        assert oauth.chatgpt_access_token(store, store.get(conn_id), transport=env.openai, clock=env.clock) == ACCESS_2
        swept = keychain_entries(windows_keyring, conn_id)
        assert "credential.pending" not in swept
        _assert_one_generation(swept)
        assert not orphans & set(swept)


def test_a_write_that_dies_after_its_header_has_its_old_pieces_swept(home, windows_keyring):
    env = Env(home)
    conn_id = env.sign_in()
    store = env.store()
    old_pieces = set(_chunk_entries(keychain_entries(windows_keyring, conn_id)))
    deleted = windows_keyring.delete_password

    def dead_delete(service, username):
        raise RuntimeError("cleanup failed")

    # The header commits, then every best-effort delete fails: the old generation and the marker stay.
    windows_keyring.delete_password = dead_delete
    try:
        store.replace_record(
            conn_id, json.dumps({**keychain_record(windows_keyring, conn_id), "access_token": LONG_ACCESS})
        )
    finally:
        windows_keyring.delete_password = deleted
    left = keychain_entries(windows_keyring, conn_id)
    assert old_pieces <= set(left) and "credential.pending" in left
    assert keychain_record(windows_keyring, conn_id)["access_token"] == LONG_ACCESS

    store.remove(conn_id)
    assert keychain_entries(windows_keyring, conn_id) == {}


FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMzQ1In0.c2lnbmF0dXJlLXNpZ25hdHVyZS1zaWc"


def test_the_stored_body_excerpt_is_redacted_before_it_reaches_the_browser(home, fresh_keyring):
    from tests.test_krell_loop_api import Client as KrellClient

    env = Env(home)
    env.clock.now = time.time()
    conn_id = env.sign_in()
    store = env.store()
    store.set_capability(conn_id, "tools")
    store.activate(conn_id)
    krell = KrellClient(home, env.openai)
    leaky = f"token {FAKE_JWT} rejected; header was Bearer {ACCESS}; keys sk-proj-ABCDEF123456 and sess-XYZ987654"
    exc = TransportError(429, _error_body("subscription_sharing_usage_limit_exceeded", message=leaky))
    exc.request_id = "req_REDACT"
    env.openai.stream_replies.append(exc)
    krell.stream("/api/v1/krell/messages", {"text": "hello"})

    convo = krell.json("GET", "/api/v1/krell/conversation")
    text = json.dumps(convo)
    for secret in (FAKE_JWT, FAKE_JWT.split(".")[1], ACCESS, "sk-proj-ABCDEF123456", "sess-XYZ987654"):
        assert secret not in text
    [notice] = [m["payload"] for m in convo["messages"] if m["payload"].get("kind") == "error"]
    detail = notice["provider"]["detail"]
    assert "[redacted-jwt]" in detail and "Bearer [redacted]" in detail and "[redacted-key]" in detail
    assert notice["provider"]["request_id"] == "req_REDACT"


def test_a_credential_cut_by_the_excerpts_end_is_still_redacted():
    from krellbot.agent import krell_loop

    failure = ProviderError("provider_rate_limited", "x", status=429, detail="x" * 290 + FAKE_JWT)
    stored = krell_loop._provider_diagnostics(failure)["stored"]["detail"]
    assert len(stored) <= krell_loop.MAX_PROVIDER_DETAIL_CHARS
    assert "eyJ" not in stored and FAKE_JWT[:12] not in stored
    assert krell_loop.redact_diagnostic("Authorization: bearer abc.DEF-123") == "Authorization: Bearer [redacted]"
