"""Agent change tools: ``pause_entries``, ``resume_entries`` and ``disarm``, on paper only (M38.6).

Each tool takes ``{venue, pair}`` and delegates to
:class:`~krellbot.application.paper.PaperService`. Effect ``change``, cost
``light``, result label ``paper``. A call runs through the agent tool core
(:mod:`krellbot.agent.core`) in this order:

1. Policy step: the operator's per-actor allow flags
   (:func:`krellbot.agent.limits.check_agent_call`). Krell may pause, resume
   and disarm by default; external MCP agents may not until the operator
   turns ``mcp_allow_pause_resume`` or ``mcp_allow_disarm`` on. A refusal is
   ``agent_change_not_allowed``, journaled as a ``user_policy`` refused row.
2. Confirm step: Krell's call returns ``needs_confirm`` with a ticket bound
   to its exact params, and runs once the human confirms
   (:mod:`krellbot.agent.confirm`). External MCP agents get no ticket.
3. The handler here: ``venue`` and ``pair`` must match the read tools'
   patterns (``mcp_invalid_request``) before anything is read. Then the
   config is loaded, and when any stored record on that venue and pair has a
   mode other than ``paper`` the call is refused with
   ``agent_live_change_refused`` without calling the service, so the config
   keeps its exact bytes. Every record is checked, not only the first,
   because ``PaperService.disarm`` removes every record on the pair. With no
   record, the service answers with its own ``not_armed``.

The core's ``agent_action`` audit record journals every outcome with its code.
``PaperService`` reads the stored mode again before it writes and refuses a
non-paper record with ``stored_mode_not_paper``, so a record re-armed live
between the two reads is still never changed.

``close_position`` sells at market, so it is never an agent tool: nothing
here registers it, and the core answers ``mcp_unknown_tool``. This module
binds only to the ``krellbot.application.paper`` facade and never imports
``run``, ``venues``, ``execution``, ``keys`` or ``secrets``.
"""

from __future__ import annotations

from collections.abc import Callable

from krellbot import sanitize
from krellbot.agent.core import (
    LABEL_PAPER,
    SCHEMA_VERSION,
    AgentToolCore,
    ToolContext,
    ToolRefusal,
    ToolSpec,
    invalid_request,
)
from krellbot.agent.limits import DISARM_TOOLS, PAUSE_RESUME_TOOLS
from krellbot.agent.tools_read import _PAIR_RE, _VENUE_RE, MESSAGE_BAD_PAIR, MESSAGE_BAD_VENUE
from krellbot.application.paper import CommandResultV1, PaperService
from krellbot.config import load_config

# The names the agent limits gate (``limits.CHANGE_TOOLS``), so the allow flags and the tools agree.
TOOL_PAUSE_ENTRIES, TOOL_RESUME_ENTRIES = PAUSE_RESUME_TOOLS
(TOOL_DISARM,) = DISARM_TOOLS

CODE_AGENT_LIVE_CHANGE_REFUSED = "agent_live_change_refused"
MESSAGE_AGENT_LIVE_CHANGE_REFUSED = (
    "this market is not armed on paper; agent change tools act on paper strategies only, "
    "and a live strategy stays under human control"
)
MAX_MESSAGE_CHARS = 200


def _market(tool: str, params: dict) -> tuple[str, str] | dict:
    """``(venue, pair)`` checked against the read tools' patterns, or the ``mcp_invalid_request`` refusal."""
    venue = params.get("venue")
    pair = params.get("pair")
    if not isinstance(venue, str) or _VENUE_RE.fullmatch(venue) is None:
        return invalid_request(tool, f"venue: {MESSAGE_BAD_VENUE}").to_dict()
    if not isinstance(pair, str) or _PAIR_RE.fullmatch(pair) is None:
        return invalid_request(tool, f"pair: {MESSAGE_BAD_PAIR}").to_dict()
    return venue, pair


def _result(tool: str, venue: str, pair: str, result: CommandResultV1) -> dict:
    """The agent result for a paper command: its ``ok``, ``effect``, ``code`` and ``message``."""
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": result.ok is True,
        "effect": result.effect,
        "code": result.code,
        "message": sanitize.text(sanitize.redact(result.message), max_len=MAX_MESSAGE_CHARS),
        "tool": tool,
        "venue": venue,
        "pair": pair,
    }


def _paper_change(tool: str, command: Callable[[PaperService, str, str], CommandResultV1]):
    """The handler for one change tool: check the market, refuse a non-paper target, then run ``command``."""

    def handler(ctx: ToolContext, params: dict) -> dict:
        market = _market(tool, params)
        if isinstance(market, dict):
            return market
        venue, pair = market
        stored = [armed for armed in load_config(ctx.home).armed if armed.venue == venue and armed.pair == pair]
        if any(armed.mode != "paper" for armed in stored):
            return ToolRefusal(CODE_AGENT_LIVE_CHANGE_REFUSED, MESSAGE_AGENT_LIVE_CHANGE_REFUSED, tool).to_dict()
        return _result(tool, venue, pair, command(PaperService(ctx.home, clock=ctx.clock), venue, pair))

    return handler


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "venue": {"type": "string", "description": "Venue name, for example kraken."},
            "pair": {"type": "string", "description": "Venue pair, for example SUIUSD."},
        },
        "required": ["venue", "pair"],
    }


_WHO_MAY = (
    "Paper strategies only: a live strategy is refused. External agents may use it only when the operator "
    "allows it, and Krell asks a human to confirm first."
)


def change_specs() -> tuple[ToolSpec, ...]:
    """The three paper change tools, in ``limits.CHANGE_TOOLS`` order. Schemas are built fresh per call."""

    def spec(name: str, description: str, command) -> ToolSpec:
        return ToolSpec(
            name=name,
            effect="change",
            cost="light",
            input_schema=_schema(),
            labels=(LABEL_PAPER,),
            description=f"{description} {_WHO_MAY}",
            handler=_paper_change(name, command),
        )

    return (
        spec(
            TOOL_PAUSE_ENTRIES,
            "Pause new entries on an armed paper strategy. It stays armed, and exits and stops keep working.",
            lambda service, venue, pair: service.pause_entries(venue=venue, pair=pair),
        ),
        spec(
            TOOL_RESUME_ENTRIES,
            "Resume entries on a paused paper strategy; this also lifts the entry hold an operator close leaves.",
            lambda service, venue, pair: service.resume_entries(venue=venue, pair=pair),
        ),
        spec(
            TOOL_DISARM,
            "Disarm a paper strategy so it stops trading. Nothing is sold: its paper holdings stay as they are.",
            lambda service, venue, pair: service.disarm(venue=venue, pair=pair),
        ),
    )


def register_change_tools(core: AgentToolCore) -> None:
    """Add the paper change tools to ``core``, each unless it is already there.

    Idempotent, so every front door (the MCP server, Krell) can call it on a
    shared core. A malformed spec still raises from ``register``.
    """
    for spec in change_specs():
        if not core.has(spec.name):
            core.register(spec)


__all__ = [
    "CODE_AGENT_LIVE_CHANGE_REFUSED",
    "MESSAGE_AGENT_LIVE_CHANGE_REFUSED",
    "TOOL_DISARM",
    "TOOL_PAUSE_ENTRIES",
    "TOOL_RESUME_ENTRIES",
    "change_specs",
    "register_change_tools",
]
