"""Message, tool and stream-event types shared by the provider adapters."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

ErrorCode = Literal[
    "provider_auth_failed",
    "provider_rate_limited",
    "provider_unavailable",
    "provider_bad_response",
    # ChatGPT plan usage (M38.23): the plan's usage limit, and plan refusals that retrying cannot fix.
    "provider_usage_limit",
    "provider_plan_not_eligible",
    "provider_plan_unsupported",
]

# Whether the model may call the offered tools: "auto" lets it choose, "none" forbids calls while the
# tools stay defined, so a history holding tool calls and results is still a well-formed request.
ToolChoice = Literal["auto", "none"]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Opaque provider metadata (e.g. Gemini thought signatures), echoed back verbatim, never interpreted.
    provider_data: dict[str, Any] | None = None


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDone:
    call: ToolCall


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Done:
    stop_reason: str


@dataclass(frozen=True)
class ProviderError:
    code: ErrorCode
    # A fixed string: never provider text, model text or a credential.
    message: str
    # Diagnostics the ChatGPT plan path keeps (M38.22: status, body, code and request id).
    # ``detail`` is the provider's bounded body: diagnostic text, never shown as the message.
    status: int | None = None
    provider_code: str | None = None
    param: str | None = None
    request_id: str | None = None
    detail: str | None = None


StreamEvent = TextDelta | ToolCallDone | Usage | Done | ProviderError


class ChatAdapter(Protocol):
    def stream(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDef],
        max_tokens: int,
        tool_choice: ToolChoice = "auto",
    ) -> Iterator[StreamEvent]: ...
