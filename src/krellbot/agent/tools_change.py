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
2. Pre-check (:attr:`~krellbot.agent.core.ToolSpec.precheck`): ``venue`` and
   ``pair`` must match the read tools' patterns (``mcp_invalid_request``)
   before anything is read. Then the config is loaded, and when any stored
   record on that venue and pair has a mode other than ``paper`` the call is
   refused with ``agent_live_change_refused``. This runs before the confirm
   step, so Krell never asks a human to confirm a change to a live (or
   non-paper) strategy, and again on the confirmed call, where a refusal
   leaves the ticket unspent. Every record is checked, not only the first,
   because ``PaperService.disarm`` removes every record on the pair.
3. Confirm step: Krell's call returns ``needs_confirm`` with a ticket bound
   to its exact params, and runs once the human confirms
   (:mod:`krellbot.agent.confirm`). External MCP agents get no ticket.
4. The handler here: the same check again on a fresh read of the config,
   then the service. With no record, the service answers with its own
   ``not_armed``. ``PaperService`` reads the stored mode once more before it
   writes; its ``stored_mode_not_paper`` (a record re-armed between the
   reads) is reported as ``agent_live_change_refused`` too.

A refused live target never reaches the service, so the config keeps its
exact bytes. The core's ``agent_action`` audit record journals every outcome
with its code.

``close_position`` sells at market, so it is never an agent tool: nothing
here registers it, and the core answers ``mcp_unknown_tool``. This module
binds only to the ``krellbot.application.paper`` facade and never imports
``run``, ``venues``, ``execution``, ``keys`` or ``secrets``.
"""

from __future__ import annotations

from collections.abc import Callable

from krellbot import sanitize
from krellbot.agent.core import LABEL_PAPER, SCHEMA_VERSION, AgentToolCore, ToolContext, ToolRefusal, ToolSpec
from krellbot.agent.limits import DISARM_TOOLS, PAUSE_RESUME_TOOLS
from krellbot.agent.tools_read import _market_names
from krellbot.application.paper import CODE_STORED_MODE_NOT_PAPER, CommandResultV1, PaperService
from krellbot.config import load_config

# The names the agent limits gate (``limits.CHANGE_TOOLS``), so the allow flags and the tools agree.
TOOL_PAUSE_ENTRIES, TOOL_RESUME_ENTRIES = PAUSE_RESUME_TOOLS
(TOOL_DISARM,) = DISARM_TOOLS

CODE_AGENT_LIVE_CHANGE_REFUSED = "agent_live_change_refused"
MESSAGE_AGENT_LIVE_CHANGE_REFUSED = (
    "this strategy is armed live (or non-paper); agent change tools act on paper strategies only, "
    "so a person must use Pause or Disarm in the workstation"
)
MAX_MESSAGE_CHARS = 200

Command = Callable[[PaperService, str, str], CommandResultV1]


def _live_change_refused(tool: str) -> dict:
    return ToolRefusal(CODE_AGENT_LIVE_CHANGE_REFUSED, MESSAGE_AGENT_LIVE_CHANGE_REFUSED, tool).to_dict()


def _paper_target(ctx: ToolContext, tool: str, params: dict) -> tuple[str, str] | dict:
    """``(venue, pair)`` of a call that may go to the paper service, or its refusal.

    ``venue`` and ``pair`` are checked against the read tools' patterns before
    the config is read. Then every stored record on that venue and pair must
    be a paper record; a missing record passes, and the service answers it.
    """
    names, refusal = _market_names(tool, params, timeframe=False)
    if refusal:
        return refusal
    venue, pair, _ = names
    stored = [armed for armed in load_config(ctx.home).armed if armed.venue == venue and armed.pair == pair]
    if any(armed.mode != "paper" for armed in stored):
        return _live_change_refused(tool)
    return venue, pair


def _result(tool: str, venue: str, pair: str, result: CommandResultV1) -> dict:
    """The agent result for a paper command: its ``ok``, ``effect``, ``code`` and ``message``.

    The service's ``stored_mode_not_paper`` (the record was re-armed live
    after the handler's read) is the same refusal as a live target.
    """
    if result.code == CODE_STORED_MODE_NOT_PAPER:
        return _live_change_refused(tool)
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


def _precheck(tool: str) -> Callable[[ToolContext, dict], dict | None]:
    """The pre-check for one change tool: its refusal, or ``None`` to go on to the confirm step."""

    def precheck(ctx: ToolContext, params: dict) -> dict | None:
        target = _paper_target(ctx, tool, params)
        return target if isinstance(target, dict) else None

    return precheck


def _handler(tool: str, command: Command) -> Callable[[ToolContext, dict], dict]:
    """The handler for one change tool: the pre-check again on a fresh read, then ``command``."""

    def handler(ctx: ToolContext, params: dict) -> dict:
        target = _paper_target(ctx, tool, params)
        if isinstance(target, dict):
            return target
        venue, pair = target
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

    def spec(name: str, description: str, command: Command) -> ToolSpec:
        return ToolSpec(
            name=name,
            effect="change",
            cost="light",
            input_schema=_schema(),
            labels=(LABEL_PAPER,),
            description=f"{description} {_WHO_MAY}",
            handler=_handler(name, command),
            precheck=_precheck(name),
        )

    return (
        spec(
            TOOL_PAUSE_ENTRIES,
            "Pause new entries on an armed paper strategy. It stays armed, and exits and stops keep working.",
            lambda service, venue, pair: service.pause_entries(venue=venue, pair=pair),
        ),
        spec(
            TOOL_RESUME_ENTRIES,
            (
                "Resume entries on a paused paper strategy. This also lifts the entry hold left when a person "
                "closed its position (close-by-operator), so the strategy may buy again."
            ),
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
