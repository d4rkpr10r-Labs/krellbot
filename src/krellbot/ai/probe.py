"""Capability probe: can this model call tools?

One small request offers a single ``ping`` tool. A ``ping`` call means the
model can drive Krellbot's tools (``tools``); an answer that ends normally
without one means Krell can only explain with it (``chat_only``); an answer
cut off by the token limit proves nothing either way (``unprobed``, so a
reasoning model that spent its budget thinking is not misjudged); a provider
error means ``failed``. Where the provider lists models, the list is fetched
first, best effort: some compatible servers have no list, and the tool round
trip decides.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from krellbot.ai.connections import Connection
from krellbot.ai.providers import PRESETS
from krellbot.ai.types import ChatAdapter, ChatMessage, Done, ProviderError, ToolCallDone, ToolDef

# Room for a reasoning model to think before it calls the tool.
PROBE_MAX_TOKENS = 512
# Stop reasons that mean the reply was cut off: OpenAI-compatible, then Anthropic.
LENGTH_STOP_REASONS = frozenset({"length", "max_tokens"})
PING_TOOL = ToolDef(
    name="ping",
    description="Connection check from Krellbot. Call it once with reply set to ok.",
    # One property: some providers reject an object schema with no properties.
    input_schema={
        "type": "object",
        "properties": {"reply": {"type": "string", "description": "Always ok."}},
        "required": ["reply"],
    },
)
PROBE_SYSTEM = "This is an automatic connection check. Call the ping tool once. Do not write any text."
PROBE_MESSAGES = (ChatMessage("user", "Call the ping tool now."),)

MESSAGE_TOOLS = "The model called the test tool, so Krell can use Krellbot's tools with it."
MESSAGE_CHAT_ONLY = "The model answered without calling the test tool, so Krell can explain but not act with it."
MESSAGE_CUT_OFF = (
    "The model reached the check's token limit before calling the test tool, so its tool support "
    "is not known yet. Run the check again, or choose another model."
)
MESSAGE_ADAPTER_FAILED = "The provider adapter failed unexpectedly."


@dataclass(frozen=True)
class ProbeResult:
    capability: Literal["tools", "chat_only", "unprobed", "failed"]
    models: list[str] = field(default_factory=list)
    message: str = ""
    # The adapter's typed error code when ``capability`` is ``failed``.
    error: str | None = None


def probe(
    conn: Connection,
    adapter: ChatAdapter,
    *,
    list_models: Callable[[], Sequence[str]] | None = None,
) -> ProbeResult:
    """Never raises. Messages are fixed strings and never carry provider text or the credential."""

    models: list[str] = []
    preset = PRESETS.get(conn.preset)
    if preset is not None and preset.lists_models and list_models is not None:
        try:
            models = [m for m in list_models() if isinstance(m, str)]
        except Exception:  # noqa: BLE001 — listing is best effort; its error text is dropped
            models = []

    called = False
    stop_reason: str | None = None
    try:
        for event in adapter.stream(
            model=conn.model,
            system=PROBE_SYSTEM,
            messages=PROBE_MESSAGES,
            tools=(PING_TOOL,),
            max_tokens=PROBE_MAX_TOKENS,
        ):
            if isinstance(event, ProviderError):
                return ProbeResult("failed", models, event.message, event.code)
            if isinstance(event, ToolCallDone) and event.call.name == PING_TOOL.name:
                called = True
            elif isinstance(event, Done):
                stop_reason = event.stop_reason
    except Exception:  # noqa: BLE001 — adapters should not raise; a probe never does
        return ProbeResult("failed", models, MESSAGE_ADAPTER_FAILED, "provider_bad_response")
    if called:
        return ProbeResult("tools", models, MESSAGE_TOOLS)
    if stop_reason in LENGTH_STOP_REASONS:
        return ProbeResult("unprobed", models, MESSAGE_CUT_OFF)
    return ProbeResult("chat_only", models, MESSAGE_CHAT_ONLY)
