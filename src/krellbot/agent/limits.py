"""Agent limits: the operator's hard limits on what agents may do (M38.3).

The limits live in ``user-policy.json`` (schema "2" ``agents``,
:class:`krellbot.application.user_policy.AgentLimits`) and bind Krell and
every external MCP agent alike. The agent tool core calls
:func:`check_agent_call` in its policy step, before ``paper_arm`` and before
the paper change tools. Every refusal is journaled as a ``kind=user_policy``
``refused`` row through ``user_policy._append_policy_refuse``, with a typed
``agent_limit_*`` or ``agent_change_not_allowed`` code and the actor.

A paper arm is checked in this order: venue, pair, paper balance, risk cap,
daily loss limit, then the number of agent arms running. A value the caller
could not resolve (``None``) is refused, never guessed.

The refusal codes live here because the core imports this module; the core
re-exports them, and ``krellbot.mcp.errors`` re-exports them from the core.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path

from krellbot.agent import arm_registry
from krellbot.agent.actors import Actor
from krellbot.application import user_policy
from krellbot.application.user_policy import AgentLimits, PolicyDecision

CODE_AGENT_LIMIT_VENUE = "agent_limit_venue"
CODE_AGENT_LIMIT_PAIR = "agent_limit_pair"
CODE_AGENT_LIMIT_BALANCE = "agent_limit_balance"
CODE_AGENT_LIMIT_RISK_CAP = "agent_limit_risk_cap"
CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED = "agent_limit_daily_loss_required"
CODE_AGENT_LIMIT_MAX_ARMS = "agent_limit_max_arms"
CODE_AGENT_CHANGE_NOT_ALLOWED = "agent_change_not_allowed"

TOOL_PAPER_ARM = "paper_arm"
# The paper change tools M38.6 registers, gated by the per-actor allow flags.
PAUSE_RESUME_TOOLS: tuple[str, ...] = ("pause_entries", "resume_entries")
DISARM_TOOLS: tuple[str, ...] = ("disarm",)
CHANGE_TOOLS: tuple[str, ...] = (*PAUSE_RESUME_TOOLS, *DISARM_TOOLS)
LIMITED_TOOLS: tuple[str, ...] = (TOOL_PAPER_ARM, *CHANGE_TOOLS)

MESSAGE_ARM_NOT_RECORDED = "the agent arm could not be counted toward the agent arm limit, so it was disarmed"


def check_agent_call(
    home: Path,
    actor: Actor,
    tool: str,
    params: Mapping[str, object],
    *,
    resolved: Mapping[str, object],
    clock: Callable[[], float] | None = None,
) -> PolicyDecision | None:
    """Check one agent call against the operator's agent limits.

    Returns ``None`` when allowed (or when ``tool`` has no agent limits), else
    the journaled refusal. For ``paper_arm``, ``resolved`` carries ``venue``,
    ``pair``, ``paper_balance``, ``risk_cap_pct`` and ``daily_loss_limit_pct``
    as ``PaperService.arm`` will use them (``None`` when unknown). The change
    tools need only the actor. ``params`` is the call's raw params; the
    current limits read only ``resolved``. A malformed policy file refuses
    every limited call with ``user_policy_invalid``.
    """
    if tool not in LIMITED_TOOLS:
        return None
    probe = user_policy.check(home, user_policy.ACTION_PAPER_ARM)
    if probe.code == user_policy.CODE_POLICY_INVALID or probe.policy is None:
        return refuse(home, actor, tool, user_policy.CODE_POLICY_INVALID, user_policy.MESSAGE_INVALID, clock=clock)
    agents = probe.policy.agents
    if tool == TOOL_PAPER_ARM:
        refusal = _paper_arm_refusal(home, agents, resolved)
    else:
        refusal = _change_refusal(agents, actor, tool)
    if refusal is None:
        return None
    code, message = refusal
    return refuse(home, actor, tool, code, message, clock=clock)


def refuse(
    home: Path,
    actor: Actor,
    tool: str,
    code: str,
    message: str,
    *,
    clock: Callable[[], float] | None = None,
) -> PolicyDecision:
    """Journal an agent-limit refusal (``kind=user_policy``) and return it."""
    user_policy._append_policy_refuse(
        home,
        ts=int((clock or time.time)()),
        action=tool,
        code=code,
        message=message,
        actor=actor.label,
    )
    return PolicyDecision(code, False, message, action=tool)


def _at_most(value: object, limit: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value <= limit


def _paper_arm_refusal(home: Path, agents: AgentLimits, resolved: Mapping[str, object]) -> tuple[str, str] | None:
    if agents.allowed_venues is not None and resolved.get("venue") not in agents.allowed_venues:
        return CODE_AGENT_LIMIT_VENUE, "the operator's agent limits do not allow this venue"
    pair = resolved.get("pair")
    if agents.allowed_pairs is not None and (not pair or pair not in agents.allowed_pairs):
        return CODE_AGENT_LIMIT_PAIR, "the operator's agent limits do not allow this pair"
    if not _at_most(resolved.get("paper_balance"), agents.max_paper_balance):
        return (
            CODE_AGENT_LIMIT_BALANCE,
            f"agent paper arms need a paper balance of at most {agents.max_paper_balance}",
        )
    if not _at_most(resolved.get("risk_cap_pct"), agents.max_risk_cap_pct):
        return (
            CODE_AGENT_LIMIT_RISK_CAP,
            (
                f"agent-armed packs may risk at most {agents.max_risk_cap_pct}% of starting cash per entry "
                "(the pack's risk.max_account_pct)"
            ),
        )
    if agents.require_daily_loss_limit and not isinstance(resolved.get("daily_loss_limit_pct"), Decimal):
        return (
            CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED,
            "agent paper arms need a daily loss limit (daily_loss_limit_pct, a percent of the paper balance)",
        )
    try:
        running = len(arm_registry.running_agent_arms(home))
    except arm_registry.AgentArmRegistryUnreadable:
        return (
            CODE_AGENT_LIMIT_MAX_ARMS,
            f"agent arms cannot be counted because {arm_registry.REGISTRY_FILENAME} cannot be read",
        )
    if running >= agents.max_agent_arms:
        return (
            CODE_AGENT_LIMIT_MAX_ARMS,
            f"agents already run {running} of at most {agents.max_agent_arms} armed strategies",
        )
    return None


def _change_refusal(agents: AgentLimits, actor: Actor, tool: str) -> tuple[str, str] | None:
    krell = actor.kind == "krell"
    if tool in PAUSE_RESUME_TOOLS:
        allowed = agents.krell_allow_pause_resume if krell else agents.mcp_allow_pause_resume
        verb = "pause or resume"
    else:
        allowed = agents.krell_allow_disarm if krell else agents.mcp_allow_disarm
        verb = "disarm"
    if allowed:
        return None
    who = "Krell" if krell else "external agents"
    return CODE_AGENT_CHANGE_NOT_ALLOWED, f"the operator has not allowed {who} to {verb} strategies"


__all__ = [
    "CHANGE_TOOLS",
    "CODE_AGENT_CHANGE_NOT_ALLOWED",
    "CODE_AGENT_LIMIT_BALANCE",
    "CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED",
    "CODE_AGENT_LIMIT_MAX_ARMS",
    "CODE_AGENT_LIMIT_PAIR",
    "CODE_AGENT_LIMIT_RISK_CAP",
    "CODE_AGENT_LIMIT_VENUE",
    "DISARM_TOOLS",
    "LIMITED_TOOLS",
    "MESSAGE_ARM_NOT_RECORDED",
    "PAUSE_RESUME_TOOLS",
    "TOOL_PAPER_ARM",
    "check_agent_call",
    "refuse",
]
