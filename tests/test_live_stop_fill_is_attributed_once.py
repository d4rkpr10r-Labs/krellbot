"""A live protective-stop fill reduces the strategy's recorded ownership once.

When Kraken or Coinbase executes the protective stop a live entry placed, the
strategy no longer holds that quantity. The venue's fill must name the entry
coid whose stop executed (``Fill.order_coid``) through documented fields only:

* Kraken: the trade's ``ordertxid`` names its order; ``QueryOrders`` shows
  that order's ``refid`` (the order that created it, for a conditional close
  the entry) and the entry's ``cl_ord_id``.
* Coinbase: the fill's ``order_id`` names its order; the order record's
  ``client_order_id`` is ``<entry coid>_stop``.

The run then journals the decrease against that identity exactly once, by
fill id, across repeated snapshots and restarts, and a later bar never sells
external units. A sell on the pair that the venue cannot link is never
guessed at: it holds that identity's exits and stop repair as an unresolved
order in Operations until the operator resolves it (M28.22). Other strategies'
and external sells are neither attributed nor held. Paper is unchanged.

Runs on fake transports with the release gate patched in-process
(``live_capable_release``); ``PAPER_ONLY_RELEASE`` stays True in source.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fakes.fake_coinbase import FakeCoinbaseTransport, order_record
from fakes.fake_coinbase import RecordedCall as CoinbaseCall
from fakes.fake_kraken import FakeKrakenTransport, order_info
from fakes.fake_kraken import RecordedCall as KrakenCall
from fakes.live_grant import write_grant

from krellbot import journal as kb_journal
from krellbot import run
from krellbot.application import operations as kb_operations
from krellbot.config import ArmedPack, Config, save_config
from krellbot.execution.obligations import read_obligations
from krellbot.pack.model import Candle
from krellbot.run.reconcile import OwnershipIdentity, project_ownership
from krellbot.storage.database import OperationalStore
from krellbot.venues.coinbase import CoinbaseVenue
from krellbot.venues.kraken import KrakenVenue, coid_userref

pytestmark = pytest.mark.usefixtures("live_capable_release")

PACK = "live-stop-fill"
OTHER_PACK = "someone-elses-pack"
H = 3_600_000
ENTRY_BAR = 2 * H
EXTERNAL = Decimal(100)

KRAKEN_KEY = "FAKE_KEY_FOR_KRAKEN_TESTS"
KRAKEN_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
COINBASE_KEY = "organizations/fake-org/apiKeys/fake-coinbase-key-001"
COINBASE_SECRET = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQGKiOPddAnxlf1S2y08ul1yymcJvx2UEhvzdIgBtA9vXA=="


def _candle(ts_ms: int, close: str) -> Candle:
    c = Decimal(close)
    return Candle(ts_ms=ts_ms, open=c, high=c + 1, low=c - 1, close=c, volume=Decimal(100))


# The pack enters when close crosses above sma2 and exits when it crosses below.
ENTRY_BARS = [_candle(0, "10"), _candle(H, "10"), _candle(ENTRY_BAR, "12")]


def _bars(*closes: str) -> list[Candle]:
    """Entry bars, then one bar per close. "13" and "14" hold; "10" after them exits."""
    return [*ENTRY_BARS, *(_candle((3 + i) * H, close) for i, close in enumerate(closes))]


def _coid(venue: str, pair: str, *, pack: str = PACK, intent: str = "entry") -> str:
    return run.coid_for(pack_id=pack, pack_version="1.0.0", venue=venue, pair=pair, bar_ts=ENTRY_BAR, intent=intent)


def _arm(home: Path, *, venue: str, pair: str) -> None:
    pack = {
        "schema_version": 1,
        "id": PACK,
        "version": "1.0.0",
        "label": "Live stop fill",
        "author": "krellbot tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 50}},
        "markets": [{"venue": venue, "pair": pair}],
    }
    pack_path = home / f"{PACK}.json"
    pack_path.write_text(json.dumps(pack), encoding="utf-8")
    armed = ArmedPack(
        pack_path=str(pack_path),
        pack_sha256="0" * 64,
        pack_id=PACK,
        pack_version="1.0.0",
        venue=venue,
        pair=pair,
        cap=Decimal(100),
        stop=Decimal(5),
        mode="live",
        starting_cash=Decimal(100),
        requires_license=False,
        armed_at_ts=1,
    )
    save_config(home, Config(armed=[armed]))


# ---- Kraken --------------------------------------------------------------------------

KRAKEN_PAIR = "SUIUSD"
ENTRY_TXID = "OENTRY-AAAAA-BBBBBB"
STOP_TXID = "OSTOP-CCCCC-DDDDDD"


class _KrakenBook(FakeKrakenTransport):
    """The Kraken fake plus documented ``TradesHistory`` and ``QueryOrders`` reads.

    ``trades`` are TradesHistory trade-info rows keyed by trade id. ``orders``
    are Query Orders Info rows keyed by txid; a txid it lacks is not returned.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.trades: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}

    def post(self, url: str, form: dict[str, str], headers: dict[str, str]) -> dict:
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "TradesHistory":
            self.calls.append(KrakenCall(url=url, form=dict(form), headers=dict(headers)))
            return {"error": [], "result": {"trades": dict(self.trades), "count": len(self.trades)}}
        if endpoint == "QueryOrders":
            self.calls.append(KrakenCall(url=url, form=dict(form), headers=dict(headers)))
            asked = [txid for txid in str(form.get("txid", "")).split(",") if txid]
            return {"error": [], "result": {txid: self.orders[txid] for txid in asked if txid in self.orders}}
        return super().post(url, form, headers)


def _kraken_trade(ordertxid: str, vol: Decimal, *, side: str = "sell", ordertype: str = "stop-loss") -> dict:
    """One documented TradesHistory trade-info row."""
    return {
        "ordertxid": ordertxid,
        "postxid": "TKH2SE-M7IF5-CFI7LT",
        "pair": KRAKEN_PAIR,
        "time": 1_700_000_100.25,
        "type": side,
        "ordertype": ordertype,
        "price": "6.0000",
        "cost": str(vol * 6),
        "fee": "0.01",
        "vol": str(vol),
        "margin": "0.00000",
        "misc": "",
        "trade_id": 4_242,
        "maker": False,
    }


def _kraken_order(vol: Decimal, *, side: str = "sell", ordertype: str = "stop-loss", **kw) -> dict:
    """One documented Query Orders Info row for an order that executed ``vol``."""
    return order_info(
        vol=str(vol), vol_exec=str(vol), status="closed", pair=KRAKEN_PAIR, side=side, ordertype=ordertype, **kw
    )


class _KrakenLive:
    venue = "kraken"
    pair = KRAKEN_PAIR
    base = "SUI"
    other_venue = "coinbase"

    def __init__(self, home: Path) -> None:
        self.home = home
        self.entry_coid = _coid(self.venue, self.pair)
        self.transport = _KrakenBook(
            responses=[{"error": [], "result": {"descr": {"order": "buy SUIUSD @ market"}, "txid": [ENTRY_TXID]}}],
            asset_pairs={self.pair: {"ordermin": "0.01", "costmin": "0.5", "lot_decimals": 5, "pair_decimals": 4}},
            balances={"USD": "100"},
        )
        self.qty = Decimal(0)

    def make_venue(self) -> KrakenVenue:
        return KrakenVenue(
            KRAKEN_KEY,
            KRAKEN_SECRET,
            self.transport,
            now_ms=lambda: 1_700_000_000_000,
            min_interval_ms=0,
            home=self.home,
        )

    def tick(self, bars: list[Candle], venue: KrakenVenue | None = None) -> None:
        rc = run.tick(
            venue=self.venue,
            venue_obj=venue or self.make_venue(),
            reader=lambda *_: bars,
            home=self.home,
            clock=lambda: 1_700_000_000,
        )
        assert rc == 0

    def _entry_row(self) -> dict:
        return order_info(
            vol=str(self.qty),
            vol_exec=str(self.qty),
            status="closed",
            pair=self.pair,
            cl_ord_id=self.entry_coid,
            userref=coid_userref(self.entry_coid),
        )

    def _stop_row(self, qty: Decimal, *, executed: bool) -> dict:
        # The conditional close Kraken created from the entry: it names the entry
        # only through ``refid``. Its userref is left at 0 so nothing relies on it.
        return order_info(
            vol=str(qty),
            vol_exec=str(qty if executed else 0),
            status="closed" if executed else "open",
            pair=self.pair,
            side="sell",
            ordertype="stop-loss",
            refid=ENTRY_TXID,
            stopprice="6.0000",
        )

    def enter(self) -> Decimal:
        """Send the entry, then reconcile it from Kraken's record with its stop resting."""
        self.tick(ENTRY_BARS)
        [add] = self._adds()
        self.qty = Decimal(add.form["volume"])
        self.transport.orders[ENTRY_TXID] = self._entry_row()
        self.rest_stop(self.qty)
        self.transport.balances[self.base] = str(self.qty + EXTERNAL)
        self.tick(ENTRY_BARS)
        return self.qty

    def rest_stop(self, qty: Decimal) -> None:
        stop = self._stop_row(qty, executed=False)
        self.transport.orders[STOP_TXID] = stop
        self.transport.set_order_books(open_rows={STOP_TXID: stop}, closed_rows={ENTRY_TXID: self._entry_row()})

    def stop_fills(self, qty: Decimal, *, pieces: int = 1) -> None:
        """The protective stop executes ``qty`` in ``pieces`` trades; external units stay."""
        stop = self._stop_row(qty, executed=True)
        self.transport.orders[STOP_TXID] = stop
        self.transport.set_order_books(closed_rows={ENTRY_TXID: self._entry_row(), STOP_TXID: stop})
        piece = (qty / pieces).quantize(Decimal("0.00001"))
        for i in range(pieces):
            vol = qty - piece * (pieces - 1) if i == pieces - 1 else piece
            self.transport.trades[f"TSTOP{i}-AAAAA-BBBBBB"] = _kraken_trade(STOP_TXID, vol)
        self.transport.balances[self.base] = str(EXTERNAL)

    def unlinkable_sell(self, qty: Decimal, *, stop_rests: bool) -> None:
        """A sell stop order with no ``cl_ord_id`` and no ``refid`` executes ``qty``.

        It may be this entry's stop after an EditOrder (which documents that
        userref is not retained) or an external stop: documented fields
        cannot tell.
        """
        self.transport.orders["OEDITED-EEEEE-FFFFFF"] = _kraken_order(qty)
        self.transport.trades["TUNLINK-AAAAA-BBBBBB"] = _kraken_trade("OEDITED-EEEEE-FFFFFF", qty)
        if stop_rests:
            self.rest_stop(self.qty)
        else:
            self.transport.set_order_books(closed_rows={ENTRY_TXID: self._entry_row()})
        self.transport.balances[self.base] = str(self.qty + EXTERNAL - qty)

    def foreign_sells(self) -> None:
        """An external market sell, another pack's stop and another client order's sell."""
        other_entry = _coid(self.venue, self.pair, pack=OTHER_PACK)
        self.transport.orders.update(
            {
                "OMANUAL-11111-111111": _kraken_order(Decimal(3), ordertype="market"),
                "OOTHERSTOP-2222-22222": _kraken_order(Decimal(2), refid="OOTHERENTRY-333-3333"),
                "OOTHERENTRY-333-3333": _kraken_order(
                    Decimal(2), side="buy", ordertype="market", cl_ord_id=other_entry
                ),
                "OCLIENT-44444-444444": _kraken_order(Decimal(1), ordertype="limit", cl_ord_id="their-own-order"),
            }
        )
        self.transport.trades.update(
            {
                "TMANUAL-AAAAA-AAAAAA": _kraken_trade("OMANUAL-11111-111111", Decimal(3), ordertype="market"),
                "TOTHER-BBBBB-BBBBBB": _kraken_trade("OOTHERSTOP-2222-22222", Decimal(2)),
                "TCLIENT-CCCCC-CCCCCC": _kraken_trade("OCLIENT-44444-444444", Decimal(1), ordertype="limit"),
            }
        )
        self.transport.balances[self.base] = str(self.qty + EXTERNAL - 6)

    def _adds(self) -> list:
        return [c for c in self.transport.calls if c.url.endswith("/AddOrder")]

    def sells(self) -> list[Decimal]:
        return [Decimal(c.form["volume"]) for c in self._adds() if c.form.get("type") == "sell"]


# ---- Coinbase ------------------------------------------------------------------------

COINBASE_PAIR = "BTC-USD"
COINBASE_PRODUCT = {
    "product_id": COINBASE_PAIR,
    "base_increment": "0.00000001",
    "quote_increment": "0.01",
    "base_min_size": "0.00000001",
    "quote_min_size": "1",
    "price": "12",
}


class _CoinbaseBook(FakeCoinbaseTransport):
    """The Coinbase fake with order history, fills and balances that tests move.

    Every List Orders call returns ``order_rows`` (the adapter matches rows
    itself); List Fills returns ``fill_rows``.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.order_rows: list[dict] = []
        self.fill_rows: list[dict] = []
        self.balances: dict[str, Decimal] = {"USD": Decimal(1000)}

    def get(self, url: str, headers: dict[str, str]) -> dict:
        if "orders/historical/batch" in url:
            self.calls.append(CoinbaseCall(method="GET", url=url, headers=dict(headers)))
            return {"orders": [dict(row) for row in self.order_rows], "has_next": False}
        if "orders/historical/fills" in url:
            self.calls.append(CoinbaseCall(method="GET", url=url, headers=dict(headers)))
            return {"fills": [dict(row) for row in self.fill_rows], "cursor": ""}
        if "/brokerage/accounts" in url:
            self.calls.append(CoinbaseCall(method="GET", url=url, headers=dict(headers)))
            return {
                "accounts": [
                    {"currency": asset, "available_balance": {"value": str(value), "currency": asset}}
                    for asset, value in self.balances.items()
                ]
            }
        return super().get(url, headers)


def _coinbase_fill(entry_id: str, order_id: str, size: Decimal, *, side: str = "SELL") -> dict:
    """One documented List Fills ``fills[]`` row: it names its order by ``order_id`` only."""
    return {
        "entry_id": entry_id,
        "trade_id": f"trade-{entry_id}",
        "order_id": order_id,
        "trade_time": "2026-11-26T10:00:00Z",
        "trade_type": "FILL",
        "price": "6",
        "size": str(size),
        "commission": "0.01",
        "product_id": COINBASE_PAIR,
        "sequence_timestamp": "2026-11-26T10:00:00.000001Z",
        "liquidity_indicator": "TAKER",
        "size_in_quote": False,
        "user_id": "docs-user",
        "side": side,
        "retail_portfolio_id": "docs-portfolio",
    }


def _coinbase_row(
    client_order_id: str, size: Decimal, *, status: str = "FILLED", stop: bool = True, side: str = "SELL"
) -> dict:
    """One documented List Orders row; a stop-limit GTC sell unless ``stop`` is False."""
    return order_record(
        client_order_id=client_order_id,
        status=status,
        filled_size=str(size if status == "FILLED" else 0),
        base_size=str(size),
        product_id=COINBASE_PAIR,
        side=side,
        stop_price="6" if stop else None,
    )


class _CoinbaseLive:
    venue = "coinbase"
    pair = COINBASE_PAIR
    base = "BTC"
    other_venue = "kraken"

    def __init__(self, home: Path) -> None:
        self.home = home
        self.entry_coid = _coid(self.venue, self.pair)
        self.stop_coid = self.entry_coid + "_stop"
        self.transport = _CoinbaseBook(
            post_responses=[
                {
                    "success": True,
                    "success_response": {
                        "order_id": f"order-{self.entry_coid}",
                        "product_id": self.pair,
                        "side": "BUY",
                        "client_order_id": self.entry_coid,
                        "status": "FILLED",
                    },
                },
                {
                    "success": True,
                    "success_response": {
                        "order_id": f"order-{self.stop_coid}",
                        "product_id": self.pair,
                        "side": "SELL",
                        "client_order_id": self.stop_coid,
                    },
                },
            ],
            product=dict(COINBASE_PRODUCT),
        )
        self.qty = Decimal(0)

    def make_venue(self) -> CoinbaseVenue:
        return CoinbaseVenue(
            COINBASE_KEY,
            COINBASE_SECRET,
            self.transport,
            product_id=self.pair,
            now_fn=lambda: 1_700_000_000,
            nonce_fn=lambda: "aa" * 16,
        )

    def tick(self, bars: list[Candle], venue: CoinbaseVenue | None = None) -> None:
        rc = run.tick(
            venue=self.venue,
            venue_obj=venue or self.make_venue(),
            reader=lambda *_: bars,
            home=self.home,
            clock=lambda: 1_700_000_000,
        )
        assert rc == 0

    def _entry_row(self) -> dict:
        return _coinbase_row(self.entry_coid, self.qty, stop=False, side="BUY")

    def enter(self) -> Decimal:
        """Send the IOC entry (filled at once) and its protective stop."""
        self.tick(ENTRY_BARS)
        buy, stop = (c for c in self.transport.calls if c.method == "POST" and c.url.endswith("/orders"))
        assert stop.body["client_order_id"] == self.stop_coid
        self.qty = Decimal(buy.body["order_configuration"]["market_market_ioc"]["base_size"])
        self.rest_stop(self.qty)
        self.transport.balances[self.base] = self.qty + EXTERNAL
        return self.qty

    def rest_stop(self, qty: Decimal) -> None:
        self.transport.order_rows = [self._entry_row(), _coinbase_row(self.stop_coid, qty, status="OPEN")]

    def stop_fills(self, qty: Decimal, *, pieces: int = 1) -> None:
        self.transport.order_rows = [self._entry_row(), _coinbase_row(self.stop_coid, qty)]
        piece = (qty / pieces).quantize(Decimal("0.00000001"))
        for i in range(pieces):
            size = qty - piece * (pieces - 1) if i == pieces - 1 else piece
            self.transport.fill_rows.append(_coinbase_fill(f"fill-stop-{i}", f"order-{self.stop_coid}", size))
        self.transport.balances[self.base] = EXTERNAL

    def unlinkable_sell(self, qty: Decimal, *, stop_rests: bool) -> None:
        """A sell whose order the bounded order lookup does not show."""
        if stop_rests:
            self.rest_stop(self.qty)
        else:
            self.transport.order_rows = [self._entry_row(), _coinbase_row(self.stop_coid, self.qty, status="CANCELLED")]
        self.transport.fill_rows.append(_coinbase_fill("fill-unshown", "order-not-in-history", qty))
        self.transport.balances[self.base] = self.qty + EXTERNAL - qty

    def foreign_sells(self) -> None:
        other_stop = _coid(self.venue, self.pair, pack=OTHER_PACK) + "_stop"
        self.transport.order_rows = [
            *self.transport.order_rows,
            _coinbase_row("manual-0f6c", Decimal(3), stop=False),
            _coinbase_row(other_stop, Decimal(2)),
        ]
        self.transport.fill_rows += [
            _coinbase_fill("fill-manual", "order-manual-0f6c", Decimal(3)),
            _coinbase_fill("fill-other", f"order-{other_stop}", Decimal(2)),
        ]
        self.transport.balances[self.base] = self.qty + EXTERNAL - 5

    def sells(self) -> list[Decimal]:
        return [
            Decimal(c.body["order_configuration"]["market_market_ioc"]["base_size"])
            for c in self.transport.calls
            if c.method == "POST"
            and c.url.endswith("/orders")
            and c.body.get("side") == "SELL"
            and "market_market_ioc" in c.body.get("order_configuration", {})
        ]


# ---- shared ----------------------------------------------------------------------------


@pytest.fixture(params=["kraken", "coinbase"])
def live(request, home, monkeypatch):
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    cls = {"kraken": _KrakenLive, "coinbase": _CoinbaseLive}[request.param]
    write_grant(home, venue=cls.venue, pair=cls.pair)
    _arm(home, venue=cls.venue, pair=cls.pair)
    return cls(home)


def _identity(live) -> OwnershipIdentity:
    return OwnershipIdentity(pack_id=PACK, venue=live.venue, pair=live.pair, mode="live")


def _owned(live) -> Decimal:
    return project_ownership(live.home, _identity(live)).attributable_qty


def _records(live) -> list[dict]:
    return [
        r["detail"]
        for r in kb_journal.read_records_lenient(live.home)
        if r.get("kind") == "tick" and r.get("pack") == PACK and isinstance(r.get("detail"), dict)
    ]


def _bar_record(live, bar_ts: int) -> dict:
    [record] = [
        r["detail"]
        for r in kb_journal.read_records_lenient(live.home)
        if r.get("kind") == "tick"
        and r.get("bar_ts") == bar_ts
        and (r.get("detail") or {}).get("reason") != run.RECONCILED_REASON
    ]
    return record


def _holds(live) -> list:
    return [o for o in read_obligations(live.home).unresolved if o.kind == run.UNLINKED_SELL_KIND]


def _marked(live) -> list[str]:
    rows = OperationalStore(live.home / "ops.sqlite").read_ledger()
    return [json.loads(payload)["coid"] for _id, kind, payload in rows if kind == "needs_reconcile"]


def _part(live, qty: Decimal) -> Decimal:
    return (qty / 4).quantize(Decimal("0.00001"))


# ---- attribution -----------------------------------------------------------------------


@pytest.mark.parametrize("pieces", [1, 2], ids=["one-fill", "two-fills"])
def test_live_stop_fill_is_journaled_once_against_the_entry_that_placed_it(live, pieces):
    qty = live.enter()
    assert _owned(live) == qty

    live.stop_fills(qty, pieces=pieces)
    live.tick(_bars("13"))

    record = _bar_record(live, 3 * H)
    assert Decimal(record["stop_qty"]) == qty
    assert len(record["stop_fill_ids"]) == pieces
    assert Decimal(record["owned_qty_after"]) == 0
    assert _owned(live) == 0
    assert _holds(live) == []
    assert live.sells() == []  # neither a repair nor an exit sold anything


def test_repeated_snapshots_and_a_restart_do_not_count_the_stop_fill_again(live):
    qty = live.enter()
    live.stop_fills(qty)

    venue = live.make_venue()
    live.tick(_bars("13"), venue)
    live.tick(_bars("13", "14"), venue)  # the same adapter instance snapshots again
    live.tick(_bars("13", "14", "13"))  # a restart builds a new one

    accounted = [r for r in _records(live) if r.get("stop_fill_ids")]
    assert len(accounted) == 1
    assert sum(Decimal(r.get("stop_qty") or "0") for r in _records(live)) == qty
    assert _owned(live) == 0
    fills = live.make_venue().snapshot().recent_fills
    assert run._stop_fills_for(live.home, _identity(live), fills) == ([], Decimal(0))


def test_a_later_bar_never_sells_external_units_after_a_live_stop_out(live):
    qty = live.enter()
    live.stop_fills(qty)

    live.tick(_bars("13"))
    live.tick(_bars("13", "10"))  # the pack exits on this bar
    live.tick(_bars("13", "10", "9"))

    assert live.sells() == []
    assert _owned(live) == 0
    assert [Decimal(_bar_record(live, ts)["owned_qty_after"]) for ts in (3 * H, 4 * H, 5 * H)] == [0, 0, 0]


def test_external_and_other_strategies_sells_are_neither_attributed_nor_held(live):
    qty = live.enter()
    live.foreign_sells()

    live.tick(_bars("13"))

    record = _bar_record(live, 3 * H)
    assert Decimal(record["stop_qty"]) == 0
    assert "stop_fill_ids" not in record
    assert _owned(live) == qty
    assert _holds(live) == []
    assert live.sells() == []


# ---- unlinkable sells ------------------------------------------------------------------


def test_a_sell_on_the_pair_that_cannot_be_linked_holds_exits_and_stop_repair(live):
    qty = live.enter()
    sold = _part(live, qty)
    live.unlinkable_sell(sold, stop_rests=False)

    live.tick(_bars("13"))  # no resting stop: the repair would otherwise act
    live.tick(_bars("13", "10"))  # the pack exits on this bar
    live.tick(_bars("13", "10", "9"))

    assert live.sells() == []
    assert _owned(live) == qty  # nothing guessed
    [hold] = _holds(live)
    assert (hold.venue, hold.mode, hold.pair, hold.pack_id, hold.qty) == (live.venue, "live", live.pair, PACK, sold)
    assert _marked(live).count(hold.coid) == 1  # audited once, not per tick
    assert [_bar_record(live, ts).get("exits_held_by") for ts in (3 * H, 4 * H, 5 * H)] == [[hold.coid]] * 3
    unresolved = kb_operations.list_unresolved_orders(live.home, now=1_700_000_000)
    assert [row["client_order_id"] for row in unresolved] == [hold.coid]
    # KQ-02: the hold is an unresolved order on this venue and mode only.
    view = read_obligations(live.home)
    assert view.blocks_entries(venue=live.venue, mode="live")
    assert not view.blocks_entries(venue=live.venue, mode="paper")
    assert not view.blocks_entries(venue=live.other_venue, mode="live")


def test_a_venue_lookup_never_settles_an_unlinkable_sell(live):
    qty = live.enter()
    live.unlinkable_sell(_part(live, qty), stop_rests=True)
    live.tick(_bars("13"))
    [hold] = _holds(live)

    assert run.reconcile_intents(venue=live.venue, venue_obj=live.make_venue(), home=live.home) == "done"
    live.tick(_bars("13", "14"))

    assert [o.coid for o in _holds(live)] == [hold.coid]
    assert run.resolve_order_by_operator(coid=hold.coid, home=live.home).code == "no_venue_evidence"
    if live.venue == "kraken":  # no order lookup is ever made for the hold's coid
        refs = {c.form.get("userref") for c in live.transport.calls if "userref" in c.form and "type" not in c.form}
        assert str(coid_userref(hold.coid)) not in refs


def test_a_hold_whose_marker_was_lost_is_audited_again_once(live):
    qty = live.enter()
    live.unlinkable_sell(_part(live, qty), stop_rests=True)
    live.tick(_bars("13"))
    [hold] = _holds(live)
    store = OperationalStore(live.home / "ops.sqlite")
    with store.transaction() as conn:  # an interruption between the commit and its marker
        conn.execute("DELETE FROM ledger WHERE kind = 'needs_reconcile' AND payload LIKE ?", (f'%"{hold.coid}"%',))
    assert hold.coid not in _marked(live)

    live.tick(_bars("13", "14"))
    live.tick(_bars("13", "14", "13"))

    assert _marked(live).count(hold.coid) == 1
    assert [o.coid for o in _holds(live)] == [hold.coid]


@pytest.mark.parametrize("outcome", ["not_executed", "executed"])
def test_the_operator_resolve_releases_the_hold_and_exits_resume(live, outcome):
    qty = live.enter()
    sold = _part(live, qty)
    live.unlinkable_sell(sold, stop_rests=False)
    live.tick(_bars("13"))
    [hold] = _holds(live)

    assert run.resolve_order_by_operator(coid=hold.coid, home=live.home).code == "no_venue_evidence"
    extra = {"executed_qty": str(sold)} if outcome == "executed" else {}
    resolved = run.resolve_order_by_operator(
        coid=hold.coid, outcome=outcome, confirmation=hold.coid, home=live.home, **extra
    )

    assert (resolved.code, resolved.ok, resolved.attributed) == ("order_resolved", True, True)
    assert _holds(live) == []
    owned = qty - sold if outcome == "executed" else qty
    assert _owned(live) == owned
    live.rest_stop(owned)
    live.tick(_bars("13", "10"))  # the pack exits on this bar
    live.tick(_bars("13", "10", "9"))

    assert live.sells() == [owned]
    assert _holds(live) == []
    assert "exits_held_by" not in _bar_record(live, 4 * H)


def test_a_flat_strategy_is_not_held_by_a_sell_it_cannot_own(live):
    qty = live.enter()
    live.stop_fills(qty)
    live.tick(_bars("13"))
    assert _owned(live) == 0

    live.unlinkable_sell(_part(live, qty), stop_rests=False)
    live.tick(_bars("13", "14"))

    assert _holds(live) == []


# ---- the adapters ----------------------------------------------------------------------


def test_kraken_links_a_stop_fill_through_ordertxid_refid_and_cl_ord_id(home):
    transport = _KrakenBook(balances={"USD": "1"})
    coid = _coid("kraken", KRAKEN_PAIR)
    transport.orders = {
        STOP_TXID: _kraken_order(Decimal(2), refid=ENTRY_TXID),
        ENTRY_TXID: _kraken_order(Decimal(2), side="buy", ordertype="market", cl_ord_id=coid),
        "OEXIT-00000-000000": _kraken_order(Decimal(1), ordertype="market", cl_ord_id="exit-coid"),
        "OMANUAL-11111-111111": _kraken_order(Decimal(1), ordertype="market"),
        "OEDITED-EEEEE-FFFFFF": _kraken_order(Decimal(1)),
        "OORPHAN-22222-222222": _kraken_order(Decimal(1), refid="OMISSING-3333-33333"),
        "OANON-44444-444444": _kraken_order(Decimal(1), refid="OANONENTRY-55-5555"),
        "OANONENTRY-55-5555": _kraken_order(Decimal(1), side="buy", ordertype="market"),
    }
    transport.trades = {
        "T-STOP": _kraken_trade(STOP_TXID, Decimal(2)),
        "T-BUY": _kraken_trade(ENTRY_TXID, Decimal(2), side="buy", ordertype="market"),
        "T-EXIT": _kraken_trade("OEXIT-00000-000000", Decimal(1), ordertype="market"),
        "T-MANUAL": _kraken_trade("OMANUAL-11111-111111", Decimal(1), ordertype="market"),
        "T-EDITED": _kraken_trade("OEDITED-EEEEE-FFFFFF", Decimal(1)),
        "T-GONE": _kraken_trade("OGONE-66666-666666", Decimal(1)),
        "T-ORPHAN": _kraken_trade("OORPHAN-22222-222222", Decimal(1)),
        "T-ANON": _kraken_trade("OANON-44444-444444", Decimal(1)),
    }
    venue = KrakenVenue(KRAKEN_KEY, KRAKEN_SECRET, transport, now_ms=lambda: 1, min_interval_ms=0, home=home)

    fills = {f.id: f for f in venue.snapshot().recent_fills}

    assert {fid: (f.order_coid, f.order_unknown) for fid, f in fills.items()} == {
        "T-STOP": (coid, False),  # the entry's conditional close
        "T-BUY": (None, False),  # a buy is never a protective stop: not looked up
        "T-EXIT": ("exit-coid", False),  # an order that carries its own client order id
        "T-MANUAL": (None, False),  # a plain market sell is not a protective stop
        "T-EDITED": (None, True),  # a stop with no documented link
        "T-GONE": (None, True),  # QueryOrders does not show the order
        "T-ORPHAN": (None, True),  # the order that created it is not shown
        "T-ANON": (None, True),  # created by an order with no client order id
    }
    queries = [c for c in transport.calls if c.url.endswith("/QueryOrders")]
    asked = [txid for c in queries for txid in c.form["txid"].split(",")]
    assert all(len(c.form["txid"].split(",")) <= 50 for c in queries)
    assert len(asked) == len(set(asked))  # each order is read once
    # Every sell's order, then each order that created one with no client order id.
    assert set(asked) == {*transport.orders, "OGONE-66666-666666", "OMISSING-3333-33333"}

    venue.snapshot()  # an order once read is not read again by this instance
    assert [c for c in transport.calls if c.url.endswith("/QueryOrders")] == queries


def test_kraken_snapshot_without_sells_makes_no_extra_private_calls(home):
    transport = _KrakenBook(balances={"USD": "1"})
    transport.trades = {"T-BUY": _kraken_trade(ENTRY_TXID, Decimal(2), side="buy", ordertype="market")}
    venue = KrakenVenue(KRAKEN_KEY, KRAKEN_SECRET, transport, now_ms=lambda: 1, min_interval_ms=0, home=home)

    [fill] = venue.snapshot().recent_fills

    assert (fill.order_coid, fill.order_unknown) == (None, False)
    assert [c.url.rsplit("/", 1)[-1] for c in transport.calls] == ["Balance", "OpenOrders", "TradesHistory"]


def test_kraken_sell_that_names_no_order_is_unknown(home):
    transport = _KrakenBook(balances={"USD": "1"})
    transport.trades = {"T-NOORDER": _kraken_trade("", Decimal(1))}
    venue = KrakenVenue(KRAKEN_KEY, KRAKEN_SECRET, transport, now_ms=lambda: 1, min_interval_ms=0, home=home)

    [fill] = venue.snapshot().recent_fills

    assert (fill.order_coid, fill.order_unknown) == (None, True)
    assert not [c for c in transport.calls if c.url.endswith("/QueryOrders")]


def test_coinbase_links_a_stop_fill_through_order_id_and_client_order_id():
    transport = _CoinbaseBook(product=dict(COINBASE_PRODUCT))
    coid = _coid("coinbase", COINBASE_PAIR)
    transport.order_rows = [
        _coinbase_row(coid + "_stop", Decimal(2)),
        _coinbase_row(coid, Decimal(2), stop=False, side="BUY"),
        _coinbase_row("manual-0f6c", Decimal(1), stop=False),
    ]
    transport.fill_rows = [
        _coinbase_fill("f-stop", f"order-{coid}_stop", Decimal(2)),
        _coinbase_fill("f-buy", f"order-{coid}", Decimal(2), side="BUY"),
        _coinbase_fill("f-manual", "order-manual-0f6c", Decimal(1)),
        _coinbase_fill("f-unshown", "order-not-in-history", Decimal(1)),
    ]
    venue = CoinbaseVenue(COINBASE_KEY, COINBASE_SECRET, transport, now_fn=lambda: 1, nonce_fn=lambda: "aa" * 16)

    fills = {f.id: f for f in venue.snapshot().recent_fills}

    assert {fid: (f.side, f.order_coid, f.order_unknown) for fid, f in fills.items()} == {
        "f-stop": ("sell", coid, False),  # ``<entry coid>_stop`` names the entry
        "f-buy": ("buy", None, False),  # a buy is never a protective stop: not looked up
        "f-manual": ("sell", "manual-0f6c", False),
        "f-unshown": ("sell", None, True),
    }
    lookups = [c.url for c in transport.calls if "orders/historical/batch?order_ids=" in c.url]
    [lookup] = lookups
    asked = parse_qs(urlsplit(lookup).query)["order_ids"]
    assert sorted(asked) == sorted([f"order-{coid}_stop", "order-manual-0f6c", "order-not-in-history"])

    venue.snapshot()  # an order once read is not read again by this instance
    assert [c.url for c in transport.calls if "orders/historical/batch?order_ids=" in c.url] == lookups


def test_coinbase_fill_order_lookup_reads_at_most_five_pages():
    class _Endless(_CoinbaseBook):
        def get(self, url: str, headers: dict[str, str]) -> dict:
            if "orders/historical/batch" in url:
                self.calls.append(CoinbaseCall(method="GET", url=url, headers=dict(headers)))
                return {"orders": [], "has_next": True, "cursor": f"c{len(self.calls)}"}
            return super().get(url, headers)

    transport = _Endless(product=dict(COINBASE_PRODUCT))
    transport.fill_rows = [_coinbase_fill("f-1", "order-somewhere", Decimal(1))]
    venue = CoinbaseVenue(COINBASE_KEY, COINBASE_SECRET, transport, now_fn=lambda: 1, nonce_fn=lambda: "aa" * 16)

    [fill] = venue.snapshot().recent_fills

    assert (fill.order_coid, fill.order_unknown) == (None, True)
    assert len([c for c in transport.calls if "orders/historical/batch" in c.url and "order_ids=" in c.url]) == 5


# ---- paper -----------------------------------------------------------------------------


def test_paper_stop_out_is_still_accounted_once_and_never_held(home):
    from tests.test_paper_stop_out_is_accounted_once import IDENTITY, _enter_then_stop_out

    paper, bought = _enter_then_stop_out(home)

    stop_tick = paper.ticks()[-1]["detail"]
    assert Decimal(stop_tick["stop_qty"]) == bought
    assert "exits_held_by" not in stop_tick
    assert all(not f.order_unknown for f in paper.venue.snapshot().recent_fills)
    assert [o for o in read_obligations(home).unresolved if o.kind == run.UNLINKED_SELL_KIND] == []
    assert project_ownership(home, IDENTITY).attributable_qty == 0
