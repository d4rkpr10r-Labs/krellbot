"""A live position whose protective stop is missing gets a new stop, not a sale.

When a live strategy owns inventory and no stop rests for it, the tick rests a
new sell stop for the owned quantity through the venue's ``place_stop``:

* Kraken: AddOrder ``ordertype=stop-loss`` whose ``price`` is the trigger,
  carrying the stop coid as ``cl_ord_id`` and no ``userref`` (the two are
  mutually exclusive). The order is found again through the ``cl_ord_id``
  filter of Get Open Orders / Get Closed Orders.
* Coinbase: a ``stop_limit_stop_limit_gtc`` sell whose ``client_order_id``
  is the stop coid, found again by exact ``client_order_id``.

Nothing is sold. A stop whose reply was lost stays an obligation until the
venue's own record shows it resting (``open``), which retires it and lifts
the entry hold; its coid is never sent again, across ticks and restarts. When
the re-placed stop later fills, M28.24's fill linking names the stop coid, so
the decrease is journaled once.

A stop the venue rejects keeps the old exit fallback, but the sale is now
journaled only from evidence: a live acknowledgement is not an executed
quantity, so the exit stays an obligation until ``order_outcome`` reports
what executed. While it is unsettled the identity's exits and stop repair are
held, so the sale is not repeated every bar and external units are never
sold. An adapter without ``place_stop`` commits no stop intent and keeps the
exit fallback. Paper repair is unchanged.

Runs on fake transports with the release gate patched in-process
(``live_capable_release``); ``PAPER_ONLY_RELEASE`` stays True in source.
"""

from __future__ import annotations

import json
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest
from fakes.fake_coinbase import FakeCoinbaseTransport, order_record
from fakes.fake_coinbase import RecordedCall as CoinbaseCall
from fakes.fake_kraken import FakeKrakenTransport, order_info
from fakes.fake_kraken import RecordedCall as KrakenCall
from fakes.live_grant import write_grant

from krellbot import journal as kb_journal
from krellbot import run
from krellbot.config import ArmedPack, Config, save_config
from krellbot.execution import obligations as kb_obligations
from krellbot.execution.obligations import read_obligations
from krellbot.pack.model import Candle
from krellbot.run.reconcile import OwnershipIdentity, project_ownership
from krellbot.storage.database import OperationalStore
from krellbot.venues.base import Balance, KeyPerms, OrderOutcome, OrderRef, PairRules, Truth
from krellbot.venues.coinbase import CoinbaseVenue
from krellbot.venues.kraken import KrakenVenue, coid_userref
from krellbot.venues.paper import PaperVenue

PACK = "missing-stop"
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
    """Entry bars, then one bar per close: "13", "14", "15" hold flat; "10" after them exits."""
    return [*ENTRY_BARS, *(_candle((3 + i) * H, close) for i, close in enumerate(closes))]


def _coid(venue: str, pair: str, *, intent: str, bar_ts: int) -> str:
    return run.coid_for(pack_id=PACK, pack_version="1.0.0", venue=venue, pair=pair, bar_ts=bar_ts, intent=intent)


def _arm(home: Path, *, venue: str, pair: str, mode: str = "live") -> None:
    pack = {
        "schema_version": 1,
        "id": PACK,
        "version": "1.0.0",
        "label": "Missing stop",
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
        mode=mode,
        starting_cash=Decimal(1000) if mode == "paper" else Decimal(100),
        requires_license=False,
        armed_at_ts=1,
    )
    save_config(home, Config(armed=[armed]))


def _seed_owned(home: Path, *, venue: str, pair: str, qty: Decimal, mode: str = "live") -> None:
    """The strategy's entry on ENTRY_BAR, journaled as the engine journals it."""
    kb_journal.append(
        {
            "ts": 1,
            "kind": "tick",
            "venue": venue,
            "pack": PACK,
            "bar_ts": ENTRY_BAR,
            "detail": {
                "pair": pair,
                "mode": mode,
                "reason": "entry",
                "entry_qty": str(qty),
                "exit_qty": "0",
                "stop_qty": "0",
                "entry_coid": _coid(venue, pair, intent="entry", bar_ts=ENTRY_BAR),
            },
        }
    )


# ---- Kraken ----------------------------------------------------------------------------

KRAKEN_PAIR = "SUIUSD"


def _kraken_trade(ordertxid: str, vol: Decimal, *, ordertype: str = "stop-loss") -> dict:
    """One documented TradesHistory trade-info row for a sell."""
    return {
        "ordertxid": ordertxid,
        "postxid": "TKH2SE-M7IF5-CFI7LT",
        "pair": KRAKEN_PAIR,
        "time": 1_700_000_100.25,
        "type": "sell",
        "ordertype": ordertype,
        "price": "6.0000",
        "cost": str(vol * 6),
        "fee": "0.01",
        "vol": str(vol),
        "margin": "0.00000",
        "misc": "",
    }


class _KrakenExchange(FakeKrakenTransport):
    """A fake Kraken account that executes what it accepts.

    Orders are kept by txid. Get Open Orders and Get Closed Orders apply the
    documented ``userref`` or ``cl_ord_id`` filter, as Kraken does, so an
    order is only found by the reference it was placed with. AddOrder rests a
    ``stop-loss`` sell and executes a market sell at once (moving the balance
    and adding a TradesHistory trade). ``stop_reply`` makes Kraken reject a
    stop (``reject``) or accept it and lose the reply (``lose_ack``).
    ``exit_listed=False`` keeps an executed market sell out of the open and
    closed lists, so its outcome cannot be established until ``reveal``.
    ``exit_executes`` is the part of a market sell that executes.
    """

    def __init__(self, *, base: Decimal) -> None:
        super().__init__(
            asset_pairs={KRAKEN_PAIR: {"ordermin": "0.01", "costmin": "0.5", "lot_decimals": 5, "pair_decimals": 4}},
            balances={"USD": "100", "SUI": str(base)},
        )
        self.open_rows: dict[str, dict] = {}
        self.closed_rows: dict[str, dict] = {}
        self.unlisted_rows: dict[str, dict] = {}
        self.trades: dict[str, dict] = {}
        self.stop_reply = "accept"
        self.exit_listed = True
        self.exit_executes: Decimal | None = None
        self._txids = 0

    def post(self, url: str, form: dict[str, str], headers: dict[str, str]) -> dict:
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint not in {"OpenOrders", "ClosedOrders", "TradesHistory", "QueryOrders", "AddOrder"}:
            return super().post(url, form, headers)
        self.calls.append(KrakenCall(url=url, form=dict(form), headers=dict(headers)))
        if endpoint == "OpenOrders":
            return {"error": [], "result": {"open": self._filtered(self.open_rows, form)}}
        if endpoint == "ClosedOrders":
            rows = self._filtered(self.closed_rows, form)
            return {"error": [], "result": {"closed": rows, "count": len(rows)}}
        if endpoint == "TradesHistory":
            return {"error": [], "result": {"trades": dict(self.trades), "count": len(self.trades)}}
        if endpoint == "QueryOrders":
            known = {**self.open_rows, **self.closed_rows, **self.unlisted_rows}
            asked = [txid for txid in str(form.get("txid", "")).split(",") if txid]
            return {"error": [], "result": {txid: dict(known[txid]) for txid in asked if txid in known}}
        return self._add_order(form)

    @staticmethod
    def _filtered(rows: dict[str, dict], form: dict[str, str]) -> dict[str, dict]:
        if "cl_ord_id" in form:
            return {t: dict(r) for t, r in rows.items() if r.get("cl_ord_id") == form["cl_ord_id"]}
        if "userref" in form:
            return {t: dict(r) for t, r in rows.items() if int(r.get("userref") or 0) == int(form["userref"])}
        return {t: dict(r) for t, r in rows.items()}

    def _add_order(self, form: dict[str, str]) -> dict:
        self._txids += 1
        txid = f"OADD{self._txids:02d}-AAAAA-BBBBBB"
        ids = {"cl_ord_id": form.get("cl_ord_id"), "userref": int(form.get("userref") or 0)}
        if form.get("ordertype") == "stop-loss":
            if self.stop_reply == "reject":
                return {"error": ["EOrder:Invalid price"]}
            self.open_rows[txid] = order_info(
                vol=form["volume"],
                vol_exec="0.00000000",
                status="open",
                pair=form["pair"],
                side="sell",
                ordertype="stop-loss",
                stopprice=form["price"],
                **ids,
            )
            if self.stop_reply == "lose_ack":
                raise OSError("connection reset after Kraken accepted the order")
        else:
            executed = Decimal(form["volume"]) if self.exit_executes is None else self.exit_executes
            row = order_info(
                vol=form["volume"],
                vol_exec=str(executed),
                status="closed" if executed == Decimal(form["volume"]) else "canceled",
                pair=form["pair"],
                side=form["type"],
                ordertype="market",
                **ids,
            )
            (self.closed_rows if self.exit_listed else self.unlisted_rows)[txid] = row
            self.trades[f"T{txid}"] = _kraken_trade(txid, executed, ordertype="market")
            self.balances["SUI"] = str(Decimal(self.balances["SUI"]) - executed)
        return {"error": [], "result": {"descr": {"order": f"{form['type']} {form['volume']}"}, "txid": [txid]}}

    def execute_stop(self) -> None:
        [(txid, row)] = [(t, r) for t, r in self.open_rows.items() if r["descr"]["ordertype"] == "stop-loss"]
        del self.open_rows[txid]
        self.closed_rows[txid] = {**row, "status": "closed", "vol_exec": row["vol"]}
        self.trades[f"T{txid}"] = _kraken_trade(txid, Decimal(row["vol"]))
        self.balances["SUI"] = str(Decimal(self.balances["SUI"]) - Decimal(row["vol"]))

    def reveal(self) -> None:
        self.closed_rows.update(self.unlisted_rows)
        self.unlisted_rows.clear()


class _Clock:
    """Epoch-microsecond clock that only moves on ``sleep``: the 1 s spacing never waits."""

    def __init__(self) -> None:
        self.us = 1_700_000_000_000_000
        self.sleeps: list[float] = []

    def now(self) -> int:
        return self.us

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.us += round(seconds * 1_000_000)


class _KrakenLive:
    venue = "kraken"
    pair = KRAKEN_PAIR
    qty = Decimal(5)

    def __init__(self, home: Path) -> None:
        self.home = home
        self.exchange = _KrakenExchange(base=self.qty + EXTERNAL)
        self.clock = _Clock()

    def make_venue(self) -> KrakenVenue:
        return KrakenVenue(
            KRAKEN_KEY, KRAKEN_SECRET, self.exchange, now_ms=self.clock.now, home=self.home, sleep=self.clock.sleep
        )

    def _adds(self) -> list[dict]:
        return [c.form for c in self.exchange.calls if c.url.endswith("/AddOrder")]

    def stop_coids(self) -> list[str]:
        return [form["cl_ord_id"] for form in self._adds() if form["ordertype"] == "stop-loss"]

    def sells(self) -> list[Decimal]:
        return [Decimal(form["volume"]) for form in self._adds() if form["ordertype"] == "market"]

    def base_balance(self) -> Decimal:
        return Decimal(self.exchange.balances["SUI"])

    def assert_stop_request(self, coid: str, qty: Decimal, stop: Decimal) -> None:
        [form] = [f for f in self._adds() if f.get("cl_ord_id") == coid]
        sent = {k: v for k, v in form.items() if k != "nonce"}
        assert sent == {
            "pair": KRAKEN_PAIR,
            "type": "sell",
            "ordertype": "stop-loss",
            "price": format(stop.quantize(Decimal("0.0001")), "f"),
            "volume": format(qty.quantize(Decimal("0.00001")), "f"),
            "cl_ord_id": coid,
        }
        # Looked up before the send by the documented cl_ord_id filter only.
        lookups = [c.form for c in self.exchange.calls if c.url.endswith(("/OpenOrders", "/ClosedOrders"))]
        assert {"cl_ord_id": coid} in [{k: v for k, v in f.items() if k != "nonce"} for f in lookups]
        assert str(coid_userref(coid)) not in {f.get("userref") for f in lookups}

    def execute_stop(self) -> None:
        self.exchange.execute_stop()


# ---- Coinbase --------------------------------------------------------------------------

COINBASE_PAIR = "BTC-USD"
COINBASE_PRODUCT = {
    "product_id": COINBASE_PAIR,
    "base_increment": "0.00000001",
    "quote_increment": "0.01",
    "base_min_size": "0.00000001",
    "quote_min_size": "1",
    "price": "13",
}


def _coinbase_fill(entry_id: str, order_id: str, size: Decimal) -> dict:
    """One documented List Fills ``fills[]`` row for a sell: it names its order by ``order_id``."""
    return {
        "entry_id": entry_id,
        "trade_id": f"trade-{entry_id}",
        "order_id": order_id,
        "trade_time": "2026-11-26T10:00:00Z",
        "trade_type": "FILL",
        "price": "6.5",
        "size": str(size),
        "commission": "0.01",
        "product_id": COINBASE_PAIR,
        "side": "SELL",
    }


class _CoinbaseExchange(FakeCoinbaseTransport):
    """A fake Coinbase account that executes what it accepts.

    List Orders returns every recorded order (the adapter matches rows
    itself) and List Fills the recorded fills. Create Order rests a
    stop-limit GTC sell as ``OPEN`` and executes a market IOC sell at once.
    ``stop_reply``, ``exit_listed`` and ``exit_executes`` are as for Kraken.
    """

    def __init__(self, *, base: Decimal) -> None:
        super().__init__(product=dict(COINBASE_PRODUCT))
        self.order_rows: list[dict] = []
        self.unlisted_rows: list[dict] = []
        self.fill_rows: list[dict] = []
        self.balances = {"USD": Decimal(1000), "BTC": base}
        self.stop_reply = "accept"
        self.exit_listed = True
        self.exit_executes: Decimal | None = None

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

    def post(self, url: str, body: dict, headers: dict[str, str]) -> dict:
        if not url.endswith("/api/v3/brokerage/orders"):
            return super().post(url, body, headers)
        self.calls.append(
            CoinbaseCall(method="POST", url=url, headers=dict(headers), body=json.loads(json.dumps(body)))
        )
        coid = body["client_order_id"]
        config = body["order_configuration"]
        if "stop_limit_stop_limit_gtc" in config:
            if self.stop_reply == "reject":
                return {
                    "success": False,
                    "failure_reason": "UNKNOWN_FAILURE_REASON",
                    "error_response": {
                        "error": "INVALID_LIMIT_PRICE",
                        "message": "Invalid limit price",
                        "error_details": "",
                        "preview_failure_reason": "PREVIEW_INVALID_LIMIT_PRICE",
                    },
                }
            box = config["stop_limit_stop_limit_gtc"]
            row = order_record(
                client_order_id=coid,
                status="OPEN",
                filled_size="0",
                base_size=box["base_size"],
                product_id=COINBASE_PAIR,
                side="SELL",
                stop_price=box["stop_price"],
            )
            self.order_rows.append(row)
            if self.stop_reply == "lose_ack":
                raise OSError("connection reset after Coinbase accepted the order")
        else:
            size = Decimal(config["market_market_ioc"]["base_size"])
            executed = size if self.exit_executes is None else self.exit_executes
            row = order_record(
                client_order_id=coid,
                status="FILLED" if executed == size else "CANCELLED",
                filled_size=str(executed),
                base_size=str(size),
                product_id=COINBASE_PAIR,
                side=body["side"],
            )
            (self.order_rows if self.exit_listed else self.unlisted_rows).append(row)
            if self.exit_listed:
                self.fill_rows.append(_coinbase_fill(f"fill-{coid}", row["order_id"], executed))
            self.balances["BTC"] -= executed
        return {
            "success": True,
            "success_response": {
                "order_id": row["order_id"],
                "product_id": COINBASE_PAIR,
                "side": body["side"],
                "client_order_id": coid,
            },
        }

    def execute_stop(self) -> None:
        [row] = [r for r in self.order_rows if r["status"] == "OPEN"]
        size = Decimal(row["order_configuration"]["stop_limit_stop_limit_gtc"]["base_size"])
        row.update(status="FILLED", filled_size=str(size))
        self.fill_rows.append(_coinbase_fill(f"fill-stop-{row['client_order_id']}", row["order_id"], size))
        self.balances["BTC"] -= size

    def reveal(self) -> None:
        for row in self.unlisted_rows:
            self.order_rows.append(row)
            self.fill_rows.append(_coinbase_fill(f"fill-{row['client_order_id']}", row["order_id"], row["filled_size"]))
        self.unlisted_rows.clear()


class _CoinbaseLive:
    venue = "coinbase"
    pair = COINBASE_PAIR
    qty = Decimal("0.5")

    def __init__(self, home: Path) -> None:
        self.home = home
        self.exchange = _CoinbaseExchange(base=self.qty + EXTERNAL)

    def make_venue(self) -> CoinbaseVenue:
        return CoinbaseVenue(
            COINBASE_KEY,
            COINBASE_SECRET,
            self.exchange,
            product_id=self.pair,
            now_fn=lambda: 1_700_000_000,
            nonce_fn=lambda: "aa" * 16,
        )

    def _orders(self) -> list[dict]:
        return [c.body for c in self.exchange.calls if c.method == "POST" and c.url.endswith("/orders")]

    def stop_coids(self) -> list[str]:
        return [b["client_order_id"] for b in self._orders() if "stop_limit_stop_limit_gtc" in b["order_configuration"]]

    def sells(self) -> list[Decimal]:
        return [
            Decimal(b["order_configuration"]["market_market_ioc"]["base_size"])
            for b in self._orders()
            if b["side"] == "SELL" and "market_market_ioc" in b["order_configuration"]
        ]

    def base_balance(self) -> Decimal:
        return self.exchange.balances["BTC"]

    def assert_stop_request(self, coid: str, qty: Decimal, stop: Decimal) -> None:
        [body] = [b for b in self._orders() if b["client_order_id"] == coid]
        cent = Decimal("0.01")
        assert body == {
            "client_order_id": coid,
            "product_id": COINBASE_PAIR,
            "side": "SELL",
            "order_configuration": {
                "stop_limit_stop_limit_gtc": {
                    "base_size": format(qty.quantize(Decimal("0.00000001")), "f"),
                    "stop_price": format(stop.quantize(cent), "f"),
                    # 0.5% under the stop, as for the entry's protective stop.
                    "limit_price": format((stop * Decimal("0.995")).quantize(cent, rounding=ROUND_DOWN), "f"),
                    "stop_direction": "STOP_DIRECTION_STOP_DOWN",
                }
            },
        }

    def execute_stop(self) -> None:
        self.exchange.execute_stop()


# ---- shared ----------------------------------------------------------------------------


@pytest.fixture(params=["kraken", "coinbase"])
def live(request, home, monkeypatch, live_capable_release):
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    cls = {"kraken": _KrakenLive, "coinbase": _CoinbaseLive}[request.param]
    write_grant(home, venue=cls.venue, pair=cls.pair)
    _arm(home, venue=cls.venue, pair=cls.pair)
    _seed_owned(home, venue=cls.venue, pair=cls.pair, qty=cls.qty)
    return cls(home)


def _tick(live, bars: list[Candle], venue=None) -> int:
    return run.tick(
        venue=live.venue,
        venue_obj=venue or live.make_venue(),
        reader=lambda *_: bars,
        home=live.home,
        clock=lambda: 1_700_000_000,
    )


def _identity(live) -> OwnershipIdentity:
    return OwnershipIdentity(pack_id=PACK, venue=live.venue, pair=live.pair, mode="live")


def _owned(live) -> Decimal:
    return project_ownership(live.home, _identity(live)).attributable_qty


def _records(home: Path) -> list[dict]:
    return [
        r["detail"]
        for r in kb_journal.read_records_lenient(home)
        if r.get("kind") == "tick" and r.get("pack") == PACK and isinstance(r.get("detail"), dict)
    ]


def _bar_record(home: Path, bar_ts: int) -> dict:
    """The tick's own record for ``bar_ts`` (not a reconcile record)."""
    [record] = [
        r["detail"]
        for r in kb_journal.read_records_lenient(home)
        if r.get("kind") == "tick"
        and r.get("bar_ts") == bar_ts
        and (r.get("detail") or {}).get("reason") != run.RECONCILED_REASON
    ]
    return record


def _ledger(home: Path) -> list[tuple[str, dict]]:
    return [(kind, json.loads(payload)) for _id, kind, payload in OperationalStore(home / "ops.sqlite").read_ledger()]


def _resolutions(home: Path) -> list[dict]:
    return [payload for kind, payload in _ledger(home) if kind == kb_obligations.RESOLVED_KIND]


def _intent_kinds(home: Path) -> list[str]:
    return [json.loads(p["body"])["kind"] for kind, p in _ledger(home) if kind == "outbox"]


def _unresolved(home: Path) -> dict[str, str | None]:
    return {o.coid: o.kind for o in read_obligations(home).unresolved}


# ---- a missing stop is re-placed --------------------------------------------------------


def test_a_missing_live_stop_is_replaced_and_nothing_is_sold(live):
    assert _tick(live, _bars("13")) == 0

    stop_coid = _coid(live.venue, live.pair, intent="stop", bar_ts=3 * H)
    assert live.stop_coids() == [stop_coid]
    live.assert_stop_request(stop_coid, live.qty, Decimal("6.5"))  # half of the 13 close
    assert live.sells() == []
    record = _bar_record(live.home, 3 * H)
    assert record["ensure_stop_failed"] is False
    assert (record["entry_coid"], record["no_double_order"]) == (stop_coid, True)
    assert (record["exit_qty"], Decimal(record["owned_qty_after"])) == ("0", live.qty)
    assert _owned(live) == live.qty
    assert _unresolved(live.home) == {}  # an acknowledged stop holds nothing
    assert live.base_balance() == live.qty + EXTERNAL


def test_on_a_bar_the_pack_exits_no_stop_is_rested_just_to_be_cancelled(live):
    assert _tick(live, _bars("10")) == 0  # the pack's own exit sells what it owns

    assert live.stop_coids() == []  # a stop that triggered at once would sell the same units twice
    assert live.sells() == [live.qty]
    assert live.base_balance() == EXTERNAL
    assert _bar_record(live.home, 3 * H)["ensure_stop_failed"] is False


def test_a_resting_replaced_stop_is_never_sent_again_across_ticks_and_restarts(live):
    venue = live.make_venue()
    assert _tick(live, _bars("13"), venue) == 0
    assert _tick(live, _bars("13", "14"), venue) == 0  # the same adapter instance
    assert _tick(live, _bars("13", "14", "15")) == 0  # a restart builds a new one

    assert live.stop_coids() == [_coid(live.venue, live.pair, intent="stop", bar_ts=3 * H)]
    assert live.sells() == []
    assert _owned(live) == live.qty
    assert all(_bar_record(live.home, ts)["ensure_stop_failed"] is False for ts in (3 * H, 4 * H, 5 * H))


def test_a_stop_whose_reply_was_lost_is_confirmed_resting_and_lifts_the_entry_hold(live):
    stop_coid = _coid(live.venue, live.pair, intent="stop", bar_ts=3 * H)
    live.exchange.stop_reply = "lose_ack"
    with pytest.raises(OSError):
        _tick(live, _bars("13"))

    # The venue holds the stop, but the run cannot know that yet.
    assert _unresolved(live.home) == {stop_coid: "stop"}
    assert read_obligations(live.home).blocks_entries(venue=live.venue, mode="live")
    assert live.sells() == []

    live.exchange.stop_reply = "accept"
    assert _tick(live, _bars("13")) == 0  # a restart replays the bar
    assert _tick(live, _bars("13", "14")) == 0
    assert _tick(live, _bars("13", "14", "15")) == 0

    assert live.stop_coids() == [stop_coid]  # sent once, never again
    assert live.sells() == []
    assert _unresolved(live.home) == {}
    assert not read_obligations(live.home).blocks_entries(venue=live.venue, mode="live")
    assert _resolutions(live.home) == [
        {"coid": stop_coid, "outcome": "open", "filled_qty": "0", "evidence": "protective_stop_resting"}
    ]
    assert _owned(live) == live.qty
    assert _bar_record(live.home, 3 * H)["ensure_stop_failed"] is False


@pytest.mark.parametrize("reply", ["accept", "lose_ack"], ids=["acknowledged", "confirmed-after-lost-reply"])
def test_a_replaced_stop_that_fills_is_credited_once(live, reply):
    stop_coid = _coid(live.venue, live.pair, intent="stop", bar_ts=3 * H)
    if reply == "lose_ack":
        live.exchange.stop_reply = "lose_ack"
        with pytest.raises(OSError):
            _tick(live, _bars("13"))
        live.exchange.stop_reply = "accept"
    assert _tick(live, _bars("13")) == 0
    live.execute_stop()

    venue = live.make_venue()
    assert _tick(live, _bars("13", "14"), venue) == 0
    assert _tick(live, _bars("13", "14", "15"), venue) == 0  # the same adapter snapshots again
    assert _tick(live, _bars("13", "14", "15", "10")) == 0  # a restart, on a bar the pack exits

    [credited] = [r for r in _records(live.home) if r.get("stop_fill_ids")]
    assert Decimal(credited["stop_qty"]) == live.qty
    assert sum(Decimal(r.get("stop_qty") or "0") for r in _records(live.home)) == live.qty
    assert _owned(live) == 0
    assert live.stop_coids() == [stop_coid]
    assert live.sells() == []  # the later exit bar sells nothing: external units stay
    assert live.base_balance() == EXTERNAL
    assert [o for o in read_obligations(live.home).unresolved if o.kind == run.UNLINKED_SELL_KIND] == []
    fills = live.make_venue().snapshot().recent_fills
    assert {f.order_coid for f in fills if f.side == "sell"} == {stop_coid}
    assert run._stop_fills_for(live.home, _identity(live), fills) == ([], Decimal(0))


# ---- a rejected stop keeps the exit fallback --------------------------------------------


@pytest.mark.parametrize(
    "outcome", ["executed", "partly-executed", "unknown-then-executed"], ids=lambda outcome: outcome
)
def test_a_rejected_stop_falls_back_to_one_exit_journaled_from_venue_evidence(live, outcome):
    stop_coid = _coid(live.venue, live.pair, intent="stop", bar_ts=3 * H)
    exit_coid = _coid(live.venue, live.pair, intent="exit", bar_ts=3 * H)
    executed = live.qty if outcome != "partly-executed" else live.qty * 2 / 5
    live.exchange.stop_reply = "reject"
    live.exchange.exit_listed = outcome != "unknown-then-executed"
    live.exchange.exit_executes = executed

    assert _tick(live, _bars("13")) == 0

    assert live.stop_coids() == [stop_coid]
    assert live.sells() == [live.qty]
    record = _bar_record(live.home, 3 * H)
    assert record["ensure_stop_failed"] is True
    assert record["exit_qty"] == "0"  # a live acknowledgement is not an executed quantity
    assert Decimal(record["owned_qty_after"]) == live.qty  # never zeroed without evidence
    assert _owned(live) == live.qty
    assert _unresolved(live.home)[exit_coid] == "exit"

    if outcome == "unknown-then-executed":
        # While the exit is unsettled nothing is sold again, not even on the exit bar.
        assert _tick(live, _bars("13", "14")) == 0
        assert _tick(live, _bars("13", "14", "10")) == 0
        assert live.sells() == [live.qty]
        assert live.stop_coids() == [stop_coid]
        assert _owned(live) == live.qty  # nothing guessed
        assert [_bar_record(live.home, ts).get("exits_pending") for ts in (4 * H, 5 * H)] == [[exit_coid]] * 2
        live.exchange.reveal()
        later = [_bars("13", "14", "10", "9"), _bars("13", "14", "10", "9", "8")]  # both flat
    else:
        live.exchange.stop_reply = "accept"  # a residual is protected again
        later = [_bars("13", "14"), _bars("13", "14", "15")]
    for bars in later:
        assert _tick(live, bars) == 0

    reconciled = [r for r in _records(live.home) if r.get("resolved_coid") == exit_coid]
    assert sum(Decimal(r["exit_qty"]) for r in reconciled) == executed  # journaled once, as executed
    assert exit_coid not in _unresolved(live.home)
    assert live.sells() == [live.qty]  # one sale, never repeated
    assert _owned(live) == live.qty - executed
    assert live.base_balance() == EXTERNAL + live.qty - executed  # external units untouched
    if outcome == "partly-executed":
        # The residual stays strategy-owned and gets its own stop.
        residual_stop = _coid(live.venue, live.pair, intent="stop", bar_ts=4 * H)
        assert live.stop_coids() == [stop_coid, residual_stop]
        live.assert_stop_request(residual_stop, live.qty - executed, Decimal(7))  # half of the 14 close
    else:
        assert live.stop_coids() == [stop_coid]


# ---- an adapter without place_stop -----------------------------------------------------


class _NoStopVenue:
    """A live adapter that cannot rest a stop on its own: it has no ``place_stop``."""

    def __init__(self, qty: Decimal, *, exit_outcome: str = "filled") -> None:
        self.calls: list[tuple] = []
        self.base = qty + EXTERNAL
        self.exit_outcome = exit_outcome
        self.executed: dict[str, Decimal] = {}

    def rules(self, pair: str) -> PairRules:
        return PairRules(ordermin=Decimal("0.01"), costmin=Decimal("0.5"), lot_decimals=5, price_decimals=4)

    def snapshot(self) -> Truth:
        return Truth(balances=[Balance(asset="SUI", free=self.base), Balance(asset="USD", free=Decimal(100))])

    def place_entry_with_stop(self, coid, qty, stop, *, pair):
        self.calls.append(("entry", coid, qty))
        raise AssertionError("never enters here")

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        self.calls.append(("exit", coid, qty))
        self.base -= qty
        self.executed[coid] = qty
        # Claims the requested quantity filled: a live claim is not evidence.
        return OrderRef(id=coid, coid=coid, pair=pair, side="sell", qty=qty, filled_qty=qty)

    def cancel_stops(self, pair: str) -> None:
        self.calls.append(("cancel_stops",))

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        self.calls.append(("raise_stop",))

    def order_by_coid(self, coid: str):
        return None

    def check_key(self) -> KeyPerms:
        return KeyPerms(can_trade=True, can_withdraw=False)

    def order_outcome(self, coid: str, *, pair: str) -> OrderOutcome:
        if coid not in self.executed:
            return OrderOutcome(status="unknown")
        if self.exit_outcome == "open":
            return OrderOutcome(status="open", authoritative=True)
        return OrderOutcome(status="filled", filled_qty=self.executed[coid], authoritative=True)


@pytest.fixture
def no_stop_live(home, monkeypatch, live_capable_release):
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    write_grant(home, venue="kraken", pair=KRAKEN_PAIR)
    _arm(home, venue="kraken", pair=KRAKEN_PAIR)
    _seed_owned(home, venue="kraken", pair=KRAKEN_PAIR, qty=Decimal(5))
    return home


def _tick_on(home: Path, venue_obj, bars: list[Candle]) -> int:
    return run.tick(venue="kraken", venue_obj=venue_obj, reader=lambda *_: bars, home=home, clock=lambda: 1_700_000_000)


def test_an_adapter_without_place_stop_keeps_the_exit_fallback_and_commits_no_stop(no_stop_live):
    home = no_stop_live
    venue = _NoStopVenue(Decimal(5))
    exit_coid = _coid("kraken", KRAKEN_PAIR, intent="exit", bar_ts=3 * H)

    assert _tick_on(home, venue, _bars("13")) == 0

    assert venue.calls == [("exit", exit_coid, Decimal(5))]
    assert _intent_kinds(home) == ["exit"]  # no stop intent that could never be sent
    record = _bar_record(home, 3 * H)
    assert record["ensure_stop_failed"] is True
    assert (record["exit_qty"], Decimal(record["owned_qty_after"])) == ("0", Decimal(5))
    assert _unresolved(home) == {exit_coid: "exit"}

    assert _tick_on(home, venue, _bars("13", "14")) == 0
    assert _tick_on(home, venue, _bars("13", "14", "10")) == 0

    assert venue.calls == [("exit", exit_coid, Decimal(5))]  # no new sends
    identity = OwnershipIdentity(pack_id=PACK, venue="kraken", pair=KRAKEN_PAIR, mode="live")
    assert project_ownership(home, identity).attributable_qty == 0  # settled from order_outcome
    assert _unresolved(home) == {}
    assert _intent_kinds(home) == ["exit"]


def test_an_exit_the_venue_shows_open_stays_unsettled_and_holds_exits(no_stop_live):
    home = no_stop_live
    venue = _NoStopVenue(Decimal(5), exit_outcome="open")
    exit_coid = _coid("kraken", KRAKEN_PAIR, intent="exit", bar_ts=3 * H)

    assert _tick_on(home, venue, _bars("13")) == 0
    assert _tick_on(home, venue, _bars("13", "14")) == 0
    assert _tick_on(home, venue, _bars("13", "14", "10")) == 0

    assert _unresolved(home) == {exit_coid: "exit"}  # open is no outcome for an exit
    assert venue.calls == [("exit", exit_coid, Decimal(5))]
    identity = OwnershipIdentity(pack_id=PACK, venue="kraken", pair=KRAKEN_PAIR, mode="live")
    assert project_ownership(home, identity).attributable_qty == Decimal(5)
    assert _resolutions(home) == []


# ---- paper -----------------------------------------------------------------------------

PAPER_IDENTITY = OwnershipIdentity(pack_id=PACK, venue="kraken", pair=KRAKEN_PAIR, mode="paper")


def _paper(home: Path, bars: list[list[Candle]], rules: dict) -> PaperVenue:
    return PaperVenue(
        "kraken",
        rules_provider=lambda _pair: PairRules(
            ordermin=rules["ordermin"], costmin=Decimal("0.5"), lot_decimals=5, price_decimals=4
        ),
        candle_reader=lambda *_: bars[0],
        home=home,
        starting_cash=Decimal(1000),
        clock=lambda: 1_700_000_000,
        timeframe_for_pair=lambda _pair: "1h",
    )


def _paper_position_without_stop(home: Path) -> tuple[PaperVenue, list[list[Candle]], dict]:
    _arm(home, venue="kraken", pair=KRAKEN_PAIR, mode="paper")
    bars = [ENTRY_BARS]
    rules = {"ordermin": Decimal("0.01")}
    paper = _paper(home, bars, rules)
    paper.place_entry_with_stop("seeded-entry", Decimal(5), Decimal(4), pair=KRAKEN_PAIR)
    paper.cancel_stops(KRAKEN_PAIR)
    _seed_owned(home, venue="kraken", pair=KRAKEN_PAIR, qty=Decimal(5), mode="paper")
    return paper, bars, rules


def _paper_tick(home: Path, paper: PaperVenue, bars: list[list[Candle]], candles: list[Candle]) -> int:
    bars[0] = candles
    return run.tick(venue="kraken", venue_obj=paper, reader=lambda *_: candles, home=home, clock=lambda: 1_700_000_000)


def test_paper_repair_still_rests_the_stop_on_the_paper_venue(home):
    paper, bars, _rules = _paper_position_without_stop(home)
    stop_coid = _coid("kraken", KRAKEN_PAIR, intent="stop", bar_ts=3 * H)

    assert _paper_tick(home, paper, bars, _bars("13")) == 0

    [stop] = [o for o in paper.snapshot().open_orders if o.stop_price is not None]
    assert (stop.coid, stop.qty, stop.stop_price) == (stop_coid, Decimal(5), Decimal("6.5"))
    assert [f for f in paper.snapshot().recent_fills if f.side == "sell"] == []
    record = _bar_record(home, 3 * H)
    assert record["ensure_stop_failed"] is False
    assert (record["entry_coid"], record["no_double_order"]) == (stop_coid, True)
    assert (record["exit_qty"], Decimal(record["owned_qty_after"])) == ("0", Decimal(5))
    assert project_ownership(home, PAPER_IDENTITY).attributable_qty == Decimal(5)


def test_a_rejected_paper_stop_exits_and_journals_what_the_paper_venue_executed(home):
    paper, bars, rules = _paper_position_without_stop(home)
    rules["ordermin"] = Decimal(10)  # the paper venue now refuses a stop for 5

    assert _paper_tick(home, paper, bars, _bars("13")) == 0

    [sold] = [f for f in paper.snapshot().recent_fills if f.side == "sell"]
    assert sold.qty == Decimal(5)
    record = _bar_record(home, 3 * H)
    assert record["ensure_stop_failed"] is True
    assert (Decimal(record["exit_qty"]), Decimal(record["owned_qty_after"])) == (Decimal(5), Decimal(0))
    assert project_ownership(home, PAPER_IDENTITY).attributable_qty == 0

    assert _paper_tick(home, paper, bars, _bars("13", "14")) == 0
    assert len([f for f in paper.snapshot().recent_fills if f.side == "sell"]) == 1
    assert _unresolved(home) == {}  # the paper venue proves the refused stop never rested


# ---- the adapters ----------------------------------------------------------------------


def test_kraken_place_stop_sends_only_documented_fields_and_never_twice(home, live_capable_release):
    exchange = _KrakenExchange(base=Decimal(5))
    clock = _Clock()
    venue = KrakenVenue(KRAKEN_KEY, KRAKEN_SECRET, exchange, now_ms=clock.now, home=home, sleep=clock.sleep)

    ref = venue.place_stop("c0ffee00c0ffee00ab", Decimal("5.123456"), Decimal("6.54321"), pair=KRAKEN_PAIR)
    again = venue.place_stop("c0ffee00c0ffee00ab", Decimal("5.123456"), Decimal("6.54321"), pair=KRAKEN_PAIR)

    [add] = [c for c in exchange.calls if c.url.endswith("/AddOrder")]
    assert {k: v for k, v in add.form.items() if k != "nonce"} == {
        "pair": KRAKEN_PAIR,
        "type": "sell",
        "ordertype": "stop-loss",
        "price": "6.5432",
        "volume": "5.12345",
        "cl_ord_id": "c0ffee00c0ffee00ab",
    }
    assert (ref.id, ref.side, ref.qty, ref.filled_qty, ref.stop_price) == (
        "OADD01-AAAAA-BBBBBB",
        "sell",
        Decimal("5.12345"),
        Decimal(0),
        Decimal("6.5432"),
    )
    assert (again.id, again.stop_price) == (ref.id, ref.stop_price)  # found resting: not sent again
    lookups = [c.form for c in exchange.calls if c.url.endswith(("/OpenOrders", "/ClosedOrders"))]
    assert lookups and all(f["cl_ord_id"] == "c0ffee00c0ffee00ab" and "userref" not in f for f in lookups)
    assert clock.sleeps and all(0 < s <= 1 for s in clock.sleeps)  # private calls stay spaced


@pytest.mark.parametrize(
    ("bucket", "vol_exec", "status", "filled", "authoritative"),
    [
        (None, None, "unknown", "0", False),
        ("open", "0.00000000", "open", "0", True),
        ("open", "2.00000000", "partial", "2", True),
        ("closed", "5.00000000", "filled", "5", True),
        ("closed", "0.00000000", "absent", "0", True),
        ("closed", None, "unknown", "0", False),
    ],
    ids=["not-found", "resting", "partly-executed", "executed", "cancelled-unexecuted", "unreadable"],
)
def test_kraken_stop_outcome_finds_the_stop_by_cl_ord_id_and_never_sends(
    home, bucket, vol_exec, status, filled, authoritative
):
    coid = "c0ffee00c0ffee00ab"
    exchange = _KrakenExchange(base=Decimal(5))
    if bucket is not None:
        row = order_info(
            vol="5",
            vol_exec=vol_exec,
            status="open" if bucket == "open" else "closed",
            pair=KRAKEN_PAIR,
            side="sell",
            ordertype="stop-loss",
            cl_ord_id=coid,
            stopprice="6.5",
        )
        (exchange.open_rows if bucket == "open" else exchange.closed_rows)["OSTOP-AAAAA-BBBBBB"] = row
    exchange.open_rows["OTHER-AAAAA-BBBBBB"] = order_info(
        vol="9", vol_exec="0", status="open", pair=KRAKEN_PAIR, side="sell", ordertype="stop-loss", userref=7
    )
    clock = _Clock()
    venue = KrakenVenue(KRAKEN_KEY, KRAKEN_SECRET, exchange, now_ms=clock.now, home=home, sleep=clock.sleep)

    outcome = venue.stop_outcome(coid, pair=KRAKEN_PAIR)

    assert (outcome.status, outcome.filled_qty, outcome.authoritative) == (status, Decimal(filled), authoritative)
    assert {c.url.rsplit("/", 1)[-1] for c in exchange.calls} <= {"OpenOrders", "ClosedOrders"}
    assert all({k: v for k, v in c.form.items() if k != "nonce"} == {"cl_ord_id": coid} for c in exchange.calls)


def test_coinbase_place_stop_sends_a_stop_limit_under_the_coid_and_never_twice(live_capable_release):
    exchange = _CoinbaseExchange(base=Decimal(1))
    venue = CoinbaseVenue(COINBASE_KEY, COINBASE_SECRET, exchange, now_fn=lambda: 1, nonce_fn=lambda: "aa" * 16)

    ref = venue.place_stop("c0ffee00c0ffee00ab", Decimal("0.123456789"), Decimal("6.789"), pair=COINBASE_PAIR)
    again = venue.place_stop("c0ffee00c0ffee00ab", Decimal("0.123456789"), Decimal("6.789"), pair=COINBASE_PAIR)

    [post] = [c for c in exchange.calls if c.method == "POST"]
    assert post.body == {
        "client_order_id": "c0ffee00c0ffee00ab",
        "product_id": COINBASE_PAIR,
        "side": "SELL",
        "order_configuration": {
            "stop_limit_stop_limit_gtc": {
                "base_size": "0.12345678",
                "stop_price": "6.78",
                "limit_price": "6.74",
                "stop_direction": "STOP_DIRECTION_STOP_DOWN",
            }
        },
    }
    assert (ref.id, ref.side, ref.qty, ref.filled_qty, ref.stop_price) == (
        "order-c0ffee00c0ffee00ab",
        "sell",
        Decimal("0.12345678"),
        Decimal(0),
        Decimal("6.78"),
    )
    assert again.id == ref.id  # found by exact client_order_id: not sent again
    outcome = venue.order_outcome("c0ffee00c0ffee00ab", pair=COINBASE_PAIR)
    assert (outcome.status, outcome.authoritative) == ("open", True)


def test_coinbase_rejected_stop_raises(live_capable_release):
    exchange = _CoinbaseExchange(base=Decimal(1))
    exchange.stop_reply = "reject"
    venue = CoinbaseVenue(COINBASE_KEY, COINBASE_SECRET, exchange, now_fn=lambda: 1, nonce_fn=lambda: "aa" * 16)

    with pytest.raises(RuntimeError):
        venue.place_stop("c0ffee00c0ffee00ab", Decimal("0.5"), Decimal("6.5"), pair=COINBASE_PAIR)
