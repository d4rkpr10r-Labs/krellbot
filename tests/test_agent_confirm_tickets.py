"""Confirm tickets: Krell asks before change or heavy work, and runs it once.

Krell is never an autopilot. A Krell call whose tool changes state, or whose
work is heavy (the tool's cost class or its estimate), is refused with
``needs_confirm`` and a single-use ticket bound to the exact tool and params.
The same call with that ticket runs once, after the policy step has checked
the limits again. Unknown, expired, used, cleared and mismatched tickets
refuse with typed codes and change nothing. External MCP actors never get
tickets: their own client asks the human before each call. A core with no
ledger fails closed for Krell.

Every candle below is synthetic local data, never market data, and every
number is a backtest fixture, never market performance.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot import config as kb_config
from krellbot.agent import arm_registry
from krellbot.agent.actors import KRELL, Actor
from krellbot.agent.confirm import (
    CODE_CONFIRM_MISMATCH,
    CODE_CONFIRM_REQUIRED,
    CODE_CONFIRM_UNKNOWN,
    MAX_LIVE_TICKETS_PER_ACTOR,
    TICKET_TTL_S,
    ConfirmLedger,
    ConfirmParamsError,
)
from krellbot.agent.core import CODE_INVALID_REQUEST, CODE_TOOL_ERROR, AgentToolCore, ToolSpec
from krellbot.agent.tools_research import register_research_tools
from krellbot.api.jobs import canonical_receipt_bytes
from krellbot.application import user_policy
from krellbot.application.strategy import StrategyDraftService
from krellbot.journal import read_records_lenient
from krellbot.mcp import McpTools
from krellbot.mcp.errors import CODE_AGENT_LIMIT_BALANCE
from tests.test_research_run_identity import HOUR_MS, _write_csv, _write_pack
from tests.test_research_run_identity import _pack as _research_pack

pytestmark = pytest.mark.usefixtures("fresh_keyring")

CURSOR = Actor.mcp("cursor")
START = 1_700_000_000.0
CONFIRM_KEYS = {"ticket_id", "tool", "params", "estimate", "limits", "expires_at", "pack_sha256"}
COMPARE_PARAMS = {"revision_ids": ["0" * 64, "1" * 64]}


class _Clock:
    """A fake clock the test moves by hand."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _ledger_clock_never_read() -> float:
    raise AssertionError("the core passes now= from its own clock; the ledger's clock is never read")


def _krell_core(home: Path) -> tuple[_Clock, ConfirmLedger, AgentToolCore]:
    """A core on a fake clock. Its ledger's own clock must never be read: expiry follows the core's clock."""
    clock = _Clock()
    ledger = ConfirmLedger(clock=_ledger_clock_never_read)
    core = AgentToolCore(home, clock=clock, confirm=ledger)
    register_research_tools(core)
    return clock, ledger, core


def _arm_pack(home: Path, *, pair: str = "SUIUSD") -> Path:
    work = home / "agent-work"
    work.mkdir(parents=True, exist_ok=True)
    pack_id = f"confirm-{pair.lower()}"
    body = {
        "schema_version": 1,
        "id": pack_id,
        "version": "1.0.0",
        "label": "Confirm tickets",
        "author": "krellbot confirm ticket tests",
        "timeframe": "1h",
        "indicators": {"sma20": {"fn": "sma", "src": "close", "len": 20}},
        "entry": ["close", ">", "sma20"],
        "exit": ["close", "<", "sma20"],
        "risk": {"max_account_pct": 2, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": "kraken", "pair": pair}],
    }
    target = work / f"{pack_id}.json"
    target.write_text(json.dumps(body), encoding="utf-8")
    return target


def _arm_params(home: Path, *, pair: str = "SUIUSD", balance: object = "1000") -> dict:
    return {
        "pack_path": str(_arm_pack(home, pair=pair)),
        "venue": "kraken",
        "mode": "paper",
        "paper_balance": balance,
        "daily_loss_limit_pct": "5",
    }


def _armed(home: Path) -> list[kb_config.ArmedPack]:
    return kb_config.load_config(home).armed


def _agent_rows(home: Path) -> list[dict]:
    return [r["detail"] for r in read_records_lenient(home) if r.get("kind") == "agent_action"]


def _journal_text(home: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted((home / "journal").glob("*.jsonl")))


def _ticket(result: dict) -> str:
    assert result["effect"] == "needs_confirm", result
    return result["confirm"]["ticket_id"]


def _assert_refused(result: dict, code: str, tool: str = "paper_arm") -> None:
    assert result["ok"] is False, result
    assert result["effect"] == "refused", result
    assert result["code"] == code, result
    assert result["tool"] == tool
    assert "confirm" not in result


def _write_policy(home: Path, **agents: object) -> None:
    section = user_policy.DEFAULT_AGENT_LIMITS.to_dict()
    section.update(agents)
    payload = {
        "schema_version": "2",
        "allow_paper_arm": True,
        "allow_live": True,
        "allow_key_export": False,
        "agents": section,
    }
    user_policy.policy_path(home).write_text(json.dumps(payload), encoding="utf-8")


def _change_stand_in(core: AgentToolCore, name: str, ran: list[tuple[str, dict]]) -> None:
    def handler(ctx, params):
        ran.append((name, dict(params)))
        return {"schema_version": "1", "ok": True, "effect": "changed", "tool": name}

    core.register(
        ToolSpec(
            name=name,
            effect="change",
            cost="light",
            input_schema={"type": "object", "properties": {}},
            labels=("paper",),
            description="test stand-in for an M38.6 change tool",
            handler=handler,
        )
    )


# ---- needs_confirm ---------------------------------------------------------


def test_krell_paper_arm_without_ticket_returns_needs_confirm_and_changes_nothing(home: Path):
    clock, _ledger, core = _krell_core(home)
    params = _arm_params(home)

    result = core.call(KRELL, "paper_arm", params)

    assert result["ok"] is False
    assert result["effect"] == "needs_confirm"
    assert result["code"] == CODE_CONFIRM_REQUIRED
    assert result["tool"] == "paper_arm"
    assert result["actor"] == "krell"
    assert result["labels"] == ["paper"]
    confirm = result["confirm"]
    assert set(confirm) == CONFIRM_KEYS
    assert confirm["tool"] == "paper_arm"
    # The exact params the agent sent: the balance is still the string it sent.
    assert confirm["params"] == params
    assert confirm["params"]["paper_balance"] == "1000"
    assert confirm["estimate"] == {"cost": "heavy", "bars": None, "packs": 1, "seconds": None, "result_tokens": None}
    assert confirm["limits"] == {
        "max_paper_balance": "10000",
        "max_risk_cap_pct": "2",
        "require_daily_loss_limit": True,
        "max_agent_arms": 3,
    }
    assert confirm["expires_at"] == START + TICKET_TTL_S == START + 600
    assert isinstance(confirm["ticket_id"], str) and len(confirm["ticket_id"]) >= 22
    # The card names the exact pack bytes that were checked (the snapshot that would be armed).
    pack_sha256 = hashlib.sha256(Path(params["pack_path"]).read_bytes()).hexdigest()
    assert confirm["pack_sha256"] == pack_sha256
    assert (arm_registry.snapshot_dir(home) / f"{pack_sha256}.json").is_file()
    json.dumps(result)  # the confirm card payload is plain JSON

    # Nothing was armed or counted.
    assert _armed(home) == []
    assert arm_registry.running_agent_arms(home) == []

    # Audited as needs_confirm. The issued ticket's id is never journaled.
    [row] = _agent_rows(home)
    assert (row["actor"], row["tool"], row["outcome"], row["code"]) == (
        "krell",
        "paper_arm",
        "needs_confirm",
        CODE_CONFIRM_REQUIRED,
    )
    assert row["confirm_ticket"] is None
    assert confirm["ticket_id"] not in _journal_text(home)
    assert clock.now == START


def test_ticket_runs_once_and_second_use_is_unknown(home: Path):
    _clock, _ledger, core = _krell_core(home)
    params = _arm_params(home)
    ticket = _ticket(core.call(KRELL, "paper_arm", params))

    armed = core.call(KRELL, "paper_arm", params, confirm_token=ticket)
    assert armed["ok"] is True, armed
    assert armed["tool"] == "paper_arm"
    assert [(a.venue, a.pair, a.mode) for a in _armed(home)] == [("kraken", "SUIUSD", "paper")]
    assert [(r["pair"], r["actor"]) for r in arm_registry.running_agent_arms(home)] == [("SUIUSD", "krell")]

    again = core.call(KRELL, "paper_arm", params, confirm_token=ticket)
    _assert_refused(again, CODE_CONFIRM_UNKNOWN)
    assert len(_armed(home)) == 1

    rows = _agent_rows(home)
    assert [(r["outcome"], r["code"], r["confirm_ticket"]) for r in rows] == [
        ("needs_confirm", CODE_CONFIRM_REQUIRED, None),
        ("ok", "armed", ticket),
        ("refused", CODE_CONFIRM_UNKNOWN, None),
    ]


def test_ticket_bound_to_params_refuses_changed_balance(home: Path):
    _clock, _ledger, core = _krell_core(home)
    params = _arm_params(home)
    ticket = _ticket(core.call(KRELL, "paper_arm", params))

    changed = core.call(KRELL, "paper_arm", {**params, "paper_balance": "2000"}, confirm_token=ticket)
    _assert_refused(changed, CODE_CONFIRM_MISMATCH)
    assert _armed(home) == []

    # A presented ticket is spent, even on a mismatch: the human confirms again.
    spent = core.call(KRELL, "paper_arm", params, confirm_token=ticket)
    _assert_refused(spent, CODE_CONFIRM_UNKNOWN)
    assert _armed(home) == []

    # The same amount in another JSON type is other params, not the same ones.
    ticket = _ticket(core.call(KRELL, "paper_arm", params))
    retyped = core.call(KRELL, "paper_arm", {**params, "paper_balance": 1000}, confirm_token=ticket)
    _assert_refused(retyped, CODE_CONFIRM_MISMATCH)
    # An extra param is other params too.
    ticket = _ticket(core.call(KRELL, "paper_arm", params))
    extra = core.call(KRELL, "paper_arm", {**params, "correlation_id": "x"}, confirm_token=ticket)
    _assert_refused(extra, CODE_CONFIRM_MISMATCH)
    assert _armed(home) == []


def test_ticket_expires_after_600_seconds(home: Path):
    clock, _ledger, core = _krell_core(home)
    params = _arm_params(home)

    ticket = _ticket(core.call(KRELL, "paper_arm", params))
    clock.now += 601
    expired = core.call(KRELL, "paper_arm", params, confirm_token=ticket)
    _assert_refused(expired, CODE_CONFIRM_UNKNOWN)
    assert _armed(home) == []

    # Inside the 600 seconds the ticket still works.
    ticket = _ticket(core.call(KRELL, "paper_arm", params))
    clock.now += 599
    assert core.call(KRELL, "paper_arm", params, confirm_token=ticket)["ok"] is True
    assert len(_armed(home)) == 1


def test_cleared_ledger_refuses_old_ticket(home: Path):
    clock, ledger, core = _krell_core(home)
    params = _arm_params(home)

    ticket = _ticket(core.call(KRELL, "paper_arm", params))
    ledger.clear()
    _assert_refused(core.call(KRELL, "paper_arm", params, confirm_token=ticket), CODE_CONFIRM_UNKNOWN)

    ticket = _ticket(core.call(KRELL, "paper_arm", params))
    ledger.discard(ticket)
    ledger.discard(ticket)  # discarding twice, or an unknown id, is a no-op
    _assert_refused(core.call(KRELL, "paper_arm", params, confirm_token=ticket), CODE_CONFIRM_UNKNOWN)

    # Tickets live in engine memory only: a new ledger (a restarted engine) knows none.
    ticket = _ticket(core.call(KRELL, "paper_arm", params))
    restarted = AgentToolCore(home, clock=clock, confirm=ConfirmLedger(clock=clock))
    _assert_refused(restarted.call(KRELL, "paper_arm", params, confirm_token=ticket), CODE_CONFIRM_UNKNOWN)

    for bad in ("", "not-a-ticket", "é" * 22, ticket + "x", ticket[:-1]):
        _assert_refused(core.call(KRELL, "paper_arm", params, confirm_token=bad), CODE_CONFIRM_UNKNOWN)
    assert _armed(home) == []


# ---- what needs a ticket ---------------------------------------------------


def test_light_reads_run_without_ticket_for_krell(home: Path):
    _clock, _ledger, core = _krell_core(home)
    csv_path = _write_csv(home / "cache", "kraken__SUIUSD__1h.csv")
    pack_path = _write_pack(home / "packs", "light.json", _research_pack())

    read = core.call(KRELL, "read", {})
    draft = core.call(KRELL, "draft", {"pack": _research_pack(strategy_id="synthetic-light")})
    revision_id = draft["result"]["revision_id"]
    validate = core.call(KRELL, "validate", {"revision_id": revision_id})
    # Six synthetic bars x one pack: a light backtest.
    backtest = core.call(KRELL, "backtest", {"pack_path": str(pack_path), "dataset_csv": str(csv_path)})

    for result in (read, draft, validate, backtest):
        assert result["ok"] is True, result
        assert "confirm" not in result
    assert [(r["tool"], r["outcome"], r["confirm_ticket"]) for r in _agent_rows(home)] == [
        ("read", "ok", None),
        ("draft", "ok", None),
        ("validate", "ok", None),
        ("backtest", "ok", None),
    ]


def test_heavy_research_needs_a_ticket_and_then_runs_once(home: Path):
    _clock, _ledger, core = _krell_core(home)
    csv_path = _write_csv(home / "cache", "kraken__SUIUSD__1h.csv")
    revisions = [
        StrategyDraftService(home=home).create(_research_pack(strategy_id="synthetic-heavy", sma_len=n))["revision_id"]
        for n in (2, 3)
    ]

    # A backtest of unknown size (no dataset named) is heavy by its estimate.
    unknown = core.call(KRELL, "backtest", {"revision_id": revisions[0]})
    assert unknown["code"] == CODE_CONFIRM_REQUIRED
    assert unknown["confirm"]["estimate"]["cost"] == "heavy"
    assert unknown["confirm"]["estimate"]["bars"] is None
    assert unknown["confirm"]["limits"] == {}

    # So is a backtest above 20000 bar x pack.
    big_csv = home / "cache" / "kraken__SUIUSD__1h-big.csv"
    with big_csv.open("w", encoding="utf-8") as handle:
        handle.write("ts_ms,open,high,low,close,volume\n")
        handle.writelines(f"{i * HOUR_MS},90,90,90,90,100\n" for i in range(20001))
    big = core.call(KRELL, "backtest", {"revision_id": revisions[0], "dataset_csv": str(big_csv)})
    assert big["confirm"]["estimate"]["bars"] == 20001
    assert big["confirm"]["estimate"]["cost"] == "heavy"

    # Compare is heavy by its cost class; with the ticket it runs once.
    params = {"revision_ids": revisions, "dataset_csv": str(csv_path)}
    asked = core.call(KRELL, "compare", params)
    assert asked["confirm"]["estimate"] == {
        "cost": "heavy",
        "bars": 6,
        "packs": 2,
        "seconds": None,
        "result_tokens": None,
    }
    assert asked["labels"] == ["simulated"]
    ticket = _ticket(asked)
    ran = core.call(KRELL, "compare", params, confirm_token=ticket)
    assert ran["ok"] is True, ran
    assert [row["revision_id"] for row in ran["rows"]] == revisions
    _assert_refused(core.call(KRELL, "compare", params, confirm_token=ticket), CODE_CONFIRM_UNKNOWN, "compare")
    assert [r["confirm_ticket"] for r in _agent_rows(home) if r["outcome"] == "ok"] == [ticket]

    # A tool the estimate does not size is held to its own heavy cost class.
    core.register(
        ToolSpec(
            name="heavy_probe",
            effect="research",
            cost="heavy",
            input_schema={"type": "object", "properties": {}},
            labels=("simulated",),
            description="test stand-in for a heavy research tool",
            handler=lambda _ctx, _params: {"schema_version": "1", "ok": True, "effect": "unchanged"},
        )
    )
    probe = core.call(KRELL, "heavy_probe", {})
    assert probe["code"] == CODE_CONFIRM_REQUIRED
    assert probe["confirm"]["estimate"]["cost"] == "light"


def test_krell_change_tools_need_a_ticket_and_a_ticket_confirms_only_its_own_tool(home: Path):
    _clock, _ledger, core = _krell_core(home)
    ran: list[tuple[str, dict]] = []
    for name in ("pause_entries", "resume_entries", "disarm"):
        _change_stand_in(core, name, ran)
    params = {"venue": "kraken", "pair": "SUIUSD"}

    pause = core.call(KRELL, "pause_entries", params)
    assert pause["code"] == CODE_CONFIRM_REQUIRED
    assert pause["confirm"]["estimate"]["cost"] == "light"
    assert pause["confirm"]["limits"] == {"krell_allow_pause_resume": True}
    assert pause["confirm"]["pack_sha256"] is None
    assert core.call(KRELL, "disarm", params)["confirm"]["limits"] == {"krell_allow_disarm": True}
    assert ran == []

    # A pause ticket cannot resume, and a paper-arm ticket cannot pause.
    ticket = _ticket(core.call(KRELL, "pause_entries", params))
    _assert_refused(
        core.call(KRELL, "resume_entries", params, confirm_token=ticket), CODE_CONFIRM_MISMATCH, "resume_entries"
    )
    arm_ticket = _ticket(core.call(KRELL, "paper_arm", _arm_params(home)))
    _assert_refused(
        core.call(KRELL, "pause_entries", params, confirm_token=arm_ticket), CODE_CONFIRM_MISMATCH, "pause_entries"
    )
    assert ran == []

    ticket = _ticket(core.call(KRELL, "pause_entries", params))
    assert core.call(KRELL, "pause_entries", params, confirm_token=ticket)["ok"] is True
    assert ran == [("pause_entries", params)]


def test_confirmed_arm_is_checked_against_the_limits_again(home: Path):
    _clock, _ledger, core = _krell_core(home)
    params = _arm_params(home, balance="5000")
    ticket = _ticket(core.call(KRELL, "paper_arm", params))

    # The operator lowers the balance limit before the human confirms.
    _write_policy(home, max_paper_balance="1000")
    refused = core.call(KRELL, "paper_arm", params, confirm_token=ticket)
    _assert_refused(refused, CODE_AGENT_LIMIT_BALANCE)
    assert _armed(home) == []
    assert arm_registry.running_agent_arms(home) == []


def test_a_limit_refusal_at_confirm_keeps_the_ticket_and_a_mismatch_spends_it(home: Path):
    _clock, _ledger, core = _krell_core(home)
    params = _arm_params(home, balance="5000")
    ticket = _ticket(core.call(KRELL, "paper_arm", params))

    # The limits refuse before the confirm step, so the ticket is not presented
    # and stays live: once the operator raises the limit again, the human's
    # confirm runs, checked against the limits again.
    _write_policy(home, max_paper_balance="1000")
    _assert_refused(core.call(KRELL, "paper_arm", params, confirm_token=ticket), CODE_AGENT_LIMIT_BALANCE)
    assert _armed(home) == []
    _write_policy(home, max_paper_balance="5000")
    assert core.call(KRELL, "paper_arm", params, confirm_token=ticket)["ok"] is True
    assert [a.pair for a in _armed(home)] == ["SUIUSD"]

    # A mismatch is presented at the confirm step, so it spends the ticket.
    other = _arm_params(home, pair="BTCUSD")
    ticket = _ticket(core.call(KRELL, "paper_arm", other))
    _assert_refused(
        core.call(KRELL, "paper_arm", {**other, "paper_balance": "999"}, confirm_token=ticket), CODE_CONFIRM_MISMATCH
    )
    _assert_refused(core.call(KRELL, "paper_arm", other, confirm_token=ticket), CODE_CONFIRM_UNKNOWN)
    assert [a.pair for a in _armed(home)] == ["SUIUSD"]


def test_paper_arm_ticket_is_bound_to_the_pack_bytes_not_the_path(home: Path):
    _clock, _ledger, core = _krell_core(home)
    params = _arm_params(home)
    pack = Path(params["pack_path"])
    original = pack.read_bytes()
    asked = core.call(KRELL, "paper_arm", params)
    assert asked["confirm"]["pack_sha256"] == hashlib.sha256(original).hexdigest()
    ticket = _ticket(asked)

    # Before the human confirms, the file at the same path becomes another
    # strategy that still passes every limit; the params still match.
    swapped_pack = json.loads(original)
    swapped_pack["risk"]["stop"]["pct"] = 3
    swapped_pack["exit"] = ["close", "<", 1]
    pack.write_text(json.dumps(swapped_pack), encoding="utf-8")
    swapped = core.call(KRELL, "paper_arm", params, confirm_token=ticket)
    _assert_refused(swapped, CODE_CONFIRM_MISMATCH)
    assert _armed(home) == []
    assert arm_registry.running_agent_arms(home) == []

    # The mismatch spent the ticket: restoring the confirmed bytes does not revive it.
    pack.write_bytes(original)
    _assert_refused(core.call(KRELL, "paper_arm", params, confirm_token=ticket), CODE_CONFIRM_UNKNOWN)
    assert _armed(home) == []


def test_each_actor_keeps_at_most_16_live_tickets(home: Path):
    assert MAX_LIVE_TICKETS_PER_ACTOR == 16
    ledger = ConfirmLedger(clock=_Clock())
    tickets = [ledger.issue(KRELL, "compare", {"n": n}) for n in range(MAX_LIVE_TICKETS_PER_ACTOR + 1)]
    # The 17th evicted the oldest; the other 16 are live.
    assert ledger.consume(tickets[0].id, "compare", {"n": 0}) == "unknown"
    for n, ticket in enumerate(tickets[1:], start=1):
        assert ledger.consume(ticket.id, "compare", {"n": n}) == "ok"

    # Repeated heavy requests through the core never hold more than 16 tickets.
    _clock, _ledger, core = _krell_core(home)
    asked = [_ticket(core.call(KRELL, "compare", COMPARE_PARAMS)) for _ in range(20)]
    for evicted in asked[:4]:
        _assert_refused(
            core.call(KRELL, "compare", COMPARE_PARAMS, confirm_token=evicted), CODE_CONFIRM_UNKNOWN, "compare"
        )
    # The oldest live one still runs (its handler then refuses the unknown revisions).
    live = core.call(KRELL, "compare", COMPARE_PARAMS, confirm_token=asked[4])
    assert live["code"] == CODE_INVALID_REQUEST, live


def test_ticket_expiry_follows_the_core_clock_not_the_ledgers(home: Path):
    # The ledger's own clock is never read when the caller passes now=.
    ledger = ConfirmLedger(clock=_ledger_clock_never_read)
    ticket = ledger.issue(KRELL, "compare", {}, now=100.0)
    assert ticket.expires_at == 100.0 + TICKET_TTL_S
    assert ledger.consume(ticket.id, "compare", {}, now=100.0 + TICKET_TTL_S - 1) == "ok"
    ticket = ledger.issue(KRELL, "compare", {}, now=100.0)
    assert ledger.consume(ticket.id, "compare", {}, now=100.0 + TICKET_TTL_S) == "unknown"

    # The core passes its clock: here the core's clock moves and the ledger's would raise.
    clock, _ledger, core = _krell_core(home)
    asked = core.call(KRELL, "compare", COMPARE_PARAMS)
    assert asked["confirm"]["expires_at"] == START + TICKET_TTL_S
    clock.now += TICKET_TTL_S
    _assert_refused(
        core.call(KRELL, "compare", COMPARE_PARAMS, confirm_token=_ticket(asked)), CODE_CONFIRM_UNKNOWN, "compare"
    )


def test_a_failing_policy_read_leaves_no_orphan_ticket(home: Path, monkeypatch: pytest.MonkeyPatch):
    _clock, ledger, core = _krell_core(home)
    issued = []
    real_issue = ledger.issue

    def recording_issue(*args, **kwargs):
        ticket = real_issue(*args, **kwargs)
        issued.append(ticket)
        return ticket

    def unreadable_policy(*_args, **_kwargs):
        raise OSError("user-policy.json is unreadable")

    monkeypatch.setattr(ledger, "issue", recording_issue)
    monkeypatch.setattr("krellbot.agent.core.confirm_limits", unreadable_policy)
    failed = core.call(KRELL, "compare", COMPARE_PARAMS)
    assert failed["ok"] is False
    assert failed["code"] == CODE_TOOL_ERROR
    assert "confirm" not in failed
    assert issued == []


# ---- who gets tickets ------------------------------------------------------


def test_mcp_actor_never_receives_a_ticket(home: Path):
    _write_policy(home, max_agent_arms=4)
    _clock, ledger, core = _krell_core(home)
    csv_path = _write_csv(home / "cache", "kraken__SUIUSD__1h.csv")
    revisions = [
        StrategyDraftService(home=home).create(_research_pack(strategy_id="synthetic-mcp", sma_len=n))["revision_id"]
        for n in (2, 3)
    ]

    # External agents run change and heavy calls without a Krellbot-side ticket:
    # their own client asks the human before each call.
    armed = core.call(CURSOR, "paper_arm", _arm_params(home, pair="BTCUSD"))
    assert armed["ok"] is True, armed
    compare = core.call(CURSOR, "compare", {"revision_ids": revisions, "dataset_csv": str(csv_path)})
    assert compare["ok"] is True, compare

    # A Krell ticket presented by an MCP actor is ignored, never consumed.
    params = _arm_params(home)
    ticket = _ticket(core.call(KRELL, "paper_arm", params))
    mcp_arm = core.call(CURSOR, "paper_arm", _arm_params(home, pair="ETHUSD"), confirm_token=ticket)
    assert mcp_arm["ok"] is True, mcp_arm
    assert core.call(KRELL, "paper_arm", params, confirm_token=ticket)["ok"] is True

    for row in _agent_rows(home):
        if row["actor"] == "mcp:cursor":
            assert (row["outcome"], row["confirm_ticket"]) == ("ok", None)

    # The ledger refuses to issue to an MCP actor at all.
    with pytest.raises(ValueError):
        ledger.issue(CURSOR, "paper_arm", _arm_params(home))

    # The MCP front door has no ledger and still runs paper arms directly.
    mcp = McpTools(home, actor=CURSOR).call("paper_arm", **_arm_params(home, pair="SOLUSD"))
    assert mcp["ok"] is True, mcp
    assert "confirm" not in mcp
    assert {a.pair for a in _armed(home)} == {"BTCUSD", "ETHUSD", "SUIUSD", "SOLUSD"}


def test_krell_on_a_core_without_a_ledger_fails_closed(home: Path):
    core = AgentToolCore(home)
    register_research_tools(core)
    ran: list[tuple[str, dict]] = []
    _change_stand_in(core, "pause_entries", ran)
    params = _arm_params(home)

    for token in (None, "any-token"):
        _assert_refused(core.call(KRELL, "paper_arm", params, confirm_token=token), CODE_CONFIRM_REQUIRED)
        _assert_refused(
            core.call(KRELL, "pause_entries", {"venue": "kraken"}, confirm_token=token),
            CODE_CONFIRM_REQUIRED,
            "pause_entries",
        )
        _assert_refused(
            core.call(KRELL, "compare", {"revision_ids": ["0" * 64, "1" * 64]}, confirm_token=token),
            CODE_CONFIRM_REQUIRED,
            "compare",
        )
    assert _armed(home) == []
    assert ran == []
    assert {r["outcome"] for r in _agent_rows(home)} == {"refused"}

    # Light calls still run.
    assert core.call(KRELL, "read", {})["ok"] is True


# ---- the ledger ------------------------------------------------------------


def test_only_plain_json_params_can_be_confirmed(home: Path):
    _clock, ledger, core = _krell_core(home)
    params = _arm_params(home)

    # The ticket binds the exact JSON the confirm card shows, so a value with no
    # exact JSON form (here in a param only the handler reads) gets no ticket.
    for bad in (Decimal(1), float("nan"), float("inf"), ("x",), {1: "x"}, {"deep": [{2: "x"}]}):
        refused = core.call(KRELL, "paper_arm", {**params, "correlation_id": bad})
        _assert_refused(refused, CODE_INVALID_REQUEST)
    assert _armed(home) == []

    with pytest.raises(ConfirmParamsError):
        ledger.issue(KRELL, "paper_arm", {**params, "paper_balance": Decimal(1000)}, now=START)
    assert issubclass(ConfirmParamsError, ValueError)

    # Consume treats params the same way: a non-JSON value never matches, and spends the ticket.
    ticket = ledger.issue(KRELL, "paper_arm", params, now=START)
    assert ledger.consume(ticket.id, "paper_arm", {**params, "paper_balance": Decimal(1000)}, now=START) == "mismatch"
    assert ledger.consume(ticket.id, "paper_arm", params, now=START) == "unknown"


def test_tickets_have_random_ids_and_keep_the_params_they_were_issued_for(home: Path):
    clock = _Clock()
    ledger = ConfirmLedger(clock=clock, ttl_s=30)
    params = {"venue": "kraken", "nested": {"b": [1, 2.5, None, True], "a": "x"}}

    first = ledger.issue(KRELL, "pause_entries", params)
    second = ledger.issue(KRELL, "pause_entries", params)
    assert first.id != second.id
    assert len(first.id) >= 22
    digest = hashlib.sha256(canonical_receipt_bytes({"tool": "pause_entries", "params": params})).hexdigest()
    assert first.digest == second.digest == digest
    assert (first.tool, first.expires_at) == ("pause_entries", START + 30)

    # Changing the caller's dict after issue changes neither the ticket nor its binding.
    sent = json.loads(json.dumps(params))
    params["nested"]["b"].append("smuggled")
    assert first.params == sent
    assert ledger.consume(first.id, "pause_entries", params) == "mismatch"
    # Key order does not matter; values do.
    reordered = {"nested": {"a": "x", "b": [1, 2.5, None, True]}, "venue": "kraken"}
    assert ledger.consume(second.id, "pause_entries", reordered) == "ok"
    assert ledger.consume(second.id, "pause_entries", reordered) == "unknown"

    third = ledger.issue(KRELL, "pause_entries", sent)
    clock.now += 30
    assert ledger.consume(third.id, "pause_entries", sent) == "unknown"
