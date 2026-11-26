"""An open order's requested quantity is not strategy-owned inventory.

A tick owns ``min(venue balance, max(journal, recognized))``. ``recognized``
exists so a position whose journal write was lost after its send stays
protected and can still exit: the protective stop the engine placed for it is
still resting. It counts only that stop:

* an unfilled resting entry is not owned: its units were never bought;
* an entry and its protective stop are one position, so only the stop counts;
* one stop listed twice counts once, and two stops of the identity cover the
  same units, so the largest counts, never their sum;
* another strategy's, another venue's (KQ-01) and external orders never count;
* a live venue lists a resting order at its requested size (Kraken ``vol``,
  Coinbase ``base_size``), which a partial fill does not lower, so on live no
  resting order raises ownership above the journal that venue evidence keeps.

External holdings on the venue are never sold. Paper runs on PaperVenue and on
a fake paper venue; live runs on fake Kraken and Coinbase transports with the
release gate patched in-process (``live_capable_release``).
``PAPER_ONLY_RELEASE`` stays True in source.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fakes.fake_coinbase import FakeCoinbaseTransport, order_record
from fakes.fake_coinbase import RecordedCall as CoinbaseCall
from fakes.fake_kraken import FakeKrakenTransport, order_info
from fakes.live_grant import write_grant

from krellbot import journal as kb_journal
from krellbot import run
from krellbot.config import ArmedPack, Config, save_config
from krellbot.pack.model import Candle
from krellbot.venues.base import Balance, OpenOrder, OrderRef, PairRules, Truth
from krellbot.venues.coinbase import CoinbaseVenue
from krellbot.venues.kraken import KrakenVenue, coid_userref
from krellbot.venues.paper import PaperVenue

PACK = "open-order-owner"
OTHER_PACK = "someone-elses-pack"
VERSION = "1.0.0"
H = 3_600_000
ENTRY_BAR = 2 * H
EXTERNAL = Decimal(100)
RULES = PairRules(ordermin=Decimal("0.01"), costmin=Decimal("0.5"), lot_decimals=5, price_decimals=4)

PAPER_VENUE = "kraken"
PAPER_PAIR = "SUIUSD"


def _candle(ts_ms: int, close: str) -> Candle:
    c = Decimal(close)
    return Candle(ts_ms=ts_ms, open=c, high=c + 1, low=c - 1, close=c, volume=Decimal(100))


# The pack enters when close crosses above sma2 and exits when it crosses below.
ENTRY_BARS = [_candle(0, "10"), _candle(H, "10"), _candle(ENTRY_BAR, "12")]
EXIT_BARS = [*ENTRY_BARS, _candle(3 * H, "10")]
HOLD_BARS = [*ENTRY_BARS, _candle(3 * H, "13")]
HOLD_THEN_EXIT_BARS = [*HOLD_BARS, _candle(4 * H, "10")]


def _coid(venue: str, pair: str, *, intent: str = "entry", pack: str = PACK) -> str:
    return run.coid_for(pack_id=pack, pack_version=VERSION, venue=venue, pair=pair, bar_ts=ENTRY_BAR, intent=intent)


def _arm(home: Path, *, venue: str, pair: str, mode: str) -> None:
    pack = {
        "schema_version": 1,
        "id": PACK,
        "version": VERSION,
        "label": "Open order owner",
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
        pack_version=VERSION,
        venue=venue,
        pair=pair,
        cap=Decimal(100),
        stop=Decimal(5),
        mode=mode,
        starting_cash=Decimal(100),
        requires_license=False,
        armed_at_ts=1,
    )
    save_config(home, Config(armed=[armed]))


def _journal(*, venue: str, pair: str, mode: str, **fields: object) -> None:
    """One tick record for this pack's identity, as an earlier tick wrote it."""
    detail = {"pair": pair, "mode": mode, "entry_qty": "0", "exit_qty": "0", "stop_qty": "0"}
    detail.update({key: str(value) for key, value in fields.items()})
    kb_journal.append({"ts": 1, "kind": "tick", "venue": venue, "pack": PACK, "bar_ts": ENTRY_BAR, "detail": detail})


def _journaled(home: Path, bar_ts: int, field: str) -> Decimal:
    """``field`` of the one tick record this pack journaled for ``bar_ts``."""
    [record] = [
        r["detail"]
        for r in kb_journal.read_records_lenient(home)
        if r.get("kind") == "tick" and r.get("pack") == PACK and r.get("bar_ts") == bar_ts
    ]
    return Decimal(record[field])


# ---- paper ------------------------------------------------------------------------


def _stop(coid: str, qty: str, *, pair: str = PAPER_PAIR) -> OpenOrder:
    return OpenOrder(id=f"id-{coid}", coid=coid, pair=pair, side="sell", qty=Decimal(qty), stop_price=Decimal(6))


def _entry(coid: str, qty: str, *, stop_price: Decimal | None = None) -> OpenOrder:
    return OpenOrder(id=f"id-{coid}", coid=coid, pair=PAPER_PAIR, side="buy", qty=Decimal(qty), stop_price=stop_price)


class _PaperBook:
    """A paper-mode venue whose balances and resting orders the test sets."""

    def __init__(self, *, sui: Decimal, orders: list[OpenOrder]) -> None:
        self.balances = {"SUI": sui, "USD": Decimal(1000)}
        self.orders = list(orders)
        self.calls: list[tuple[str, Decimal]] = []

    def rules(self, pair: str) -> PairRules:
        return RULES

    def snapshot(self) -> Truth:
        balances = [Balance(asset=asset, free=qty) for asset, qty in self.balances.items() if qty > 0]
        return Truth(balances=balances, open_orders=list(self.orders), recent_fills=[])

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        self.calls.append(("entry", qty))
        self.balances["SUI"] += qty
        return OrderRef(id=coid, coid=coid, pair=pair, side="buy", qty=qty, filled_qty=qty, stop_price=stop)

    def place_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> None:
        self.calls.append(("stop", qty))
        self.orders.append(OpenOrder(id=coid, coid=coid, pair=pair, side="sell", qty=qty, stop_price=stop))

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        self.calls.append(("exit", qty))
        self.balances["SUI"] -= qty
        return OrderRef(id=coid, coid=coid, pair=pair, side="sell", qty=qty, filled_qty=qty)

    def cancel_stops(self, pair: str) -> None:
        self.orders = [o for o in self.orders if not (o.pair == pair and o.stop_price is not None)]

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        pass

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        return next((o for o in self.orders if o.coid == coid), None)

    def sold(self) -> list[Decimal]:
        return [qty for method, qty in self.calls if method in ("exit", "stop")]

    def exits(self) -> list[Decimal]:
        return [qty for method, qty in self.calls if method == "exit"]


@pytest.fixture
def paper(home):
    _arm(home, venue=PAPER_VENUE, pair=PAPER_PAIR, mode="paper")
    return home


def _paper_exit_bar(home: Path, book: _PaperBook) -> None:
    assert run.tick(venue=PAPER_VENUE, venue_obj=book, reader=lambda *_: EXIT_BARS, home=home, clock=lambda: 1) == 0


PAPER_ENTRY = _coid(PAPER_VENUE, PAPER_PAIR)
PAPER_REPAIRED_STOP = _coid(PAPER_VENUE, PAPER_PAIR, intent="stop")


@pytest.mark.parametrize("stop_price", [None, Decimal(6)], ids=["plain-entry", "entry-carrying-a-stop-price"])
def test_unfilled_resting_entry_is_not_owned(paper, stop_price):
    # Nothing was bought: the venue's units are all external.
    book = _PaperBook(sui=EXTERNAL, orders=[_entry(PAPER_ENTRY, "5", stop_price=stop_price)])

    _paper_exit_bar(paper, book)

    assert book.sold() == []
    assert book.balances["SUI"] == EXTERNAL
    assert _journaled(paper, 3 * H, "owned_qty_after") == 0


@pytest.mark.parametrize("stop_coid", [PAPER_ENTRY, PAPER_REPAIRED_STOP], ids=["entry-stop", "repaired-stop"])
def test_entry_and_its_protective_stop_count_once(paper, stop_coid):
    # The journal never recorded the send; the venue holds 5 bought plus external units.
    book = _PaperBook(sui=5 + EXTERNAL, orders=[_entry(PAPER_ENTRY, "5"), _stop(stop_coid, "5")])

    _paper_exit_bar(paper, book)

    assert book.exits() == [Decimal(5)]
    assert book.balances["SUI"] == EXTERNAL
    assert _journaled(paper, 3 * H, "exit_qty") == 5


@pytest.mark.parametrize(
    ("stops", "owned"),
    [
        ([_stop(PAPER_ENTRY, "5"), _stop(PAPER_ENTRY, "5")], Decimal(5)),
        ([_stop(PAPER_ENTRY, "5"), _stop(PAPER_ENTRY, "4")], Decimal(4)),
        ([_stop(PAPER_ENTRY, "5"), _stop(PAPER_REPAIRED_STOP, "5")], Decimal(5)),
    ],
    ids=["one-coid-listed-twice", "one-coid-at-two-sizes-counts-the-smaller", "entry-stop-and-repaired-stop"],
)
def test_one_position_counts_once_however_many_stops_list_it(paper, stops, owned):
    book = _PaperBook(sui=5 + EXTERNAL, orders=stops)

    _paper_exit_bar(paper, book)

    assert book.exits() == [owned]
    assert book.balances["SUI"] == 5 + EXTERNAL - owned


@pytest.mark.parametrize(
    "order",
    [
        _stop(_coid(PAPER_VENUE, PAPER_PAIR, pack=OTHER_PACK), "5"),
        _stop(_coid(PAPER_VENUE, PAPER_PAIR, pack=OTHER_PACK) + "_stop", "5"),
        _stop("manual-0f6c", "5"),
        _stop(PAPER_ENTRY, "5", pair="ADAUSD"),
        OpenOrder(
            id="exit", coid=_coid(PAPER_VENUE, PAPER_PAIR, intent="exit"), pair=PAPER_PAIR, side="sell", qty=Decimal(5)
        ),
    ],
    ids=["other-strategy-stop", "other-strategy-suffixed-stop", "external-stop", "other-pair", "own-exit-order"],
)
def test_other_strategies_and_external_orders_are_not_owned(paper, order):
    book = _PaperBook(sui=EXTERNAL, orders=[order])

    _paper_exit_bar(paper, book)

    assert book.sold() == []
    assert book.balances["SUI"] == EXTERNAL
    assert _journaled(paper, 3 * H, "owned_qty_after") == 0


@pytest.mark.parametrize(
    ("stop_venue", "owned"),
    [(PAPER_VENUE, Decimal(5)), ("coinbase", Decimal(0))],
    ids=["stop-placed-on-this-venue", "stop-placed-on-another-venue"],
)
def test_a_stop_counts_only_on_the_venue_whose_arm_placed_it(paper, stop_venue, owned):
    # KQ-01: ownership is (pack, venue, pair, mode). The same pack's position on
    # another venue, journaled there, owns nothing here.
    _journal(venue="coinbase", pair=PAPER_PAIR, mode="paper", entry_qty=Decimal(5))
    book = _PaperBook(sui=5 + EXTERNAL, orders=[_stop(_coid(stop_venue, PAPER_PAIR), "5")])

    _paper_exit_bar(paper, book)

    assert book.exits() == ([owned] if owned else [])
    assert book.balances["SUI"] == 5 + EXTERNAL - owned


@pytest.mark.parametrize(
    ("journal", "stop", "sui", "owned"),
    [
        (Decimal(5), "3", 5 + EXTERNAL, Decimal(5)),
        (Decimal(2), "5", 5 + EXTERNAL, Decimal(5)),
        (Decimal(0), "5", Decimal(3), Decimal(3)),
        (Decimal(4), None, 4 + EXTERNAL, Decimal(4)),
    ],
    ids=["journal-above-stop", "stop-above-journal", "venue-balance-caps-both", "journal-without-stop"],
)
def test_owned_is_the_venue_balance_capped_larger_of_journal_and_resting_stop(paper, journal, stop, sui, owned):
    if journal:
        _journal(venue=PAPER_VENUE, pair=PAPER_PAIR, mode="paper", entry_qty=journal)
    book = _PaperBook(sui=sui, orders=[_stop(PAPER_ENTRY, stop)] if stop else [])

    _paper_exit_bar(paper, book)

    assert book.exits() == [owned]
    assert book.balances["SUI"] == sui - owned
    assert _journaled(paper, 3 * H, "exit_qty") == owned


def _paper_venue(home: Path, bars: list[Candle]) -> PaperVenue:
    return PaperVenue(
        PAPER_VENUE,
        rules_provider=lambda _pair: RULES,
        candle_reader=lambda *_: bars,
        home=home,
        clock=lambda: 1,
    )


def _seed_paper_account(home: Path) -> None:
    """A paper account that already holds external SUI beside its cash."""
    state = home / "run" / f"paper-{PAPER_VENUE}.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "venue": PAPER_VENUE,
                "seeded": True,
                "balances": {"USD": "1000", "SUI": str(EXTERNAL)},
                "recent_fills": [],
                "open_orders": [],
            }
        ),
        encoding="utf-8",
    )


def _paper_tick(home: Path, bars: list[Candle]) -> PaperVenue:
    venue = _paper_venue(home, bars)  # a fresh venue each tick: a process restart
    assert run.tick(venue=PAPER_VENUE, venue_obj=venue, reader=lambda *_: bars, home=home, clock=lambda: 1) == 0
    return venue


def test_resting_paper_stop_keeps_protecting_a_journaled_position_after_restart(paper):
    _seed_paper_account(paper)
    _paper_tick(paper, ENTRY_BARS)
    bought = _journaled(paper, ENTRY_BAR, "entry_qty")
    assert bought > 0

    held = _paper_tick(paper, HOLD_BARS)

    assert _journaled(paper, 3 * H, "owned_qty_after") == bought
    [stop] = held.snapshot().open_orders
    assert (stop.coid, stop.side, stop.qty) == (PAPER_ENTRY, "sell", bought)

    exited = _paper_tick(paper, HOLD_THEN_EXIT_BARS)

    assert _journaled(paper, 4 * H, "exit_qty") == bought
    assert exited.owned_qty(PAPER_PAIR) == EXTERNAL
    assert exited.snapshot().open_orders == []


def test_resting_paper_stop_lets_an_unjournaled_position_exit_without_selling_external_units(paper):
    # The send reached the venue and the process stopped before the journal write.
    _seed_paper_account(paper)
    _paper_venue(paper, ENTRY_BARS).place_entry_with_stop(PAPER_ENTRY, Decimal(5), Decimal(6), pair=PAPER_PAIR)

    exited = _paper_tick(paper, EXIT_BARS)

    assert _journaled(paper, 3 * H, "exit_qty") == 5
    assert exited.owned_qty(PAPER_PAIR) == EXTERNAL


# ---- live -------------------------------------------------------------------------

KRAKEN_KEY = "FAKE_KEY_FOR_KRAKEN_TESTS"
KRAKEN_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
COINBASE_KEY = "organizations/fake-org/apiKeys/fake-coinbase-key-001"
COINBASE_SECRET = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQGKiOPddAnxlf1S2y08ul1yymcJvx2UEhvzdIgBtA9vXA=="


class _KrakenAccount:
    """A Kraken account on the fake transport: balances and the open-order book."""

    venue = "kraken"
    pair = "SUIUSD"
    base = "SUI"

    def __init__(self, home: Path) -> None:
        self.home = home
        self.entry = _coid(self.venue, self.pair)
        self.repaired_stop = _coid(self.venue, self.pair, intent="stop")
        self.transport = FakeKrakenTransport(
            asset_pairs={self.pair: {"ordermin": "0.01", "costmin": "0.5", "lot_decimals": 5, "pair_decimals": 4}},
            balances={"USD": "100"},
        )

    def make_venue(self) -> KrakenVenue:
        return KrakenVenue(
            KRAKEN_KEY,
            KRAKEN_SECRET,
            self.transport,
            now_ms=lambda: 1_700_000_000_000,
            min_interval_ms=0,
            home=self.home,
        )

    def hold(self, base: Decimal, orders: dict[str, dict]) -> None:
        self.transport.balances = {"USD": "100", self.base: str(base)}
        self.transport.set_order_books(open_rows=orders)

    def unfilled_entry(self, qty: Decimal) -> dict[str, dict]:
        row = order_info(
            vol=str(qty),
            vol_exec="0",
            status="open",
            pair=self.pair,
            ordertype="limit",
            cl_ord_id=self.entry,
            userref=coid_userref(self.entry),
        )
        return {"OENTRY-AAAAA-BBBBBB": row}

    def stop(self, qty: Decimal, executed: Decimal, *, repaired: bool) -> dict[str, dict]:
        """A resting sell stop listed at its requested ``vol``, ``executed`` of it done.

        The entry's own stop is its conditional close: no ``cl_ord_id``, only a
        ``refid`` to the entry. A repaired stop carries the stop coid.
        """
        row = order_info(
            vol=str(qty),
            vol_exec=str(executed),
            status="open",
            pair=self.pair,
            side="sell",
            ordertype="stop-loss",
            cl_ord_id=self.repaired_stop if repaired else None,
            refid=None if repaired else "OENTRY-AAAAA-BBBBBB",
            stopprice="6.0000",
        )
        return {"OSTOP-CCCCC-DDDDDD": row}

    def orders_placed(self) -> list[dict]:
        return [c.form for c in self.transport.calls if c.url.endswith("/AddOrder")]

    def sells(self) -> list[Decimal]:
        return [Decimal(form["volume"]) for form in self.orders_placed() if form.get("type") == "sell"]


class _CoinbaseBook(FakeCoinbaseTransport):
    """The Coinbase fake with order rows and balances the test sets."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.order_rows: list[dict] = []
        self.balances: dict[str, Decimal] = {"USD": Decimal(100)}

    def get(self, url: str, headers: dict[str, str]) -> dict:
        if "orders/historical/batch" in url:
            self.calls.append(CoinbaseCall(method="GET", url=url, headers=dict(headers)))
            return {"orders": [dict(row) for row in self.order_rows], "has_next": False}
        if "/brokerage/accounts" in url:
            self.calls.append(CoinbaseCall(method="GET", url=url, headers=dict(headers)))
            return {
                "accounts": [
                    {"currency": asset, "available_balance": {"value": str(value), "currency": asset}}
                    for asset, value in self.balances.items()
                ]
            }
        return super().get(url, headers)


class _CoinbaseAccount:
    """A Coinbase account on the fake transport: balances and the open-order list."""

    venue = "coinbase"
    pair = "BTC-USD"
    base = "BTC"

    def __init__(self, home: Path) -> None:
        self.home = home
        self.entry = _coid(self.venue, self.pair)
        self.repaired_stop = _coid(self.venue, self.pair, intent="stop")
        self.transport = _CoinbaseBook(
            product={
                "product_id": self.pair,
                "base_increment": "0.00000001",
                "quote_increment": "0.01",
                "base_min_size": "0.00000001",
                "quote_min_size": "1",
                "price": "12",
            }
        )

    def make_venue(self) -> CoinbaseVenue:
        return CoinbaseVenue(
            COINBASE_KEY,
            COINBASE_SECRET,
            self.transport,
            product_id=self.pair,
            now_fn=lambda: 1_700_000_000,
            nonce_fn=lambda: "aa" * 16,
        )

    def hold(self, base: Decimal, orders: list[dict]) -> None:
        self.transport.balances = {"USD": Decimal(100), self.base: base}
        self.transport.order_rows = list(orders)

    def unfilled_entry(self, qty: Decimal) -> list[dict]:
        return [
            order_record(
                client_order_id=self.entry, status="OPEN", filled_size="0", base_size=str(qty), product_id=self.pair
            )
        ]

    def stop(self, qty: Decimal, executed: Decimal, *, repaired: bool) -> list[dict]:
        """A resting stop-limit sell listed at its requested ``base_size``, ``executed`` of it done.

        The entry's own stop is ``<entry coid>_stop``; a repaired stop carries the stop coid.
        """
        client_order_id = self.repaired_stop if repaired else self.entry + "_stop"
        row = order_record(
            client_order_id=client_order_id,
            status="OPEN",
            filled_size=str(executed),
            base_size=str(qty),
            product_id=self.pair,
            side="SELL",
            stop_price="6",
        )
        return [row]

    def orders_placed(self) -> list[dict]:
        return [c.body or {} for c in self.transport.calls if c.method == "POST" and c.url.endswith("/orders")]

    def sells(self) -> list[Decimal]:
        return [
            Decimal(body["order_configuration"]["market_market_ioc"]["base_size"])
            for body in self.orders_placed()
            if body.get("side") == "SELL" and "market_market_ioc" in body.get("order_configuration", {})
        ]


@pytest.fixture(params=["kraken", "coinbase"])
def live(request, home, monkeypatch, live_capable_release):
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    account = {"kraken": _KrakenAccount, "coinbase": _CoinbaseAccount}[request.param](home)
    write_grant(home, venue=account.venue, pair=account.pair)
    _arm(home, venue=account.venue, pair=account.pair, mode="live")
    return account


def _live_tick(live, bars: list[Candle]) -> None:
    rc = run.tick(
        venue=live.venue,
        venue_obj=live.make_venue(),  # a fresh adapter each tick: a process restart
        reader=lambda *_: bars,
        home=live.home,
        clock=lambda: 1_700_000_000,
    )
    assert rc == 0


def test_live_unfilled_resting_entry_is_not_owned(live):
    live.hold(EXTERNAL, live.unfilled_entry(Decimal(5)))

    _live_tick(live, EXIT_BARS)

    assert live.orders_placed() == []
    assert _journaled(live.home, 3 * H, "owned_qty_after") == 0


@pytest.mark.parametrize("repaired", [False, True], ids=["entry-stop", "repaired-stop"])
def test_live_stop_listed_at_its_requested_size_does_not_undo_a_journaled_partial_fill(live, repaired):
    # The stop sold 2 of 5 and that fill is journaled; the venue still lists the stop at 5.
    _journal(venue=live.venue, pair=live.pair, mode="live", entry_qty=Decimal(5), entry_coid=live.entry)
    _journal(venue=live.venue, pair=live.pair, mode="live", stop_qty=Decimal(2))
    live.hold(3 + EXTERNAL, live.stop(Decimal(5), Decimal(2), repaired=repaired))

    _live_tick(live, EXIT_BARS)

    assert live.sells() == [Decimal(3)]
    assert _journaled(live.home, 3 * H, "exit_qty") == 3


def test_live_resting_stop_keeps_protecting_a_journaled_position_after_restart(live):
    _journal(venue=live.venue, pair=live.pair, mode="live", entry_qty=Decimal(5), entry_coid=live.entry)
    live.hold(5 + EXTERNAL, live.stop(Decimal(5), Decimal(0), repaired=False))

    _live_tick(live, HOLD_BARS)

    # The stop is resting: nothing is placed, and the journaled position is held.
    assert live.orders_placed() == []
    assert _journaled(live.home, 3 * H, "owned_qty_after") == 5

    _live_tick(live, HOLD_THEN_EXIT_BARS)

    assert live.sells() == [Decimal(5)]
    assert _journaled(live.home, 4 * H, "exit_qty") == 5
