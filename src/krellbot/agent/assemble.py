"""Build the agent tool core with every agent tool registered.

Both front doors (the stdio MCP server in :mod:`krellbot.mcp.stdio`, and
Krell in the engine) build their core here, so they offer the same tools:
the five built-in verbs, the read tools, ``compare`` and the paper change
tools. Each ``register_*`` call is idempotent (``core.has``).

``confirm`` is the engine's confirm-ticket ledger. The MCP server passes
none: MCP actors never get tickets, because their client asks the human
before each call.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from krellbot.agent.confirm import ConfirmLedger
from krellbot.agent.core import AgentToolCore
from krellbot.agent.tools_change import register_change_tools
from krellbot.agent.tools_read import register_read_tools
from krellbot.agent.tools_research import register_research_tools


def build_agent_core(
    home: Path,
    *,
    clock: Callable[[], float] | None = None,
    fetch: Callable[..., Any] | None = None,
    confirm: ConfirmLedger | None = None,
) -> AgentToolCore:
    """Return a core bound to ``home`` with the read, research and change tools registered."""
    core = AgentToolCore(Path(home), clock=clock, fetch=fetch, confirm=confirm)
    register_read_tools(core)
    register_research_tools(core)
    register_change_tools(core)
    return core


__all__ = ["build_agent_core"]
