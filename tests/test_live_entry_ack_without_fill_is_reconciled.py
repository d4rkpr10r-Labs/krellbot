"""A live entry the venue accepted, without fill evidence, is reconciled.

Kraken acknowledges a market buy with a txid and no executed volume. That
acknowledgement is not an execution: the engine credits nothing for it,
records the entry as a reconcile obligation (which holds new entries on that
venue and mode only), and later journals exactly what Kraken's own order
record says executed (``vol_exec``), once, under ``resolved_coid``. An order
Kraken cannot show keeps the hold and is never sent again.

Runs on a fake Kraken transport with the release gate patched in-process
(``live_capable_release``); ``PAPER_ONLY_RELEASE`` stays True in source.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes.fake_kraken import FakeKrakenTransport, order_info
from fakes.live_grant import write_grant

from krellbot import journal as kb_journal
from krellbot import run
from krellbot.config import ArmedPack, Config, save_config
from krellbot.execution import obligations as kb_obligations
from krellbot.execution.obligations import read_obligations
from krellbot.pack.model import Candle
from krellbot.run.reconcile import OwnershipIdentity, project_ownership
from krellbot.storage.database import OperationalStore
from krellbot.venues.kraken import KrakenVenue, coid_userref

pytestmark = pytest.mark.usefixtures("live_capable_release")

KEY = "FAKE_KEY_FOR_KRAKEN_TESTS"
SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
PACK = "live-ack"
PAIR = "SUIUSD"
H = 3_600_000
ENTRY_BAR = 2 * H
ENTRY_TXID = "OENTRY-AAAAA-BBBBBB"
STOP_TXID = "OSTOP-CCCCC-DDDDDD"
IDENTITY = OwnershipIdentity(pack_id=PACK, venue="kraken", pair=PAIR, mode="live")


def _candle(ts_ms: int, close: str) -> Candle:
    c = Decimal(close)
    return Candle(ts_ms=ts_ms, open=c, high=c + 1, low=c - 1, close=c, volume=Decimal(100))


ENTRY_BARS = [_candle(0, "10"), _candle(H, "10"), _candle(ENTRY_BAR, "12")]
# Three bars later the pack signals a fresh entry (close crosses above sma2).
NEXT_ENTRY_BARS = [*ENTRY_BARS, _candle(3 * H, "10"), _candle(4 * H, "9"), _candle(5 * H, "12")]
ENTRY_COID = run.coid_for(
    pack_id=PACK, pack_version="1.0.0", venue="kraken", pair=PAIR, bar_ts=ENTRY_BAR, intent="entry"
)


@pytest.fixture
def live(home, monkeypatch) -> Path:
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    write_grant(home, venue="kraken", pair=PAIR)
    pack = {
        "schema_version": 1,
        "id": PACK,
        "version": "1.0.0",
        "label": "Live ack",
        "author": "krellbot tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 50}},
        "markets": [{"venue": "kraken", "pair": PAIR}],
    }
    pack_path = home / f"{PACK}.json"
    pack_path.write_text(json.dumps(pack), encoding="utf-8")
    armed = ArmedPack(
        pack_path=str(pack_path),
        pack_sha256="0" * 64,
        pack_id=PACK,
        pack_version="1.0.0",
        venue="kraken",
        pair=PAIR,
        cap=Decimal(100),
        stop=Decimal(5),
        mode="live",
        starting_cash=Decimal(100),
        requires_license=False,
        armed_at_ts=1,
    )
    save_config(home, Config(armed=[armed]))
    return home


def _transport() -> FakeKrakenTransport:
    return FakeKrakenTransport(
        responses=[{"error": [], "result": {"descr": {"order": "buy SUIUSD @ market"}, "txid": [ENTRY_TXID]}}],
        asset_pairs={PAIR: {"ordermin": "0.01", "costmin": "0.5", "lot_decimals": 5, "pair_decimals": 4}},
        balances={"USD": "100"},
    )


def _venue(home: Path, transport: FakeKrakenTransport, cls: type[KrakenVenue] = KrakenVenue, **kw) -> KrakenVenue:
    return cls(KEY, SECRET, transport, now_ms=lambda: 1_700_000_000_000, min_interval_ms=0, home=home, **kw)


def _tick(home: Path, transport: FakeKrakenTransport, bars: list[Candle], venue: KrakenVenue | None = None) -> None:
    venue = venue or _venue(home, transport)
    assert (
        run.tick(venue="kraken", venue_obj=venue, reader=lambda *_: bars, home=home, clock=lambda: 1_700_000_000) == 0
    )


def _add_orders(transport: FakeKrakenTransport) -> list:
    return [c for c in transport.calls if c.url.endswith("/AddOrder")]


def _ledger(home: Path) -> list[tuple[str, dict]]:
    return [(kind, json.loads(payload)) for _id, kind, payload in OperationalStore(home / "ops.sqlite").read_ledger()]


def _marked(home: Path) -> list[str]:
    return [p["coid"] for kind, p in _ledger(home) if kind == "needs_reconcile"]


def _resolutions(home: Path) -> list[dict]:
    return [p for kind, p in _ledger(home) if kind == kb_obligations.RESOLVED_KIND]


def _tick_records(home: Path, bar_ts: int) -> list[dict]:
    return [
        r["detail"]
        for r in kb_journal.read_records_lenient(home)
        if r.get("kind") == "tick"
        and r.get("bar_ts") == bar_ts
        and (r.get("detail") or {}).get("reason") != run.RECONCILED_REASON
    ]


def _reconciled_entry_qty(home: Path) -> list[Decimal]:
    return [
        Decimal(r["detail"]["entry_qty"])
        for r in kb_journal.read_records_lenient(home)
        if r.get("kind") == "tick" and (r.get("detail") or {}).get("resolved_coid") == ENTRY_COID
    ]


def _held(home: Path) -> bool:
    return read_obligations(home).blocks_entries(venue="kraken", mode="live")


def _entry_row(*, vol: Decimal, vol_exec: Decimal, status: str) -> dict:
    return {
        ENTRY_TXID: order_info(
            vol=str(vol),
            vol_exec=str(vol_exec),
            status=status,
            pair=PAIR,
            cl_ord_id=ENTRY_COID,
            userref=coid_userref(ENTRY_COID),
        )
    }


def _resting_stop(qty: Decimal) -> dict:
    """Kraken's conditional close for the entry: a sell stop created by it.

    It carries the entry's userref here, so only ``cl_ord_id`` and ``refid``
    tell the two orders apart.
    """
    return {
        STOP_TXID: order_info(
            vol=str(qty),
            vol_exec="0.00000000",
            status="open",
            pair=PAIR,
            side="sell",
            ordertype="stop-loss",
            userref=coid_userref(ENTRY_COID),
            refid=ENTRY_TXID,
            stopprice="6.0000",
        )
    }


def _enter(home: Path, transport: FakeKrakenTransport, venue: KrakenVenue | None = None) -> Decimal:
    """Tick the entry bar; return the requested quantity Kraken acknowledged."""
    _tick(home, transport, ENTRY_BARS, venue)

    [add] = _add_orders(transport)
    assert add.form["type"] == "buy" and add.form["cl_ord_id"] == ENTRY_COID
    [record] = _tick_records(home, ENTRY_BAR)
    assert record["entry_qty"] == "0"  # an acknowledgement is not an execution
    assert record["owned_qty_after"] == "0"
    assert project_ownership(home, IDENTITY).attributable_qty == 0
    assert _marked(home) == [ENTRY_COID]
    view = read_obligations(home)
    assert [o.coid for o in view.unresolved] == [ENTRY_COID]
    assert view.blocks_entries(venue="kraken", mode="live")
    assert not view.blocks_entries(venue="kraken", mode="paper")
    assert not view.blocks_entries(venue="coinbase", mode="live")
    return Decimal(add.form["volume"])


# ---- the run engine --------------------------------------------------------------


def test_accepted_entry_that_filled_is_journaled_once_from_executed_volume(live):
    transport = _transport()
    requested = _enter(live, transport)

    transport.set_order_books(
        open_rows=_resting_stop(requested),
        closed_rows=_entry_row(vol=requested, vol_exec=requested, status="closed"),
    )
    transport.balances["SUI"] = str(requested)
    for _ in range(2):
        _tick(live, transport, ENTRY_BARS)

    assert _reconciled_entry_qty(live) == [requested]
    assert project_ownership(live, IDENTITY).attributable_qty == requested
    [resolution] = _resolutions(live)
    assert resolution["coid"] == ENTRY_COID and resolution["outcome"] == "filled"
    assert Decimal(resolution["filled_qty"]) == requested
    assert not _held(live)
    assert len(_add_orders(transport)) == 1


def test_partially_executed_entry_is_journaled_at_its_executed_volume(live):
    transport = _transport()
    requested = _enter(live, transport)
    first = (requested / 4).quantize(Decimal("0.00001"))
    final = (requested / 2).quantize(Decimal("0.00001"))

    # Still working on the book with some volume executed: account it, keep the hold.
    transport.set_order_books(open_rows=_entry_row(vol=requested, vol_exec=first, status="open"))
    transport.balances["SUI"] = str(first)
    _tick(live, transport, ENTRY_BARS)
    assert _reconciled_entry_qty(live) == [first]
    assert _resolutions(live) == []
    assert _held(live)

    # Kraken closed the rest unexecuted: the executed volume is final.
    transport.set_order_books(
        open_rows=_resting_stop(final),
        closed_rows=_entry_row(vol=requested, vol_exec=final, status="canceled"),
    )
    transport.balances["SUI"] = str(final)
    for _ in range(2):
        _tick(live, transport, ENTRY_BARS)

    assert sum(_reconciled_entry_qty(live)) == final < requested
    assert project_ownership(live, IDENTITY).attributable_qty == final
    [resolution] = _resolutions(live)
    assert Decimal(resolution["filled_qty"]) == final
    assert not _held(live)
    assert len(_add_orders(transport)) == 1


def test_entry_kraken_cannot_find_keeps_the_hold_and_is_never_resent(live):
    transport = _transport()
    _enter(live, transport)

    _tick(live, transport, NEXT_ENTRY_BARS)  # the pack wants a new entry on a later bar

    assert len(_add_orders(transport)) == 1
    lookups = [c for c in transport.calls if c.url.endswith(("/OpenOrders", "/ClosedOrders")) and "userref" in c.form]
    assert {c.form["userref"] for c in lookups} == {str(coid_userref(ENTRY_COID))}
    assert _held(live)
    assert _marked(live) == [ENTRY_COID]  # audited once, not per tick
    assert _resolutions(live) == []
    assert not [k for k, _p in _ledger(live) if k.startswith("reconcile_")]
    [later] = _tick_records(live, 5 * H)
    assert later["entry_qty"] == "0"
    assert _reconciled_entry_qty(live) == []
    assert project_ownership(live, IDENTITY).attributable_qty == 0


class _AckWithUntypedFill(KrakenVenue):
    """A venue whose acknowledgement reports a fill that is not a Decimal."""

    def __init__(self, *args, shape: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._shape = shape

    def place_entry_with_stop(self, coid, qty, stop, *, pair):
        ref = super().place_entry_with_stop(coid, qty, stop, pair=pair)
        if self._shape == "missing":
            return SimpleNamespace(id=ref.id, coid=ref.coid, pair=ref.pair, side=ref.side, qty=ref.qty)
        return replace(ref, filled_qty=str(qty) if self._shape == "text" else float(qty))


@pytest.mark.parametrize("shape", ["missing", "text", "float"])
def test_entry_fill_that_is_not_a_decimal_is_not_credited_as_the_requested_qty(live, shape):
    transport = _transport()
    venue = _venue(live, transport, _AckWithUntypedFill, shape=shape)

    _enter(live, transport, venue)  # credits nothing and leaves an obligation


# ---- the Kraken adapter ------------------------------------------------------------

_COID = "c0ffee00c0ffee00ab"
_REF = coid_userref(_COID)


def _own(*, vol: str = "10", vol_exec: str | None, status: str) -> dict:
    return {"OWN": order_info(vol=vol, vol_exec=vol_exec, status=status, pair=PAIR, cl_ord_id=_COID, userref=_REF)}


_OTHERS_ON_THE_USERREF = {
    "CHILD": order_info(
        vol="10", vol_exec="0", status="open", pair=PAIR, side="sell", userref=_REF, refid="OWN", stopprice="6"
    ),
    "STRANGER": order_info(vol="10", vol_exec="10", status="open", pair=PAIR, cl_ord_id="someone-else", userref=_REF),
}


@pytest.mark.parametrize(
    ("open_rows", "closed_rows", "status", "filled", "authoritative"),
    [
        ({}, {}, "unknown", "0", False),
        (_own(vol_exec="0.00000000", status="open"), {}, "open", "0", True),
        (_own(vol_exec="4.00000000", status="open"), {}, "partial", "4", True),
        ({}, _own(vol_exec="10.00000000", status="closed"), "filled", "10", True),
        ({}, _own(vol_exec="4.00000000", status="canceled"), "filled", "4", True),
        ({}, _own(vol_exec="0.00000000", status="expired"), "absent", "0", True),
        ({}, _own(vol_exec=None, status="closed"), "unknown", "0", False),
        ({}, _own(vol_exec="NaN", status="closed"), "unknown", "0", False),
        (_OTHERS_ON_THE_USERREF, {}, "unknown", "0", False),
        (_OTHERS_ON_THE_USERREF, _own(vol_exec="3", status="closed"), "filled", "3", True),
    ],
    ids=[
        "not-found",
        "open-unexecuted",
        "open-partly-executed",
        "closed-executed",
        "cancelled-partly-executed",
        "expired-unexecuted",
        "executed-volume-missing",
        "executed-volume-unreadable",
        "only-other-orders-share-the-userref",
        "own-order-beside-others-on-the-userref",
    ],
)
def test_kraken_order_outcome_reads_executed_volume_and_never_sends(
    home, open_rows, closed_rows, status, filled, authoritative
):
    transport = FakeKrakenTransport()
    transport.set_order_books(open_rows=open_rows, closed_rows=closed_rows)

    outcome = _venue(home, transport).order_outcome(_COID, pair=PAIR)

    assert (outcome.status, outcome.filled_qty, outcome.authoritative) == (status, Decimal(filled), authoritative)
    assert {c.url.rsplit("/", 1)[-1] for c in transport.calls} <= {"OpenOrders", "ClosedOrders"}
    assert all(c.form["userref"] == str(_REF) for c in transport.calls)


@pytest.mark.parametrize(
    ("open_rows", "closed_rows", "order_id", "filled"),
    [
        (_own(vol="1.25", vol_exec="0.00000000", status="open"), {}, "OWN", "0"),
        ({}, _own(vol="1.25", vol_exec="0.50000000", status="canceled"), "OWN", "0.5"),
        # Resend suppression stays broad: any order on the coid's userref
        # blocks a second send, though only the coid's own order is an outcome.
        ({"CHILD": _OTHERS_ON_THE_USERREF["CHILD"]}, {}, "CHILD", "0"),
    ],
    ids=["resting-unexecuted", "closed-partly-executed", "only-a-conditional-close-on-the-userref"],
)
def test_kraken_entry_it_already_holds_reports_executed_not_requested_volume(
    home, open_rows, closed_rows, order_id, filled
):
    transport = FakeKrakenTransport()
    transport.set_order_books(open_rows=open_rows, closed_rows=closed_rows)

    ref = _venue(home, transport).place_entry_with_stop(_COID, Decimal("1.25"), Decimal(6), pair=PAIR)

    assert ref.id == order_id
    assert ref.filled_qty == Decimal(filled)
    assert _add_orders(transport) == []


def _unreadable(**kw) -> dict:
    """A listed order on the coid's userref whose requested volume cannot be read."""
    return order_info(vol="", vol_exec="0", status="open", pair=PAIR, userref=_REF, **kw)


@pytest.mark.parametrize(
    ("open_rows", "closed_rows", "order_id", "filled"),
    [
        (
            {
                "UNREADABLE": _unreadable(),
                "READABLE": order_info(vol="1.25", vol_exec="0", status="open", pair=PAIR, userref=_REF),
            },
            {},
            "READABLE",
            "0",
        ),
        (
            {"UNREADABLE": _unreadable(cl_ord_id=_COID)},
            {"OWN": order_info(vol="1.25", vol_exec="1.25", status="closed", pair=PAIR, cl_ord_id=_COID, userref=_REF)},
            "OWN",
            "1.25",
        ),
    ],
    ids=["readable-row-after-an-unreadable-one", "readable-closed-order-after-an-unreadable-open-one"],
)
def test_kraken_resend_guard_skips_unreadable_rows_and_keeps_scanning(home, open_rows, closed_rows, order_id, filled):
    transport = FakeKrakenTransport()
    transport.set_order_books(open_rows=open_rows, closed_rows=closed_rows)

    ref = _venue(home, transport).place_entry_with_stop(_COID, Decimal("1.25"), Decimal(6), pair=PAIR)

    assert _add_orders(transport) == []  # an order on the coid's userref is listed: never send again
    assert ref.id == order_id
    assert ref.filled_qty == Decimal(filled)
