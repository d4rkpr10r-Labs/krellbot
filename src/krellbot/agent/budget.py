"""Token budget check against the operator's agent limits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from krellbot.agent.conversation_store import ConversationStore
from krellbot.application.user_policy import AgentLimits


@dataclass(frozen=True)
class BudgetState:
    conversation_used: int
    conversation_limit: int
    month_used: int
    month_limit: int
    exhausted: bool
    reason: Literal["conversation", "month"] | None


def check(store: ConversationStore, limits: AgentLimits, *, now: int) -> BudgetState:
    """Exhausted once used >= limit, so a limit of 0 is always exhausted.

    The conversation limit is reported first when both are reached.
    """
    conversation_used = store.conversation_tokens()
    month_used = store.month_tokens(now=now)
    conversation_limit = limits.token_budget_per_conversation
    month_limit = limits.token_budget_per_month
    reason: Literal["conversation", "month"] | None = None
    if conversation_used >= conversation_limit:
        reason = "conversation"
    elif month_used >= month_limit:
        reason = "month"
    return BudgetState(
        conversation_used=conversation_used,
        conversation_limit=conversation_limit,
        month_used=month_used,
        month_limit=month_limit,
        exhausted=reason is not None,
        reason=reason,
    )
