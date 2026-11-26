"""Close position sells exactly the strategy-owned quantity, on request.

``positions.close`` (``run.close_position``, the pair typed back) runs the
tick's exit now, under the venue's tick lock, without waiting for a bar, for
paper and live:

* it cancels the arm's stop and sells exactly what the strategy owns, never
  the external units the same account holds;
* paper journals what its venue executed; live journals ``exit_qty`` 0 and
  ``_resolve_obligations`` settles it once from ``order_outcome`` (M28.27);
* it holds new entries with ``closed_by_operator_at``, never with
  ``entries_paused``, so the restore fence does not read it: a live close
  under the kill switch leaves the venue's next tick, its exits, stops and
  reconcile running. Paper resume or a new arm brings entries back;
* the restore fence, a wrong or missing typed pair, ambiguous ownership, an
  open obligation and a running tick refuse it, and a refusal writes
  nothing;
* the kill switch alone does not stop it, and a repeat is refused with no
  resend;
* a read-only Tailscale session cannot send it;
* the daily-loss gate (M28.31) prices a paper close's exit, so an arm with a
  limit does not read ``loss_limit_unavailable`` after it;
* an arm that was never closed keeps its exact config bytes.

Paper runs on the paper venue; live on Kraken and Coinbase adapters over fake
transports, and on an in-memory venue, with the release gate patched
in-process (``live_capable_release``). ``PAPER_ONLY_RELEASE`` stays True.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fakes.fake_coinbase import FakeCoinbaseTransport, order_record
from fakes.fake_coinbase import RecordedCall as CoinbaseCall
from fakes.fake_kraken import FakeKrakenTransport, order_info
from fakes.fake_kraken import RecordedCall as KrakenCall
from fakes.live_grant import DEFAULT_EXPIRES_AT, write_grant

from krellbot import config as kb_config
from krellbot import journal as kb_journal
from krellbot import run
from krellbot.application import live_gate
from krellbot.application.paper import PaperService
from krellbot.config import ArmedPack, Config, load_config, save_config
from krellbot.execution.obligations import read_obligations
from krellbot.pack.model import Candle
from krellbot.run import daily_loss
from krellbot.run.lock import TickLock
from krellbot.run.reconcile import OwnershipIdentity, project_ownership
from krellbot.storage.database import OperationalStore
from krellbot.storage.outbox import Outbox
from krellbot.venues.base import (
    UNKNOWN_OUTCOME,
    Balance,
    Fill,
    KeyPerms,
    OpenOrder,
    OrderOutcome,
    OrderRef,
    PairRules,
    Truth,
)
from krellbot.venues.coinbase import CoinbaseVenue
from krellbot.venues.kraken import KrakenVenue
from krellbot.venues.paper import PaperVenue

PACK = "close-position"
VERSION = "1.0.0"
H = 3_600_000
ENTRY_BAR = 2 * H
EXTERNAL = Decimal(100)  # units the account holds that are not the strategy's
NOW = 1_700_000_000
KILLED_AT = 1_699_999_000

# The pack enters when close crosses above sma2 and exits when it crosses below.
ENTRY = ("10", "10", "12")  # entry signal on bar 2 (stop 6)
HOLD = (*ENTRY, "13")  # bar 3 neither enters nor exits
EXIT = (*HOLD, "10")  # exit signal on bar 4
REENTRY = (*EXIT, "13")  # entry signal on bar 5
HOLD2 = (*HOLD, "14")  # bar 4 neither enters nor exits
STOP = "6"

KRAKEN_KEY = "FAKE_KEY_FOR_KRAKEN_TESTS"
KRAKEN_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
COINBASE_KEY = "organizations/fake-org/apiKeys/fake-coinbase-key-001"
COINBASE_SECRET = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQGKiOPddAnxlf1S2y08ul1yymcJvx2UEhvzdIgBtA9vXA=="


def _candle(i: int, close: str) -> Candle:
    c = Decimal(close)
    return Candle(ts_ms=i * H, open=c, high=c + 1, low=c - 1, close=c, volume=Decimal(100))


def _bars(closes: tuple[str, ...]) -> list[Candle]:
    return [_candle(i, close) for i, close in enumerate(closes)]


def _bar_ts(closes: tuple[str, ...]) -> int:
    return (len(closes) - 1) * H


def _coid(venue: str, pair: str, intent: str, bar_ts: int, pack: str = PACK) -> str:
    return run.coid_for(pack_id=pack, pack_version=VERSION, venue=venue, pair=pair, bar_ts=bar_ts, intent=intent)


def _write_pack(home: Path, *, pack: str, venue: str, pair: str) -> Path:
    body = {
        "schema_version": 1,
        "id": pack,
        "version": VERSION,
        "label": "Close position",
        "author": "krellbot tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 50}},
        "markets": [{"venue": venue, "pair": pair}],
    }
    path = home / "packs" / f"{pack}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _armed(home: Path, *, venue: str, pair: str, mode: str, pack: str = PACK) -> ArmedPack:
    return ArmedPack(
        pack_path=str(_write_pack(home, pack=pack, venue=venue, pair=pair)),
        pack_sha256="0" * 64,
        pack_id=pack,
        pack_version=VERSION,
        venue=venue,
        pair=pair,
        cap=Decimal(100),
        stop=Decimal(5),
        mode=mode,
        starting_cash=Decimal(1000),
        requires_license=False,
        armed_at_ts=1,
    )


def _seed_owned(home: Path, *, venue: str, pair: str, mode: str, qty: Decimal, pack: str = PACK) -> None:
    """The arm's entry on ENTRY_BAR, journaled as the engine journals it."""
    kb_journal.append(
        {
            "ts": 2,
            "kind": "tick",
            "venue": venue,
            "pack": pack,
            "bar_ts": ENTRY_BAR,
            "detail": {
                "pair": pair,
                "mode": mode,
                "reason": "entry",
                "entry_qty": str(qty),
                "exit_qty": "0",
                "stop_qty": "0",
                "entry_coid": _coid(venue, pair, "entry", ENTRY_BAR, pack),
            },
        }
    )


def _records(home: Path, pack: str = PACK) -> list[dict]:
    return [r for r in kb_journal.read_records_lenient(home) if r.get("kind") == "tick" and r.get("pack") == pack]


def _close_records(home: Path) -> list[dict]:
    return [r for r in _records(home) if r["detail"].get("reason") == run.CLOSE_REASON]


def _settled(home: Path, coid: str) -> list[Decimal]:
    """Each exit quantity reconcile journaled for ``coid``, in order."""
    return [Decimal(r["detail"]["exit_qty"]) for r in _records(home) if r["detail"].get("resolved_coid") == coid]


def _owned(home: Path, *, venue: str, pair: str, mode: str, pack: str = PACK) -> Decimal:
    return project_ownership(home, OwnershipIdentity(pack_id=pack, venue=venue, pair=pair, mode=mode)).attributable_qty


def _unresolved(home: Path) -> dict[str, str | None]:
    return {o.coid: o.kind for o in read_obligations(home).unresolved}


def _ledger(home: Path) -> list[tuple[str, str]]:
    path = home / "ops.sqlite"
    if not path.exists():
        return []
    return [(kind, payload) for _id, kind, payload in OperationalStore(path).read_ledger()]


def _durable(home: Path) -> tuple:
    """Everything a refusal must leave as it was: config bytes, journal and ledger."""
    config = kb_config.config_path(home)
    journal = (
        sorted((p.name, p.read_bytes()) for p in (home / "journal").glob("*.jsonl"))
        if (home / "journal").is_dir()
        else []
    )
    return (config.read_bytes() if config.exists() else None, journal, _ledger(home))


def _rules(_pair: str) -> PairRules:
    return PairRules(ordermin=Decimal("0.01"), costmin=Decimal("0.5"), lot_decimals=5, price_decimals=4)


# ---- markets: paper, Kraken and Coinbase -----------------------------------------------


class _Market:
    """One arm that owns ``qty`` on an account that also holds EXTERNAL, with its stop resting."""

    venue: str
    pair: str
    base: str
    mode: str
    qty: Decimal

    def __init__(self, home: Path) -> None:
        self.home = home
        self.bars = _bars(HOLD)
        self.opened = 0

    def open_venue(self, armed_list: list[ArmedPack]):
        assert {a.venue for a in armed_list} == {self.venue}
        self.opened += 1
        return self.make_venue(), lambda _venue, _pair: list(self.bars)

    def close(self, confirmation: object = "<pair>", **kwargs) -> run.ClosePositionResult:
        return run.close_position(
            venue=self.venue,
            pair=self.pair,
            confirmation=self.pair if confirmation == "<pair>" else confirmation,
            open_venue=self.open_venue,
            home=self.home,
            clock=lambda: NOW,
            **kwargs,
        )

    def tick(self, closes: tuple[str, ...]) -> int:
        return run.tick(
            venue=self.venue,
            venue_obj=self.make_venue(),
            reader=lambda _venue, _pair: _bars(closes),
            home=self.home,
            clock=lambda: NOW + 60,
        )

    def owned(self) -> Decimal:
        return _owned(self.home, venue=self.venue, pair=self.pair, mode=self.mode)


class _PaperMarket(_Market):
    venue, pair, base, mode, qty = "kraken", "SUIUSD", "SUI", "paper", Decimal(5)

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        save_config(home, Config(armed=[_armed(home, venue=self.venue, pair=self.pair, mode="paper")]))
        self.bars = _bars(ENTRY)
        self.make_venue().place_entry_with_stop(
            _coid(self.venue, self.pair, "entry", ENTRY_BAR), self.qty, Decimal(STOP), pair=self.pair
        )
        _seed_owned(home, venue=self.venue, pair=self.pair, mode="paper", qty=self.qty)
        state = self._state()
        state["balances"][self.base] = str(Decimal(str(state["balances"][self.base])) + EXTERNAL)
        self._state_path().write_text(json.dumps(state), encoding="utf-8")
        self.bars = _bars(HOLD)

    def make_venue(self) -> PaperVenue:
        return PaperVenue(
            self.venue,
            rules_provider=_rules,
            candle_reader=lambda _venue, _pair: list(self.bars),
            home=self.home,
            starting_cash=Decimal(1000),
            clock=lambda: NOW,
            timeframe_for_pair=lambda _pair: "1h",
        )

    def _state_path(self) -> Path:
        return self.home / "run" / f"paper-{self.venue}.json"

    def _state(self) -> dict:
        return json.loads(self._state_path().read_text(encoding="utf-8"))

    def exits(self) -> list[tuple[str, Decimal]]:
        fills = self._state()["recent_fills"]
        return [(f["coid"], Decimal(str(f["qty"]))) for f in fills if f["side"] == "sell" and f.get("kind") != "stop"]

    def buys(self) -> list[str]:
        return [f["coid"] for f in self._state()["recent_fills"] if f["side"] == "buy"][1:]  # after the seeded entry

    def resting_stops(self) -> list[Decimal]:
        return [Decimal(str(o["qty"])) for o in self._state()["open_orders"] if o.get("stop_price") is not None]

    def base_balance(self) -> Decimal:
        return Decimal(str(self._state()["balances"][self.base]))


class _Clock:
    """Epoch-microsecond clock that only moves on ``sleep``: Kraken's call spacing never waits."""

    def __init__(self) -> None:
        self.us = 1_700_000_000_000_000

    def now(self) -> int:
        return self.us

    def sleep(self, seconds: float) -> None:
        self.us += round(seconds * 1_000_000)


class _KrakenExchange(FakeKrakenTransport):
    """A fake Kraken account that executes what it accepts and cancels what it is asked to.

    Orders are kept by txid; Get Open Orders and Get Closed Orders apply the
    documented ``userref``/``cl_ord_id`` filter. A market sell executes
    ``exit_executes`` of its volume (all of it by default), moves the balance,
    closes with that ``vol_exec`` and adds a trade. CancelOrder moves a
    resting order to the closed book; ``cancel`` = ``refuse`` answers with an
    error body and ``ignore`` answers success but leaves the order resting.
    """

    def __init__(self, *, base: Decimal, stop_qty: Decimal) -> None:
        super().__init__(
            asset_pairs={"SUIUSD": {"ordermin": "0.01", "costmin": "0.5", "lot_decimals": 5, "pair_decimals": 4}},
            balances={"USD": "1000", "SUI": str(base)},
            ticker_last="13",
        )
        self.open_rows: dict[str, dict] = {
            "OSTOP1-AAAAA-BBBBBB": order_info(
                vol=str(stop_qty),
                vol_exec="0.00000000",
                status="open",
                pair="SUIUSD",
                side="sell",
                ordertype="stop-loss",
                stopprice=STOP,
            )
        }
        self.closed_rows: dict[str, dict] = {}
        self.trades: dict[str, dict] = {}
        self.exit_executes: Decimal | None = None
        self.cancel = "accept"
        self._txids = 0

    def post(self, url: str, form: dict[str, str], headers: dict[str, str]) -> dict:
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint not in {"OpenOrders", "ClosedOrders", "TradesHistory", "QueryOrders", "AddOrder", "CancelOrder"}:
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
            known = {**self.open_rows, **self.closed_rows}
            asked = [txid for txid in str(form.get("txid", "")).split(",") if txid]
            return {"error": [], "result": {txid: dict(known[txid]) for txid in asked if txid in known}}
        if endpoint == "CancelOrder":
            if self.cancel == "refuse":
                return {"error": ["EService:Unavailable"]}
            if self.cancel == "accept" and form["txid"] in self.open_rows:
                row = self.open_rows.pop(form["txid"])
                self.closed_rows[form["txid"]] = {**row, "status": "canceled"}
            return {"error": [], "result": {"count": 1}}
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
            executed = Decimal(form["volume"]) if self.exit_executes is None else self.exit_executes
            self.closed_rows[txid] = order_info(
                vol=form["volume"],
                vol_exec=str(executed),
                status="closed" if executed == Decimal(form["volume"]) else "canceled",
                pair=form["pair"],
                side=form["type"],
                ordertype="market",
                **ids,
            )
            if executed > 0:
                self.trades[f"T{txid}"] = {
                    "ordertxid": txid,
                    "pair": "SUIUSD",
                    "time": 1_700_000_100.25,
                    "type": "sell",
                    "ordertype": "market",
                    "price": "13.0000",
                    "cost": str(executed * 13),
                    "fee": "0.01",
                    "vol": str(executed),
                    "margin": "0.00000",
                    "misc": "",
                }
            self.balances["SUI"] = str(Decimal(self.balances["SUI"]) - executed)
        return {"error": [], "result": {"descr": {"order": f"{form['type']} {form['volume']}"}, "txid": [txid]}}


class _KrakenMarket(_Market):
    venue, pair, base, mode, qty = "kraken", "SUIUSD", "SUI", "live", Decimal(5)

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        save_config(home, Config(armed=[_armed(home, venue=self.venue, pair=self.pair, mode="live")]))
        _seed_owned(home, venue=self.venue, pair=self.pair, mode="live", qty=self.qty)
        self.exchange = _KrakenExchange(base=self.qty + EXTERNAL, stop_qty=self.qty)
        self.clock = _Clock()

    def make_venue(self) -> KrakenVenue:
        return KrakenVenue(
            KRAKEN_KEY, KRAKEN_SECRET, self.exchange, now_ms=self.clock.now, home=self.home, sleep=self.clock.sleep
        )

    def _adds(self) -> list[dict]:
        return [c.form for c in self.exchange.calls if c.url.endswith("/AddOrder")]

    def exits(self) -> list[tuple[str, Decimal]]:
        return [(f["cl_ord_id"], Decimal(f["volume"])) for f in self._adds() if f["ordertype"] == "market"]

    def buys(self) -> list[str]:
        return [f["cl_ord_id"] for f in self._adds() if f["type"] == "buy"]

    def resting_stops(self) -> list[Decimal]:
        return [Decimal(r["vol"]) for r in self.exchange.open_rows.values()]

    def base_balance(self) -> Decimal:
        return Decimal(self.exchange.balances["SUI"])

    def set_cancel(self, behaviour: str) -> None:
        self.exchange.cancel = behaviour

    def set_exit_executes(self, qty: Decimal) -> None:
        self.exchange.exit_executes = qty


class _CoinbaseExchange(FakeCoinbaseTransport):
    """A fake Coinbase account that executes what it accepts and cancels what it is asked to.

    List Orders returns every recorded order (the adapter keeps the ``OPEN``
    ones for a snapshot) and List Fills the recorded fills. A market IOC sell
    executes ``exit_executes`` of its size (all by default) and is recorded
    ``FILLED`` (or ``CANCELLED`` with the executed ``filled_size``). Cancel
    Orders marks the named orders ``CANCELLED``; ``cancel`` = ``refuse``
    loses the connection and ``ignore`` answers success but leaves them open.
    """

    def __init__(self, *, base: Decimal, stop_qty: Decimal) -> None:
        super().__init__(
            product={
                "product_id": "BTC-USD",
                "base_increment": "0.00000001",
                "quote_increment": "0.01",
                "base_min_size": "0.00000001",
                "quote_min_size": "1",
                "price": "13",
            }
        )
        self.order_rows: list[dict] = [
            order_record(
                client_order_id=_coid("coinbase", "BTC-USD", "entry", ENTRY_BAR) + "_stop",
                status="OPEN",
                filled_size="0",
                base_size=str(stop_qty),
                product_id="BTC-USD",
                side="SELL",
                stop_price=STOP,
            )
        ]
        self.fill_rows: list[dict] = []
        self.balances = {"USD": Decimal(1000), "BTC": base}
        self.exit_executes: Decimal | None = None
        self.cancel = "accept"

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
        if url.endswith("/api/v3/brokerage/orders/cancel"):
            self.calls.append(CoinbaseCall(method="POST", url=url, headers=dict(headers), body=dict(body)))
            if self.cancel == "refuse":
                raise OSError("connection reset before Coinbase answered")
            if self.cancel == "accept":
                for row in self.order_rows:
                    if row["order_id"] in body["order_ids"]:
                        row["status"] = "CANCELLED"
            return {"results": [{"success": True, "order_id": order_id} for order_id in body["order_ids"]]}
        if not url.endswith("/api/v3/brokerage/orders"):
            return super().post(url, body, headers)
        self.calls.append(
            CoinbaseCall(method="POST", url=url, headers=dict(headers), body=json.loads(json.dumps(body)))
        )
        coid = body["client_order_id"]
        config = body["order_configuration"]
        if "stop_limit_stop_limit_gtc" in config:
            box = config["stop_limit_stop_limit_gtc"]
            self.order_rows.append(
                order_record(
                    client_order_id=coid,
                    status="OPEN",
                    filled_size="0",
                    base_size=box["base_size"],
                    product_id="BTC-USD",
                    side="SELL",
                    stop_price=box["stop_price"],
                )
            )
            row = self.order_rows[-1]
        else:
            size = Decimal(config["market_market_ioc"]["base_size"])
            executed = size if self.exit_executes is None else self.exit_executes
            row = order_record(
                client_order_id=coid,
                status="FILLED" if executed == size else "CANCELLED",
                filled_size=str(executed),
                base_size=str(size),
                product_id="BTC-USD",
                side=body["side"],
            )
            self.order_rows.append(row)
            if executed > 0:
                self.fill_rows.append(
                    {
                        "entry_id": f"fill-{coid}",
                        "trade_id": f"trade-{coid}",
                        "order_id": row["order_id"],
                        "trade_time": "2026-11-26T10:00:00Z",
                        "trade_type": "FILL",
                        "price": "13",
                        "size": str(executed),
                        "commission": "0.01",
                        "product_id": "BTC-USD",
                        "side": "SELL",
                    }
                )
            self.balances["BTC"] -= executed
        return {
            "success": True,
            "success_response": {
                "order_id": row["order_id"],
                "product_id": "BTC-USD",
                "side": body["side"],
                "client_order_id": coid,
            },
        }


class _CoinbaseMarket(_Market):
    venue, pair, base, mode, qty = "coinbase", "BTC-USD", "BTC", "live", Decimal("0.5")

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        save_config(home, Config(armed=[_armed(home, venue=self.venue, pair=self.pair, mode="live")]))
        _seed_owned(home, venue=self.venue, pair=self.pair, mode="live", qty=self.qty)
        self.exchange = _CoinbaseExchange(base=self.qty + EXTERNAL, stop_qty=self.qty)

    def make_venue(self) -> CoinbaseVenue:
        return CoinbaseVenue(
            COINBASE_KEY,
            COINBASE_SECRET,
            self.exchange,
            product_id=self.pair,
            now_fn=lambda: NOW,
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

    def buys(self) -> list[str]:
        return [b["client_order_id"] for b in self._orders() if b["side"] == "BUY"]

    def resting_stops(self) -> list[Decimal]:
        return [
            Decimal(r["order_configuration"]["stop_limit_stop_limit_gtc"]["base_size"])
            for r in self.exchange.order_rows
            if r["status"] == "OPEN"
        ]

    def base_balance(self) -> Decimal:
        return self.exchange.balances["BTC"]

    def set_cancel(self, behaviour: str) -> None:
        self.exchange.cancel = behaviour

    def set_exit_executes(self, qty: Decimal) -> None:
        self.exchange.exit_executes = qty


MARKETS = {"paper": _PaperMarket, "kraken": _KrakenMarket, "coinbase": _CoinbaseMarket}


@pytest.fixture(params=sorted(MARKETS))
def market(request, home, monkeypatch, live_capable_release) -> _Market:
    cls = MARKETS[request.param]
    if cls.mode == "live":
        monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
        write_grant(home, venue=cls.venue, pair=cls.pair)
    return cls(home)


@pytest.fixture(params=["kraken", "coinbase"])
def live(request, home, monkeypatch, live_capable_release) -> _Market:
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    cls = MARKETS[request.param]
    write_grant(home, venue=cls.venue, pair=cls.pair)
    return cls(home)


# ---- it sells exactly the owned quantity, after cancelling the stop --------------------


def test_close_cancels_the_stop_and_sells_exactly_the_owned_quantity(market):
    assert market.resting_stops() == [market.qty]

    result = market.close()

    expected = "position_closed" if market.mode == "paper" else "close_pending"
    assert (result.code, result.ok, result.effect) == (expected, True, "changed")
    assert market.exits() == [(result.client_order_id, market.qty)]
    assert result.requested_qty == market.qty
    assert market.resting_stops() == []
    assert market.base_balance() == EXTERNAL  # the external units are untouched
    assert market.buys() == []
    [record] = _close_records(market.home)
    assert record["bar_ts"] == _bar_ts(HOLD)  # the latest closed bar the close read
    assert record["detail"]["close_coid"] == result.client_order_id
    body = next(
        json.loads(json.loads(payload)["body"])
        for kind, payload in _ledger(market.home)
        if kind == "outbox" and json.loads(payload)["coid"] == result.client_order_id
    )
    assert (body["kind"], Decimal(body["qty"]), body["bar_ts"]) == ("exit", market.qty, record["bar_ts"])


def test_paper_close_journals_what_its_venue_executed(home):
    market = _PaperMarket(home)

    result = market.close()

    assert (result.code, result.sold_qty) == ("position_closed", market.qty)
    [record] = _close_records(home)
    detail = record["detail"]
    assert (Decimal(detail["exit_qty"]), Decimal(detail["owned_qty_after"])) == (market.qty, Decimal(0))
    assert (detail["mode"], detail["intent_refused"]) == ("paper", None)
    assert market.owned() == 0
    assert _unresolved(home) == {}


@pytest.mark.parametrize("executed", ["all", "part"])
def test_live_close_journals_zero_then_settles_once_from_the_venue_record(live, executed):
    sold = live.qty if executed == "all" else live.qty * 2 / 5
    live.set_exit_executes(sold)

    result = live.close()

    assert result.code == "close_pending"
    coid = result.client_order_id
    [record] = _close_records(live.home)
    assert (record["detail"]["exit_qty"], Decimal(record["detail"]["owned_qty_after"])) == ("0", live.qty)
    assert live.owned() == live.qty  # an acknowledgement is not an executed quantity
    assert _unresolved(live.home) == {coid: "exit"}  # an obligation from before the send

    assert live.tick(HOLD) == 0
    assert live.tick(HOLD2) == 0

    assert _settled(live.home, coid) == [sold]  # once, from what the venue's record executed
    assert live.owned() == live.qty - sold
    assert _unresolved(live.home) == {}
    assert live.exits() == [(coid, live.qty)]  # never sent again
    assert live.base_balance() == EXTERNAL + live.qty - sold
    # A residual stays the strategy's and is re-protected, for itself only.
    assert live.resting_stops() == ([] if sold == live.qty else [live.qty - sold])
    assert live.buys() == []


# ---- entries stay off after a close, by the close's own marker -------------------------


def test_entries_stay_off_after_a_close_until_paper_resume(home):
    market = _PaperMarket(home)
    assert market.close().code == "position_closed"
    [armed] = load_config(home).armed
    assert armed.closed_by_operator_at == NOW
    assert armed.entries_paused is False  # its own marker, not the pause

    assert market.tick(REENTRY) == 0  # an entry signal

    assert market.buys() == []
    [record] = [r for r in _records(home) if r["bar_ts"] == _bar_ts(REENTRY)]
    assert (record["detail"]["reason"], record["detail"]["entries_paused"]) == ("entry", True)

    resumed = PaperService(home=home).resume_entries(venue=market.venue, pair=market.pair)
    assert (resumed.code, resumed.effect) == ("entries_resumed", "changed")
    assert load_config(home).armed[0].closed_by_operator_at is None
    assert market.tick((*REENTRY, "10", "13")) == 0  # the next entry signal
    assert len(market.buys()) == 1


def test_live_entries_stay_off_after_a_close_until_the_arm_is_armed_again(live):
    result = live.close()
    assert live.tick(HOLD) == 0  # settles the close

    assert live.tick(REENTRY) == 0  # an entry signal, nothing owned

    assert live.buys() == []
    [record] = [r for r in _records(live.home) if r["bar_ts"] == _bar_ts(REENTRY)]
    assert record["detail"]["entries_paused"] is True
    [armed] = load_config(live.home).armed
    assert (armed.closed_by_operator_at, armed.entries_paused) == (NOW, False)
    # The workstation's resume is paper-only; a live arm's marker goes with the arm.
    refused = PaperService(home=live.home).resume_entries(venue=live.venue, pair=live.pair)
    assert refused.code == "stored_mode_not_paper"
    assert run.disarm_pack(venue=live.venue, pair=live.pair, home=live.home) == 0
    save_config(live.home, Config(armed=[_armed(live.home, venue=live.venue, pair=live.pair, mode="live")]))
    assert load_config(live.home).armed[0].closed_by_operator_at is None
    assert result.client_order_id not in _unresolved(live.home)


# ---- the kill switch and the restore fence ----------------------------------------------


def test_the_kill_switch_alone_does_not_stop_a_close(market):
    live_gate.engage_kill(market.home, reason="operator halt", now=KILLED_AT)

    result = market.close()

    assert result.code == ("position_closed" if market.mode == "paper" else "close_pending")
    assert market.exits() == [(result.client_order_id, market.qty)]
    assert market.base_balance() == EXTERNAL
    assert _close_records(market.home)[0]["detail"]["kill_switch"] is True


class _MemVenue:
    """In-memory live venue for several pairs that records every call made on it.

    A market sell executes ``executes`` of its quantity (all by default) and
    acknowledges nothing executed, as a live acknowledgement proves nothing.
    ``order_outcome`` answers from ``outcomes``.
    """

    def __init__(self, *, balances: dict[str, Decimal], stops: list[OpenOrder], fills: list[Fill] | None = None):
        self.balances = dict(balances)
        self.orders = list(stops)
        self.fills = list(fills or [])
        self.outcomes: dict[str, OrderOutcome] = {}
        self.executes: dict[str, Decimal] = {}
        self.calls: list[tuple] = []

    def sent(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in {"entry", "exit", "stop", "cancel_stops", "raise_stop"}]

    def rules(self, pair: str) -> PairRules:
        return _rules(pair)

    def snapshot(self) -> Truth:
        self.calls.append(("snapshot",))
        balances = [Balance(asset=asset, free=free) for asset, free in self.balances.items()]
        return Truth(balances=balances, open_orders=list(self.orders), recent_fills=list(self.fills))

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        self.calls.append(("entry", coid, pair, qty))
        return OrderRef(id=coid, coid=coid, pair=pair, side="buy", qty=qty, filled_qty=qty, stop_price=stop)

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        self.calls.append(("exit", coid, pair, qty))
        base = run._base_quote(pair)[0]
        self.balances[base] -= self.executes.get(pair, qty)
        return OrderRef(id=coid, coid=coid, pair=pair, side="sell", qty=qty, filled_qty=Decimal(0))

    def place_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        self.calls.append(("stop", coid, pair, qty))
        self.orders.append(OpenOrder(id=coid, coid=coid, pair=pair, side="sell", qty=qty, stop_price=stop))
        return OrderRef(id=coid, coid=coid, pair=pair, side="sell", qty=qty, filled_qty=Decimal(0), stop_price=stop)

    def cancel_stops(self, pair: str) -> None:
        self.calls.append(("cancel_stops", pair))
        self.orders = [o for o in self.orders if not (o.pair == pair and o.stop_price is not None)]

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        self.calls.append(("raise_stop", pair, new_stop))

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        return None

    def order_outcome(self, coid: str, *, pair: str) -> OrderOutcome:
        self.calls.append(("order_outcome", coid))
        return self.outcomes.get(coid, UNKNOWN_OUTCOME)

    def check_key(self) -> KeyPerms:
        return KeyPerms(can_trade=True, can_withdraw=False)


OTHER = "close-other"  # a second live arm on the same venue


def _grant(home: Path, *pairs: str) -> None:
    payload = {
        "schema_version": "1",
        "granted_by": "operator",
        "expires_at": DEFAULT_EXPIRES_AT,
        "grants": [{"venue": "kraken", "pair": pair} for pair in pairs],
    }
    live_gate.auth_path(home).write_text(json.dumps(payload), encoding="utf-8")


def _two_live_arms(home: Path, monkeypatch) -> _MemVenue:
    """Two live Kraken arms: this pack owns 5 SUI with a resting stop, another owns 7 ADA with none."""
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    _grant(home, "SUIUSD", "ADAUSD")
    save_config(
        home,
        Config(
            armed=[
                _armed(home, venue="kraken", pair="SUIUSD", mode="live"),
                _armed(home, venue="kraken", pair="ADAUSD", mode="live", pack=OTHER),
            ]
        ),
    )
    _seed_owned(home, venue="kraken", pair="SUIUSD", mode="live", qty=Decimal(5))
    _seed_owned(home, venue="kraken", pair="ADAUSD", mode="live", qty=Decimal(7), pack=OTHER)
    return _MemVenue(
        balances={"USD": Decimal(1000), "SUI": Decimal(5) + EXTERNAL, "ADA": Decimal(7)},
        stops=[OpenOrder(id="OSTOP1", coid="", pair="SUIUSD", side="sell", qty=Decimal(5), stop_price=Decimal(6))],
    )


def _mem_close(home: Path, venue: _MemVenue, pair: str = "SUIUSD", confirmation: object = None):
    return run.close_position(
        venue="kraken",
        pair=pair,
        confirmation=pair if confirmation is None else confirmation,
        open_venue=lambda _armed: (venue, lambda _v, _p: _bars(HOLD)),
        home=home,
        clock=lambda: NOW,
    )


def _mem_tick(home: Path, venue: _MemVenue, closes: tuple[str, ...]) -> int:
    return run.tick(
        venue="kraken", venue_obj=venue, reader=lambda _v, _p: _bars(closes), home=home, clock=lambda: NOW + 60
    )


@pytest.mark.usefixtures("live_capable_release")
def test_a_live_close_under_the_kill_switch_leaves_the_venue_tick_running(home, monkeypatch):
    venue = _two_live_arms(home, monkeypatch)
    live_gate.engage_kill(home, reason="operator halt", now=KILLED_AT)
    venue.executes["SUIUSD"] = Decimal(2)  # the close executes 2 of 5

    result = _mem_close(home, venue)

    assert result.code == "close_pending"
    coid = result.client_order_id
    assert venue.sent() == [("cancel_stops", "SUIUSD"), ("exit", coid, "SUIUSD", Decimal(5))]
    closed, other = load_config(home).armed
    assert (closed.closed_by_operator_at, closed.entries_paused, other.closed_by_operator_at) == (NOW, False, None)
    assert not live_gate.restore_fence_engaged(live_gate.kill_state(home), entries_paused=closed.entries_paused)

    venue.outcomes[coid] = OrderOutcome(status="filled", filled_qty=Decimal(2), authoritative=True)
    venue.calls.clear()
    assert _mem_tick(home, venue, HOLD) == 0

    assert not any(r["detail"].get("reason") == "live_fenced" for r in kb_journal.read_records_lenient(home))
    assert ("order_outcome", coid) in venue.calls  # reconcile ran
    assert _settled(home, coid) == [Decimal(2)]  # the close settled once
    placed = [c[:4] for c in venue.sent() if c[0] in {"entry", "exit", "stop"}]
    assert placed == [
        ("stop", _coid("kraken", "SUIUSD", "stop", _bar_ts(HOLD)), "SUIUSD", Decimal(3)),  # the residual re-protected
        ("stop", _coid("kraken", "ADAUSD", "stop", _bar_ts(HOLD), OTHER), "ADAUSD", Decimal(7)),  # the other arm's stop
    ]

    venue.calls.clear()
    assert _mem_tick(home, venue, EXIT) == 0  # an exit signal for both

    exits = [c[:4] for c in venue.sent() if c[0] == "exit"]
    assert exits == [
        ("exit", _coid("kraken", "SUIUSD", "exit", _bar_ts(EXIT)), "SUIUSD", Decimal(3)),
        ("exit", _coid("kraken", "ADAUSD", "exit", _bar_ts(EXIT), OTHER), "ADAUSD", Decimal(7)),
    ]
    assert venue.balances["SUI"] >= EXTERNAL  # never an external unit


def _restored_home(home: Path, tmp_path_factory, monkeypatch) -> Path:
    """A live arm owning 5 SUI, backed up, then restored into a new home: the restore fence."""
    from krellbot.storage import home_backup

    save_config(home, Config(armed=[_armed(home, venue="kraken", pair="SUIUSD", mode="live")]))
    OperationalStore(home / "ops.sqlite").connect()
    archive = tmp_path_factory.mktemp("backup") / "home-backup.tar.gz"
    home_backup.create(home, archive, now=KILLED_AT)
    restored = tmp_path_factory.mktemp("restore") / "home"
    home_backup.restore(archive, restored)
    monkeypatch.setenv("KRELLBOT_HOME", str(restored))
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    write_grant(restored, venue="kraken", pair="SUIUSD")
    _seed_owned(restored, venue="kraken", pair="SUIUSD", mode="live", qty=Decimal(5))
    assert live_gate.kill_state(restored).engaged is True
    return restored


@pytest.mark.usefixtures("live_capable_release")
@pytest.mark.parametrize("change", ["restored", "operator_kill"])
def test_the_restore_fence_still_refuses_a_close(home, tmp_path_factory, monkeypatch, change):
    restored = _restored_home(home, tmp_path_factory, monkeypatch)
    if change == "operator_kill":
        live_gate.engage_kill(restored, reason="operator halt", now=KILLED_AT + 1)  # the arm's entries stay paused
    venue = _MemVenue(balances={"SUI": Decimal(105)}, stops=[])
    before = _durable(restored)

    result = _mem_close(restored, venue)

    assert (result.code, result.effect) == ("restore_fence", "refused")
    assert venue.calls == [], "the fence touches no venue"
    assert _durable(restored) == before


def test_the_shipped_paper_only_release_refuses_a_live_close(home, monkeypatch):
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    write_grant(home, venue="kraken", pair="SUIUSD")
    save_config(home, Config(armed=[_armed(home, venue="kraken", pair="SUIUSD", mode="live")]))
    _seed_owned(home, venue="kraken", pair="SUIUSD", mode="live", qty=Decimal(5))
    venue = _MemVenue(balances={"SUI": Decimal(105)}, stops=[])
    before = _durable(home)

    result = _mem_close(home, venue)

    assert result.code == live_gate.CODE_PAPER_ONLY_RELEASE
    assert venue.calls == []
    assert _durable(home) == before


# ---- refusals write nothing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("confirmation", "code"),
    [
        (None, "confirmation_required"),
        ("", "confirmation_required"),
        ("suiusd-wrong", "confirmation_mismatch"),
        (" <pair> ", "confirmation_mismatch"),
        (42, "bad_input"),
    ],
)
def test_a_missing_or_wrong_typed_pair_is_refused_and_writes_nothing(market, confirmation, code):
    if confirmation == " <pair> ":
        confirmation = f" {market.pair} "
    before = _durable(market.home)

    result = market.close(confirmation=confirmation)

    assert (result.code, result.ok, result.effect) == (code, False, "refused")
    assert market.opened == 0, "no venue is opened"
    assert _durable(market.home) == before
    assert market.resting_stops() == [market.qty]
    assert market.exits() == []


def test_a_close_of_a_pair_that_is_not_armed_is_refused(market):
    before = _durable(market.home)

    result = run.close_position(
        venue=market.venue, pair="ETHUSD", confirmation="ETHUSD", open_venue=market.open_venue, home=market.home
    )

    assert result.code == "not_armed"
    assert market.opened == 0
    assert _durable(market.home) == before


def test_ambiguous_ownership_refuses_a_close(market):
    # A record for this pack and pair that does not say its mode cannot be attributed.
    kb_journal.append(
        {
            "ts": 3,
            "kind": "tick",
            "venue": market.venue,
            "pack": PACK,
            "bar_ts": ENTRY_BAR,
            "detail": {"pair": market.pair, "reason": "entry", "entry_qty": "1", "exit_qty": "0", "stop_qty": "0"},
        }
    )
    before = _durable(market.home)

    result = market.close()

    assert result.code == "ownership_ambiguous"
    assert market.opened == 0
    assert _durable(market.home) == before
    assert market.exits() == []


@pytest.mark.usefixtures("live_capable_release")
def test_an_unrecorded_unlinked_sell_refuses_a_close_and_commits_no_hold(home, monkeypatch):
    venue = _two_live_arms(home, monkeypatch)
    venue.fills.append(
        Fill(
            id="T-UNKNOWN",
            coid="",
            pair="SUIUSD",
            side="sell",
            qty=Decimal(2),
            price=Decimal(6),
            ts_ms=1,
            order_unknown=True,
        )
    )
    before = _durable(home)

    result = _mem_close(home, venue)

    assert result.code == "ownership_ambiguous"
    assert venue.sent() == []
    assert _durable(home) == before  # no exit hold is committed by a close


def test_an_open_obligation_refuses_a_close(market):
    # An entry committed on an earlier bar whose outcome is not known.
    body = json.dumps(
        {
            "kind": "entry",
            "qty": "1",
            "stop": STOP,
            "pair": market.pair,
            "pack_id": PACK,
            "pack_version": VERSION,
            "bar_ts": H,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    store = OperationalStore(market.home / "ops.sqlite")
    store.connect()

    def reply_lost(_coid: str, _body: str) -> None:
        raise OSError("connection reset before the reply")

    with pytest.raises(OSError):
        Outbox(store).dispatch(
            _coid(market.venue, market.pair, "entry", H), body, reply_lost, mode=market.mode, venue=market.venue
        )
    before = _durable(market.home)

    result = market.close()

    assert result.code == "obligation_open"
    assert market.opened == 0
    assert _durable(market.home) == before


def test_a_running_tick_refuses_a_close(market):
    lock = TickLock(market.home, market.venue)
    assert lock.acquire()
    before = _durable(market.home)
    try:
        result = market.close()
    finally:
        lock.release()

    assert result.code == "tick_running"
    assert market.opened == 0
    assert _durable(market.home) == before


@pytest.mark.parametrize("behaviour", ["refuse", "ignore"])
def test_a_stop_that_cannot_be_cancelled_refuses_the_close_and_sells_nothing(live, behaviour):
    live.set_cancel(behaviour)
    before = _durable(live.home)

    result = live.close()

    assert (result.code, result.effect) == ("stop_cancel_failed", "refused")
    assert live.exits() == []
    assert _durable(live.home) == before  # the marker is put back
    assert live.base_balance() == live.qty + EXTERNAL


# ---- a repeat is refused, and nothing is resent -----------------------------------------


def test_a_repeat_close_is_refused_and_never_resent(market):
    first = market.close()

    second = market.close()

    assert second.code == ("nothing_to_close" if market.mode == "paper" else "obligation_open")
    assert market.exits() == [(first.client_order_id, market.qty)]
    assert len(_close_records(market.home)) == 1

    if market.mode == "live":
        assert market.tick(HOLD) == 0  # settles the first close
        assert market.close().code == "nothing_to_close"
        assert market.exits() == [(first.client_order_id, market.qty)]


@pytest.mark.usefixtures("live_capable_release")
def test_a_documented_venue_refusal_is_journaled_and_resolved_as_not_executed(home, monkeypatch):
    venue = _two_live_arms(home, monkeypatch)

    def refuse(coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        from krellbot.venues.base import VenueRejectedError

        venue.calls.append(("exit", coid, pair, qty))
        raise VenueRejectedError("kraken", "order_minimum_not_met")

    venue.place_exit = refuse

    result = _mem_close(home, venue)

    assert (result.code, result.ok, result.effect) == ("venue_rejected", False, "changed")
    [record] = _close_records(home)
    assert (record["detail"]["intent_refused"], record["detail"]["exit_qty"]) == ("order_minimum_not_met", "0")
    assert _unresolved(home) == {}  # resolved as not executed: it holds nothing
    assert _owned(home, venue="kraken", pair="SUIUSD", mode="live") == Decimal(5)


# ---- the loopback API and a read-only Tailscale session ---------------------------------

PORT = 8080


def _asgi(app, path: str, payload: dict, headers: list[tuple[str, str]]) -> tuple[int, bytes, list[str]]:
    body = json.dumps(payload).encode("utf-8")
    raw = [("host", f"127.0.0.1:{PORT}"), ("content-type", "application/json"), ("content-length", str(len(body)))]
    received = {"done": False}
    captured: dict = {"status": 500, "headers": [], "body": b""}

    async def receive() -> dict:
        if received["done"]:
            return {"type": "http.disconnect"}
        received["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            captured["status"] = int(message["status"])
            captured["headers"] = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "server": ("127.0.0.1", PORT),
        "client": ("127.0.0.1", 12345),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in [*raw, *headers]],
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    cookies = [v.decode("latin-1") for k, v in captured["headers"] if k.decode("latin-1").lower() == "set-cookie"]
    return captured["status"], captured["body"], cookies


def _loopback_app(home: Path, opener):
    from krellbot.api.app import create_app

    token = "boot-close-position"
    app = create_app(home=home, port=PORT, bootstrap_token=token)
    app.state.krellbot.close_venue_opener = opener
    origin = ("Origin", f"http://127.0.0.1:{PORT}")
    status, body, cookies = _asgi(app, "/api/v1/session/bootstrap", {"token": token}, [origin])
    assert status == 200, body
    session = next(c for c in cookies if c.startswith("krellbot_session=")).split(";", 1)[0]
    return app, [origin, ("X-Krellbot-CSRF", json.loads(body)["csrf_token"]), ("Cookie", session)]


def _command(pair: str, confirmation: object) -> dict:
    return {
        "schema_version": "1",
        "command": "positions.close",
        "payload": {"venue": "kraken", "pair": pair, "confirmation": confirmation},
    }


def test_positions_close_through_the_loopback_api(home):
    market = _PaperMarket(home)
    app, auth = _loopback_app(home, market.open_venue)

    status, body, _ = _asgi(app, "/api/v1/commands", _command(market.pair, "SUI"), auth)
    assert status == 200, body
    refused = json.loads(body)
    assert (refused["code"], refused["ok"], refused["effect"]) == ("confirmation_mismatch", False, "refused")
    assert market.opened == 0

    status, body, _ = _asgi(
        app, "/api/v1/commands", _command(market.pair, market.pair), [h for h in auth if h[0] != "X-Krellbot-CSRF"]
    )
    assert status == 403, "a state change still needs the CSRF token"
    assert market.opened == 0

    status, body, _ = _asgi(app, "/api/v1/commands", _command(market.pair, market.pair), auth)

    assert status == 200, body
    closed = json.loads(body)
    assert (closed["code"], closed["ok"], closed["effect"], closed["mode"]) == (
        "position_closed",
        True,
        "changed",
        "paper",
    )
    assert (Decimal(closed["requested_qty"]), Decimal(closed["sold_qty"])) == (market.qty, market.qty)
    assert market.exits() == [(closed["client_order_id"], market.qty)]
    assert market.base_balance() == EXTERNAL


TAILNET_ORIGIN = "https://workstation.example.ts.net"
TAILNET_HOST = "workstation.example.ts.net"
TAILNET_USER = "owner@example.com"


def _http(port: int, method: str, path: str, headers: dict[str, str], payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


@contextlib.contextmanager
def _tailnet_server(tmp_path: Path, home: Path):
    from krellbot.api.serve import WorkstationServer

    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text("<html><head></head><body>Workstation</body></html>")
    server = WorkstationServer(home=home, dist_dir=dist, tailscale_origin=TAILNET_ORIGIN, tailscale_user=TAILNET_USER)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_a_read_only_tailscale_session_cannot_close_a_position(home, tmp_path, monkeypatch):
    market = _PaperMarket(home)
    monkeypatch.setattr(run, "_default_close_venue", lambda _home: market.open_venue)
    with _tailnet_server(tmp_path, home) as server:
        remote = {"Host": TAILNET_HOST, "Tailscale-User-Login": TAILNET_USER}
        status, headers, _ = _http(server.bound_port, "POST", "/login", {**remote, "Origin": TAILNET_ORIGIN})
        assert status == 303
        authed = {**remote, "Cookie": headers["set-cookie"].split(";", 1)[0], "Origin": TAILNET_ORIGIN}
        status, _, body = _http(server.bound_port, "GET", "/api/v1/session/csrf", authed)
        assert status == 200, body
        authed = {**authed, "X-Krellbot-CSRF": json.loads(body)["csrf_token"], "Content-Type": "application/json"}
        before = _durable(home)

        status, _, body = _http(
            server.bound_port, "POST", "/api/v1/commands", authed, _command(market.pair, market.pair)
        )

    assert status == 403
    assert json.loads(body)["code"] == "remote_read_only"
    assert market.opened == 0
    assert _durable(home) == before
    assert market.exits() == []


# ---- the daily-loss gate prices a paper close (M28.31) ----------------------------------


def test_a_paper_close_is_priced_by_the_daily_loss_gate(home):
    market = _PaperMarket(home)
    config = load_config(home)
    config.armed[0].daily_loss_limit_pct = Decimal(50)
    save_config(home, config)

    assert market.close().code == "position_closed"
    assert market.tick(HOLD2) == 0  # measures the arm's equity after the close

    [armed] = load_config(home).armed
    projected = daily_loss.project(home, armed, now=NOW + 60)
    assert projected["code"] != daily_loss.CODE_LOSS_LIMIT_UNAVAILABLE, projected
    assert projected["equity"] is not None
    identity = OwnershipIdentity(pack_id=PACK, venue=market.venue, pair=market.pair, mode="paper")
    assert "unavailable" not in daily_loss.read_states(home, market.venue)[daily_loss.arm_key(identity)]


# ---- config bytes -----------------------------------------------------------------------


def test_an_arm_never_closed_keeps_its_config_bytes(home):
    armed = _armed(home, venue="kraken", pair="SUIUSD", mode="paper")
    save_config(home, Config(armed=[armed]))
    original = kb_config.config_path(home).read_bytes()

    assert b"closed_by_operator_at" not in original
    save_config(home, load_config(home))
    assert kb_config.config_path(home).read_bytes() == original

    config = load_config(home)
    config.armed[0].closed_by_operator_at = NOW
    save_config(home, config)
    assert json.loads(kb_config.config_path(home).read_bytes())["armed"][0]["closed_by_operator_at"] == NOW
    config.armed[0].closed_by_operator_at = None
    save_config(home, config)
    assert kb_config.config_path(home).read_bytes() == original


@pytest.mark.parametrize("stored", ["yesterday", 1.5, True])
def test_a_malformed_close_marker_still_holds_entries(home, stored):
    armed = _armed(home, venue="kraken", pair="SUIUSD", mode="paper")
    save_config(home, Config(armed=[armed]))
    raw = json.loads(kb_config.config_path(home).read_bytes())
    raw["armed"][0]["closed_by_operator_at"] = stored
    kb_config.config_path(home).write_text(json.dumps(raw), encoding="utf-8")

    [loaded] = load_config(home).armed

    assert loaded.closed_by_operator_at == 0
    assert kb_config.entries_held(loaded)
