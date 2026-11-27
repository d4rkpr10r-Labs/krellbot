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
]


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
    message: str


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
    ) -> Iterator[StreamEvent]: ...
