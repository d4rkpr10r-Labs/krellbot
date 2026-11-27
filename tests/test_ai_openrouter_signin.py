"""Sign in with OpenRouter: PKCE S256 through a single-use loopback callback.

The browser goes to OpenRouter's ``/auth`` page. OpenRouter redirects to a
short-lived listener on ``127.0.0.1`` with ``?code=``; the engine exchanges
the code (with the PKCE verifier) for a key at ``/api/v1/auth/keys`` and
stores it in the keychain as an ``oauth`` connection.

OpenRouter's docs disagree on whether a ``state`` query parameter comes back
on the callback, so each flow is bound by an unguessable callback path
segment (``/callback/<state>``) instead. Any other path, host or method is a
404 that leaves the flow waiting.

Fakes only: ``FakeListener`` stands in for the loopback socket,
``FakeAiTransport`` for OpenRouter and ``FakeKeyring`` for the keychain. One
test drives the real listener over ``127.0.0.1``; nothing leaves the machine.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from krellbot.ai import oauth
from krellbot.ai.connections import ConnectionRefused, ConnectionStore
from krellbot.ai.http import TransportError
from tests.fakes.fake_ai_transport import FakeAiTransport
from tests.test_ns06_api import TEST_PORT, _bootstrap_and_get_csrf, _build_app, asgi_call

CODE = "or-auth-CODE-7f3a9c1e5b"
KEY = "sk-or-v1-SECRET-oauth-key-0042"
FLOW_ID_RE = re.compile(r"^[0-9a-f]{32}$")
CONN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
TOKEN = "boot-ai-signin"


# ---- fakes ----------------------------------------------------------------------


class FakeListener:
    """The loopback listener without a socket: tests deliver requests by hand.

    Like the real one it answers a request, closes when the reply says so,
    and only then runs the reply's ``after`` step (the key exchange).
    """

    port = 51423

    def __init__(self, dispatch, lifetime: float) -> None:
        self.dispatch = dispatch
        self.lifetime = lifetime
        self.closed = False

    def request(self, target: str, *, method: str = "GET", host: str | None = None):
        """The reply, or None when the listener is closed (connection refused)."""

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
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def key_reply(key: str = KEY) -> list[str]:
    return [json.dumps({"key": key, "user_id": "user_123"})]


def start(home: Path, transport: FakeAiTransport, *, clock: Clock | None = None, model: str = "openrouter/auto"):
    factory = ListenerFactory()
    registry = oauth.SigninRegistry()
    flow = oauth.start_openrouter_signin(
        home,
        model=model,
        clock=clock or Clock(),
        transport=transport,
        listener_factory=factory,
        registry=registry,
        allow_injected_fake_backend=True,
    )
    return flow, factory.last, registry


def callback_path(flow) -> str:
    return f"/callback/{flow.state}"


def _store(home: Path) -> ConnectionStore:
    return ConnectionStore(home, allow_injected_fake_backend=True)


# ---- 1. the authorize URL --------------------------------------------------------


def test_signin_url_uses_pkce_s256_and_a_loopback_callback(home, fresh_keyring):
    transport = FakeAiTransport([key_reply()])
    flow, listener, registry = start(home, transport)

    parts = urlsplit(flow.authorize_url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "openrouter.ai", "/auth")
    query = parse_qs(parts.query, strict_parsing=True)
    assert set(query) == {"callback_url", "code_challenge", "code_challenge_method"}
    assert query["code_challenge_method"] == ["S256"]
    assert query["callback_url"] == [f"http://127.0.0.1:{listener.port}/callback/{flow.state}"]
    challenge = query["code_challenge"][0]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge)  # base64url SHA-256, no padding

    # The per-flow callback secret is unguessable and URL-safe; the flow id is a different value.
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", flow.state)
    assert FLOW_ID_RE.match(flow.id)
    assert flow.state not in flow.id
    assert listener.lifetime == 300
    assert oauth.signin_status(flow.id, registry=registry) == {"state": "waiting"}

    # The verifier sent at exchange time hashes to the challenge in the URL.
    listener.request(f"{callback_path(flow)}?code={CODE}")
    verifier = transport.requests[0]["body"]["code_verifier"]
    assert 43 <= len(verifier) <= 128
    assert len(base64.urlsafe_b64decode(verifier + "=" * (-len(verifier) % 4))) == 64
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert digest == challenge
    assert verifier not in flow.authorize_url

    # A second flow gets fresh values.
    other, _listener, _registry = start(home, FakeAiTransport())
    other_query = parse_qs(urlsplit(other.authorize_url).query)
    assert other.state != flow.state
    assert other.id != flow.id
    assert other_query["code_challenge"] != [challenge]


# ---- 2. wrong state -----------------------------------------------------------------


def test_callback_with_wrong_state_is_rejected_and_nothing_stored(home, fresh_keyring):
    transport = FakeAiTransport([key_reply()])
    flow, listener, registry = start(home, transport)
    good = callback_path(flow)
    port = listener.port

    probes = [
        ("GET", f"/callback/{'A' * len(flow.state)}?code={CODE}", None),
        ("GET", f"/callback?code={CODE}", None),
        ("GET", f"/callback?code={CODE}&state={flow.state}", None),
        ("GET", f"{good}x?code={CODE}", None),
        ("GET", f"{good}/?code={CODE}", None),
        ("GET", f"/{flow.state}?code={CODE}", None),
        ("GET", f"http://127.0.0.1:{port}{good}?code={CODE}", None),  # absolute-form target
        ("POST", f"{good}?code={CODE}", None),
        ("HEAD", f"{good}?code={CODE}", None),
        ("GET", f"{good}?code={CODE}", f"evil.example:{port}"),  # DNS rebinding
        ("GET", f"{good}?code={CODE}", f"localhost:{port}"),
        ("GET", f"{good}?code={CODE}", f"127.0.0.1:{port + 1}"),
    ]
    for method, target, host in probes:
        reply = listener.request(target, method=method, host=host)
        assert reply is not None, (method, target, host)
        assert reply.status == 404, (method, target, host)
        assert not reply.close, (method, target, host)
        assert reply.after is None
        assert CODE.encode() not in reply.body
        assert flow.state.encode() not in reply.body

    assert not listener.closed
    assert transport.calls == 0
    assert fresh_keyring._store == {}
    assert _store(home).list() == []
    assert oauth.signin_status(flow.id, registry=registry) == {"state": "waiting"}

    # None of that consumed the flow: the real callback still completes it.
    reply = listener.request(f"{good}?code={CODE}")
    assert reply.status == 200
    status = oauth.signin_status(flow.id, registry=registry)
    assert status["state"] == "connected"


# ---- 3. single use, 300 s -------------------------------------------------------------


def test_listener_closes_after_one_callback_or_300_seconds(home, fresh_keyring):
    # One callback: answered, then the listener is gone.
    transport = FakeAiTransport([key_reply(), key_reply()])
    flow, listener, registry = start(home, transport)
    reply = listener.request(f"{callback_path(flow)}?code={CODE}")
    assert reply.status == 200
    assert reply.close
    assert listener.closed
    assert listener.request(f"{callback_path(flow)}?code={CODE}") is None
    # Even a callback that reaches the flow after it was consumed does nothing.
    late = listener.dispatch("GET", f"{callback_path(flow)}?code={CODE}", f"127.0.0.1:{listener.port}")
    assert late.status == 404 and late.after is None
    assert transport.calls == 1
    assert len(_store(home).list()) == 1

    # No callback: waiting until 300 s, then expired and closed.
    clock = Clock()
    transport = FakeAiTransport([key_reply()])
    flow, listener, registry = start(home, transport, clock=clock)
    clock.now += 299
    assert oauth.signin_status(flow.id, registry=registry) == {"state": "waiting"}
    assert not listener.closed
    clock.now += 1
    status = oauth.signin_status(flow.id, registry=registry)
    assert status["state"] == "expired"
    assert status["code"] == "ai_signin_expired"
    assert listener.closed
    late = listener.dispatch("GET", f"{callback_path(flow)}?code={CODE}", f"127.0.0.1:{listener.port}")
    assert late.status == 404 and late.after is None
    assert transport.calls == 0

    # A callback that arrives after 300 s, before anyone polled, is refused too.
    clock = Clock()
    flow, listener, registry = start(home, transport, clock=clock)
    clock.now += 300
    reply = listener.request(f"{callback_path(flow)}?code={CODE}")
    assert reply.status == 404 and reply.after is None
    assert listener.closed
    assert transport.calls == 0
    assert oauth.signin_status(flow.id, registry=registry)["state"] == "expired"
    assert len(_store(home).list()) == 1  # only the first flow's connection


def test_loopback_listener_binds_127_0_0_1_serves_one_callback_and_logs_nothing(home, fresh_keyring, capfd, caplog):
    caplog.set_level(logging.DEBUG)
    transport = FakeAiTransport([key_reply()])
    registry = oauth.SigninRegistry()
    flow = oauth.start_openrouter_signin(
        home,
        model="openrouter/auto",
        clock=time.monotonic,
        transport=transport,
        registry=registry,
        allow_injected_fake_backend=True,
    )
    callback = urlsplit(parse_qs(urlsplit(flow.authorize_url).query)["callback_url"][0])
    assert callback.hostname == "127.0.0.1"
    port = callback.port
    assert port and port > 0

    def get(target: str, *, method: str = "GET", host: str | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.putrequest(method, target, skip_host=True)
            conn.putheader("Host", host or f"127.0.0.1:{port}")
            conn.endheaders()
            res = conn.getresponse()
            return res.status, {k.lower(): v for k, v in res.getheaders()}, res.read()
        finally:
            conn.close()

    # Wrong path, method and host: 404 with a fixed body that reflects nothing.
    marker = "REFLECT-<script>alert(1)</script>"
    for method, target, host in (
        ("GET", f"/callback/wrong?code={CODE}&x={marker}", None),
        ("POST", f"{callback.path}?code={CODE}", None),
        ("BREW", f"{callback.path}?code={CODE}", None),
        ("GET", f"{callback.path}?code={CODE}", "evil.example"),
    ):
        status, _headers, body = get(target, method=method, host=host)
        assert status == 404, (method, target)
        assert b"REFLECT" not in body and CODE.encode() not in body
    assert oauth.signin_status(flow.id, registry=registry) == {"state": "waiting"}

    status, headers, body = get(f"{callback.path}?code={CODE}&note={marker}")
    assert status == 200
    assert b"You can close this tab" in body
    assert b"REFLECT" not in body and CODE.encode() not in body
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"
    assert "default-src 'none'" in headers["content-security-policy"]

    deadline = time.monotonic() + 5
    while oauth.signin_status(flow.id, registry=registry)["state"] == "waiting" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert oauth.signin_status(flow.id, registry=registry)["state"] == "connected"

    # Single use: the port no longer answers.
    with pytest.raises(OSError):
        for _ in range(50):  # the listener thread closes the socket right after answering
            get(f"{callback.path}?code={CODE}")
            time.sleep(0.05)

    out, err = capfd.readouterr()
    logged = out + err + caplog.text
    for secret in (CODE, KEY, flow.state, transport.requests[0]["body"]["code_verifier"]):
        assert secret not in logged


def test_loopback_listener_closes_when_its_lifetime_ends():
    listener = oauth.LoopbackListener(lambda method, target, host: oauth.CallbackReply(404, b""), 0.2)
    assert listener.port > 0
    listener.join(timeout=5)
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", listener.port, timeout=1).connect()


# ---- 4. the exchange and the keychain --------------------------------------------------


def test_exchanged_key_lands_in_the_ai_keychain_namespace(home, fresh_keyring):
    transport = FakeAiTransport([key_reply()])
    flow, listener, registry = start(home, transport, model="anthropic/claude-sonnet-4")
    listener.request(f"{callback_path(flow)}?code={CODE}")

    request = transport.requests[0]
    assert request["url"] == "https://openrouter.ai/api/v1/auth/keys"
    assert request["body"] == {
        "code": CODE,
        "code_verifier": request["body"]["code_verifier"],
        "code_challenge_method": "S256",
    }
    assert "authorization" not in {h.lower() for h in request["headers"]}

    status = oauth.signin_status(flow.id, registry=registry)
    assert status["state"] == "connected"
    conn_id = status["connection_id"]
    assert CONN_ID_RE.match(conn_id)
    assert set(status) == {"state", "connection_id"}

    assert fresh_keyring._store == {(f"krellbot-ai:{conn_id}", "credential"): KEY}
    [conn] = _store(home).list()
    assert (conn.id, conn.preset, conn.auth, conn.model) == (
        conn_id,
        "openrouter",
        "oauth",
        "anthropic/claude-sonnet-4",
    )
    assert conn.base_url == "https://openrouter.ai/api/v1"
    assert conn.capability == "unprobed"
    raw = (home / "ai" / "connections.json").read_text()
    assert KEY not in raw and CODE not in raw
    assert _store(home).active() is None  # activation is the caller's choice, after a probe


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (TransportError(400, f'{{"error": "invalid code {CODE}"}}'), "ai_signin_rejected"),
        (TransportError(403, ""), "ai_signin_rejected"),
        (TransportError(None, "URLError"), "ai_signin_unavailable"),
        (TransportError(502, KEY), "ai_signin_unavailable"),
        ([json.dumps({"user_id": "u"})], "ai_signin_bad_response"),
        ([json.dumps({"key": ""})], "ai_signin_bad_response"),
        (["not json"], "ai_signin_bad_response"),
        ([json.dumps(["key", KEY])], "ai_signin_bad_response"),
        (["{" + " " * 70_000 + "}"], "ai_signin_bad_response"),
        ([json.dumps({"key": KEY + "\nX-Evil: 1"})], "ai_credential_invalid"),
        ([json.dumps({"key": KEY}), TransportError(None, "reset")], "ai_signin_unavailable"),
    ],
)
def test_a_failed_exchange_is_typed_and_never_echoes_the_code_or_key(home, fresh_keyring, answer, code):
    transport = FakeAiTransport([answer])
    flow, listener, registry = start(home, transport)
    reply = listener.request(f"{callback_path(flow)}?code={CODE}")
    assert reply.status == 200  # the browser tab is told to close either way

    status = oauth.signin_status(flow.id, registry=registry)
    assert status["state"] == "failed"
    assert status["code"] == code
    assert isinstance(status["message"], str) and status["message"]
    text = json.dumps(status)
    assert CODE not in text and KEY not in text
    assert fresh_keyring._store == {}
    assert _store(home).list() == []


@pytest.mark.parametrize(
    "query",
    ["", "?error=access_denied", "?code=", f"?code={CODE}&code=other", "?code=has%20space", "?code=" + "c" * 5000],
)
def test_a_callback_without_a_usable_code_fails_the_flow_without_an_exchange(home, fresh_keyring, query):
    transport = FakeAiTransport([key_reply()])
    flow, listener, registry = start(home, transport)
    reply = listener.request(f"{callback_path(flow)}{query}")
    assert reply.status == 200
    assert listener.closed
    status = oauth.signin_status(flow.id, registry=registry)
    assert (status["state"], status["code"]) == ("failed", "ai_signin_no_code")
    assert transport.calls == 0
    assert fresh_keyring._store == {}


def test_a_new_signin_replaces_a_waiting_one(home, fresh_keyring):
    factory = ListenerFactory()
    registry = oauth.SigninRegistry()
    kwargs = {
        "model": "openrouter/auto",
        "clock": Clock(),
        "transport": FakeAiTransport(),
        "listener_factory": factory,
        "registry": registry,
        "allow_injected_fake_backend": True,
    }
    first = oauth.start_openrouter_signin(home, **kwargs)
    first_listener = factory.last
    second = oauth.start_openrouter_signin(home, **kwargs)
    assert first_listener.closed
    assert not factory.last.closed
    status = oauth.signin_status(first.id, registry=registry)
    assert (status["state"], status["code"]) == ("failed", "ai_signin_superseded")
    assert oauth.signin_status(second.id, registry=registry) == {"state": "waiting"}


def test_signin_refuses_a_bad_model_or_keychain_before_opening_a_listener(home, fresh_keyring):
    factory = ListenerFactory()
    for model in ("", "   ", "m\nx", "m" * 300, None):
        with pytest.raises(ConnectionRefused) as refused:
            oauth.start_openrouter_signin(
                home,
                model=model,  # type: ignore[arg-type]
                clock=Clock(),
                transport=FakeAiTransport(),
                listener_factory=factory,
                registry=oauth.SigninRegistry(),
                allow_injected_fake_backend=True,
            )
        assert refused.value.code == "ai_model_invalid"
    # Without the tests-only opt-in, the in-tree fake keyring is not a safe keychain.
    with pytest.raises(ConnectionRefused) as refused:
        oauth.start_openrouter_signin(
            home,
            model="openrouter/auto",
            clock=Clock(),
            transport=FakeAiTransport(),
            listener_factory=factory,
            registry=oauth.SigninRegistry(),
        )
    assert refused.value.code == "ai_keychain_unavailable"
    assert factory.made == []


def test_unknown_or_malformed_flow_ids_are_not_found(home):
    registry = oauth.SigninRegistry()
    for flow_id in ("", "0" * 32, "../etc", "A" * 32, "0" * 31, "0" * 33):
        with pytest.raises(oauth.SigninRefused) as refused:
            oauth.signin_status(flow_id, registry=registry)
        assert refused.value.code == "ai_signin_not_found"


# ---- 5. routes -----------------------------------------------------------------------------


class Client:
    def __init__(self, home: Path, transport: FakeAiTransport, factory: ListenerFactory, clock: Clock) -> None:
        self.app = _build_app(home, bootstrap_token=TOKEN)
        state = self.app.state.krellbot
        state.ai_transport = transport
        state.ai_signin_listener_factory = factory
        state.clock = clock
        state.keys_allow_fake_backend = True  # tests-only FakeKeyring opt-in
        self.csrf = _bootstrap_and_get_csrf(self.app, TOKEN)
        self.session = next(iter(state.sessions))

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
        return status, raw


def test_signin_routes_are_gated_and_report_the_flow_state(home, fresh_keyring):
    transport = FakeAiTransport([key_reply()])
    factory = ListenerFactory()
    clock = Clock(1_700_000_000)
    client = Client(home, transport, factory, clock)

    # Gates: state change needs CSRF; the status read needs a session.
    status, _raw = client.call("POST", "/api/v1/ai/signin/openrouter", {"model": "openrouter/auto"}, csrf=False)
    assert status == 403
    assert factory.made == []
    status, raw = client.call("POST", "/api/v1/ai/signin/openrouter", {"model": " "})
    assert status == 400 and json.loads(raw)["code"] == "ai_model_invalid"
    status, raw = client.call("POST", "/api/v1/ai/signin/openrouter", ["model"])
    assert status == 400 and json.loads(raw)["code"] == "ai_body_invalid"
    assert factory.made == []

    status, raw = client.call("POST", "/api/v1/ai/signin/openrouter", {"model": "openrouter/auto"})
    assert status == 200, raw
    started = json.loads(raw)
    flow_id = started["flow_id"]
    assert FLOW_ID_RE.match(flow_id)
    assert started["authorize_url"].startswith("https://openrouter.ai/auth?callback_url=")
    listener = factory.last

    status, _raw = client.call("GET", f"/api/v1/ai/signin/{flow_id}", cookie=False)
    assert status == 403
    for bad in ("not-a-flow", "0" * 32, "A" * 32):
        status, raw = client.call("GET", f"/api/v1/ai/signin/{bad}")
        assert status == 404 and json.loads(raw)["code"] == "ai_signin_not_found"

    status, raw = client.call("GET", f"/api/v1/ai/signin/{flow_id}")
    assert status == 200
    assert json.loads(raw)["state"] == "waiting"

    callback = parse_qs(urlsplit(started["authorize_url"]).query)["callback_url"][0]
    listener.request(urlsplit(callback).path + f"?code={CODE}")

    status, raw = client.call("GET", f"/api/v1/ai/signin/{flow_id}")
    assert status == 200
    body = json.loads(raw)
    assert body["state"] == "connected"
    conn_id = body["connection_id"]
    assert CODE.encode() not in raw and KEY.encode() not in raw

    status, raw = client.call("GET", "/api/v1/ai/connections")
    listed = json.loads(raw)
    assert [(c["id"], c["auth"], c["preset"]) for c in listed["connections"]] == [(conn_id, "oauth", "openrouter")]
    assert KEY.encode() not in raw
    assert fresh_keyring._store == {(f"krellbot-ai:{conn_id}", "credential"): KEY}

    # The flow is in engine memory only: nothing about it is written to the home.
    written = "".join(p.read_text(errors="replace") for p in home.rglob("*") if p.is_file())
    assert CODE not in written and KEY not in written and urlsplit(callback).path not in written


def test_signin_status_route_reports_expiry(home, fresh_keyring):
    factory = ListenerFactory()
    clock = Clock(1_700_000_000)
    client = Client(home, FakeAiTransport(), factory, clock)
    status, raw = client.call("POST", "/api/v1/ai/signin/openrouter", {"model": "openrouter/auto"})
    flow_id = json.loads(raw)["flow_id"]
    clock.now += 301
    status, raw = client.call("GET", f"/api/v1/ai/signin/{flow_id}")
    body = json.loads(raw)
    assert status == 200
    assert (body["state"], body["code"]) == ("expired", "ai_signin_expired")
    assert factory.last.closed
