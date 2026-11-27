"""Anthropic Messages streaming adapter."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from krellbot.ai.http import (
    BadStream,
    HttpTransport,
    StreamFailure,
    error_from_exception,
    iter_sse_data,
    parse_json_object,
    parse_tool_arguments,
)
from krellbot.ai.types import (
    ChatMessage,
    Done,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallDone,
    ToolDef,
    Usage,
)

_TIMEOUT = 120.0
_VERSION = "2023-06-01"
_ERROR_CODES = {
    "authentication_error": "provider_auth_failed",
    "permission_error": "provider_auth_failed",
    "rate_limit_error": "provider_rate_limited",
    "overloaded_error": "provider_unavailable",
    "api_error": "provider_unavailable",
}


def _wire_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id or "", "content": m.content}
            last = out[-1] if out else None
            # Parallel tool results must share one user message.
            if last and last["role"] == "user" and isinstance(last["content"], list) and last.get("_results"):
                last["content"].append(block)
            else:
                out.append({"role": "user", "content": [block], "_results": True})
        elif m.role == "assistant" and m.tool_calls:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls]
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m.role, "content": m.content})
    for msg in out:
        msg.pop("_results", None)
    return out


class AnthropicMessagesAdapter:
    """Messages API over SSE. ``base_url`` is the host (``https://api.anthropic.com``)."""

    def __init__(self, base_url: str, credential: str | None, transport: HttpTransport) -> None:
        root = base_url.rstrip("/").removesuffix("/v1")
        self._url = root + "/v1/messages"
        self._credential = credential
        self._transport = transport

    def stream(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        usage_box: list[Usage | None] = [None]
        try:
            yield from self._stream(model, system, messages, tools, max_tokens, usage_box)
        except Exception as exc:  # noqa: BLE001 — adapters never raise; coerced to ProviderError
            # Tokens already spent still count toward budgets: Usage precedes the error.
            if usage_box[0] is not None:
                yield usage_box[0]
            yield error_from_exception(exc)

    def _stream(
        self,
        model: str,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDef],
        max_tokens: int,
        usage_box: list[Usage | None],
    ) -> Iterator[StreamEvent]:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _wire_messages(messages),
            "stream": True,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
            ]
        headers = {"accept": "text/event-stream", "anthropic-version": _VERSION}
        if self._credential:
            headers["x-api-key"] = self._credential

        input_tokens = 0
        output_tokens = 0
        stop_reason: str | None = None
        blocks: dict[int, dict[str, Any]] = {}
        saw_usage = False
        finished = False
        completed: list[ToolCall] = []
        for payload in iter_sse_data(self._transport.post_stream(self._url, body, headers, _TIMEOUT)):
            event = parse_json_object(payload, "stream event")
            kind = event.get("type")
            if kind == "error":
                err = event.get("error")
                etype = err.get("type") if isinstance(err, dict) else None
                raise StreamFailure(
                    ProviderError(_ERROR_CODES.get(etype, "provider_bad_response"), "The provider reported an error.")  # type: ignore[arg-type]
                )
            if kind == "message_start":
                usage = (event.get("message") or {}).get("usage") or {}
                saw_usage = saw_usage or bool(usage)
                input_tokens = _count(usage.get("input_tokens"))
                output_tokens = _count(usage.get("output_tokens"))
                if saw_usage:
                    usage_box[0] = Usage(input_tokens, output_tokens)
            elif kind == "content_block_start":
                block = event.get("content_block")
                if not isinstance(block, dict) or not isinstance(event.get("index"), int):
                    raise BadStream("malformed content block")
                if block.get("type") == "tool_use":
                    initial = block.get("input")
                    if initial is not None and not isinstance(initial, dict):
                        raise BadStream("tool_use input is not an object")
                    blocks[event["index"]] = {
                        "id": block.get("id") if isinstance(block.get("id"), str) else "",
                        "name": block.get("name") if isinstance(block.get("name"), str) else "",
                        "json": "",
                        "initial": initial or {},
                    }
                elif block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
                    yield TextDelta(block["text"])
            elif kind == "content_block_delta":
                delta = event.get("delta")
                if not isinstance(delta, dict):
                    raise BadStream("malformed delta")
                if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                    if delta["text"]:
                        yield TextDelta(delta["text"])
                elif delta.get("type") == "input_json_delta":
                    slot = blocks.get(event.get("index"))  # type: ignore[arg-type]
                    if slot is None or not isinstance(delta.get("partial_json"), str):
                        raise BadStream("input_json_delta without a tool_use block")
                    slot["json"] += delta["partial_json"]
            elif kind == "content_block_stop":
                slot = blocks.pop(event.get("index"), None)  # type: ignore[arg-type]
                if slot is not None:
                    if not slot["id"] or not slot["name"]:
                        raise BadStream("tool_use missing id or name")
                    if slot["json"].strip():
                        arguments = parse_tool_arguments(slot["json"])
                    else:
                        arguments = slot["initial"]
                    completed.append(ToolCall(slot["id"], slot["name"], arguments))
            elif kind == "message_delta":
                delta = event.get("delta") or {}
                if isinstance(delta, dict) and delta.get("stop_reason"):
                    stop_reason = str(delta["stop_reason"])
                usage = event.get("usage") or {}
                if isinstance(usage, dict) and "output_tokens" in usage:
                    output_tokens = _count(usage.get("output_tokens"))
                    saw_usage = True
                    usage_box[0] = Usage(input_tokens, output_tokens)
            elif kind == "message_stop":
                finished = True
                break
            # ping and unknown event types are ignored.

        if blocks:
            raise BadStream("stream ended inside a tool call")
        if not finished:
            raise BadStream("stream ended before message_stop")
        # Tool calls are released only once the whole message is known to be well formed.
        for call in completed:
            yield ToolCallDone(call)
        if usage_box[0] is not None:
            yield usage_box[0]
        yield Done(stop_reason or "end_turn")


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
