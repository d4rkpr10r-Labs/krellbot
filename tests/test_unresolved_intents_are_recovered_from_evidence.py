"""Unresolved order intents are settled from venue evidence, never resent.

An intent committed to the Outbox whose outcome is unknown holds new entries
on its venue and mode until the venue's own record shows what happened.
Evidence settles it: a proven absence retires it, an execution is accounted
once to the strategy recorded in the intent, and an executed entry stays an
obligation until it is protected. Exits and protection keep running.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot import journal as kb_journal
from krellbot import run
from krellbot.application import alerts as kb_alerts
from krellbot.application import operations as kb_operations
from krellbot.application.paper import PaperService
from krellbot.execution import obligations as kb_obligations
from krellbot.execution.obligations import ObligationsUnavailable, read_obligations
from krellbot.run.lock import TickLock
from krellbot.run.reconcile import OwnershipIdentity, project_ownership
from krellbot.storage.database import OperationalStore
from krellbot.storage.outbox import Outbox
from krellbot.venues.base import OrderOutcome
from krellbot.venues.paper import PaperVenue
from tests.test_ns14_reservations import _RULES, _candle, _FakeVenue, _write_pack

PACK = "kq02"
PAIR = "SUIUSD"
H = 3_600_000
ENTRY_BAR = 2 * H
BARS = [_candle(0, "10"), _candle(H, "9"), _candle(ENTRY_BAR, "12")]
LATER_BARS = [*BARS, _candle(3 * H, "8"), _candle(4 * H, "7"), _candle(5 * H, "12")]
REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = OwnershipIdentity(pack_id=PACK, venue="kraken", pair=PAIR, mode="paper")


def _arm(home: Path, pack_id: str = PACK) -> None:
    assert os.environ["KRELLBOT_HOME"] == str(home)
    path = _write_pack(home, pack_id=pack_id, cap_pct=25)
    result = PaperService(home).arm(path, venue="kraken", mode="paper", paper_balance=Decimal(1000))
    assert result.ok, result.to_dict()


def _paper(home: Path, bars: list, clock: float = 1_000_000.0) -> PaperVenue:
    return PaperVenue(
        "kraken",
        rules_provider=lambda _: _RULES,
        candle_reader=lambda *_: bars,
        home=home,
        starting_cash=Decimal(1000),
        clock=lambda: clock,
        timeframe_for_pair=lambda _: "1h",
    )


def _old_coid(pack_id: str = PACK) -> str:
    return run.coid_for(
        pack_id=pack_id, pack_version="1.0.0", venue="kraken", pair=PAIR, bar_ts=ENTRY_BAR, intent="entry"
    )


def _intent_body(*, qty: str = "20", stop: str = "6", kind: str = "entry", pack_id: str = PACK) -> str:
    return json.dumps(
        {
            "kind": kind,
            "pair": PAIR,
            "qty": qty,
            "stop": stop,
            "pack_id": pack_id,
            "pack_version": "1.0.0",
            "bar_ts": ENTRY_BAR,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _crash(
    *, home: Path, venue_obj=None, venue: str = "kraken", coid: str | None = None, body: str | None = None
) -> str:
    """Commit an intent, then 'crash': before send, or after the venue accepted it."""
    coid = coid or _old_coid()
    body = body or _intent_body()

    def send(c: str, b: str) -> None:
        if venue_obj is not None:
            payload = json.loads(b)
            venue_obj.place_entry_with_stop(c, Decimal(payload["qty"]), Decimal(payload["stop"]), pair=payload["pair"])
        raise RuntimeError("process died before the send was acknowledged")

    with pytest.raises(RuntimeError):
        Outbox(OperationalStore(home / "ops.sqlite")).dispatch(coid, body, send, mode="paper", venue=venue)
    return coid


_CHILD_TICK = """
import json, os, sys
from decimal import Decimal
from pathlib import Path
from krellbot import run
from tests.test_unresolved_intents_are_recovered_from_evidence import BARS, LATER_BARS, _paper
home = Path(os.environ["KRELLBOT_HOME"])
bars = LATER_BARS if sys.argv[1] == "later" else BARS
venue = _paper(home, bars, clock=float(sys.argv[2]))
assert run.tick(venue="kraken", venue_obj=venue, reader=lambda *_: bars, home=home, clock=lambda: float(sys.argv[2])) == 0
"""


def _tick_in_fresh_process(home: Path, *, later: bool, clock: float = 1_000_000.0) -> None:
    paths = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    env = {**os.environ, "KRELLBOT_HOME": str(home), "PYTHONPATH": paths}
    subprocess.run(
        [sys.executable, "-c", _CHILD_TICK, "later" if later else "same", str(clock)],
        env=env,
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _ledger(home: Path) -> list[tuple[str, dict]]:
    return [(kind, json.loads(payload)) for _id, kind, payload in OperationalStore(home / "ops.sqlite").read_ledger()]


def _resolutions(home: Path) -> dict[str, str]:
    return {p["coid"]: p["outcome"] for kind, p in _ledger(home) if kind == kb_obligations.RESOLVED_KIND}


def _paper_state(home: Path) -> dict:
    return json.loads((home / "run" / "paper-kraken.json").read_text(encoding="utf-8"))


def _reconciled(home: Path, coid: str) -> list[dict]:
    return [
        r["detail"]
        for r in kb_journal.read_records_lenient(home)
        if r.get("kind") == "tick" and (r.get("detail") or {}).get("resolved_coid") == coid
    ]


# ---- interruption before send -------------------------------------------------


def test_intent_that_never_reached_the_venue_is_retired_on_a_later_bar_without_resubmit(home):
    _arm(home)
    old = _crash(home=home)
    assert read_obligations(home).blocks_entries(venue="kraken", mode="paper")

    _tick_in_fresh_process(home, later=True)

    assert _resolutions(home) == {old: "absent"}
    outbox = {p["coid"]: p["sent"] for kind, p in _ledger(home) if kind == "outbox"}
    assert outbox[old] is False  # retired from evidence, never resent
    fills = _paper_state(home)["recent_fills"]
    assert [f for f in fills if f["coid"] == old] == []
    assert [f["side"] for f in fills] == ["buy"]  # one new entry for the new bar's decision


# ---- interruption after acceptance, before acknowledgement --------------------


@pytest.mark.parametrize("later", [False, True], ids=["same-bar", "later-bar"])
def test_accepted_entry_whose_ack_was_lost_is_accounted_once_and_never_replayed(home, later):
    _arm(home)
    old = _crash(home=home, venue_obj=_paper(home, BARS))
    filled = Decimal(_paper_state(home)["recent_fills"][0]["qty"])

    _tick_in_fresh_process(home, later=later)
    _tick_in_fresh_process(home, later=later, clock=1_003_600.0)

    assert _resolutions(home) == {old: "filled"}
    assert [Decimal(d["entry_qty"]) for d in _reconciled(home, old)] == [filled]
    assert project_ownership(home, IDENTITY).attributable_qty == filled
    fills = _paper_state(home)["recent_fills"]
    assert [f["coid"] for f in fills if f["side"] == "buy"] == [old]  # no replay, no stacked entry


def test_executed_entry_without_protection_stays_an_obligation_until_a_stop_rests(home):
    _arm(home)
    old = _crash(home=home, venue_obj=_paper(home, BARS))
    state = _paper_state(home)
    state["open_orders"] = []  # the protective stop was refused after the fill
    (home / "run" / "paper-kraken.json").write_text(json.dumps(state), encoding="utf-8")

    venue = _paper(home, BARS)
    assert run.tick(venue="kraken", venue_obj=venue, reader=lambda *_: BARS, home=home, clock=lambda: 1_000_000) == 0

    assert old not in _resolutions(home)
    assert read_obligations(home).blocks_entries(venue="kraken", mode="paper")
    assert [o for o in _paper_state(home)["open_orders"] if o.get("stop_price") is not None]  # ensure-stop acted

    venue = _paper(home, BARS)
    assert run.tick(venue="kraken", venue_obj=venue, reader=lambda *_: BARS, home=home, clock=lambda: 1_000_000) == 0
    assert _resolutions(home) == {old: "filled"}
    assert [f["coid"] for f in _paper_state(home)["recent_fills"] if f["side"] == "buy"] == [old]


def test_interrupted_resolution_converges_without_double_counting(home, monkeypatch):
    _arm(home)
    old = _crash(home=home, venue_obj=_paper(home, BARS))
    real = kb_obligations.record_resolution

    def die(*args, **kwargs):
        raise RuntimeError("interrupted between accounting and resolution")

    monkeypatch.setattr(kb_obligations, "record_resolution", die)
    with pytest.raises(RuntimeError):
        run.tick(
            venue="kraken", venue_obj=_paper(home, BARS), reader=lambda *_: BARS, home=home, clock=lambda: 1_000_000
        )
    monkeypatch.setattr(kb_obligations, "record_resolution", real)
    for _ in range(2):
        run.tick(
            venue="kraken", venue_obj=_paper(home, BARS), reader=lambda *_: BARS, home=home, clock=lambda: 1_000_000
        )

    assert len(_reconciled(home, old)) == 1
    assert _resolutions(home) == {old: "filled"}


def test_explicit_reconcile_and_tick_cannot_interleave(home):
    _arm(home)
    old = _crash(home=home, venue_obj=_paper(home, BARS))
    lock = TickLock(home, "kraken")
    assert lock.acquire()
    try:
        assert run.reconcile_intents(venue="kraken", venue_obj=_paper(home, BARS), home=home) == "busy"
        assert _reconciled(home, old) == []
    finally:
        lock.release()

    assert run.reconcile_intents(venue="kraken", venue_obj=_paper(home, BARS), home=home) == "done"
    assert run.reconcile_intents(venue="kraken", venue_obj=_paper(home, BARS), home=home) == "done"
    assert len(_reconciled(home, old)) == 1


# ---- outcomes the venue cannot establish ---------------------------------------


class _BtcUsdtVenue(_FakeVenue):
    @staticmethod
    def _split(pair: str) -> tuple[str, str]:
        return ("BTC", "USDT")


def _arm_bybit(home: Path) -> None:
    body = {
        **json.loads(_write_pack(home, pack_id="other-venue", cap_pct=25).read_text(encoding="utf-8")),
        "markets": [{"venue": "bybit", "pair": "BTCUSDT"}],
    }
    path = home / "other-venue-bybit.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    assert PaperService(home).arm(path, venue="bybit", mode="paper", paper_balance=Decimal(1000)).ok


def _entries(fake: _FakeVenue) -> list:
    return [c for c in fake.calls if c.method == "entry"]


def test_unknown_outcome_holds_entries_on_its_venue_only_and_is_audited_once(home):
    _arm(home)
    _arm_bybit(home)
    old = _crash(home=home)
    kraken = _FakeVenue(initial_balances={"USD": Decimal(1000)})  # no authoritative lookup
    bybit = _BtcUsdtVenue(venue="bybit", pair="BTCUSDT", initial_balances={"USDT": Decimal(1000)})

    for clock in (1_000_000, 1_003_600):
        run.tick(venue="kraken", venue_obj=kraken, reader=lambda *_: LATER_BARS, home=home, clock=lambda now=clock: now)
    run.tick(venue="bybit", venue_obj=bybit, reader=lambda *_: BARS, home=home, clock=lambda: 1_000_000)

    assert _entries(kraken) == []
    assert len(_entries(bybit)) == 1
    assert [p["coid"] for kind, p in _ledger(home) if kind == "needs_reconcile"] == [old]
    assert old not in _resolutions(home)


def test_intent_without_a_recorded_venue_holds_entries_everywhere(home):
    _arm_bybit(home)
    store = OperationalStore(home / "ops.sqlite")
    with pytest.raises(RuntimeError):
        Outbox(store).dispatch("legacy-coid", _intent_body(), lambda *_: (_ for _ in ()).throw(RuntimeError()))
    bybit = _BtcUsdtVenue(venue="bybit", pair="BTCUSDT", initial_balances={"USDT": Decimal(1000)})

    run.tick(venue="bybit", venue_obj=bybit, reader=lambda *_: BARS, home=home, clock=lambda: 1_000_000)

    assert _entries(bybit) == []


def test_exits_continue_while_entries_are_held(home):
    _arm(home)
    kb_journal.append(
        {
            "ts": 1,
            "kind": "tick",
            "venue": "kraken",
            "pack": PACK,
            "bar_ts": H,
            "detail": {"pair": PAIR, "mode": "paper", "entry_qty": "5", "exit_qty": "0", "stop_qty": "0"},
        }
    )
    _crash(home=home, coid="unrelated-unknown", body=_intent_body(pack_id="someone-else"))
    kraken = _FakeVenue(initial_balances={"USD": Decimal(0), "SUI": Decimal(5)})
    exit_bars = [*BARS, _candle(3 * H, "8")]

    run.tick(venue="kraken", venue_obj=kraken, reader=lambda *_: exit_bars, home=home, clock=lambda: 1_003_600)

    assert [c.qty for c in kraken.calls if c.method == "exit"] == [Decimal(5)]


def test_unreadable_store_holds_entries_and_is_never_shown_as_clean(home):
    (home / "ops.sqlite").write_bytes(b"this is not a sqlite database" * 64)

    view = read_obligations(home)
    assert view.available is False
    assert view.blocks_entries(venue="kraken", mode="paper")
    with pytest.raises(ObligationsUnavailable):
        kb_operations.list_unresolved_orders(home, now=0)
    assert any(a.kind == "store_fault" for a in kb_alerts.collect(home, now=0))


class _PartialVenue(_FakeVenue):
    def __init__(self, outcomes):
        super().__init__(initial_balances={"USD": Decimal(1000)})
        self.outcomes = list(outcomes)

    def order_outcome(self, coid, *, pair):
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]


def test_partial_executions_are_accounted_once_as_they_accumulate(home):
    _arm(home)
    old = _crash(home=home, body=_intent_body(kind="exit", qty="5"))
    venue = _PartialVenue(
        [
            OrderOutcome(status="partial", filled_qty=Decimal(2), authoritative=True),
            OrderOutcome(status="partial", filled_qty=Decimal(2), authoritative=True),
            OrderOutcome(status="filled", filled_qty=Decimal(5), authoritative=True),
        ]
    )

    for _ in range(3):
        run.reconcile_intents(venue="kraken", venue_obj=venue, home=home)
        assert sum(Decimal(d["exit_qty"]) for d in _reconciled(home, old)) <= 5
    for _ in range(2):
        run.reconcile_intents(venue="kraken", venue_obj=venue, home=home)

    assert sum(Decimal(d["exit_qty"]) for d in _reconciled(home, old)) == 5
    assert _resolutions(home) == {old: "filled"}


def test_resolved_intents_leave_operations_and_alerts(home):
    _arm(home)
    old = _crash(home=home)
    assert [r["client_order_id"] for r in kb_operations.list_unresolved_orders(home, now=0)] == [old]

    run.reconcile_intents(venue="kraken", venue_obj=_paper(home, BARS), home=home)

    assert kb_operations.list_unresolved_orders(home, now=0) == []
    assert not read_obligations(home).blocks_entries(venue="kraken", mode="paper")
    assert not [a for a in kb_alerts.collect(home, now=0) if a.kind == "needs_reconcile"]
