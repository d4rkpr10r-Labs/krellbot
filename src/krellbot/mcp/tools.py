"""MCP tool surface — read / draft / validate / backtest / paper-arm.

A thin front door over :class:`krellbot.agent.core.AgentToolCore`: every
``call`` runs through the core with an ``mcp:<client>`` actor, so the hard
live/key refusals, the secret-param refusal, result labels and the
``agent_action`` audit record match Krell's. The verbs bind only to
``krellbot.application`` facades. Nothing here imports ``run``, ``venues``,
``execution``, ``keys``, ``secrets.get``, ``live_gate``, or ``live_preflight``.
Live arm and key read are refused with typed codes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from krellbot.agent.actors import Actor
from krellbot.agent.core import AgentToolCore
from krellbot.application import user_policy

# Canonical tool names (underscore form matches Python kwargs; aliases accepted).
ALLOWED_TOOLS: tuple[str, ...] = (
    "read",
    "draft",
    "validate",
    "backtest",
    "paper_arm",
)


class McpTools:
    """In-process MCP tool surface for local agents (no network host required)."""

    def __init__(
        self,
        home: Path,
        *,
        clock: Callable[[], float] | None = None,
        fetch: Callable[..., Any] | None = None,
        actor: Actor | None = None,
    ) -> None:
        self._home = Path(home)
        self._actor = actor if actor is not None else Actor.mcp(None)
        self._core = AgentToolCore(self._home, clock=clock, fetch=fetch)

    def list_tools(self) -> list[str]:
        """Return the closed allowlist (no live / keys tools)."""
        return list(ALLOWED_TOOLS)

    def policy_check(self, action: str) -> dict:
        """Evaluate one closed operator-policy action (does not mutate).

        Used by agents and tests to honor ``paper_arm`` / ``live`` /
        ``key_export`` without expanding the tool allowlist. Probes use
        :func:`user_policy.check` so they do not journal a refuse receipt;
        enforce seams (e.g. ``paper_arm``) call ``ensure_allowed`` instead.
        Hard MCP refuse codes for live/key tool names stay unchanged (M22.1).
        """
        decision = user_policy.check(self._home, str(action))
        body = decision.to_dict()
        body["tool"] = "policy_check"
        if not decision.ok:
            body["effect"] = "refused"
        return body

    def call(self, tool: str, **params: Any) -> dict:
        """Dispatch one tool through the agent core; never raises.

        Refusals return ``ok=False`` typed dicts. Every result also carries
        ``actor`` (``mcp:<client>``) and ``labels``, and every call appends
        one ``agent_action`` journal record.
        """
        return self._core.call(self._actor, tool, params)


__all__ = [
    "ALLOWED_TOOLS",
    "McpTools",
]
