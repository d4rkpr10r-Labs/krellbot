from __future__ import annotations

import json

import pytest

from krellbot.ai.anthropic_messages import AnthropicMessagesAdapter
from krellbot.ai.http import TransportError, UrllibTransport
from krellbot.ai.openai_compat import OpenAICompatAdapter
from krellbot.ai.types import (
    ChatMessage,
    Done,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolCallDone,
    ToolDef,
    Usage,
)
from tests.fakes.fake_ai_transport import FakeAiTransport, sse

SECRET = "sk-test-SECRET-credential-123"
TOOLS = [ToolDef("get_price", "Fetch a price", {"type": "object", "properties": {"symbol": {"type": "string"}}})]
HISTORY = [ChatMessage("user", "price of BTC?")]


def run(adapter, messages=HISTORY, tools=TOOLS):
    return list(adapter.stream(model="m1", system="be brief", messages=messages, tools=tools, max_tokens=256))


def openai(transport, credential=SECRET, **kw):
    return OpenAICompatAdapter("https://llm.example/v1", credential, transport, **kw)


def anthropic(transport, credential=SECRET):
    return AnthropicMessagesAdapter("https://api.anthropic.example", credential, transport)


def oa_chunk(delta=None, finish=None, usage=None, choices=True):
    chunk: dict = {"id": "c1", "object": "chat.completion.chunk"}
    if choices:
        chunk["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    else:
        chunk["choices"] = []
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def oa_tool_stream(arg_fragments, name="get_price", call_id="call_1", finish="tool_calls"):
    events = [oa_chunk({"role": "assistant", "content": "Checking. "})]
    first = True
    for frag in arg_fragments:
        fn = {"arguments": frag}
        tc = {"index": 0, "function": fn}
        if first:
            tc["id"] = call_id
            tc["type"] = "function"
            fn["name"] = name
            first = False
        events.append(oa_chunk({"tool_calls": [tc]}))
    if finish:
        events.append(oa_chunk(finish=finish))
    return events


def an_events(blocks_json, stop_reason="tool_use"):
    return [
        {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 11, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "check."}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_price", "input": {}},
        },
        *[
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": p}}
            for p in blocks_json
        ],
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    ]


def test_openai_stream_yields_text_then_tool_call_then_usage():
    events = oa_tool_stream(['{"sym', 'bol": "BT', 'C"}'])
    events.append(oa_chunk(choices=False, usage={"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}))
    transport = FakeAiTransport(responses=[sse(*events, "[DONE]")])
    out = run(openai(transport))
    assert out == [
        TextDelta("Checking. "),
        ToolCallDone(ToolCall("call_1", "get_price", {"symbol": "BTC"})),
        Usage(9, 4),
        Done("tool_calls"),
    ]
    body = transport.requests[0]["body"]
    assert transport.requests[0]["url"] == "https://llm.example/v1/chat/completions"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["model"] == "m1"
    assert body["max_tokens"] == 256
    assert body["tools"][0]["function"]["name"] == "get_price"


def test_openai_parallel_tool_calls_are_assembled_per_index():
    events = [
        oa_chunk(
            {
                "tool_calls": [
                    {"index": 0, "id": "a", "function": {"name": "get_price", "arguments": '{"symbol":'}},
                    {"index": 1, "id": "b", "function": {"name": "get_price", "arguments": '{"symbol":'}},
                ]
            }
        ),
        oa_chunk({"tool_calls": [{"index": 1, "function": {"arguments": ' "ETH"}'}}]}),
        oa_chunk({"tool_calls": [{"index": 0, "function": {"arguments": ' "BTC"}'}}]}),
        oa_chunk(finish="tool_calls"),
    ]
    out = run(openai(FakeAiTransport(responses=[sse(*events, "[DONE]")])))
    calls = [e.call for e in out if isinstance(e, ToolCallDone)]
    assert calls == [
        ToolCall("a", "get_price", {"symbol": "BTC"}),
        ToolCall("b", "get_price", {"symbol": "ETH"}),
    ]
    assert out[-1] == Done("tool_calls")


def test_openai_tool_call_with_empty_arguments_is_an_empty_object():
    events = oa_tool_stream([""], name="list_things")
    out = run(openai(FakeAiTransport(responses=[sse(*events, "[DONE]")])))
    assert ToolCallDone(ToolCall("call_1", "list_things", {})) in out


def test_openai_plain_text_reply_ends_with_done_stop():
    events = [oa_chunk({"content": "Hi"}), oa_chunk({"content": " there"}), oa_chunk(finish="stop")]
    out = run(openai(FakeAiTransport(responses=[sse(*events, "[DONE]")])))
    assert out == [TextDelta("Hi"), TextDelta(" there"), Done("stop")]


def test_anthropic_stream_assembles_partial_json_tool_input():
    transport = FakeAiTransport(responses=[sse(*an_events(['{"sym', 'bol": "B', 'TC"}']), named=True)])
    out = run(anthropic(transport))
    assert out == [
        TextDelta("Let me "),
        TextDelta("check."),
        ToolCallDone(ToolCall("toolu_1", "get_price", {"symbol": "BTC"})),
        Usage(11, 42),
        Done("tool_use"),
    ]
    req = transport.requests[0]
    assert req["url"] == "https://api.anthropic.example/v1/messages"
    assert req["body"]["stream"] is True
    assert req["body"]["system"] == "be brief"
    assert req["body"]["max_tokens"] == 256
    assert req["body"]["tools"] == [
        {"name": "get_price", "description": "Fetch a price", "input_schema": TOOLS[0].input_schema}
    ]
    assert req["headers"]["x-api-key"] == SECRET
    assert req["headers"]["anthropic-version"] == "2023-06-01"


def test_anthropic_tool_use_with_no_input_deltas_is_an_empty_object():
    out = run(anthropic(FakeAiTransport(responses=[sse(*an_events([]), named=True)])))
    assert ToolCallDone(ToolCall("toolu_1", "get_price", {})) in out


def test_invalid_tool_arguments_become_provider_bad_response_and_no_tool_call():
    for arguments in (['{"symbol": "BT'], ["[1, 2]"], ['"just a string"'], ["{not json}"]):
        oa = run(openai(FakeAiTransport(responses=[sse(*oa_tool_stream(arguments), "[DONE]")])))
        assert not any(isinstance(e, ToolCallDone) for e in oa), arguments
        assert isinstance(oa[-1], ProviderError) and oa[-1].code == "provider_bad_response"
        assert not any(isinstance(e, Done) for e in oa)

        an = run(anthropic(FakeAiTransport(responses=[sse(*an_events(arguments), named=True)])))
        assert not any(isinstance(e, ToolCallDone) for e in an), arguments
        assert isinstance(an[-1], ProviderError) and an[-1].code == "provider_bad_response"
        assert not any(isinstance(e, Done) for e in an)


def test_one_bad_openai_tool_call_in_a_batch_suppresses_the_whole_batch():
    events = [
        oa_chunk(
            {
                "tool_calls": [
                    {"index": 0, "id": "a", "function": {"name": "get_price", "arguments": '{"symbol": "BTC"}'}},
                    {"index": 1, "id": "b", "function": {"name": "get_price", "arguments": "{oops"}},
                ]
            }
        ),
        oa_chunk(finish="tool_calls"),
    ]
    out = run(openai(FakeAiTransport(responses=[sse(*events, "[DONE]")])))
    assert not any(isinstance(e, ToolCallDone) for e in out)
    assert out[-1] == ProviderError("provider_bad_response", out[-1].message)


def test_tool_call_without_id_or_name_is_bad_response():
    no_id = [
        oa_chunk({"tool_calls": [{"index": 0, "function": {"name": "x", "arguments": "{}"}}]}),
        oa_chunk(finish="tool_calls"),
    ]
    no_name = [
        oa_chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"arguments": "{}"}}]}),
        oa_chunk(finish="tool_calls"),
    ]
    for events in (no_id, no_name):
        out = run(openai(FakeAiTransport(responses=[sse(*events, "[DONE]")])))
        assert not any(isinstance(e, ToolCallDone) for e in out)
        assert isinstance(out[-1], ProviderError) and out[-1].code == "provider_bad_response"


def test_stream_cut_mid_tool_call_is_bad_response():
    # OpenAI: arguments still arriving, then the connection ends with no finish_reason.
    events = oa_tool_stream(['{"symbol": "B'], finish=None)
    out = run(openai(FakeAiTransport(responses=[sse(*events)])))
    assert not any(isinstance(e, ToolCallDone) for e in out)
    assert isinstance(out[-1], ProviderError) and out[-1].code == "provider_bad_response"

    # Anthropic: the tool block never gets its content_block_stop.
    cut = an_events(['{"symbol": "B'])[:7]
    out = run(anthropic(FakeAiTransport(responses=[sse(*cut, named=True)])))
    assert not any(isinstance(e, ToolCallDone) for e in out)
    assert isinstance(out[-1], ProviderError) and out[-1].code == "provider_bad_response"


def test_connection_dropped_mid_stream_is_unavailable_and_not_raised():
    lines = sse(oa_chunk({"content": "partial"})) + [TransportError(None, "ConnectionResetError")]
    out = run(openai(FakeAiTransport(responses=[lines])))
    assert out[0] == TextDelta("partial")
    assert isinstance(out[-1], ProviderError) and out[-1].code == "provider_unavailable"


def test_unparseable_stream_line_is_bad_response_without_tool_call():
    out = run(openai(FakeAiTransport(responses=[["data: {this is not json\n", "\n"]])))
    assert out == [ProviderError("provider_bad_response", out[0].message)]
    out = run(anthropic(FakeAiTransport(responses=[["event: message_start\n", "data: <html>\n", "\n"]])))
    assert len(out) == 1 and isinstance(out[0], ProviderError) and out[0].code == "provider_bad_response"


def test_sse_comments_blank_lines_and_crlf_are_ignored():
    lines = [": keep-alive\r\n", "\r\n", f"data: {json.dumps(oa_chunk({'content': 'ok'}))}\r\n", "\r\n"]
    lines += sse(oa_chunk(finish="stop"), "[DONE]")
    assert run(openai(FakeAiTransport(responses=[lines]))) == [TextDelta("ok"), Done("stop")]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "provider_auth_failed"),
        (403, "provider_auth_failed"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
        (None, "provider_unavailable"),
        (400, "provider_bad_response"),
    ],
)
def test_transport_failures_map_to_provider_error_codes(status, code):
    for make in (openai, anthropic):
        out = run(make(FakeAiTransport(responses=[TransportError(status, f"nope {SECRET}")])))
        assert len(out) == 1 and isinstance(out[0], ProviderError)
        assert out[0].code == code
        assert SECRET not in out[0].message


def test_401_maps_to_provider_auth_failed_without_retry():
    for make in (openai, anthropic):
        transport = FakeAiTransport(responses=[TransportError(401, "unauthorized"), sse("[DONE]")])
        out = run(make(transport))
        assert out == [ProviderError("provider_auth_failed", out[0].message)]
        assert transport.calls == 1


def test_anthropic_error_event_maps_to_provider_error():
    cases = {
        "authentication_error": "provider_auth_failed",
        "permission_error": "provider_auth_failed",
        "rate_limit_error": "provider_rate_limited",
        "overloaded_error": "provider_unavailable",
        "api_error": "provider_unavailable",
        "invalid_request_error": "provider_bad_response",
    }
    for etype, code in cases.items():
        events = [{"type": "error", "error": {"type": etype, "message": "boom"}}]
        out = run(anthropic(FakeAiTransport(responses=[sse(*events, named=True)])))
        assert len(out) == 1 and isinstance(out[0], ProviderError) and out[0].code == code, etype


def test_anthropic_stream_ending_without_message_stop_is_bad_response():
    events = an_events([])[:5]
    out = run(anthropic(FakeAiTransport(responses=[sse(*events, named=True)])))
    assert isinstance(out[-1], ProviderError) and out[-1].code == "provider_bad_response"


def test_credential_is_sent_only_in_headers_never_in_body_or_url():
    for make in (openai, anthropic):
        transport = FakeAiTransport(responses=[sse(oa_chunk(finish="stop"), "[DONE]")])
        run(make(transport))
        req = transport.requests[0]
        assert SECRET not in req["url"]
        assert SECRET not in json.dumps(req["body"])
        assert any(SECRET in v for v in req["headers"].values())
    transport = FakeAiTransport(responses=[sse(oa_chunk(finish="stop"), "[DONE]")])
    run(openai(transport))
    assert transport.requests[0]["headers"]["authorization"] == f"Bearer {SECRET}"


def test_openai_without_credential_sends_no_authorization_header_and_applies_extra_headers():
    transport = FakeAiTransport(responses=[sse(oa_chunk(finish="stop"), "[DONE]")])
    run(openai(transport, credential=None, extra_headers={"x-title": "Krellbot"}))
    headers = transport.requests[0]["headers"]
    assert "authorization" not in {k.lower() for k in headers}
    assert headers["x-title"] == "Krellbot"


def test_openai_translates_history_with_tool_calls_and_results():
    history = [
        ChatMessage("user", "price?"),
        ChatMessage("assistant", "", tool_calls=(ToolCall("call_1", "get_price", {"symbol": "BTC"}),)),
        ChatMessage("tool", '{"price": "1"}', tool_call_id="call_1"),
        ChatMessage("assistant", "It is 1."),
    ]
    transport = FakeAiTransport(responses=[sse(oa_chunk(finish="stop"), "[DONE]")])
    run(openai(transport), messages=history)
    assert transport.requests[0]["body"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "price?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_price", "arguments": '{"symbol": "BTC"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"price": "1"}'},
        {"role": "assistant", "content": "It is 1."},
    ]


def test_anthropic_translates_history_with_tool_use_and_merged_tool_results():
    history = [
        ChatMessage("user", "prices?"),
        ChatMessage(
            "assistant",
            "Checking.",
            tool_calls=(
                ToolCall("t1", "get_price", {"symbol": "BTC"}),
                ToolCall("t2", "get_price", {"symbol": "ETH"}),
            ),
        ),
        ChatMessage("tool", "1", tool_call_id="t1"),
        ChatMessage("tool", "2", tool_call_id="t2"),
    ]
    transport = FakeAiTransport(responses=[sse(*an_events([]), named=True)])
    run(anthropic(transport), messages=history)
    body = transport.requests[0]["body"]
    assert "system" not in body["messages"][0]
    assert body["messages"] == [
        {"role": "user", "content": "prices?"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "t1", "name": "get_price", "input": {"symbol": "BTC"}},
                {"type": "tool_use", "id": "t2", "name": "get_price", "input": {"symbol": "ETH"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "1"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "2"},
            ],
        },
    ]


def test_request_without_tools_omits_tools_field():
    for make, final in ((openai, sse(oa_chunk(finish="stop"), "[DONE]")), (anthropic, sse(*an_events([]), named=True))):
        transport = FakeAiTransport(responses=[final])
        run(make(transport), tools=[])
        assert "tools" not in transport.requests[0]["body"]


def test_adapter_never_raises_on_unexpected_internal_failure():
    class Boom:
        def post_stream(self, *a, **k):
            raise RuntimeError("kaboom")

    for make in (openai, anthropic):
        out = run(make(Boom()))
        assert len(out) == 1 and isinstance(out[0], ProviderError)
        assert out[0].code == "provider_unavailable" or out[0].code == "provider_bad_response"
        assert "kaboom" not in out[0].message


def test_urllib_transport_streams_lines_and_maps_http_errors(monkeypatch):
    import io
    import urllib.error

    import krellbot.ai.http as http_mod

    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["req"] = req
        seen["timeout"] = timeout
        if req.full_url.endswith("/bad"):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error":"denied"}'))
        return Resp(b"data: a\n\ndata: b\n\n")

    monkeypatch.setattr(http_mod, "urlopen", fake_urlopen)
    t = UrllibTransport()
    lines = list(t.post_stream("https://x.example/ok", {"a": 1}, {"authorization": "Bearer z"}, 5.0))
    assert lines == [b"data: a\n", b"\n", b"data: b\n", b"\n"]
    assert seen["req"].get_method() == "POST"
    assert json.loads(seen["req"].data) == {"a": 1}
    assert seen["req"].get_header("Authorization") == "Bearer z"
    assert seen["timeout"] == 5.0
    with pytest.raises(TransportError) as err:
        list(t.post_stream("https://x.example/bad", {}, {}, 5.0))
    assert err.value.status == 401 and "denied" in err.value.body
