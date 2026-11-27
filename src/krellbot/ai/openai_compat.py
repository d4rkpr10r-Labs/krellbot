"""OpenAI-compatible Chat Completions streaming adapter."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

from krellbot.ai.http import (
    BadStream,
    HttpTransport,
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


def _wire_messages(system: str, messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id or "", "content": m.content})
        elif m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in m.tool_calls
                    ],
                }
            )
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def _error_code_from_body(err: Any) -> str:
    text = json.dumps(err).lower() if err is not None else ""
    if "rate_limit" in text or "rate limit" in text:
        return "provider_rate_limited"
    if "invalid_api_key" in text or "authentication" in text or "unauthorized" in text:
        return "provider_auth_failed"
    if "overloaded" in text or "unavailable" in text or "server_error" in text:
        return "provider_unavailable"
    return "provider_bad_response"


class OpenAICompatAdapter:
    """Chat Completions over SSE. ``base_url`` includes the version path (``.../v1``)."""

    def __init__(
        self,
        base_url: str,
        credential: str | None,
        transport: HttpTransport,
        *,
        extra_headers: dict | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._credential = credential
        self._transport = transport
        self._extra = dict(extra_headers or {})

    def stream(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        try:
            yield from self._stream(model, system, messages, tools, max_tokens)
        except Exception as exc:  # noqa: BLE001 — adapters never raise; coerced to ProviderError
            yield error_from_exception(exc)

    def _stream(
        self,
        model: str,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> Iterator[StreamEvent]:
        body: dict[str, Any] = {
            "model": model,
            "messages": _wire_messages(system, messages),
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
                }
                for t in tools
            ]
        headers = {"accept": "text/event-stream", **self._extra}
        if self._credential:
            headers["authorization"] = f"Bearer {self._credential}"

        pending: dict[int, dict[str, str]] = {}
        finish: str | None = None
        saw_done = False
        for payload in iter_sse_data(self._transport.post_stream(self._url, body, headers, _TIMEOUT)):
            if payload == "[DONE]":
                saw_done = True
                break
            chunk = parse_json_object(payload, "stream chunk")
            if chunk.get("error") is not None:
                yield ProviderError(_error_code_from_body(chunk["error"]), "The provider reported an error.")  # type: ignore[arg-type]
                return
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                yield Usage(_count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens")))
            choices = chunk.get("choices") or []
            if not isinstance(choices, list):
                raise BadStream("choices is not a list")
            if not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                raise BadStream("choice is not an object")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise BadStream("delta is not an object")
            text = delta.get("content")
            if isinstance(text, str) and text:
                yield TextDelta(text)
            _collect_tool_fragments(delta.get("tool_calls"), pending)
            reason = choice.get("finish_reason")
            if reason:
                finish = str(reason)
                calls = _finish_tool_calls(pending)
                pending = {}
                for call in calls:
                    yield ToolCallDone(call)

        if pending:
            raise BadStream("stream ended inside a tool call")
        if finish is None:
            raise BadStream(
                "stream ended without a finish reason" if not saw_done else "no finish reason before [DONE]"
            )
        yield Done(finish)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _collect_tool_fragments(fragments: Any, pending: dict[int, dict[str, str]]) -> None:
    if fragments is None:
        return
    if not isinstance(fragments, list):
        raise BadStream("tool_calls is not a list")
    for frag in fragments:
        if not isinstance(frag, dict) or not isinstance(frag.get("index", 0), int):
            raise BadStream("malformed tool call fragment")
        slot = pending.setdefault(frag.get("index", 0), {"id": "", "name": "", "arguments": ""})
        if isinstance(frag.get("id"), str):
            slot["id"] = frag["id"]
        fn = frag.get("function") or {}
        if not isinstance(fn, dict):
            raise BadStream("malformed tool call function")
        if isinstance(fn.get("name"), str):
            slot["name"] += fn["name"]
        if isinstance(fn.get("arguments"), str):
            slot["arguments"] += fn["arguments"]


def _finish_tool_calls(pending: dict[int, dict[str, str]]) -> list[ToolCall]:
    """Validate every pending call first so a bad one suppresses the whole batch."""
    calls = []
    for index in sorted(pending):
        slot = pending[index]
        if not slot["id"] or not slot["name"]:
            raise BadStream("tool call missing id or name")
        calls.append(ToolCall(slot["id"], slot["name"], parse_tool_arguments(slot["arguments"])))
    return calls
