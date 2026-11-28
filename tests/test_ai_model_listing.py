"""Model listing: the models a provider offers, for a stored connection or one being set up.

Fakes only: ``FakeAiTransport``, the fake keyring and an isolated home. The key
(stored, draft or ChatGPT access token) goes only into the provider request header.
"""

from __future__ import annotations

import json
import logging

import pytest

from krellbot.ai import models as ai_models
from krellbot.ai.connections import ConnectionStore
from krellbot.ai.http import TransportError
from krellbot.ai.models import ModelInfo, context_window, filter_chat_models
from tests.fakes.fake_ai_transport import FakeAiTransport
from tests.test_ai_chatgpt_signin import ACCESS, MODELS_URL, Env
from tests.test_ai_chatgpt_signin import Client as ChatgptClient
from tests.test_ai_connections_api import Client, _store

DRAFT_KEY = "sk-draft-SECRET-model-list-0099"
STORED_KEY = "sk-stored-SECRET-model-list-0077"


@pytest.fixture(autouse=True)
def _fresh_context_cache():
    ai_models.clear_context_cache()
    yield
    ai_models.clear_context_cache()


def ids(body: dict) -> list[str]:
    return [m["id"] for m in body["models"]]


# ---- the filter -----------------------------------------------------------------------------------


def test_filter_drops_obvious_non_chat_models_and_sorts_by_id():
    names = [
        "gpt-4o",
        "text-embedding-3-large",
        "gpt-4o-mini-tts",
        "whisper-1",
        "dall-e-3",
        "omni-moderation-latest",
        "gpt-4o-transcribe",
        "gpt-image-1",
        "gpt-4o-audio-preview",
        "gpt-4o-realtime-preview",
        "gpt-4o-search-preview",
        "Text-EMBED-v2",
        "claude-sonnet",
        "o3",
    ]
    kept = filter_chat_models([ModelInfo(n) for n in names])
    assert [m.id for m in kept] == ["claude-sonnet", "gpt-4o", "o3"]


# ---- each provider format -------------------------------------------------------------------------


def test_openai_compatible_draft_lists_models_with_the_draft_key_on_the_wire_only(home, fresh_keyring):
    transport = FakeAiTransport(
        json_responses=[
            {
                "data": [
                    {"id": "zeta-chat", "context_window": 32000},
                    {"id": "alpha-chat"},
                    {"id": "text-embedding-x"},
                ]
            }
        ]
    )
    client = Client(home, transport)
    status, raw = client.call("POST", "/api/v1/ai/models", {"preset": "openai", "api_key": DRAFT_KEY})
    assert status == 200, raw
    body = json.loads(raw)
    assert body["listed"] is True
    assert body["models"] == [
        {"id": "alpha-chat", "display_name": None, "context_window": None},
        {"id": "zeta-chat", "display_name": None, "context_window": 32000},
    ]
    [request] = transport.get_requests
    assert request["url"] == "https://api.openai.com/v1/models"
    assert request["headers"] == {"authorization": f"Bearer {DRAFT_KEY}"}
    assert DRAFT_KEY.encode() not in raw
    # Nothing was stored: no connection, no keychain entry.
    assert fresh_keyring._store == {}
    assert _store(home).list() == []


def test_custom_openai_endpoint_without_a_key_sends_no_authorization(home, fresh_keyring):
    transport = FakeAiTransport(json_responses=[{"data": [{"id": "llama3"}]}])
    client = Client(home, transport)
    body = client.json(
        "POST", "/api/v1/ai/models", {"preset": "custom_openai", "base_url": "http://localhost:11434/v1/"}
    )
    assert ids(body) == ["llama3"]
    [request] = transport.get_requests
    assert request["url"] == "http://localhost:11434/v1/models"
    assert request["headers"] == {}


def test_anthropic_listing_follows_pagination_and_reads_names_and_context(home, fresh_keyring):
    transport = FakeAiTransport(
        json_responses=[
            {
                "data": [{"id": "claude-b", "display_name": "Claude B", "max_input_tokens": 200000}],
                "has_more": True,
                "last_id": "claude-b",
            },
            {"data": [{"id": "claude-a", "display_name": "Claude A"}], "has_more": False, "last_id": "claude-a"},
        ]
    )
    client = Client(home, transport)
    body = client.json("POST", "/api/v1/ai/models", {"preset": "anthropic", "api_key": DRAFT_KEY})
    assert body["models"] == [
        {"id": "claude-a", "display_name": "Claude A", "context_window": None},
        {"id": "claude-b", "display_name": "Claude B", "context_window": 200000},
    ]
    first, second = transport.get_requests
    assert first["url"] == "https://api.anthropic.com/v1/models?limit=1000"
    assert second["url"] == "https://api.anthropic.com/v1/models?limit=1000&after_id=claude-b"
    for request in (first, second):
        assert request["headers"] == {"anthropic-version": "2023-06-01", "x-api-key": DRAFT_KEY}


def test_anthropic_pagination_stops_at_the_page_cap_and_on_a_repeated_cursor(home, fresh_keyring):
    endless = [{"data": [{"id": f"m{n}"}], "has_more": True, "last_id": f"m{n}"} for n in range(50)]
    transport = FakeAiTransport(json_responses=endless)
    body = Client(home, transport).json("POST", "/api/v1/ai/models", {"preset": "anthropic", "api_key": DRAFT_KEY})
    assert len(transport.get_requests) == ai_models._MAX_PAGES
    assert len(body["models"]) == ai_models._MAX_PAGES

    stuck = FakeAiTransport(json_responses=[{"data": [{"id": "a"}], "has_more": True, "last_id": "same"}] * 5)
    Client(home, stuck).json("POST", "/api/v1/ai/models", {"preset": "anthropic", "api_key": DRAFT_KEY})
    assert len(stuck.get_requests) == 2


def test_openrouter_connection_lists_models_with_context_length_using_the_stored_key(home, fresh_keyring):
    store = _store(home)
    conn = store.add("openrouter", model="x/y", credential=STORED_KEY)
    transport = FakeAiTransport(
        json_responses=[
            {
                "data": [
                    {"id": "vendor/big", "name": "Vendor: Big", "context_length": 1000000},
                    {"id": "vendor/small", "name": "Vendor: Small", "context_length": 8192},
                    {"id": "vendor/embedder-embed", "context_length": 512},
                ]
            }
        ]
    )
    client = Client(home, transport)
    status, raw = client.call("POST", "/api/v1/ai/models", {"connection_id": conn.id})
    assert status == 200, raw
    body = json.loads(raw)
    assert body["models"] == [
        {"id": "vendor/big", "display_name": "Vendor: Big", "context_window": 1000000},
        {"id": "vendor/small", "display_name": "Vendor: Small", "context_window": 8192},
    ]
    [request] = transport.get_requests
    assert request["url"] == "https://openrouter.ai/api/v1/models"
    assert request["headers"] == {"authorization": f"Bearer {STORED_KEY}"}
    assert STORED_KEY.encode() not in raw


def test_connection_listing_uses_the_stored_key_not_a_body_key(home, fresh_keyring):
    conn = _store(home).add("openai", model="m", credential=STORED_KEY)
    transport = FakeAiTransport(json_responses=[{"data": [{"id": "gpt-x"}]}])
    client = Client(home, transport)
    assert ids(client.json("POST", "/api/v1/ai/models", {"connection_id": conn.id})) == ["gpt-x"]
    assert transport.get_requests[0]["headers"] == {"authorization": f"Bearer {STORED_KEY}"}
    # A body that mixes the two paths is refused before any request.
    status, raw = client.call("POST", "/api/v1/ai/models", {"connection_id": conn.id, "api_key": DRAFT_KEY})
    assert status == 400 and json.loads(raw)["code"] == "ai_body_invalid"
    assert len(transport.get_requests) == 1


def test_chatgpt_signin_lists_plan_models_with_the_sign_in_token(home, fresh_keyring):
    env = Env(home)
    client = ChatgptClient(env)
    conn_id = client.sign_in()
    body = client.json("POST", "/api/v1/ai/models", {"connection_id": conn_id})
    assert body["listed"] is True
    assert body["models"] == [
        {"id": "gpt-plan-a", "display_name": "Plan A", "context_window": None},
        {"id": "gpt-plan-b", "display_name": "Plan B", "context_window": None},
    ]
    models_calls = [r for r in env.openai.get_requests if r["url"] == MODELS_URL]
    assert [r["headers"]["authorization"] for r in models_calls] == [f"Bearer {ACCESS}"]
    assert ACCESS not in json.dumps(body)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_chatgpt_signin_that_may_not_read_models_falls_back_to_a_typed_id(home, fresh_keyring, status):
    env = Env(home)
    client = ChatgptClient(env)
    conn_id = client.sign_in()
    original = env.openai.get_json

    def refuse_models(url, headers, timeout):
        if url == MODELS_URL:
            raise TransportError(status, "insufficient scope")
        return original(url, headers, timeout)

    env.openai.get_json = refuse_models
    body = client.json("POST", "/api/v1/ai/models", {"connection_id": conn_id})
    assert body["models"] == [] and body["listed"] is False
    assert body["reason"] == "chatgpt_models_not_allowed"
    assert ACCESS not in json.dumps(body)


# ---- no list, and provider errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    "answer", [TransportError(404, ""), TransportError(405, ""), {"object": "list"}, {"data": "x"}]
)
def test_an_endpoint_with_no_model_list_answers_empty_and_flags_it(home, fresh_keyring, answer):
    transport = FakeAiTransport(json_responses=[answer])
    body = Client(home, transport).json(
        "POST", "/api/v1/ai/models", {"preset": "custom_openai", "base_url": "https://llm.example.com/v1"}
    )
    assert body["models"] == [] and body["listed"] is False and body["reason"] == "no_model_list"


def test_a_preset_that_never_lists_models_makes_no_request(home, fresh_keyring):
    transport = FakeAiTransport()
    body = Client(home, transport).json(
        "POST",
        "/api/v1/ai/models",
        {"preset": "custom_anthropic", "base_url": "https://gateway.example.com", "api_key": DRAFT_KEY},
    )
    assert body["models"] == [] and body["listed"] is False and body["reason"] == "no_model_list"
    assert transport.get_requests == []


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (TransportError(401, "bad key sk-echo"), "provider_auth_failed"),
        (TransportError(429, ""), "provider_rate_limited"),
        (TransportError(None, "ConnectionError"), "provider_unavailable"),
        (TransportError(400, "nope"), "provider_bad_response"),
    ],
)
def test_provider_failures_use_the_existing_codes_and_never_echo_the_key_or_body(home, fresh_keyring, failure, code):
    transport = FakeAiTransport(json_responses=[failure])
    status, raw = Client(home, transport).call("POST", "/api/v1/ai/models", {"preset": "openai", "api_key": DRAFT_KEY})
    body = json.loads(raw)
    assert status == 502 and body["ok"] is False and body["code"] == code
    assert body["models"] == [] and body["listed"] is False
    assert DRAFT_KEY.encode() not in raw and b"sk-echo" not in raw


# ---- body validation and the gate -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "code", "status"),
    [
        ({}, "ai_body_invalid", 400),
        ([], "ai_body_invalid", 400),
        ({"preset": 3}, "ai_body_invalid", 400),
        ({"preset": "openai", "api_key": 5}, "ai_body_invalid", 400),
        ({"preset": "openai", "extra": 1}, "ai_body_invalid", 400),
        ({"preset": "nope", "api_key": "k"}, "ai_preset_unknown", 400),
        ({"preset": "openai"}, "ai_credential_required", 400),
        ({"preset": "openai", "api_key": "has space"}, "ai_credential_invalid", 400),
        ({"preset": "openai", "api_key": "k", "base_url": "https://evil.example/v1"}, "ai_endpoint_fixed", 400),
        ({"preset": "custom_openai", "api_key": "k"}, "ai_endpoint_invalid", 400),
        ({"preset": "custom_openai", "base_url": "http://example.com/v1"}, "ai_endpoint_insecure", 400),
        ({"connection_id": 7}, "ai_body_invalid", 400),
        ({"connection_id": "../x"}, "ai_connection_not_found", 404),
        ({"connection_id": "0123456789abcdef"}, "ai_connection_not_found", 404),
    ],
)
def test_bad_bodies_are_refused_before_any_request(home, fresh_keyring, payload, code, status):
    transport = FakeAiTransport()
    got, raw = Client(home, transport).call("POST", "/api/v1/ai/models", payload)
    assert (got, json.loads(raw)["code"]) == (status, code)
    assert b"has space" not in raw
    assert transport.get_requests == []


def test_listing_route_is_session_csrf_and_origin_gated(home, fresh_keyring):
    transport = FakeAiTransport(json_responses=[{"data": [{"id": "m"}]}] * 5)
    client = Client(home, transport)
    payload = {"preset": "openai", "api_key": DRAFT_KEY}
    for kwargs in ({"cookie": False}, {"csrf": False}, {"origin": "https://evil.example"}, {"origin": None}):
        status, raw = client.call("POST", "/api/v1/ai/models", payload, **kwargs)
        assert status == 403, (kwargs, raw)
        assert DRAFT_KEY.encode() not in raw
    assert transport.get_requests == []
    assert client.call("GET", "/api/v1/ai/models")[0] in (403, 405)
    assert client.call("POST", "/api/v1/ai/models", payload)[0] == 200


# ---- the key stays in the engine ------------------------------------------------------------------


def test_the_draft_key_reaches_no_response_log_or_file(home, fresh_keyring, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    transport = FakeAiTransport(
        json_responses=[{"data": [{"id": "m"}]}, TransportError(401, DRAFT_KEY), {"data": [{"id": "n"}]}]
    )
    client = Client(home, transport)
    raws = [
        client.call("POST", "/api/v1/ai/models", {"preset": "openai", "api_key": DRAFT_KEY})[1],
        client.call("POST", "/api/v1/ai/models", {"preset": "openai", "api_key": DRAFT_KEY})[1],
        client.call("POST", "/api/v1/ai/models", {"preset": "openai", "api_key": DRAFT_KEY, "x": 1})[1],
        client.call("POST", "/api/v1/ai/models", {"preset": "nope", "api_key": DRAFT_KEY})[1],
        client.call("GET", "/api/v1/ai/connections")[1],
    ]
    out, err = capfd.readouterr()
    assert all(DRAFT_KEY.encode() not in raw for raw in raws)
    assert DRAFT_KEY not in out + err + caplog.text
    on_disk = "".join(p.read_text(errors="replace") for p in home.rglob("*") if p.is_file())
    assert DRAFT_KEY not in on_disk  # connections file, journal, everything under the home
    assert DRAFT_KEY not in repr(fresh_keyring._store)


# ---- the context-window cache for the chat --------------------------------------------------------


def test_context_window_fills_from_one_listing_then_answers_without_network(home, fresh_keyring):
    store = _store(home)
    conn = store.add("openrouter", model="x/y", credential=STORED_KEY)
    transport = FakeAiTransport(
        json_responses=[
            {
                "data": [
                    {"id": "vendor/big", "context_length": 1000000},
                    {"id": "vendor/unknown"},
                    {"id": "vendor/embedder-embed", "context_length": 512},
                ]
            }
        ]
    )
    assert context_window(home, conn.id, "vendor/big", store=store, transport=transport) == 1000000
    assert len(transport.get_requests) == 1
    # Cached: known, unknown-to-the-provider and absent ids all answer with no further request.
    assert context_window(home, conn.id, "vendor/big", store=store, transport=transport) == 1000000
    assert context_window(home, conn.id, "vendor/unknown", store=store, transport=transport) is None
    assert context_window(home, conn.id, "not-listed", store=store, transport=transport) is None
    assert context_window(home, conn.id, "vendor/embedder-embed", store=store, transport=transport) == 512
    assert len(transport.get_requests) == 1


def test_the_listing_route_fills_the_cache_for_its_connection(home, fresh_keyring):
    conn = _store(home).add("openrouter", model="x/y", credential=STORED_KEY)
    transport = FakeAiTransport(json_responses=[{"data": [{"id": "vendor/big", "context_length": 64000}]}])
    Client(home, transport).json("POST", "/api/v1/ai/models", {"connection_id": conn.id})
    assert context_window(home, conn.id, "vendor/big", store=_store(home), transport=transport) == 64000
    assert len(transport.get_requests) == 1


def test_context_window_is_none_when_the_listing_fails_and_is_retried_after_the_backoff(
    home, fresh_keyring, monkeypatch
):
    now = [0.0]
    monkeypatch.setattr(ai_models, "_monotonic", lambda: now[0])
    store = _store(home)
    conn = store.add("openrouter", model="x/y", credential=STORED_KEY)
    transport = FakeAiTransport(
        json_responses=[TransportError(None, "down"), {"data": [{"id": "vendor/big", "context_length": 4096}]}]
    )
    assert context_window(home, conn.id, "vendor/big", store=store, transport=transport) is None
    now[0] += ai_models._RETRY_AFTER + 1
    assert context_window(home, conn.id, "vendor/big", store=store, transport=transport) == 4096
    assert context_window(home, "0123456789abcdef", "m", store=store, transport=FakeAiTransport()) is None


def test_context_window_remembers_that_an_endpoint_has_no_list(home, fresh_keyring):
    store = ConnectionStore(home, allow_injected_fake_backend=True)
    conn = store.add("custom_openai", model="m", credential=None, base_url="https://llm.example.com/v1")
    transport = FakeAiTransport(json_responses=[TransportError(404, "")])
    assert context_window(home, conn.id, "m", store=store, transport=transport) is None
    assert context_window(home, conn.id, "m", store=store, transport=transport) is None
    assert len(transport.get_requests) == 1


# ---- fix round 1 -----------------------------------------------------------------------------------


def test_a_stored_connection_whose_preset_never_lists_makes_no_request_or_keychain_read(home, fresh_keyring):
    store = _store(home)
    conn = store.add("custom_anthropic", model="m", credential=STORED_KEY, base_url="https://gateway.example.com")
    transport = FakeAiTransport()
    body = Client(home, transport).json("POST", "/api/v1/ai/models", {"connection_id": conn.id})
    assert body["models"] == [] and body["listed"] is False and body["reason"] == "no_model_list"
    assert transport.get_requests == []


@pytest.mark.parametrize("status", [401, 403, 400])
def test_context_window_fetches_a_refused_listing_once(home, fresh_keyring, status):
    store = _store(home)
    conn = store.add("openrouter", model="x/y", credential=STORED_KEY)
    transport = FakeAiTransport(json_responses=[TransportError(status, "no")])
    for _ in range(3):
        assert context_window(home, conn.id, "m", store=store, transport=transport) is None
    assert len(transport.get_requests) == 1


@pytest.mark.parametrize("failure", [TransportError(503, ""), TransportError(None, "down"), TransportError(429, "")])
def test_context_window_retries_transient_failures_only_after_the_backoff(home, fresh_keyring, monkeypatch, failure):
    now = [1000.0]
    monkeypatch.setattr(ai_models, "_monotonic", lambda: now[0])
    store = _store(home)
    conn = store.add("openrouter", model="x/y", credential=STORED_KEY)
    transport = FakeAiTransport(json_responses=[failure, {"data": [{"id": "m", "context_length": 9000}]}])
    assert context_window(home, conn.id, "m", store=store, transport=transport) is None
    now[0] += 299
    assert context_window(home, conn.id, "m", store=store, transport=transport) is None
    assert len(transport.get_requests) == 1
    now[0] += 2
    assert context_window(home, conn.id, "m", store=store, transport=transport) == 9000
    assert len(transport.get_requests) == 2


def test_models_shaped_body_is_a_model_list_only_for_chatgpt(home, fresh_keyring):
    plan_shape = {"models": [{"slug": "gpt-plan-a", "visibility": "list"}]}
    transport = FakeAiTransport(json_responses=[plan_shape])
    body = Client(home, transport).json(
        "POST", "/api/v1/ai/models", {"preset": "custom_openai", "base_url": "https://llm.example.com/v1"}
    )
    assert body["models"] == [] and body["listed"] is False and body["reason"] == "no_model_list"


def test_anthropic_pagination_stops_at_the_model_cap(home, fresh_keyring, monkeypatch):
    monkeypatch.setattr(ai_models, "_MAX_MODELS", 3)
    pages = [{"data": [{"id": f"m{n}a"}, {"id": f"m{n}b"}], "has_more": True, "last_id": f"m{n}b"} for n in range(5)]
    transport = FakeAiTransport(json_responses=pages)
    body = Client(home, transport).json("POST", "/api/v1/ai/models", {"preset": "anthropic", "api_key": DRAFT_KEY})
    assert len(transport.get_requests) == 2
    assert len(body["models"]) == 3


def test_removing_a_connection_evicts_its_cached_context_windows(home, fresh_keyring):
    store = _store(home)
    conn = store.add("openrouter", model="x/y", credential=STORED_KEY)
    transport = FakeAiTransport(json_responses=[{"data": [{"id": "m", "context_length": 5}]}])
    client = Client(home, transport)
    client.json("POST", "/api/v1/ai/models", {"connection_id": conn.id})
    assert ai_models._cache[(str(home), conn.id)] == {"m": 5}
    client.json("DELETE", f"/api/v1/ai/connections/{conn.id}")
    assert (str(home), conn.id) not in ai_models._cache
