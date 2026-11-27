"""AI connections: keychain storage, capability probe, egress disclosure and routes.

Users connect their own model so Krell can run on it. The credential lives
only in the OS keychain (service ``krellbot-ai:<id>``, username
``credential``); ``<home>/ai/connections.json`` holds metadata and no secret.
Plain HTTP is refused unless the endpoint is on this computer. The probe
marks a model ``tools``, ``chat_only`` or ``failed``. Every route sits behind
the existing session gate (and CSRF for state changes).

Fakes only: ``FakeKeyring`` and ``FakeAiTransport``. No socket is opened.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import keyring
import keyring.errors
import pytest

from krellbot.ai.connections import ConnectionRefused, ConnectionStore, validate_base_url
from krellbot.ai.disclosure import egress_disclosure
from krellbot.ai.http import TransportError
from krellbot.ai.probe import probe
from krellbot.ai.providers import PRESETS
from tests.fakes.fake_ai_transport import FakeAiTransport, sse
from tests.test_ns06_api import TEST_PORT, _bootstrap_and_get_csrf, _build_app, asgi_call

SECRET = "sk-test-SECRET-ai-credential-0042"
ID_RE = re.compile(r"^[0-9a-f]{16}$")
TOKEN = "boot-ai-connections"


# ---- scripted provider replies -------------------------------------------------


def oa_ping_call() -> list[str]:
    tool = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "ping", "arguments": '{"reply":"ok"}'}}
    return sse(
        {"choices": [{"index": 0, "delta": {"tool_calls": [tool]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )


def oa_text_only() -> list[str]:
    return sse(
        {"choices": [{"index": 0, "delta": {"content": "pong"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    )


def an_ping_call() -> list[str]:
    return sse(
        {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 5, "output_tokens": 1}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "ping", "input": {}},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
        named=True,
    )


def an_text_only() -> list[str]:
    return sse(
        {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 5, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "pong"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
        named=True,
    )


MODELS = {"object": "list", "data": [{"id": "model-a"}, {"id": "model-b"}]}
PUBLIC_FIELDS = {
    "id",
    "preset",
    "label",
    "format",
    "base_url",
    "host",
    "model",
    "auth",
    "capability",
    "created_at",
    "active",
    "disclosure",
}


# ---- API helpers ---------------------------------------------------------------


class Client:
    def __init__(self, home: Path, transport: FakeAiTransport | None = None) -> None:
        self.app = _build_app(home, bootstrap_token=TOKEN)
        self.app.state.krellbot.ai_transport = transport
        self.csrf = _bootstrap_and_get_csrf(self.app, TOKEN)
        self.session = next(iter(self.app.state.krellbot.sessions))

    def call(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        cookie: bool = True,
        csrf: bool = True,
        origin: str | None = f"http://127.0.0.1:{TEST_PORT}",
    ) -> tuple[int, bytes]:
        headers = [("Host", f"127.0.0.1:{TEST_PORT}")]
        if origin is not None:
            headers.append(("Origin", origin))
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

    def json(self, method: str, path: str, payload: object = None, *, expect: int = 200) -> dict:
        status, raw = self.call(method, path, payload)
        assert status == expect, (status, raw)
        decoded = json.loads(raw)
        assert isinstance(decoded, dict)
        return decoded


def _connections_file(home: Path) -> Path:
    return home / "ai" / "connections.json"


# ---- 1. keychain namespace, never returned -------------------------------------


def test_api_key_is_stored_in_the_ai_keychain_namespace_and_never_returned(home, fresh_keyring):
    transport = FakeAiTransport([oa_ping_call(), oa_ping_call()], [MODELS, MODELS])
    client = Client(home, transport)

    status, raw = client.call(
        "POST", "/api/v1/ai/connections", {"preset": "openai", "model": "gpt-x", "credential": SECRET}
    )
    assert status == 200, raw
    assert SECRET.encode() not in raw
    added = json.loads(raw)
    conn_id = added["connection"]["id"]
    assert ID_RE.match(conn_id)
    assert added["connection"]["capability"] == "tools"
    assert added["connection"]["auth"] == "api_key"
    assert added["probe"]["models"] == ["model-a", "model-b"]

    # The only keychain entry is the AI namespace for this connection.
    assert fresh_keyring._store == {(f"krellbot-ai:{conn_id}", "credential"): SECRET}

    # The probe used the key on the wire (and nowhere else), with OpenAI's token field.
    assert transport.get_requests[0]["url"] == "https://api.openai.com/v1/models"
    request = transport.requests[0]
    assert request["url"] == "https://api.openai.com/v1/chat/completions"
    assert request["headers"]["authorization"] == f"Bearer {SECRET}"
    assert request["body"]["max_completion_tokens"] == 64
    assert "max_tokens" not in request["body"]
    assert [t["function"]["name"] for t in request["body"]["tools"]] == ["ping"]

    for method, path in (
        ("GET", "/api/v1/ai/connections"),
        ("POST", f"/api/v1/ai/connections/{conn_id}/probe"),
        ("POST", f"/api/v1/ai/connections/{conn_id}/activate"),
        ("GET", "/api/v1/ai/providers"),
    ):
        status, raw = client.call(method, path)
        assert status == 200, (method, path, raw)
        assert SECRET.encode() not in raw, (method, path)

    listed = client.json("GET", "/api/v1/ai/connections")
    assert [c["id"] for c in listed["connections"]] == [conn_id]
    assert listed["active"] == conn_id
    assert set(listed["connections"][0]) == PUBLIC_FIELDS


def test_a_rejected_key_is_never_echoed_by_a_refusal_or_a_failed_probe(home, fresh_keyring):
    echoing = TransportError(401, f'{{"error": "Incorrect API key provided: {SECRET}"}}')
    client = Client(home, FakeAiTransport([echoing], [TransportError(401, SECRET)]))

    status, raw = client.call(
        "POST", "/api/v1/ai/connections", {"preset": "anthropic", "model": "m", "credential": SECRET + "\nX-Evil: 1"}
    )
    assert status == 400
    assert json.loads(raw)["code"] == "ai_credential_invalid"
    assert SECRET.encode() not in raw
    assert fresh_keyring._store == {}

    status, raw = client.call(
        "POST", "/api/v1/ai/connections", {"preset": "anthropic", "model": "m", "credential": SECRET}
    )
    assert status == 200, raw
    assert SECRET.encode() not in raw
    decoded = json.loads(raw)
    assert decoded["connection"]["capability"] == "failed"
    assert decoded["probe"]["error"] == "provider_auth_failed"
    assert decoded["probe"]["models"] == []


# ---- 2. metadata file holds no secret ------------------------------------------


def test_connections_file_holds_no_secret_bytes(home, fresh_keyring):
    store = ConnectionStore(home)
    first = store.add("anthropic", model="claude-x", credential=SECRET)
    second = store.add(
        "custom_openai", model="llama", credential=SECRET + "-2", base_url="https://gateway.example/v1", label="Gateway"
    )
    store.activate(second.id)

    path = _connections_file(home)
    data = path.read_bytes()
    assert SECRET.encode() not in data
    assert b"sk-" not in data
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    record = json.loads(data)
    assert record["active"] == second.id
    for entry in record["connections"]:
        assert set(entry) == {
            "id",
            "preset",
            "label",
            "format",
            "base_url",
            "model",
            "auth",
            "capability",
            "created_at",
        }
    assert store.credential(first.id) == SECRET
    assert store.credential(second.id) == SECRET + "-2"

    # A credential smuggled into the URL is refused before anything is written.
    for url in ("https://user:" + SECRET + "@gateway.example/v1", "https://gateway.example/v1?key=" + SECRET):
        with pytest.raises(ConnectionRefused) as refused:
            store.add("custom_openai", model="llama", credential=None, base_url=url)
        assert refused.value.code == "ai_endpoint_invalid"
        assert SECRET not in str(refused.value)
    assert path.read_bytes() == data
    assert len(fresh_keyring._store) == 2


def test_a_plaintext_keyring_backend_is_refused_and_nothing_is_written(home, monkeypatch):
    class PlaintextKeyring:
        """Stands in for keyrings.alt's file backend, which would put the key in a file."""

        __module__ = "keyrings.alt.file"

        def __init__(self) -> None:
            self.writes: list[tuple] = []

        def set_password(self, *args) -> None:
            self.writes.append(args)

        def get_password(self, *args) -> None:
            return None

    unsafe = PlaintextKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: unsafe)
    with pytest.raises(ConnectionRefused) as refused:
        ConnectionStore(home).add("openai", model="gpt-x", credential=SECRET)
    assert refused.value.code == "ai_keychain_unavailable"
    assert unsafe.writes == []
    assert not _connections_file(home).exists()


# ---- 3. plain HTTP only on loopback --------------------------------------------


def test_plain_http_remote_endpoint_is_refused_but_local_ollama_is_allowed(home):
    for url in (
        "http://example.com/v1",
        "http://192.168.1.5:11434/v1",
        "http://127.0.0.1.evil.example/v1",
        "http://localhost.evil.example/v1",
        "ftp://example.com/v1",
        "file:///etc/passwd",
        "ws://localhost:11434/v1",
        "localhost:11434/v1",
    ):
        with pytest.raises(ConnectionRefused) as refused:
            validate_base_url(url)
        assert refused.value.code == "ai_endpoint_insecure", url
    for url in ("https://", "http://", "https://exa mple.com/v1", "http://localhost@evil.example/v1"):
        with pytest.raises(ConnectionRefused) as refused:
            validate_base_url(url)
        assert refused.value.code in {"ai_endpoint_invalid", "ai_endpoint_insecure"}, url
    for url in (
        "http://localhost:11434/v1",
        "http://LOCALHOST:11434/v1",
        "http://127.0.0.1:1234/v1",
        "http://[::1]:8000/v1",
        "https://gateway.example/v1",
    ):
        validate_base_url(url)

    transport = FakeAiTransport([oa_text_only()], [MODELS])
    client = Client(home, transport)
    status, raw = client.call(
        "POST",
        "/api/v1/ai/connections",
        {"preset": "custom_openai", "model": "llama3", "base_url": "http://example.com/v1", "credential": SECRET},
    )
    assert status == 400
    assert json.loads(raw)["code"] == "ai_endpoint_insecure"
    assert transport.calls == 0 and transport.get_requests == []
    assert not _connections_file(home).exists()

    added = client.json(
        "POST",
        "/api/v1/ai/connections",
        {"preset": "custom_openai", "model": "llama3", "base_url": "http://localhost:11434/v1", "label": "Ollama"},
    )
    assert added["connection"]["auth"] == "none"
    assert added["connection"]["base_url"] == "http://localhost:11434/v1"
    assert transport.requests[0]["url"] == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in transport.requests[0]["headers"]
    assert transport.requests[0]["body"]["max_tokens"] == 64


def test_a_tampered_metadata_file_cannot_point_a_connection_at_plain_http(home, fresh_keyring):
    store = ConnectionStore(home)
    conn = store.add("custom_openai", model="m", credential=SECRET, base_url="https://gateway.example/v1")
    path = _connections_file(home)
    record = json.loads(path.read_text())
    record["connections"][0]["base_url"] = "http://attacker.example/v1"
    path.write_text(json.dumps(record))

    assert store.list() == []
    transport = FakeAiTransport([oa_ping_call()])
    tampered = conn.__class__(**record["connections"][0])
    with pytest.raises(ConnectionRefused) as refused:
        store.adapter(tampered, transport)
    assert refused.value.code == "ai_endpoint_insecure"
    assert transport.calls == 0


def test_a_fixed_provider_endpoint_cannot_be_overridden(home, fresh_keyring):
    with pytest.raises(ConnectionRefused) as refused:
        ConnectionStore(home).add("anthropic", model="m", credential=SECRET, base_url="https://proxy.example")
    assert refused.value.code == "ai_endpoint_fixed"
    assert fresh_keyring._store == {}


# ---- 4. capability probe -------------------------------------------------------


def test_probe_marks_a_model_without_tool_calls_chat_only(home):
    transport = FakeAiTransport([an_text_only(), an_ping_call(), TransportError(None, "down")])
    client = Client(home, transport)

    added = client.json(
        "POST",
        "/api/v1/ai/connections",
        {"preset": "custom_anthropic", "model": "local-model", "base_url": "http://127.0.0.1:8080"},
    )
    conn_id = added["connection"]["id"]
    assert added["connection"]["capability"] == "chat_only"
    assert added["probe"]["capability"] == "chat_only"
    assert added["probe"]["models"] == []  # custom Anthropic endpoints are not asked for a model list
    assert transport.get_requests == []
    request = transport.requests[0]
    assert request["url"] == "http://127.0.0.1:8080/v1/messages"
    assert request["body"]["max_tokens"] == 64
    assert [t["name"] for t in request["body"]["tools"]] == ["ping"]
    assert "x-api-key" not in request["headers"]

    listed = client.json("GET", "/api/v1/ai/connections")
    assert listed["connections"][0]["capability"] == "chat_only"

    reprobed = client.json("POST", f"/api/v1/ai/connections/{conn_id}/probe")
    assert reprobed["probe"]["capability"] == "tools"
    assert client.json("GET", "/api/v1/ai/connections")["connections"][0]["capability"] == "tools"

    failed = client.json("POST", f"/api/v1/ai/connections/{conn_id}/probe")
    assert failed["probe"]["capability"] == "failed"
    assert failed["probe"]["error"] == "provider_unavailable"
    assert client.json("GET", "/api/v1/ai/connections")["connections"][0]["capability"] == "failed"


def test_probe_lists_models_best_effort_and_only_a_ping_call_counts(home, fresh_keyring):
    store = ConnectionStore(home)
    conn = store.add("openrouter", model="some/model", credential=SECRET)
    assert conn.capability == "unprobed"

    def broken_lister():
        raise TransportError(404, SECRET)

    transport = FakeAiTransport([oa_ping_call()])
    result = probe(conn, store.adapter(conn, transport), list_models=broken_lister)
    assert (result.capability, result.models) == ("tools", [])
    assert SECRET not in result.message

    other_tool = [line.replace('"ping"', '"other"') for line in oa_ping_call()]
    result = probe(conn, store.adapter(conn, FakeAiTransport([other_tool])), list_models=lambda: ["a"])
    assert (result.capability, result.models) == ("chat_only", ["a"])


def test_only_the_openai_preset_uses_max_completion_tokens(home, fresh_keyring):
    store = ConnectionStore(home)
    for preset in PRESETS.values():
        if preset.format != "openai_compatible":
            continue
        base_url = "https://gateway.example/v1" if not preset.base_url else None
        conn = store.add(preset.id, model="m", credential=SECRET, base_url=base_url)
        transport = FakeAiTransport([oa_ping_call()])
        assert probe(conn, store.adapter(conn, transport)).capability == "tools"
        body = transport.requests[0]["body"]
        expected = "max_completion_tokens" if preset.id == "openai" else "max_tokens"
        assert body.get(expected) == 64, preset.id
        assert set(body) & {"max_tokens", "max_completion_tokens"} == {expected}, preset.id


def test_providers_route_lists_every_preset_with_its_base_url(home):
    providers = Client(home).json("GET", "/api/v1/ai/providers")["providers"]
    by_id = {p["id"]: p for p in providers}
    assert {k: v["base_url"] for k, v in by_id.items()} == {
        "anthropic": "https://api.anthropic.com",
        "openai": "https://api.openai.com/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "xai": "https://api.x.ai/v1",
        "mistral": "https://api.mistral.ai/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "custom_openai": None,
        "custom_anthropic": None,
    }
    assert by_id["anthropic"]["format"] == "anthropic_messages"
    assert by_id["custom_anthropic"]["format"] == "anthropic_messages"
    assert by_id["openrouter"]["signin"] == "openrouter"
    assert by_id["openai"]["signin"] == "chatgpt"
    assert by_id["custom_openai"]["credential"] == "optional"
    assert by_id["mistral"]["credential"] == "required"


# ---- 5. remove deletes the keychain entry --------------------------------------


def test_removing_a_connection_deletes_its_keychain_entry(home, fresh_keyring):
    client = Client(home, FakeAiTransport([oa_ping_call(), oa_ping_call()], [MODELS, MODELS]))
    first = client.json("POST", "/api/v1/ai/connections", {"preset": "xai", "model": "grok", "credential": SECRET})
    second = client.json("POST", "/api/v1/ai/connections", {"preset": "mistral", "model": "m", "credential": "sk-2"})
    first_id, second_id = first["connection"]["id"], second["connection"]["id"]
    client.json("POST", f"/api/v1/ai/connections/{first_id}/activate")
    assert (f"krellbot-ai:{first_id}", "credential") in fresh_keyring._store

    removed = client.json("DELETE", f"/api/v1/ai/connections/{first_id}")
    assert removed["removed"] == first_id
    assert fresh_keyring._store == {(f"krellbot-ai:{second_id}", "credential"): "sk-2"}
    listed = client.json("GET", "/api/v1/ai/connections")
    assert [c["id"] for c in listed["connections"]] == [second_id]
    assert listed["active"] is None
    assert ConnectionStore(home).active() is None
    assert ConnectionStore(home).credential(first_id) is None

    status, raw = client.call("DELETE", f"/api/v1/ai/connections/{first_id}")
    assert status == 404, raw


def test_a_keychain_failure_keeps_the_keyed_connection_but_never_blocks_a_keyless_one(home, fresh_keyring, monkeypatch):
    store = ConnectionStore(home)
    keyed = store.add("openai", model="gpt-x", credential=SECRET)
    keyless = store.add("custom_openai", model="llama3", credential=None, base_url="http://localhost:11434/v1")

    class LockedKeychain:
        def get_password(self, *args):
            raise keyring.errors.KeyringError("locked: " + SECRET)

        set_password = delete_password = get_password

    monkeypatch.setattr(keyring, "get_keyring", lambda: LockedKeychain())
    client = Client(home)
    status, raw = client.call("DELETE", f"/api/v1/ai/connections/{keyed.id}")
    assert status == 503, raw
    assert json.loads(raw)["code"] == "ai_keychain_unavailable"
    assert SECRET.encode() not in raw
    assert client.json("DELETE", f"/api/v1/ai/connections/{keyless.id}")["removed"] == keyless.id
    assert [c.id for c in store.list()] == [keyed.id]

    monkeypatch.undo()
    keyring.set_keyring(fresh_keyring)
    store.remove(keyed.id)
    assert store.list() == [] and fresh_keyring._store == {}


def test_activate_switches_the_active_connection(home, fresh_keyring):
    store = ConnectionStore(home)
    a = store.add("deepseek", model="chat", credential=SECRET)
    b = store.add("gemini", model="gemini-x", credential=SECRET)
    assert store.active() is None
    store.activate(a.id)
    assert store.active() == a
    store.activate(b.id)
    assert store.active() == b
    with pytest.raises(ConnectionRefused) as refused:
        store.activate("0123456789abcdef")
    assert refused.value.code == "ai_connection_not_found"


# ---- 6. egress disclosure ------------------------------------------------------


def test_disclosure_names_the_provider_host(home, fresh_keyring):
    store = ConnectionStore(home)
    anthropic = store.add("anthropic", model="claude-x", credential=SECRET)
    assert egress_disclosure(anthropic) == (
        "Your conversation with Krell and the Krellbot data she reads are sent to Anthropic "
        "(api.anthropic.com). Nothing is sent to krellbot.dev."
    )
    local = store.add(
        "custom_openai", model="llama3", credential=None, base_url="http://localhost:11434/v1", label="Ollama"
    )
    assert egress_disclosure(local) == (
        "Your conversation with Krell and the Krellbot data she reads are sent to Ollama "
        "(localhost). Nothing is sent to krellbot.dev."
    )

    listed = Client(home).json("GET", "/api/v1/ai/connections")
    disclosures = {c["id"]: c["disclosure"] for c in listed["connections"]}
    assert disclosures == {anthropic.id: egress_disclosure(anthropic), local.id: egress_disclosure(local)}


# ---- 7. session and CSRF gates -------------------------------------------------


def test_routes_are_session_and_csrf_gated(home):
    transport = FakeAiTransport()
    client = Client(home, transport)
    some_id = "0123456789abcdef"
    reads = ["/api/v1/ai/providers", "/api/v1/ai/connections"]
    writes = [
        (
            "POST",
            "/api/v1/ai/connections",
            {"preset": "custom_openai", "model": "m", "base_url": "http://localhost/v1"},
        ),
        ("DELETE", f"/api/v1/ai/connections/{some_id}", None),
        ("POST", f"/api/v1/ai/connections/{some_id}/activate", None),
        ("POST", f"/api/v1/ai/connections/{some_id}/probe", None),
    ]

    for path in reads:
        assert client.call("GET", path, cookie=False)[0] == 403, path
        assert client.call("GET", path, origin="https://evil.example")[0] == 403, path
        assert client.call("GET", path, csrf=False, origin=None)[0] == 200, path

    for method, path, payload in writes:
        for kwargs in ({"cookie": False}, {"csrf": False}, {"origin": "https://evil.example"}, {"origin": None}):
            status, raw = client.call(method, path, payload, **kwargs)
            assert status == 403, (method, path, kwargs, raw)

    assert transport.calls == 0 and transport.get_requests == []
    assert not _connections_file(home).exists()

    # Ids are generated by Krellbot; anything else never reaches the store or the keychain.
    for bad in ("not-an-id", "0123456789ABCDEF", "0123456789abcdef0", "..%2F..%2Fetc", "x" * 16):
        for method, path in (
            ("DELETE", f"/api/v1/ai/connections/{bad}"),
            ("POST", f"/api/v1/ai/connections/{bad}/activate"),
            ("POST", f"/api/v1/ai/connections/{bad}/probe"),
        ):
            status, _raw = client.call(method, path)
            assert status == 404, (method, path, status)
    with pytest.raises(ConnectionRefused):
        ConnectionStore(home).credential("../../etc")


def test_the_connections_file_is_never_overwritten_when_unreadable(home, fresh_keyring):
    path = _connections_file(home)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    client = Client(home, FakeAiTransport([oa_ping_call()], [MODELS]))

    status, raw = client.call(
        "POST", "/api/v1/ai/connections", {"preset": "openai", "model": "m", "credential": SECRET}
    )
    assert status == 409, raw
    assert json.loads(raw)["code"] == "ai_connections_unreadable"
    assert path.read_text() == "{not json"
    assert fresh_keyring._store == {}
    assert client.call("GET", "/api/v1/ai/connections")[0] == 409
