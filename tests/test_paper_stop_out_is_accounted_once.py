"""A paper stop-out reduces the strategy's recorded ownership exactly once.

When the paper venue fills a strategy's resting stop, the strategy no longer
holds that quantity. The journal must record the decrease against the
strategy that owned the stop, once, however many ticks or restarts follow;
otherwise a later tick protects or sells external holdings as if they were
the stopped-out position.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from krellbot import journal as kb_journal
from krellbot import run
from krellbot.application.paper import PaperService
from krellbot.run.reconcile import OwnershipIdentity, project_ownership
from krellbot.venues.base import Fill
from krellbot.venues.paper import PaperVenue
from tests.test_ns14_reservations import _RULES, _candle, _write_pack

PACK = "kq08-stop-out"
PAIR = "SUIUSD"
H = 3_600_000
EXTERNAL = Decimal(100)
IDENTITY = OwnershipIdentity(pack_id=PACK, venue="kraken", pair=PAIR, mode="paper")


class _Run:
    """Arm one paper pack on Kraken and drive real PaperVenue ticks."""

    def __init__(self, home: Path) -> None:
        assert os.environ["KRELLBOT_HOME"] == str(home)
        self.home = home
        self.bars = [_candle(0, "10"), _candle(H, "9"), _candle(2 * H, "12")]
        self.clock = 1_000_000.0
        self.service = PaperService(home)
        armed = self.service.arm(
            _write_pack(home, pack_id=PACK, cap_pct=25), venue="kraken", mode="paper", paper_balance=Decimal(1000)
        )
        assert armed.ok, armed.to_dict()
        self.venue = self.restart()

    def restart(self) -> PaperVenue:
        self.venue = PaperVenue(
            "kraken",
            rules_provider=lambda _: _RULES,
            candle_reader=lambda *_: self.bars,
            home=self.home,
            starting_cash=Decimal(1000),
            clock=lambda: self.clock,
            timeframe_for_pair=lambda _: "1h",
        )
        return self.venue

    def tick(self, *bars) -> None:
        self.bars.extend(bars)
        if bars:
            self.clock += 3600
        assert (
            run.tick(
                venue="kraken",
                venue_obj=self.venue,
                reader=lambda *_: self.bars,
                home=self.home,
                clock=lambda: self.clock,
            )
            == 0
        )

    def sui(self) -> Decimal:
        return self.venue.balances().get("SUI", Decimal(0))

    def add_external(self) -> None:
        state_path = self.home / "run" / "paper-kraken.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["balances"]["SUI"] = str(Decimal(str(state["balances"].get("SUI", "0"))) + EXTERNAL)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.restart()

    def stops(self) -> list[dict]:
        state = json.loads((self.home / "run" / "paper-kraken.json").read_text(encoding="utf-8"))
        return [o for o in state["open_orders"] if o.get("stop_price") is not None]

    def ticks(self) -> list[dict]:
        return [
            r for r in kb_journal.read_records_lenient(self.home) if r.get("kind") == "tick" and r.get("pack") == PACK
        ]


def _enter_then_stop_out(home: Path, *, external_during_stop_out: bool = False) -> tuple[_Run, Decimal]:
    paper = _Run(home)
    paper.tick()
    bought = paper.sui()
    assert bought > 0 and len(paper.stops()) == 1
    assert paper.service.pause_entries(venue="kraken", pair=PAIR).ok
    if external_during_stop_out:
        paper.add_external()
    paper.tick(_candle(3 * H, "12", open_="12", low="5"))  # the resting 50% stop fills
    assert paper.stops() == []
    return paper, bought


def test_stop_out_before_external_holdings_arrive_never_touches_them(home):
    paper, _ = _enter_then_stop_out(home)
    assert paper.sui() == 0
    paper.add_external()

    paper.tick(_candle(4 * H, "13", open_="13", low="12"))
    assert paper.stops() == []
    paper.tick(_candle(5 * H, "13", open_="13", low="1"))

    assert paper.sui() == EXTERNAL
    assert project_ownership(home, IDENTITY).attributable_qty == 0


def test_external_holdings_present_during_a_stop_out_are_preserved(home):
    paper, _ = _enter_then_stop_out(home, external_during_stop_out=True)
    assert paper.sui() == EXTERNAL

    paper.tick(_candle(4 * H, "13", open_="13", low="12"))
    assert paper.stops() == []
    paper.tick(_candle(5 * H, "13", open_="13", low="1"))

    assert paper.sui() == EXTERNAL
    assert project_ownership(home, IDENTITY).attributable_qty == 0


def test_stop_fill_found_in_the_ticks_own_snapshot_is_recorded_by_that_tick(home):
    paper, bought = _enter_then_stop_out(home)

    stop_tick = paper.ticks()[-1]["detail"]
    assert Decimal(stop_tick["stop_qty"]) == bought
    assert len(stop_tick["stop_fill_ids"]) == 1


def test_stop_fill_is_accounted_once_across_ticks_and_restarts(home):
    paper, bought = _enter_then_stop_out(home)
    paper.tick(_candle(4 * H, "13"))
    paper.restart()
    paper.tick(_candle(5 * H, "14"))
    paper.restart()
    paper.tick()

    accounted = [r["detail"] for r in paper.ticks() if r["detail"].get("stop_fill_ids")]
    assert len(accounted) == 1
    assert sum(Decimal(r["detail"].get("stop_qty") or "0") for r in paper.ticks()) == bought
    assert project_ownership(home, IDENTITY).attributable_qty == 0


def test_a_new_position_after_a_stop_out_is_protected_for_its_own_quantity(home):
    paper, _ = _enter_then_stop_out(home)
    paper.add_external()
    assert paper.service.resume_entries(venue="kraken", pair=PAIR).ok

    paper.tick(_candle(4 * H, "8"), _candle(5 * H, "13"))

    entered = paper.sui() - EXTERNAL
    assert entered > 0
    assert [Decimal(str(o["qty"])) for o in paper.stops()] == [entered]
    assert project_ownership(home, IDENTITY).attributable_qty == entered


def test_a_stop_fill_for_another_order_is_not_this_strategys_decrease(home):
    paper = _Run(home)
    paper.tick()
    bought = paper.sui()
    foreign = Fill(
        id="stop-SUIUSD-999",
        coid="stop-SUIUSD-999",
        pair=PAIR,
        side="sell",
        qty=Decimal(1),
        price=Decimal(9),
        ts_ms=1,
        order_coid="someone-elses-order",
    )
    assert run._stop_fills_for(home, IDENTITY, [foreign]) == ([], Decimal(0))
    assert project_ownership(home, IDENTITY).attributable_qty == bought
