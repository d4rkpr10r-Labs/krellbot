"""A live exit is journaled from what executed, and a residual stays the strategy's.

A live exit's acknowledgement is not an executed quantity. Kraken's AddOrder
reply carries none (the adapter reports ``filled_qty`` 0), and Coinbase's
Create Order reply only echoes the requested ``base_size`` (the adapter reports
``filled_qty`` = the lot). So the tick journals ``exit_qty`` 0 for the exit it
sends and leaves the strategy's ownership as recorded. The exit is an
obligation from its commit, before the venue call. On a later tick
``order_outcome`` reads what the venue's own record executed (Kraken
``vol_exec``, found by ``userref``/``cl_ord_id``; Coinbase ``filled_size``,
found by exact ``client_order_id``), and ``_resolve_obligations`` journals
that once, by ``resolved_coid``.

* A partly executed exit leaves the residual strategy-owned. The next bar rests
  a stop for it through M28.25's repair; the exit coid is not sent again.
* While the outcome is not settled, the identity's exits and stop repair are
  held (``exits_pending``) and new entries on the venue and mode wait.
* The exit coid is never sent twice, including after a restart between the
  send and the journal write.
* A documented refusal (M28.28) resolves the exit as not executed.
* External units are never sold.
* Paper is unchanged: the paper venue's acknowledgement is its record, so its
  exit is journaled on its own bar.

Runs on fake transports with the release gate patched in-process
(``live_capable_release``); ``PAPER_ONLY_RELEASE`` stays True in source.
"""

from __future__ import annotations

import json
from decimal import Decimal
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
from krellbot.storage.outbox import Outbox
from krellbot.venues.base import PairRules
from krellbot.venues.coinbase import CoinbaseVenue
from krellbot.venues.kraken import KrakenVenue
from krellbot.venues.paper import PaperVenue

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "venues"
PACK = "exit-residual"
H = 3_600_000
ENTRY_BAR = 2 * H
EXIT_BAR = 3 * H
EXTERNAL = Decimal(100)

KRAKEN_KEY = "FAKE_KEY_FOR_KRAKEN_TESTS"
KRAKEN_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
COINBASE_KEY = "organizations/fake-org/apiKeys/fake-coinbase-key-001"
COINBASE_SECRET = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQGKiOPddAnxlf1S2y08ul1yymcJvx2UEhvzdIgBtA9vXA=="


def _documented(venue: str, name: str) -> dict:
    """A documented response body from the venue fixtures; the first line is its provenance."""
    lines = (FIXTURES / venue / f"{name}.json").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("#")
    return json.loads("\n".join(lines[1:]))


def _candle(ts_ms: int, close: str) -> Candle:
    c = Decimal(close)
    return Candle(ts_ms=ts_ms, open=c, high=c + 1, low=c - 1, close=c, volume=Decimal(100))


# The pack enters when close crosses above sma2 and exits when it crosses below.
ENTRY_BARS = [_candle(0, "10"), _candle(H, "10"), _candle(ENTRY_BAR, "12")]


def _bars(*closes: str) -> list[Candle]:
    """Entry bars, then one bar per close from EXIT_BAR on.

    "10" first is the exit bar. After it, "9" and "8" stay flat; "13" crosses
    back above (an entry signal) and a "10" after it crosses below again.
    """
    return [*ENTRY_BARS, *(_candle(EXIT_BAR + i * H, close) for i, close in enumerate(closes))]


def _coid(venue: str, pair: str, *, intent: str, bar_ts: int) -> str:
    return run.coid_for(pack_id=PACK, pack_version="1.0.0", venue=venue, pair=pair, bar_ts=bar_ts, intent=intent)


def _arm(home: Path, *, venue: str, pair: str, mode: str = "live") -> None:
    pack = {
        "schema_version": 1,
        "id": PACK,
        "version": "1.0.0",
        "label": "Exit residual",
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


class _KrakenExchange(FakeKrakenTransport):
    """A fake Kraken account that executes what it accepts.

    Orders are kept by txid. Get Open Orders and Get Closed Orders apply the
    documented ``userref`` or ``cl_ord_id`` filter, so an order is only found
    by the reference it was placed with; Query Orders answers by txid (the
    sell-fill link). AddOrder rests a ``stop-loss`` sell. A market sell
    executes ``exit_executes`` of its volume at once (all of it by default):
    the balance moves, the order closes with that ``vol_exec`` and, when
    anything executed, a TradesHistory trade is added. ``exit_reply`` makes
    Kraken refuse a market sell with the documented "order minimum not met"
    body (``refuse``), or execute it and lose the reply (``lose_ack``).
    ``exit_listed=False`` keeps the executed order out of the open and closed
    lists, so its outcome cannot be established until ``reveal``.
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
        self.exit_reply = "accept"
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
        else:
            if self.exit_reply == "refuse":
                return _documented("kraken", "order_minimum_not_met")
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
            if executed > 0:
                self.trades[f"T{txid}"] = {
                    "ordertxid": txid,
                    "postxid": "TKH2SE-M7IF5-CFI7LT",
                    "pair": KRAKEN_PAIR,
                    "time": 1_700_000_100.25,
                    "type": "sell",
                    "ordertype": "market",
                    "price": "6.0000",
                    "cost": str(executed * 6),
                    "fee": "0.01",
                    "vol": str(executed),
                    "margin": "0.00000",
                    "misc": "",
                }
            self.balances["SUI"] = str(Decimal(self.balances["SUI"]) - executed)
            if self.exit_reply == "lose_ack":
                raise OSError("connection reset after Kraken accepted the order")
        return {"error": [], "result": {"descr": {"order": f"{form['type']} {form['volume']}"}, "txid": [txid]}}

    def reveal(self) -> None:
        self.closed_rows.update(self.unlisted_rows)
        self.unlisted_rows.clear()


class _Clock:
    """Epoch-microsecond clock that only moves on ``sleep``: the 1 s spacing never waits."""

    def __init__(self) -> None:
        self.us = 1_700_000_000_000_000

    def now(self) -> int:
        return self.us

    def sleep(self, seconds: float) -> None:
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

    def exits(self) -> list[tuple[str, Decimal]]:
        """Every market sell sent, as (coid, requested volume)."""
        return [(f["cl_ord_id"], Decimal(f["volume"])) for f in self._adds() if f["ordertype"] == "market"]

    def stops(self) -> list[tuple[str, Decimal]]:
        """Every stop sent, as (coid, volume)."""
        return [(f["cl_ord_id"], Decimal(f["volume"])) for f in self._adds() if f["ordertype"] == "stop-loss"]

    def buys(self) -> list[dict]:
        return [f for f in self._adds() if f["type"] == "buy"]

    def base_balance(self) -> Decimal:
        return Decimal(self.exchange.balances["SUI"])


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

    List Orders returns every recorded order (the adapter matches rows itself)
    and List Fills the recorded fills. Create Order rests a stop-limit GTC sell
    as ``OPEN``; a market IOC sell executes ``exit_executes`` of its size at
    once and is recorded ``FILLED``, or ``CANCELLED`` with the executed
    ``filled_size`` when only part (or none) of it executed. The reply always
    succeeds and names the order, as Create Order does, whatever executed.
    ``exit_reply`` and ``exit_listed`` are as for Kraken.
    """

    def __init__(self, *, base: Decimal) -> None:
        super().__init__(product=dict(COINBASE_PRODUCT))
        self.order_rows: list[dict] = []
        self.unlisted_rows: list[dict] = []
        self.fill_rows: list[dict] = []
        self.balances = {"USD": Decimal(1000), "BTC": base}
        self.exit_reply = "accept"
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
        else:
            if self.exit_reply == "refuse":
                return _documented("coinbase", "order_minimum_not_met")
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
            if self.exit_listed and executed > 0:
                self.fill_rows.append(_coinbase_fill(f"fill-{coid}", row["order_id"], executed))
            self.balances["BTC"] -= executed
            if self.exit_reply == "lose_ack":
                raise OSError("connection reset after Coinbase accepted the order")
        return {
            "success": True,
            "success_response": {
                "order_id": row["order_id"],
                "product_id": COINBASE_PAIR,
                "side": body["side"],
                "client_order_id": coid,
            },
        }

    def reveal(self) -> None:
        for row in self.unlisted_rows:
            self.order_rows.append(row)
            if Decimal(row["filled_size"]) > 0:
                self.fill_rows.append(
                    _coinbase_fill(f"fill-{row['client_order_id']}", row["order_id"], row["filled_size"])
                )
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

    def exits(self) -> list[tuple[str, Decimal]]:
        return [
            (b["client_order_id"], Decimal(b["order_configuration"]["market_market_ioc"]["base_size"]))
            for b in self._orders()
            if b["side"] == "SELL" and "market_market_ioc" in b["order_configuration"]
        ]

    def stops(self) -> list[tuple[str, Decimal]]:
        return [
            (b["client_order_id"], Decimal(b["order_configuration"]["stop_limit_stop_limit_gtc"]["base_size"]))
            for b in self._orders()
            if "stop_limit_stop_limit_gtc" in b["order_configuration"]
        ]

    def buys(self) -> list[dict]:
        return [b for b in self._orders() if b["side"] == "BUY"]

    def base_balance(self) -> Decimal:
        return self.exchange.balances["BTC"]


# ---- shared ----------------------------------------------------------------------------


@pytest.fixture(params=["kraken", "coinbase"])
def live(request, home, monkeypatch, live_capable_release):
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    cls = {"kraken": _KrakenLive, "coinbase": _CoinbaseLive}[request.param]
    write_grant(home, venue=cls.venue, pair=cls.pair)
    _arm(home, venue=cls.venue, pair=cls.pair)
    _seed_owned(home, venue=cls.venue, pair=cls.pair, qty=cls.qty)
    return cls(home)


def _tick(live, bars: list[Candle], venue=None, journal_sink=None) -> int:
    return run.tick(
        venue=live.venue,
        venue_obj=venue or live.make_venue(),
        reader=lambda *_: bars,
        home=live.home,
        clock=lambda: 1_700_000_000,
        journal_sink=journal_sink,
    )


def _exit_coid(live, bar_ts: int = EXIT_BAR) -> str:
    return _coid(live.venue, live.pair, intent="exit", bar_ts=bar_ts)


def _stop_coid(live, bar_ts: int) -> str:
    return _coid(live.venue, live.pair, intent="stop", bar_ts=bar_ts)


def _owned(live) -> Decimal:
    identity = OwnershipIdentity(pack_id=PACK, venue=live.venue, pair=live.pair, mode="live")
    return project_ownership(live.home, identity).attributable_qty


def _records(home: Path) -> list[dict]:
    return [
        r["detail"]
        for r in kb_journal.read_records_lenient(home)
        if r.get("kind") == "tick" and r.get("pack") == PACK and isinstance(r.get("detail"), dict)
    ]


def _bar_records(home: Path, bar_ts: int) -> list[dict]:
    """The tick's own records for ``bar_ts`` (not reconcile records)."""
    return [
        r["detail"]
        for r in kb_journal.read_records_lenient(home)
        if r.get("kind") == "tick"
        and r.get("bar_ts") == bar_ts
        and (r.get("detail") or {}).get("reason") != run.RECONCILED_REASON
    ]


def _bar_record(home: Path, bar_ts: int) -> dict:
    [record] = _bar_records(home, bar_ts)
    return record


def _journaled_exits(home: Path, coid: str) -> list[Decimal]:
    """Each ``exit_qty`` journaled for ``coid`` by reconcile, in order."""
    return [Decimal(r["exit_qty"]) for r in _records(home) if r.get("resolved_coid") == coid]


def _total_exited(home: Path) -> Decimal:
    return sum((Decimal(r.get("exit_qty") or "0") for r in _records(home)), Decimal(0))


def _ledger(home: Path) -> list[tuple[str, dict]]:
    return [(kind, json.loads(payload)) for _id, kind, payload in OperationalStore(home / "ops.sqlite").read_ledger()]


def _resolutions(home: Path) -> list[dict]:
    return [payload for kind, payload in _ledger(home) if kind == kb_obligations.RESOLVED_KIND]


def _unresolved(home: Path) -> dict[str, str | None]:
    return {o.coid: o.kind for o in read_obligations(home).unresolved}


def _entries_held(live) -> bool:
    return read_obligations(live.home).blocks_entries(venue=live.venue, mode="live")


# ---- a live exit is settled from order_outcome ------------------------------------------


def test_a_live_exit_is_journaled_once_from_order_outcome_not_from_its_acknowledgement(live):
    exit_coid = _exit_coid(live)

    assert _tick(live, _bars("10")) == 0

    assert live.exits() == [(exit_coid, live.qty)]
    record = _bar_record(live.home, EXIT_BAR)
    assert record["reason"] == "exit"
    assert record["exit_qty"] == "0"  # an acknowledgement is not an executed quantity
    assert Decimal(record["owned_qty_after"]) == live.qty  # ownership stays as recorded
    assert _owned(live) == live.qty
    assert _unresolved(live.home) == {exit_coid: "exit"}
    assert _entries_held(live)

    assert _tick(live, _bars("10", "9")) == 0
    assert _tick(live, _bars("10", "9", "8")) == 0

    assert _journaled_exits(live.home, exit_coid) == [live.qty]  # once, from the venue's record
    assert _total_exited(live.home) == live.qty
    [resolution] = _resolutions(live.home)
    assert (resolution["coid"], resolution["outcome"], resolution["evidence"]) == (exit_coid, "filled", "venue_fill")
    assert Decimal(resolution["filled_qty"]) == live.qty
    assert _owned(live) == 0
    assert _unresolved(live.home) == {}
    assert not _entries_held(live)
    assert live.exits() == [(exit_coid, live.qty)]  # never sent again
    assert live.stops() == []  # nothing left to protect
    assert live.base_balance() == EXTERNAL  # external units untouched


@pytest.mark.parametrize("executed", ["part", "none"])
def test_a_live_exit_that_executed_less_leaves_the_residual_owned_and_protected(live, executed):
    exit_coid = _exit_coid(live)
    sold = live.qty * 2 / 5 if executed == "part" else Decimal(0)
    residual = live.qty - sold
    live.exchange.exit_executes = sold

    assert _tick(live, _bars("10")) == 0

    record = _bar_record(live.home, EXIT_BAR)
    assert (record["exit_qty"], Decimal(record["owned_qty_after"])) == ("0", live.qty)

    assert _tick(live, _bars("10", "9")) == 0  # settles the exit, then protects what is left
    assert _tick(live, _bars("10", "9", "8")) == 0

    assert _journaled_exits(live.home, exit_coid) == ([sold] if sold else [])
    assert _total_exited(live.home) == sold
    [resolution] = _resolutions(live.home)
    assert resolution["coid"] == exit_coid
    assert resolution["outcome"] == ("filled" if sold else "absent")
    assert Decimal(resolution["filled_qty"]) == sold
    assert _owned(live) == residual  # the residual stays the strategy's
    assert live.stops() == [(_stop_coid(live, EXIT_BAR + H), residual)]  # re-protected once, for the residual only
    assert live.exits() == [(exit_coid, live.qty)]  # the exit coid is not sent again
    assert live.base_balance() == EXTERNAL + residual
    assert _unresolved(live.home) == {}
    assert Decimal(_bar_record(live.home, EXIT_BAR + H)["owned_qty_after"]) == residual


@pytest.mark.parametrize("live", ["coinbase"], indirect=True)
def test_coinbase_echoed_exit_size_is_never_credited(live):
    exit_coid = _exit_coid(live)
    sold = Decimal("0.2")
    live.exchange.exit_executes = sold
    venue = live.make_venue()
    acks = []
    place_exit = venue.place_exit

    def _recording(coid, qty, *, pair):
        acks.append(place_exit(coid, qty, pair=pair))
        return acks[-1]

    venue.place_exit = _recording

    assert _tick(live, _bars("10"), venue) == 0

    [ack] = acks
    assert ack.filled_qty == live.qty  # Create Order's reply only echoes the requested size
    record = _bar_record(live.home, EXIT_BAR)
    assert (record["exit_qty"], Decimal(record["owned_qty_after"])) == ("0", live.qty)
    assert _owned(live) == live.qty

    assert _tick(live, _bars("10", "9")) == 0

    assert _journaled_exits(live.home, exit_coid) == [sold]  # what filled_size says executed
    assert _owned(live) == live.qty - sold
    assert live.stops() == [(_stop_coid(live, EXIT_BAR + H), live.qty - sold)]


def test_exits_and_stop_repair_are_held_while_a_live_exit_is_unsettled(live):
    exit_coid = _exit_coid(live)
    live.exchange.exit_listed = False  # the venue's record does not show the exit yet

    assert _tick(live, _bars("10")) == 0
    assert _tick(live, _bars("10", "13")) == 0  # an entry signal
    assert _tick(live, _bars("10", "13", "10")) == 0  # an exit signal

    assert live.exits() == [(exit_coid, live.qty)]  # nothing sold again
    assert live.stops() == []  # nothing re-protected on a guess
    assert live.buys() == []  # entries wait on the venue and mode
    assert _owned(live) == live.qty  # nothing guessed
    assert [_bar_record(live.home, ts).get("exits_pending") for ts in (EXIT_BAR + H, EXIT_BAR + 2 * H)] == [
        [exit_coid]
    ] * 2
    assert _unresolved(live.home) == {exit_coid: "exit"}
    assert _entries_held(live)

    live.exchange.reveal()
    assert _tick(live, _bars("10", "13", "10", "9")) == 0
    assert _tick(live, _bars("10", "13", "10", "9", "8")) == 0

    assert _journaled_exits(live.home, exit_coid) == [live.qty]
    assert _owned(live) == 0
    assert _unresolved(live.home) == {}
    assert live.exits() == [(exit_coid, live.qty)]
    assert live.stops() == []
    assert live.base_balance() == EXTERNAL


def test_a_documented_exit_refusal_resolves_as_not_executed(live):
    exit_coid = _exit_coid(live)
    live.exchange.exit_reply = "refuse"  # the documented "order minimum not met" body

    assert _tick(live, _bars("10")) == 0

    record = _bar_record(live.home, EXIT_BAR)
    assert (record["intent_refused"], record["exit_qty"]) == ("order_minimum_not_met", "0")
    assert Decimal(record["owned_qty_after"]) == live.qty
    assert _resolutions(live.home) == [
        {
            "coid": exit_coid,
            "outcome": "absent",
            "filled_qty": "0",
            "evidence": "venue_rejected",
            "code": "order_minimum_not_met",
        }
    ]
    assert _unresolved(live.home) == {}
    assert not _entries_held(live)

    live.exchange.exit_reply = "accept"
    assert _tick(live, _bars("10", "9")) == 0

    assert live.exits() == [(exit_coid, live.qty)]  # the refused coid is never sent again
    assert live.stops() == [(_stop_coid(live, EXIT_BAR + H), live.qty)]  # still owned, so protected again
    assert _owned(live) == live.qty
    assert _total_exited(live.home) == 0
    assert live.base_balance() == live.qty + EXTERNAL


# ---- a restart between the send and the journal write -----------------------------------


@pytest.mark.parametrize("executed", ["all", "part"])
def test_a_restart_after_a_lost_exit_reply_never_resends_and_journals_once(live, executed):
    exit_coid = _exit_coid(live)
    sold = live.qty if executed == "all" else live.qty * 2 / 5
    live.exchange.exit_executes = sold
    live.exchange.exit_reply = "lose_ack"  # the venue executes it, the reply is lost

    with pytest.raises(OSError):
        _tick(live, _bars("10"))

    assert _bar_records(live.home, EXIT_BAR) == []
    assert _unresolved(live.home) == {exit_coid: "exit"}
    assert _owned(live) == live.qty

    live.exchange.exit_reply = "accept"
    assert _tick(live, _bars("10")) == 0  # a restart replays the bar
    assert _tick(live, _bars("10", "9")) == 0
    assert _tick(live, _bars("10", "9", "8")) == 0

    assert live.exits() == [(exit_coid, live.qty)]  # sent once, never again
    assert _journaled_exits(live.home, exit_coid) == [sold]
    assert _total_exited(live.home) == sold
    assert _owned(live) == live.qty - sold
    assert live.base_balance() == EXTERNAL + live.qty - sold
    assert _unresolved(live.home) == {}
    assert live.stops() == ([] if sold == live.qty else [(_stop_coid(live, EXIT_BAR + H), live.qty - sold)])


class _ProcessStopped(BaseException):
    """The process stops here: nothing after this point runs."""


def _stops_before_writing(_record: dict) -> None:
    raise _ProcessStopped


@pytest.mark.parametrize("restart_bar", ["same", "later"])
@pytest.mark.parametrize("executed", ["all", "part"])
def test_a_restart_before_the_exit_is_journaled_never_resends_and_journals_once(live, executed, restart_bar):
    exit_coid = _exit_coid(live)
    sold = live.qty if executed == "all" else live.qty * 2 / 5
    live.exchange.exit_executes = sold

    with pytest.raises(_ProcessStopped):
        _tick(live, _bars("10"), journal_sink=_stops_before_writing)  # sent and acknowledged, never journaled

    assert live.exits() == [(exit_coid, live.qty)]
    assert _bar_records(live.home, EXIT_BAR) == []
    assert _unresolved(live.home) == {exit_coid: "exit"}

    restart = _bars("10") if restart_bar == "same" else _bars("10", "9")
    assert _tick(live, restart) == 0
    assert _tick(live, _bars("10", "9")) == 0
    assert _tick(live, _bars("10", "9", "8")) == 0

    assert live.exits() == [(exit_coid, live.qty)]  # never resent
    assert _journaled_exits(live.home, exit_coid) == [sold]  # journaled once
    assert _total_exited(live.home) == sold
    assert _owned(live) == live.qty - sold
    # Only the residual is ever re-protected: a stop for units already sold could sell external ones.
    assert live.stops() == ([] if sold == live.qty else [(_stop_coid(live, EXIT_BAR + H), live.qty - sold)])
    assert live.base_balance() == EXTERNAL + live.qty - sold
    assert _unresolved(live.home) == {}


@pytest.mark.parametrize("path", ["signal-exit", "stop-repair-fallback"])
def test_a_restart_right_after_the_exit_send_is_recorded_never_resends_and_journals_once(live, monkeypatch, path):
    """The process stops as soon as the Outbox records the exit as sent: nothing else is written."""
    dispatch = Outbox.dispatch

    def _stops_after_an_exit_send(self, coid, body, send, **kwargs):
        state = dispatch(self, coid, body, send, **kwargs)
        if json.loads(body)["kind"] == "exit":
            raise _ProcessStopped
        return state

    if path == "signal-exit":
        first, later = _bars("10"), [_bars("10", "9")]
    else:
        # No stop rests for the position and the venue does not take one, so
        # the repair falls back to an exit on a bar the pack does not exit.
        first, later = _bars("13"), [_bars("13", "14"), _bars("13", "14", "15")]

        def _no_stop(*_args, **_kwargs):
            raise RuntimeError("stop not accepted")

        monkeypatch.setattr(type(live.make_venue()), "place_stop", _no_stop)
    exit_coid = _exit_coid(live)
    monkeypatch.setattr(Outbox, "dispatch", _stops_after_an_exit_send)

    with pytest.raises(_ProcessStopped):
        _tick(live, first)

    monkeypatch.setattr(Outbox, "dispatch", dispatch)
    assert live.exits() == [(exit_coid, live.qty)]
    assert _bar_records(live.home, EXIT_BAR) == []
    assert _unresolved(live.home).get(exit_coid) == "exit"  # an obligation from before the send

    for bars in [first, *later]:  # a restart replays the bar, then later bars follow
        assert _tick(live, bars) == 0

    assert live.exits() == [(exit_coid, live.qty)]  # never resent
    assert _journaled_exits(live.home, exit_coid) == [live.qty]  # journaled once
    assert _total_exited(live.home) == live.qty
    assert _owned(live) == 0
    assert live.base_balance() == EXTERNAL  # no stop rested for units already sold
    assert exit_coid not in _unresolved(live.home)


# ---- paper -----------------------------------------------------------------------------


def test_a_paper_exit_is_still_journaled_on_its_own_bar_from_the_paper_venue(home):
    _arm(home, venue="kraken", pair=KRAKEN_PAIR, mode="paper")
    bars = [ENTRY_BARS]
    paper = PaperVenue(
        "kraken",
        rules_provider=lambda _pair: PairRules(
            ordermin=Decimal("0.01"), costmin=Decimal("0.5"), lot_decimals=5, price_decimals=4
        ),
        candle_reader=lambda *_: bars[0],
        home=home,
        starting_cash=Decimal(1000),
        clock=lambda: 1_700_000_000,
        timeframe_for_pair=lambda _pair: "1h",
    )
    paper.place_entry_with_stop("seeded-entry", Decimal(5), Decimal(4), pair=KRAKEN_PAIR)
    _seed_owned(home, venue="kraken", pair=KRAKEN_PAIR, qty=Decimal(5), mode="paper")
    exit_coid = _coid("kraken", KRAKEN_PAIR, intent="exit", bar_ts=EXIT_BAR)
    bars[0] = _bars("10")

    assert (
        run.tick(venue="kraken", venue_obj=paper, reader=lambda *_: bars[0], home=home, clock=lambda: 1_700_000_000)
        == 0
    )

    [sold] = [f for f in paper.snapshot().recent_fills if f.side == "sell"]
    assert (sold.coid, sold.qty) == (exit_coid, Decimal(5))
    record = _bar_record(home, EXIT_BAR)
    assert (Decimal(record["exit_qty"]), Decimal(record["owned_qty_after"])) == (Decimal(5), Decimal(0))
    identity = OwnershipIdentity(pack_id=PACK, venue="kraken", pair=KRAKEN_PAIR, mode="paper")
    assert project_ownership(home, identity).attributable_qty == 0
    assert _unresolved(home) == {}
    assert [p for kind, p in _ledger(home) if kind == "needs_reconcile"] == []  # no obligation for paper
    assert [r for r in _records(home) if r.get("reason") == run.RECONCILED_REASON] == []
