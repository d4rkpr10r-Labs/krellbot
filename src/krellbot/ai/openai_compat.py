"""OpenAI-compatible streaming adapter: Chat Completions, or the Responses API for ChatGPT plan use.

The Responses path (M38.23) serves OAuth connections to OpenAI (Sign in with
ChatGPT) and follows the M38.22 record and OpenAI's preview limits for
ChatGPT plan usage: ``POST /v1/responses`` with ``store: false`` and
``stream: true``; ``input`` carries the full history, the system prompt goes
in ``instructions`` (never a ``system``-role item), function tools are
grouped in one namespace, and every unsupported field is left out. Only
``response.completed`` is success. Plan-usage errors stop inference: there is
no retry and no fallback to another billing path, and the status, body,
code and request id are kept on the :class:`ProviderError`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Literal

from krellbot.ai.http import (
    BadStream,
    HttpTransport,
    StreamFailure,
    TransportError,
    error_from_exception,
    error_from_transport,
    iter_sse_data,
    parse_json_object,
    parse_tool_arguments,
)
from krellbot.ai.types import (
    ChatMessage,
    Done,
    ErrorCode,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallDone,
    ToolChoice,
    ToolDef,
    Usage,
)

_TIMEOUT = 120.0
_MODELS_TIMEOUT = 15.0
_MAX_MODELS = 1000
_MAX_MODEL_ID = 256
_MAX_DIAGNOSTIC = 2000
_MAX_PROVIDER_CODE = 128

Api = Literal["chat_completions", "responses"]

# Fields OpenAI's ChatGPT plan preview rejects on /v1/responses. The Responses
# body is built from an allow-list, so none of these is ever sent.
RESPONSES_UNSUPPORTED_FIELDS = frozenset(
    {
        "background",
        "conversation",
        "max_output_tokens",
        "max_tool_calls",
        "metadata",
        "moderation",
        "multi_agent",
        "previous_response_id",
        "prompt",
        "prompt_cache_retention",
        "safety_identifier",
        "temperature",
        "top_logprobs",
        "top_p",
        "truncation",
        "user",
    }
)
TOOL_NAMESPACE = "krellbot"
TOOL_NAMESPACE_DESCRIPTION = "Krellbot workstation tools: read the paper workstation and ask for confirmed changes."


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
                    "tool_calls": [_wire_tool_call(c) for c in m.tool_calls],
                }
            )
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def _wire_tool_call(c: ToolCall) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "id": c.id,
        "type": "function",
        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
    }
    if c.provider_data is not None:
        wire["extra_content"] = c.provider_data
    return wire


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
    """Chat Completions (or the Responses API) over SSE. ``base_url`` includes the version path (``.../v1``).

    ``api="responses"`` is the ChatGPT plan path. Its bearer token is
    ``credential``, or, when ``token_source`` is given, whatever that returns
    at the start of each stream (so a long turn picks up a refreshed token).
    """

    def __init__(
        self,
        base_url: str,
        credential: str | None,
        transport: HttpTransport,
        *,
        extra_headers: dict | None = None,
        token_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
        api: Api = "chat_completions",
        token_source: Callable[[], str] | None = None,
    ) -> None:
        self._api = api
        self._url = base_url.rstrip("/") + ("/responses" if api == "responses" else "/chat/completions")
        self._credential = credential
        self._token_source = token_source
        self._transport = transport
        self._extra = dict(extra_headers or {})
        self._token_limit_field = token_limit_field

    def stream(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDef],
        max_tokens: int,
        tool_choice: ToolChoice = "auto",
    ) -> Iterator[StreamEvent]:
        usage_box: list[Usage | None] = [None]
        try:
            if self._api == "responses":
                # No token limit: max_output_tokens is not supported for ChatGPT plan usage. No tool_choice
                # either: the M38.22 record does not list it, so the allow-listed body never carries it.
                yield from self._responses_stream(model, system, messages, tools, usage_box)
            else:
                yield from self._stream(model, system, messages, tools, max_tokens, tool_choice, usage_box)
        except TransportError as exc:
            if usage_box[0] is not None:
                yield usage_box[0]
            yield error_from_responses_transport(exc) if self._api == "responses" else error_from_transport(exc)
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
        tool_choice: ToolChoice,
        usage_box: list[Usage | None],
    ) -> Iterator[StreamEvent]:
        body: dict[str, Any] = {
            "model": model,
            "messages": _wire_messages(system, messages),
            self._token_limit_field: max_tokens,
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
            if tool_choice == "none":
                # The tools stay defined, so a history with tool calls stays valid; the model may not call one.
                body["tool_choice"] = "none"
        headers = {"accept": "text/event-stream", **self._extra}
        if self._credential:
            headers["authorization"] = f"Bearer {self._credential}"

        pending: dict[int, dict[str, Any]] = {}
        finish: str | None = None
        saw_done = False
        for payload in iter_sse_data(self._transport.post_stream(self._url, body, headers, _TIMEOUT)):
            if payload == "[DONE]":
                saw_done = True
                break
            chunk = parse_json_object(payload, "stream chunk")
            if chunk.get("error") is not None:
                raise StreamFailure(
                    ProviderError(_error_code_from_body(chunk["error"]), "The provider reported an error.")  # type: ignore[arg-type]
                )
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                usage_box[0] = Usage(_count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens")))
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
        if usage_box[0] is not None:
            yield usage_box[0]
        yield Done(finish)

    # ---- the Responses API (ChatGPT plan usage) --------------------------------------

    def _bearer(self) -> str | None:
        return self._token_source() if self._token_source is not None else self._credential

    def _responses_stream(
        self,
        model: str,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDef],
        usage_box: list[Usage | None],
    ) -> Iterator[StreamEvent]:
        # An allow-list: nothing in RESPONSES_UNSUPPORTED_FIELDS can reach the body.
        body: dict[str, Any] = {
            "model": model,
            "input": responses_input(messages),
            "store": False,
            "stream": True,
        }
        if system:
            body["instructions"] = system
        if tools:
            body["tools"] = [responses_namespace(tools)]
        headers = {"accept": "text/event-stream", **self._extra}
        token = self._bearer()
        if token:
            headers["authorization"] = f"Bearer {token}"

        pending: dict[int, dict[str, Any]] = {}
        for payload in iter_sse_data(self._transport.post_stream(self._url, body, headers, _TIMEOUT)):
            if payload == "[DONE]":
                break
            event = parse_json_object(payload, "stream event")
            kind = event.get("type")
            if kind in ("response.output_text.delta", "response.refusal.delta"):
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    yield TextDelta(delta)
            elif kind == "response.output_item.added" or kind == "response.output_item.done":
                _collect_function_item(event, pending, done=kind == "response.output_item.done")
            elif kind == "response.function_call_arguments.delta":
                slot = _function_slot(event, pending)
                if slot is not None and isinstance(event.get("delta"), str) and not slot["final"]:
                    slot["arguments"] += event["delta"]
            elif kind == "response.function_call_arguments.done":
                slot = _function_slot(event, pending)
                if slot is not None and isinstance(event.get("arguments"), str):
                    slot["arguments"] = event["arguments"]
            elif kind == "response.completed":
                response = _response_object(event)
                usage_box[0] = _responses_usage(response) or usage_box[0]
                calls = _finish_function_calls(pending)
                for call in calls:
                    yield ToolCallDone(call)
                if usage_box[0] is not None:
                    yield usage_box[0]
                yield Done("completed")
                return
            elif kind == "response.failed":
                response = _response_object(event)
                usage_box[0] = _responses_usage(response) or usage_box[0]
                err = response.get("error")
                err = err if isinstance(err, dict) else {}
                raise StreamFailure(_plan_error(None, err.get("code"), err.get("param"), None, _diagnostic(event)))
            elif kind == "response.incomplete":
                response = _response_object(event)
                usage_box[0] = _responses_usage(response) or usage_box[0]
                raise StreamFailure(
                    ProviderError(
                        "provider_bad_response",
                        "The model stopped before finishing its answer, so nothing ran.",
                        detail=_diagnostic(event),
                    )
                )
            elif kind == "error":
                raise StreamFailure(_plan_error(None, event.get("code"), event.get("param"), None, _diagnostic(event)))
            elif not isinstance(kind, str):
                raise BadStream("stream event has no type")
            # Every other event (created, in_progress, content parts, reasoning, ...) carries nothing Krell uses.
        # An interrupted stream, or one that ended without a terminal event, is never success.
        raise BadStream("stream ended without response.completed")


# ---- Responses API pieces ----------------------------------------------------------------


def responses_input(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """The full history as Responses input items. No ``system``-role item is ever produced."""

    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            out.append({"type": "function_call_output", "call_id": m.tool_call_id or "", "output": m.content})
            continue
        if m.content or m.role == "user":
            out.append({"role": m.role, "content": m.content})
        if m.role == "assistant":
            for c in m.tool_calls:
                item: dict[str, Any] = {
                    "type": "function_call",
                    "call_id": c.id,
                    "name": c.name,
                    "arguments": json.dumps(c.arguments),
                }
                namespace = c.provider_data.get("namespace") if isinstance(c.provider_data, dict) else None
                if isinstance(namespace, str) and namespace:
                    item["namespace"] = namespace
                out.append(item)
    return out


def responses_namespace(tools: Sequence[ToolDef]) -> dict[str, Any]:
    """Krellbot's function tools, grouped in one namespace as the ChatGPT plan preview requires."""

    return {
        "type": "namespace",
        "name": TOOL_NAMESPACE,
        "description": TOOL_NAMESPACE_DESCRIPTION,
        "tools": [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.input_schema}
            for t in tools
        ],
    }


def _response_object(event: dict) -> dict:
    response = event.get("response")
    if not isinstance(response, dict):
        raise BadStream("terminal event has no response object")
    return response


def _responses_usage(response: dict) -> Usage | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    return Usage(_count(usage.get("input_tokens")), _count(usage.get("output_tokens")))


def _function_slot(event: dict, pending: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    index = event.get("output_index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise BadStream("function call event without an output index")
    return pending.get(index)


def _collect_function_item(event: dict, pending: dict[int, dict[str, Any]], *, done: bool) -> None:
    item = event.get("item")
    if not isinstance(item, dict):
        raise BadStream("output item is not an object")
    if item.get("type") != "function_call":
        return  # messages and reasoning items arrive as deltas, or carry nothing Krell uses
    index = event.get("output_index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise BadStream("function call without an output index")
    slot = pending.setdefault(index, {"call_id": "", "name": "", "namespace": None, "arguments": "", "final": False})
    for key in ("call_id", "name"):
        if isinstance(item.get(key), str) and item[key]:
            slot[key] = item[key]
    if isinstance(item.get("namespace"), str) and item["namespace"]:
        slot["namespace"] = item["namespace"]
    arguments = item.get("arguments")
    if isinstance(arguments, str) and (done or not slot["arguments"]):
        slot["arguments"] = arguments
    elif arguments is not None and not isinstance(arguments, str):
        raise BadStream("function call arguments are not a string")
    if done:
        slot["final"] = True


def _finish_function_calls(pending: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """Validate every call first so a bad one suppresses the whole batch."""

    calls = []
    for index in sorted(pending):
        slot = pending[index]
        if not slot["call_id"] or not slot["name"]:
            raise BadStream("function call missing call_id or name")
        arguments = parse_tool_arguments(slot["arguments"])
        provider_data = {"namespace": slot["namespace"]} if slot["namespace"] else None
        calls.append(ToolCall(slot["call_id"], slot["name"], arguments, provider_data))
    return calls


def _diagnostic(value: object) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value)
    except (TypeError, ValueError, RecursionError):
        return ""
    return text[:_MAX_DIAGNOSTIC]


def _clean_code(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_PROVIDER_CODE or not value.isprintable():
        return None
    return value


# OpenAI's structured ChatGPT plan error codes (Errors and recovery), and what each means here.
_PLAN_CODES: dict[str, ErrorCode] = {
    "subscription_sharing_usage_limit_exceeded": "provider_usage_limit",
    "subscription_sharing_usage_unavailable": "provider_unavailable",
    "subscription_sharing_user_unavailable": "provider_unavailable",
    "subscription_sharing_user_not_eligible": "provider_plan_not_eligible",
    "chatpass_v2_scope_not_authorized": "provider_plan_not_eligible",
    "chatpass_v2_invalid_authorization_context": "provider_plan_not_eligible",
    "subscription_sharing_unsupported_capability": "provider_plan_unsupported",
    "subscription_sharing_route_not_supported": "provider_plan_unsupported",
    "subscription_sharing_invalid_user": "provider_auth_failed",
}

PLAN_MESSAGES: dict[str, str] = {
    "provider_usage_limit": (
        "You reached a ChatGPT usage limit, for your plan or for Krellbot. Requests are paused: "
        "review the limit in ChatGPT settings with Manage usage. Nothing was retried or billed elsewhere."
    ),
    "provider_plan_not_eligible": (
        "OpenAI did not allow ChatGPT plan usage here (account, workspace, region or permission). "
        "Nothing was retried. You can connect an OpenAI API key instead."
    ),
    "provider_plan_unsupported": (
        "ChatGPT plan usage does not support part of this request, so it was not sent again."
    ),
    "provider_auth_failed": "OpenAI did not accept the ChatGPT sign-in. Sign in with ChatGPT again.",
    "provider_unavailable": "ChatGPT plan usage is unavailable right now. Try again later.",
    "provider_rate_limited": "OpenAI is rate limiting requests. Wait a moment, then try again.",
    "provider_bad_response": "OpenAI sent a reply Krellbot could not use, so nothing ran.",
}


def _code_for_status(status: int | None) -> ErrorCode:
    if status == 401:
        return "provider_auth_failed"
    if status == 403:
        return "provider_plan_not_eligible"
    if status == 429:
        return "provider_rate_limited"
    if status is None or status >= 500:
        return "provider_unavailable"
    return "provider_bad_response"


def _plan_error(
    status: int | None, provider_code: object, param: object, request_id: str | None, detail: str
) -> ProviderError:
    code = _clean_code(provider_code)
    mapped = _PLAN_CODES.get(code or "") or _code_for_status(status)
    return ProviderError(
        mapped,
        PLAN_MESSAGES[mapped],
        status=status,
        provider_code=code,
        param=_clean_code(param),
        request_id=request_id,
        detail=detail or None,
    )


def error_from_responses_transport(exc: TransportError) -> ProviderError:
    """A pre-stream failure on the ChatGPT plan path: keep status, body, code and request id.

    A standard ``{"error": {...}}`` body gives the exact code and param. A
    direct-admission ``{"detail": "..."}`` body is diagnostic text only; the
    status decides.
    """

    body = exc.body if isinstance(exc.body, str) else ""
    provider_code: object = None
    param: object = None
    try:
        parsed = json.loads(body) if body else None
    except (ValueError, RecursionError):
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
        provider_code = parsed["error"].get("code")
        param = parsed["error"].get("param")
    return _plan_error(exc.status, provider_code, param, exc.request_id, body[:_MAX_DIAGNOSTIC])


def parse_chatgpt_models(payload: Any) -> list[str]:
    """Model slugs to show for a ChatGPT plan: ``visibility == "list"`` only, in the server's order."""

    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in models:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug = item.get("slug")
        if not isinstance(slug, str):
            continue
        slug = slug.strip()
        if not slug or len(slug) > _MAX_MODEL_ID or not slug.isprintable() or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
        if len(out) >= _MAX_MODELS:
            break
    return out


def fetch_chatgpt_models(base_url: str, access_token: str, transport: HttpTransport) -> list[str]:
    """``GET /v1/models`` with the same access token used for inference. Raises ``TransportError``."""

    url = base_url.rstrip("/") + "/models"
    return parse_chatgpt_models(transport.get_json(url, {"authorization": f"Bearer {access_token}"}, _MODELS_TIMEOUT))


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _collect_tool_fragments(fragments: Any, pending: dict[int, dict[str, Any]]) -> None:
    if fragments is None:
        return
    if not isinstance(fragments, list):
        raise BadStream("tool_calls is not a list")
    for frag in fragments:
        if not isinstance(frag, dict) or not isinstance(frag.get("index", 0), int):
            raise BadStream("malformed tool call fragment")
        slot = pending.setdefault(
            frag.get("index", 0), {"id": "", "name": "", "arguments": "", "args_obj": None, "extra": None}
        )
        if isinstance(frag.get("id"), str):
            slot["id"] = frag["id"]
        fn = frag.get("function") or {}
        if not isinstance(fn, dict):
            raise BadStream("malformed tool call function")
        if isinstance(fn.get("name"), str):
            slot["name"] += fn["name"]
        args = fn.get("arguments")
        if isinstance(args, str):
            slot["arguments"] += args
        elif isinstance(args, dict):
            slot["args_obj"] = args
        elif args is not None:
            raise BadStream("tool call arguments have an unexpected type")
        if isinstance(frag.get("extra_content"), dict):
            slot["extra"] = frag["extra_content"]


def _finish_tool_calls(pending: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """Validate every pending call first so a bad one suppresses the whole batch."""
    calls = []
    for index in sorted(pending):
        slot = pending[index]
        if not slot["id"] or not slot["name"]:
            raise BadStream("tool call missing id or name")
        if slot["args_obj"] is not None and not slot["arguments"].strip():
            arguments = slot["args_obj"]
        elif slot["args_obj"] is not None:
            raise BadStream("tool call arguments mix object and string forms")
        else:
            arguments = parse_tool_arguments(slot["arguments"])
        calls.append(ToolCall(slot["id"], slot["name"], arguments, slot["extra"]))
    return calls
