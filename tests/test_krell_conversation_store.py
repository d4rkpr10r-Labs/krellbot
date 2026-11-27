from __future__ import annotations

import os
import stat
import sys
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from krellbot.agent import budget
from krellbot.agent.conversation_store import ConversationStore, Profile
from krellbot.application.user_policy import LOCKED_AGENT_LIMITS, AgentLimits


def _limits(conversation: int = 100, month: int = 1000) -> AgentLimits:
    return replace(
        LOCKED_AGENT_LIMITS,
        token_budget_per_conversation=conversation,
        token_budget_per_month=month,
    )


def _ts(y: int, m: int, d: int, h: int = 12) -> int:
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


def test_history_round_trips_in_order_and_clear_empties_it(home):
    ticks = iter(range(1000, 2000))
    store = ConversationStore(home, clock=lambda: next(ticks))
    first = store.append("user", {"text": "hi"})
    second = store.append("assistant", {"text": "hello", "n": 2})
    third = store.append("notice", {"kind": "budget"})
    history = store.history()
    assert [m.id for m in history] == [first, second, third]
    assert [m.role for m in history] == ["user", "assistant", "notice"]
    assert history[1].payload == {"text": "hello", "n": 2}
    assert [m.ts for m in history] == [1000, 1001, 1002]
    assert store.profile() == Profile(mode=None, answers={})
    store.set_profile(mode="advanced")
    store.set_profile(answers={"goal": "learn"})
    assert store.profile() == Profile(mode="advanced", answers={"goal": "learn"})
    store.clear()
    assert store.history() == []
    assert store.profile() == Profile(mode=None, answers={})


def test_append_refuses_non_dict_payloads_and_unknown_roles(home):
    store = ConversationStore(home)
    with pytest.raises(ValueError):
        store.append("user", ["not", "a", "dict"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.append("system", {"text": "x"})  # type: ignore[arg-type]
    assert store.history() == []


def test_clearing_does_not_reset_the_monthly_token_count(home):
    store = ConversationStore(home)
    now = _ts(2026, 11, 20)
    store.record_usage(10, 5, ts=now)
    store.record_usage(20, 5, ts=now + 60)
    assert store.conversation_tokens() == 40
    assert store.month_tokens(now=now) == 40
    store.clear()
    assert store.month_tokens(now=now) == 40
    assert store.conversation_tokens() == 0
    store.record_usage(1, 1, ts=now + 120)
    assert store.conversation_tokens() == 2
    assert store.month_tokens(now=now) == 42


def test_budget_exhausts_at_the_conversation_limit_and_names_the_reason(home):
    store = ConversationStore(home)
    now = _ts(2026, 11, 20)
    store.record_usage(60, 30, ts=now)
    state = budget.check(store, _limits(conversation=100), now=now)
    assert (state.conversation_used, state.conversation_limit) == (90, 100)
    assert state.exhausted is False and state.reason is None
    store.record_usage(5, 5, ts=now)
    state = budget.check(store, _limits(conversation=100), now=now)
    assert state.exhausted is True
    assert state.reason == "conversation"
    assert (state.month_used, state.month_limit) == (100, 1000)


def test_budget_names_the_month_and_locked_limits_are_exhausted(home):
    store = ConversationStore(home)
    now = _ts(2026, 11, 20)
    state = budget.check(store, LOCKED_AGENT_LIMITS, now=now)
    assert state.exhausted is True
    store.record_usage(500, 500, ts=now)
    state = budget.check(store, _limits(conversation=5000, month=1000), now=now)
    assert state.exhausted is True and state.reason == "month"


def test_month_rollover_resets_month_usage(home):
    store = ConversationStore(home)
    store.record_usage(100, 0, ts=_ts(2026, 11, 30, 23))
    store.record_usage(7, 0, ts=_ts(2026, 12, 1, 0))
    assert store.month_tokens(now=_ts(2026, 11, 30, 23)) == 100
    assert store.month_tokens(now=_ts(2026, 12, 15)) == 7
    assert store.month_tokens(now=_ts(2027, 1, 1)) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_store_file_is_private_to_the_user(home):
    old = os.umask(0o022)
    try:
        store = ConversationStore(home)
        store.append("user", {"text": "x"})
        store.record_usage(1, 1, ts=1)
    finally:
        os.umask(old)
    assert (home / "krell.sqlite").exists()
    for name in ("krell.sqlite", "krell.sqlite-wal", "krell.sqlite-shm"):
        path = home / name
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
