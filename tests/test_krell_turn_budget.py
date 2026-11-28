"""Krell's per-turn tool budget: a turn ends with her answer, not a hard cut.

A turn allows up to 24 tool calls. A call whose tool and arguments match one
already refused in the same turn is not sent again: Krell gets a refusal
saying it already failed, and the call is journaled with no params. When the
budget runs out, one last provider call with no tools lets Krell write her
own summary, and a "Keep going" quick reply continues. If the token budget
is spent, or that summary call fails or still asks for tools, the turn ends
with the ``agent_tool_cap_reached`` error, as before.

Fakes only: ``FakeKeyring`` and a scripted ``FakeAiTransport`` model (no
socket).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from krellbot.agent import krell_loop
from krellbot.agent.confirm import ConfirmLedger
from krellbot.agent.conversation_store import ConversationStore
from krellbot.agent.tools_read import CODE_TOUR_STEP_NOT_FOUND
from krellbot.ai.connections import ConnectionStore
from krellbot.ai.http import TransportError
from tests.test_krell_loop_api import (
    Client,
    ScriptedModel,
    _agent_rows,
    _arm_params,
    _connect,
    _of,
    _system,
    _text,
    _types,
    _write_policy,
    reply_text,
    reply_tools,
)

pytestmark = pytest.mark.usefixtures("fresh_keyring")

CODE_REPEAT = "agent_repeat_refused"
CODE_CAP = krell_loop.CODE_TOOL_CAP
KEEP_GOING = {"type": "quick_replies", "quick_replies": ["Keep going"]}
MISSING_STEP = {"step_id": "no-such-step"}


def _loop(home: Path, model: ScriptedModel, *, cap: int) -> krell_loop.KrellLoop:
    """A loop with a small turn budget, on its own core and the core's ledger."""
    ledger = ConfirmLedger(clock=time.time)
    core = krell_loop.build_krell_core(home, clock=time.time, ledger=ledger)
    return krell_loop.KrellLoop(
        home,
        core=core,
        store=ConversationStore(home),
        connections=ConnectionStore(home, allow_injected_fake_backend=True),
        clock=time.time,
        ledger=ledger,
        transport=model,
        max_tool_calls_per_turn=cap,
    )


def _send(home: Path, model: ScriptedModel, text: str, *, cap: int) -> list[dict]:
    events = list(_loop(home, model, cap=cap).send(text))
    assert events[-1] == {"type": "done"}
    return events


def _codes(events: list[dict]) -> list[str | None]:
    return [event["result"].get("code") for event in _of(events, "tool_result")]


def _rows(home: Path) -> list[tuple[str, str, str]]:
    return [(r["tool"], r["outcome"], r["code"]) for r in _agent_rows(home)]


def _answered_in_order(body: dict) -> bool:
    """Every assistant tool call in a request is answered by a tool message, in order."""
    messages = body["messages"]
    called = [c["id"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    return called == answered


def _ends_with_the_cap_error(events: list[dict]) -> None:
    [error] = _of(events, "error")
    assert (error["code"], error["message"]) == (CODE_CAP, krell_loop.MESSAGES[CODE_CAP])
    assert _types(events)[-2:] == ["error", "done"]
    assert _of(events, "quick_replies") == []


# ---- the budget --------------------------------------------------------------------


def test_the_turn_budget_is_24_tool_calls_and_the_cap_message_names_it(home):
    assert krell_loop.MAX_TOOL_CALLS_PER_TURN == 24
    assert "24 tool calls" in krell_loop.MESSAGES[CODE_CAP]
    model = ScriptedModel()
    assert _loop(home, model, cap=24) is not None
    for cap in (0, 25):
        with pytest.raises(ValueError, match="1-24"):
            _loop(home, model, cap=cap)


# ---- the repeat guard ----------------------------------------------------------------


def test_an_identical_call_refused_earlier_in_the_turn_is_not_sent_again(home):
    _connect(home)
    model = ScriptedModel(
        [
            reply_tools(("tour_step", MISSING_STEP)),
            # The same call twice more, in one answer: neither is sent.
            reply_tools(("tour_step", MISSING_STEP), ("tour_step", dict(MISSING_STEP))),
            # Krell's own tools too, and the arguments match whatever their key order.
            reply_tools(("set_profile", {"mode": "expert", "goals": "learn"})),
            reply_tools(("set_profile", {"goals": "learn", "mode": "expert"})),
            # The same tool with other arguments runs.
            reply_tools(("tour_step", {"step_id": "another-missing-step"}), ("tour_step", {})),
            reply_text("Those steps do not exist, so here is the list instead."),
        ]
    )
    client = Client(home, model)

    events = client.stream("/api/v1/krell/messages", {"text": "Show me the hidden step."})

    assert krell_loop.CODE_REPEAT_REFUSED == CODE_REPEAT
    assert _codes(events) == [
        CODE_TOUR_STEP_NOT_FOUND,
        CODE_REPEAT,
        CODE_REPEAT,
        krell_loop.CODE_PROFILE_INVALID,
        CODE_REPEAT,
        CODE_TOUR_STEP_NOT_FOUND,
        None,
    ]
    results = [e["result"] for e in _of(events, "tool_result")]
    first, repeat = results[0], results[1]
    assert (repeat["ok"], repeat["effect"], repeat["tool"]) == (False, "refused", "tour_step")
    # The refusal says the identical call already failed this turn, and quotes why.
    assert "already refused" in repeat["message"]
    assert first["message"] in repeat["message"]
    assert krell_loop.MESSAGES[krell_loop.CODE_PROFILE_INVALID] in results[4]["message"]
    # The repeats never reached the core: each is journaled by the loop as refused, with no params.
    assert _rows(home) == [
        ("tour_step", "refused", CODE_TOUR_STEP_NOT_FOUND),
        ("tour_step", "refused", CODE_REPEAT),
        ("tour_step", "refused", CODE_REPEAT),
        ("set_profile", "refused", CODE_REPEAT),
        ("tour_step", "refused", CODE_TOUR_STEP_NOT_FOUND),
        ("tour_step", "ok", "ok"),
    ]
    rows = _agent_rows(home)
    assert [rows[i]["params"] for i in (1, 2, 3)] == [{}, {}, {}]
    assert rows[0]["params"] == MISSING_STEP
    assert rows[4]["params"] == {"step_id": "another-missing-step"}
    # Krell hears the repeat refusal as data, and every call is answered.
    assert model.calls == 6
    assert _text(events) == "Those steps do not exist, so here is the list instead."
    told = [m["content"] for m in model.bodies[2]["messages"] if m["role"] == "tool"]
    assert CODE_REPEAT in told[-1]
    assert all(_answered_in_order(body) for body in model.bodies)


def test_a_call_refused_in_an_earlier_turn_is_sent_again_in_a_new_turn(home):
    _connect(home)
    model = ScriptedModel(
        [
            reply_tools(("tour_step", MISSING_STEP)),
            reply_text("That step does not exist."),
            reply_tools(("tour_step", MISSING_STEP)),
            reply_text("Still missing."),
        ]
    )
    client = Client(home, model)

    client.stream("/api/v1/krell/messages", {"text": "Show me the hidden step."})
    events = client.stream("/api/v1/krell/messages", {"text": "Try that step again."})

    assert _codes(events) == [CODE_TOUR_STEP_NOT_FOUND]
    assert _rows(home) == [("tour_step", "refused", CODE_TOUR_STEP_NOT_FOUND)] * 2


def test_a_repeat_counts_toward_the_turn_budget(home):
    _connect(home)
    model = ScriptedModel(
        [
            reply_tools(("tour_step", MISSING_STEP)),
            reply_tools(("tour_step", MISSING_STEP)),
            reply_tools(("tour_step", {})),
            reply_text("I ran out of steps for this turn."),
        ]
    )

    events = _send(home, model, "Show me the hidden step.", cap=2)

    assert _codes(events) == [CODE_TOUR_STEP_NOT_FOUND, CODE_REPEAT, CODE_CAP]
    assert _rows(home) == [
        ("tour_step", "refused", CODE_TOUR_STEP_NOT_FOUND),
        ("tour_step", "refused", CODE_REPEAT),
        ("tour_step", "refused", CODE_CAP),
    ]
    assert model.calls == 4
    assert _of(events, "quick_replies") == [KEEP_GOING]


# ---- the summary at the cap ------------------------------------------------------------


def test_the_summary_request_carries_no_tools_and_its_note_is_for_that_call_only(home):
    _connect(home)
    summary = "I read the tour and stopped before the highlight. Keep going?"
    model = ScriptedModel(
        [
            reply_tools(("tour_step", {})),
            reply_tools(("tour_step", {"step_id": "welcome"}), ("highlight", {"test_id": "nav-strategies"})),
            reply_text(summary),
            reply_text("Back to it."),
        ]
    )

    events = _send(home, model, "Tour me, and point at Strategies.", cap=1)

    # Both calls in the over-budget answer are refused and answered; the highlight never ran.
    assert _codes(events) == [None, CODE_CAP, CODE_CAP]
    assert _of(events, "highlight") == []
    assert _rows(home) == [
        ("tour_step", "ok", "ok"),
        ("tour_step", "refused", CODE_CAP),
        ("highlight", "refused", CODE_CAP),
    ]
    assert all(r["params"] == {} for r in _agent_rows(home)[1:])
    assert model.calls == 3
    asked = model.bodies[2]
    assert "tools" not in asked and "tool_choice" not in asked
    assert "tools" in model.bodies[0] and "tools" in model.bodies[1]
    assert krell_loop.NOTE_TOOL_BUDGET_USED in _system(asked)
    assert krell_loop.NOTE_TOOL_BUDGET_USED not in _system(model.bodies[1])
    assert _answered_in_order(asked)
    assert _text(events) == summary
    assert _of(events, "error") == []
    assert _types(events)[-3:] == ["message_done", "quick_replies", "done"]
    assert _of(events, "quick_replies") == [KEEP_GOING]

    # The note is not stored: the next turn's request does not carry it, and has its tools back.
    _send(home, model, "Keep going", cap=1)
    nxt = model.bodies[3]
    assert krell_loop.NOTE_TOOL_BUDGET_USED not in json.dumps(nxt)
    assert "tools" in nxt
    assert nxt["messages"][-1] == {"role": "user", "content": "Keep going"}
    assert {"role": "assistant", "content": summary} in nxt["messages"]


def test_a_confirm_card_at_the_budget_ends_the_turn_without_a_summary(home):
    _connect(home)
    params = _arm_params(home)
    model = ScriptedModel(
        [
            reply_tools(("tour_step", {})),
            reply_tools(("paper_arm", params), ("tour_step", {})),
            reply_text("never asked for"),
        ]
    )

    events = _send(home, model, "Tour me, then arm it on paper.", cap=2)

    # The card waits for the person, whose choice resumes the turn: no summary call is made.
    assert model.calls == 2
    [card] = _of(events, "confirm_request")
    assert card["confirm"]["tool"] == "paper_arm"
    assert _codes(events) == [None, "agent_confirm_required", CODE_CAP]
    _ends_with_the_cap_error(events)


def test_a_spent_token_budget_skips_the_summary_and_ends_with_the_cap_error(home):
    _connect(home)
    _write_policy(home, token_budget_per_conversation=100)
    model = ScriptedModel(
        [
            reply_tools(("tour_step", {}), usage=(40, 10)),
            reply_tools(("tour_step", {}), usage=(40, 10)),
            reply_text("never asked for"),
        ]
    )

    events = _send(home, model, "Tour me.", cap=1)

    # The budget was spent by the over-budget answer, so no summary call is made.
    assert model.calls == 2
    assert _codes(events) == [None, CODE_CAP]
    _ends_with_the_cap_error(events)
    assert ConversationStore(home).conversation_tokens() == 100


def test_a_summary_that_still_asks_for_tools_runs_none_and_ends_with_the_cap_error(home):
    _connect(home)
    model = ScriptedModel(
        [
            reply_tools(("tour_step", {})),
            reply_tools(("tour_step", {"step_id": "welcome"})),
            reply_tools(("tour_step", {"step_id": "strategies"})),
            reply_text("Back to it."),
        ]
    )

    events = _send(home, model, "Tour me.", cap=1)

    assert model.calls == 3
    _ends_with_the_cap_error(events)
    # The summary's call ran nothing: it is refused, answered and journaled like an over-budget call.
    assert _codes(events) == [None, CODE_CAP, CODE_CAP]
    assert _rows(home) == [
        ("tour_step", "ok", "ok"),
        ("tour_step", "refused", CODE_CAP),
        ("tour_step", "refused", CODE_CAP),
    ]
    assert all(r["params"] == {} for r in _agent_rows(home)[1:])
    # History stays well formed for the next turn.
    _send(home, model, "Keep going", cap=1)
    assert _answered_in_order(model.bodies[3])
    called = [c for m in model.bodies[3]["messages"] if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    assert len(called) == 3


def test_a_failed_summary_call_ends_with_the_cap_error_and_is_not_retried(home):
    _connect(home)
    model = ScriptedModel(
        [
            reply_tools(("tour_step", {})),
            reply_tools(("tour_step", {})),
            TransportError(503, "upstream unavailable"),
        ]
    )

    events = _send(home, model, "Tour me.", cap=1)

    assert model.calls == 3
    _ends_with_the_cap_error(events)
    stored = ConversationStore(home).history()
    assert (stored[-1].role, stored[-1].payload["kind"], stored[-1].payload["code"]) == ("notice", "error", CODE_CAP)


def test_an_empty_summary_ends_with_the_cap_error(home):
    _connect(home)
    model = ScriptedModel([reply_tools(("tour_step", {})), reply_tools(("tour_step", {})), reply_text("")])

    events = _send(home, model, "Tour me.", cap=1)

    assert model.calls == 3
    _ends_with_the_cap_error(events)
