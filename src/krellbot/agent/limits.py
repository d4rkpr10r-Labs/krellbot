"""Agent limits: the operator's hard limits on what agents may do (M38.3).

The limits live in ``user-policy.json`` (schema "2" ``agents``,
:class:`krellbot.application.user_policy.AgentLimits`) and bind Krell and
every external MCP agent alike. The agent tool core calls
:func:`check_agent_call` in its policy step, before ``paper_arm`` and before
the paper change tools. Every refusal is journaled as a ``kind=user_policy``
``refused`` row through ``user_policy._append_policy_refuse``, with a typed
``agent_limit_*``, ``agent_change_not_allowed`` or ``agent_arm_*`` code and
the actor.

A paper arm is checked twice. Before the arm, :func:`check_agent_call`
checks venue, pair, paper balance, risk cap, daily loss limit, then the
number of agent arms running. After the arm, :func:`settle_agent_arm` checks
the stored ``ArmedPack`` (the authority) against the same limits, then
re-counts and records the arm under the registry lock
(:func:`krellbot.agent.arm_registry.claim_agent_arm`). A value that cannot be
resolved (``None``) is refused, never guessed.

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
from krellbot.application.user_policy import AgentLimits, PolicyDecision, plain_decimal
from krellbot.config import ArmedPack

CODE_AGENT_LIMIT_VENUE = "agent_limit_venue"
CODE_AGENT_LIMIT_PAIR = "agent_limit_pair"
CODE_AGENT_LIMIT_BALANCE = "agent_limit_balance"
CODE_AGENT_LIMIT_RISK_CAP = "agent_limit_risk_cap"
CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED = "agent_limit_daily_loss_required"
CODE_AGENT_LIMIT_MAX_ARMS = "agent_limit_max_arms"
CODE_AGENT_CHANGE_NOT_ALLOWED = "agent_change_not_allowed"
# After a successful arm: the arm could not be verified or counted (it is
# disarmed), or it could not be disarmed (it may still be running).
CODE_AGENT_ARM_UNVERIFIED = "agent_arm_unverified"
CODE_AGENT_ARM_CLEANUP_FAILED = "agent_arm_cleanup_failed"

TOOL_PAPER_ARM = "paper_arm"
# The paper change tools M38.6 registers, gated by the per-actor allow flags.
PAUSE_RESUME_TOOLS: tuple[str, ...] = ("pause_entries", "resume_entries")
DISARM_TOOLS: tuple[str, ...] = ("disarm",)
CHANGE_TOOLS: tuple[str, ...] = (*PAUSE_RESUME_TOOLS, *DISARM_TOOLS)
LIMITED_TOOLS: tuple[str, ...] = (TOOL_PAPER_ARM, *CHANGE_TOOLS)

Refusal = tuple[str, str]


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
    agents = _agent_limits(home)
    if agents is None:
        refusal: Refusal | None = (user_policy.CODE_POLICY_INVALID, user_policy.MESSAGE_INVALID)
    elif tool == TOOL_PAPER_ARM:
        refusal = _arm_values_refusal(agents, resolved) or _running_arms_refusal(home, agents)
    else:
        refusal = _change_refusal(agents, actor, tool)
    if refusal is None:
        return None
    code, message = refusal
    return refuse(home, actor, tool, code, message, clock=clock)


def settle_agent_arm(home: Path, actor: Actor, armed: ArmedPack, *, revision_id: str | None) -> Refusal | None:
    """Check a new agent arm as stored, then count and record it.

    ``armed`` is the stored record the arm wrote; it is the authority, so a
    pack that changed between the pre-check and the arm is still caught. The
    running agent arms are re-counted and the arm recorded under the registry
    lock. Returns ``None`` when the arm is recorded, else the ``(code,
    message)`` refusal; the caller disarms and journals it. Raises when the
    registry cannot be read or written.
    """
    agents = _agent_limits(home)
    if agents is None:
        return user_policy.CODE_POLICY_INVALID, user_policy.MESSAGE_INVALID
    if armed.mode != "paper":
        return CODE_AGENT_ARM_UNVERIFIED, "the stored arm is not a paper arm"
    stored = {
        "venue": armed.venue,
        "pair": armed.pair,
        "paper_balance": armed.starting_cash,
        "risk_cap_pct": armed.cap,
        "daily_loss_limit_pct": armed.daily_loss_limit_pct,
    }
    refusal = _arm_values_refusal(agents, stored)
    if refusal is not None:
        return refusal
    claimed = arm_registry.claim_agent_arm(
        home,
        venue=armed.venue,
        pair=armed.pair,
        actor=actor,
        armed_at_ts=armed.armed_at_ts,
        revision_id=revision_id,
        max_agent_arms=agents.max_agent_arms,
    )
    if not claimed:
        return CODE_AGENT_LIMIT_MAX_ARMS, f"agents already run at most {agents.max_agent_arms} armed strategies"
    return None


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


def _agent_limits(home: Path) -> AgentLimits | None:
    """The operator's agent limits; ``None`` when the policy file is malformed."""
    probe = user_policy.check(home, user_policy.ACTION_PAPER_ARM)
    if probe.code == user_policy.CODE_POLICY_INVALID or probe.policy is None:
        return None
    return probe.policy.agents


def _at_most(value: object, limit: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value <= limit


def _is_loss_limit(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and Decimal(0) < value <= Decimal(100)


def _arm_values_refusal(agents: AgentLimits, values: Mapping[str, object]) -> Refusal | None:
    """Venue, pair, balance, risk cap and daily loss limit, in that order."""
    if agents.allowed_venues is not None and values.get("venue") not in agents.allowed_venues:
        return CODE_AGENT_LIMIT_VENUE, "the operator's agent limits do not allow this venue"
    pair = values.get("pair")
    if agents.allowed_pairs is not None and (not pair or pair not in agents.allowed_pairs):
        return CODE_AGENT_LIMIT_PAIR, "the operator's agent limits do not allow this pair"
    if not _at_most(values.get("paper_balance"), agents.max_paper_balance):
        return (
            CODE_AGENT_LIMIT_BALANCE,
            f"agent paper arms need a paper balance of at most {plain_decimal(agents.max_paper_balance)}",
        )
    if not _at_most(values.get("risk_cap_pct"), agents.max_risk_cap_pct):
        return (
            CODE_AGENT_LIMIT_RISK_CAP,
            (
                f"agent-armed packs may risk at most {plain_decimal(agents.max_risk_cap_pct)}% of starting cash "
                "per entry (the pack's risk.max_account_pct)"
            ),
        )
    if agents.require_daily_loss_limit and not _is_loss_limit(values.get("daily_loss_limit_pct")):
        return (
            CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED,
            "agent paper arms need a daily loss limit (daily_loss_limit_pct, a percent of the paper balance)",
        )
    return None


def _running_arms_refusal(home: Path, agents: AgentLimits) -> Refusal | None:
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


def _change_refusal(agents: AgentLimits, actor: Actor, tool: str) -> Refusal | None:
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
    "CODE_AGENT_ARM_CLEANUP_FAILED",
    "CODE_AGENT_ARM_UNVERIFIED",
    "CODE_AGENT_CHANGE_NOT_ALLOWED",
    "CODE_AGENT_LIMIT_BALANCE",
    "CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED",
    "CODE_AGENT_LIMIT_MAX_ARMS",
    "CODE_AGENT_LIMIT_PAIR",
    "CODE_AGENT_LIMIT_RISK_CAP",
    "CODE_AGENT_LIMIT_VENUE",
    "DISARM_TOOLS",
    "LIMITED_TOOLS",
    "PAUSE_RESUME_TOOLS",
    "TOOL_PAPER_ARM",
    "Refusal",
    "check_agent_call",
    "refuse",
    "settle_agent_arm",
]
