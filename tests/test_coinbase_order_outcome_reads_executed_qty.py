"""Coinbase reports an order's outcome from its own record's executed size.

A Coinbase market IOC entry whose response carries no fill evidence raises
'coinbase entry was not filled; stop not placed' after the Outbox commit, so
the intent stays an obligation that holds new live Coinbase entries. The same
error follows a rejected entry. ``CoinbaseVenue.order_outcome`` finds the
coid's own order by exact ``client_order_id`` in the bounded order history
(``_find_order``) and reads what executed (``filled_size``), never the
requested ``base_size``. The run journals that quantity once under
``resolved_coid`` and releases the hold. An order the five bounded pages do
not show, or a record that cannot prove what executed, is ``unknown``: the
hold stays and nothing is sent again.

Runs on a fake Coinbase transport with the release gate patched in-process
(``live_capable_release``); ``PAPER_ONLY_RELEASE`` stays True in source.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fakes.fake_coinbase import FakeCoinbaseTransport, order_record
from fakes.live_grant import write_grant

from krellbot import journal as kb_journal
from krellbot import run
from krellbot.config import ArmedPack, Config, save_config
from krellbot.execution import obligations as kb_obligations
from krellbot.execution.obligations import read_obligations
from krellbot.pack.model import Candle
from krellbot.run.reconcile import OwnershipIdentity, project_ownership
from krellbot.storage.database import OperationalStore
from krellbot.venues.base import OrderOutcome
from krellbot.venues.coinbase import CoinbaseVenue

pytestmark = pytest.mark.usefixtures("live_capable_release")

KEY = "organizations/fake-org/apiKeys/fake-coinbase-key-001"
SECRET = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQGKiOPddAnxlf1S2y08ul1yymcJvx2UEhvzdIgBtA9vXA=="
VENUE = "coinbase"
PACK = "cb-ioc-outcome"
PAIR = "BTC-USD"
H = 3_600_000
ENTRY_BAR = 2 * H
IDENTITY = OwnershipIdentity(pack_id=PACK, venue=VENUE, pair=PAIR, mode="live")
TRADE_ONLY = [
    {"scope": "view", "can_view": True, "can_transfer": False},
    {"scope": "trade", "can_trade": True, "can_transfer": False},
]
PRODUCT = {
    "product_id": PAIR,
    "base_increment": "0.00000001",
    "quote_increment": "0.01",
    "base_min_size": "0.00000001",
    "quote_min_size": "1",
    "price": "12",
}


def _candle(ts_ms: int, close: str) -> Candle:
    c = Decimal(close)
    return Candle(ts_ms=ts_ms, open=c, high=c + 1, low=c - 1, close=c, volume=Decimal(100))


ENTRY_BARS = [_candle(0, "10"), _candle(H, "10"), _candle(ENTRY_BAR, "12")]
# Three bars later the pack signals a fresh entry (close crosses above sma2).
NEXT_ENTRY_BARS = [*ENTRY_BARS, _candle(3 * H, "10"), _candle(4 * H, "9"), _candle(5 * H, "12")]
ENTRY_COID = run.coid_for(pack_id=PACK, pack_version="1.0.0", venue=VENUE, pair=PAIR, bar_ts=ENTRY_BAR, intent="entry")

# The documented Create Order success carries no status and no filled size.
ACK_WITHOUT_FILL = {
    "success": True,
    "success_response": {"order_id": "entry-order", "product_id": PAIR, "side": "BUY", "client_order_id": ENTRY_COID},
}
REJECTED = {
    "success": False,
    "failure_reason": "UNKNOWN_FAILURE_REASON",
    "error_response": {
        "error": "INSUFFICIENT_FUND",
        "message": "Insufficient balance in source account",
        "preview_failure_reason": "PREVIEW_INSUFFICIENT_FUND",
    },
}


@pytest.fixture
def live(home, monkeypatch) -> Path:
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    write_grant(home, venue=VENUE, pair=PAIR)
    pack = {
        "schema_version": 1,
        "id": PACK,
        "version": "1.0.0",
        "label": "Coinbase IOC outcome",
        "author": "krellbot tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 50}},
        "markets": [{"venue": VENUE, "pair": PAIR}],
    }
    pack_path = home / f"{PACK}.json"
    pack_path.write_text(json.dumps(pack), encoding="utf-8")
    armed = ArmedPack(
        pack_path=str(pack_path),
        pack_sha256="0" * 64,
        pack_id=PACK,
        pack_version="1.0.0",
        venue=VENUE,
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


def _page(*rows: dict, next_cursor: str | None = None) -> dict:
    page: dict = {"orders": list(rows), "has_next": next_cursor is not None}
    if next_cursor is not None:
        page["cursor"] = next_cursor
    return page


def _transport(*, orders: dict, post_responses: list[dict] | None = None, btc: str = "0") -> FakeCoinbaseTransport:
    accounts = [{"currency": "USD", "available_balance": {"value": "1000", "currency": "USD"}}]
    if Decimal(btc):
        accounts.append({"currency": "BTC", "available_balance": {"value": btc, "currency": "BTC"}})
    return FakeCoinbaseTransport(
        post_responses=post_responses,
        key_permissions=list(TRADE_ONLY),
        product=dict(PRODUCT),
        accounts={"accounts": accounts},
        orders=orders,
        fills={"fills": []},
    )


def _venue(transport: FakeCoinbaseTransport, *, product_id: str = PAIR) -> CoinbaseVenue:
    return CoinbaseVenue(
        KEY, SECRET, transport, product_id=product_id, now_fn=lambda: 1_700_000_000, nonce_fn=lambda: "aa" * 16
    )


def _tick(home: Path, transport: FakeCoinbaseTransport, bars: list[Candle]) -> None:
    rc = run.tick(
        venue=VENUE, venue_obj=_venue(transport), reader=lambda *_: bars, home=home, clock=lambda: 1_700_000_000
    )
    assert rc == 0


def _posts(transport: FakeCoinbaseTransport) -> list:
    return [c for c in transport.calls if c.method == "POST"]


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
    return read_obligations(home).blocks_entries(venue=VENUE, mode="live")


def _enter(home: Path, ack: dict) -> Decimal:
    """Tick the entry bar; return the requested size of the one IOC buy sent."""
    transport = _transport(orders=_page(), post_responses=[ack])
    _tick(home, transport, ENTRY_BARS)

    [buy] = _posts(transport)  # the entry only: no protective stop follows it
    assert buy.body["side"] == "BUY" and buy.body["client_order_id"] == ENTRY_COID
    [record] = _tick_records(home, ENTRY_BAR)
    assert record["entry_qty"] == "0"  # no fill evidence: nothing is credited
    assert project_ownership(home, IDENTITY).attributable_qty == 0
    assert [p["sent"] for kind, p in _ledger(home) if kind == "outbox"] == [False]
    view = read_obligations(home)
    assert [o.coid for o in view.unresolved] == [ENTRY_COID]
    assert view.blocks_entries(venue=VENUE, mode="live")
    assert not view.blocks_entries(venue=VENUE, mode="paper")
    assert not view.blocks_entries(venue="kraken", mode="live")
    return Decimal(buy.body["order_configuration"]["market_market_ioc"]["base_size"])


def _entry_record(*, requested: Decimal, filled: str, status: str) -> dict:
    return order_record(client_order_id=ENTRY_COID, status=status, filled_size=filled, base_size=str(requested))


def _resting_stop(qty: Decimal) -> dict:
    return order_record(
        client_order_id=f"{ENTRY_COID}_stop",
        status="OPEN",
        filled_size="0",
        base_size=str(qty),
        side="SELL",
        stop_price="6.00",
    )


# ---- the run engine --------------------------------------------------------------


def test_ioc_entry_without_fill_evidence_is_journaled_once_from_its_filled_size(live):
    requested = _enter(live, ACK_WITHOUT_FILL)

    later = []
    for _ in range(2):
        transport = _transport(
            orders=_page(
                _entry_record(requested=requested, filled=str(requested), status="FILLED"), _resting_stop(requested)
            ),
            btc=str(requested),
        )
        _tick(live, transport, ENTRY_BARS)
        later.append(transport)

    assert _reconciled_entry_qty(live) == [requested]
    assert project_ownership(live, IDENTITY).attributable_qty == requested
    [resolution] = _resolutions(live)
    assert (resolution["coid"], resolution["outcome"], resolution["evidence"]) == (ENTRY_COID, "filled", "venue_fill")
    assert Decimal(resolution["filled_qty"]) == requested
    assert not _held(live)
    assert [_posts(t) for t in later] == [[], []]


def test_partly_executed_ioc_entry_is_journaled_at_its_filled_size(live):
    requested = _enter(live, ACK_WITHOUT_FILL)
    first = (requested / 4).quantize(Decimal("0.00000001"))
    final = (requested / 2).quantize(Decimal("0.00000001"))

    # Still working with some size executed: account it, keep the hold.
    working = _transport(
        orders=_page(_entry_record(requested=requested, filled=str(first), status="OPEN")), btc=str(first)
    )
    _tick(live, working, ENTRY_BARS)
    assert _reconciled_entry_qty(live) == [first]
    assert _resolutions(live) == []
    assert _held(live)

    # Coinbase cancelled the IOC remainder: the filled size is final.
    later = []
    for _ in range(2):
        transport = _transport(
            orders=_page(
                _entry_record(requested=requested, filled=str(final), status="CANCELLED"), _resting_stop(final)
            ),
            btc=str(final),
        )
        _tick(live, transport, ENTRY_BARS)
        later.append(transport)

    assert sum(_reconciled_entry_qty(live)) == final < requested
    assert project_ownership(live, IDENTITY).attributable_qty == final
    [resolution] = _resolutions(live)
    assert resolution["outcome"] == "filled" and Decimal(resolution["filled_qty"]) == final
    assert not _held(live)
    assert [_posts(t) for t in [working, *later]] == [[], [], []]


def test_ioc_entry_coinbase_recorded_with_nothing_filled_is_absent_and_releases_the_hold(live):
    requested = _enter(live, ACK_WITHOUT_FILL)

    transport = _transport(orders=_page(_entry_record(requested=requested, filled="0", status="CANCELLED")))
    assert (
        run.reconcile_intents(venue=VENUE, venue_obj=_venue(transport), home=live, clock=lambda: 1_700_000_000)
        == "done"
    )

    [resolution] = _resolutions(live)
    assert (resolution["outcome"], resolution["evidence"]) == ("absent", "venue_record_complete")
    assert Decimal(resolution["filled_qty"]) == 0
    assert _reconciled_entry_qty(live) == []
    assert project_ownership(live, IDENTITY).attributable_qty == 0
    assert not _held(live)
    assert _posts(transport) == []


@pytest.mark.parametrize("ack", [ACK_WITHOUT_FILL, REJECTED], ids=["accepted-without-fill", "rejected"])
def test_entry_coinbase_does_not_list_keeps_the_hold_and_is_never_resent(live, ack):
    _enter(live, ack)

    stop_only = _page(_resting_stop(Decimal("0.5")))  # its own stop is not the entry's record
    transports = [_transport(orders=stop_only), _transport(orders=stop_only)]
    _tick(live, transports[0], ENTRY_BARS)
    _tick(live, transports[1], NEXT_ENTRY_BARS)  # the pack wants a new entry on a later bar

    assert [_posts(t) for t in transports] == [[], []]
    assert _held(live)
    assert not read_obligations(live).blocks_entries(venue=VENUE, mode="paper")
    assert not read_obligations(live).blocks_entries(venue="kraken", mode="live")
    assert _marked(live) == [ENTRY_COID]  # audited once, not per tick
    assert _resolutions(live) == []
    assert not [k for k, _p in _ledger(live) if k.startswith("reconcile_")]
    [later] = _tick_records(live, 5 * H)
    assert later["entry_qty"] == "0"
    assert _reconciled_entry_qty(live) == []
    assert project_ownership(live, IDENTITY).attributable_qty == 0


# ---- the Coinbase adapter ----------------------------------------------------------

COID = "c0ffee00c0ffee00ab"
# The requested size planted in every record. It must never come back as an
# executed quantity.
CANARY = "9.87654321"


def _own(*, status: str, filled: str | None) -> dict:
    return order_record(client_order_id=COID, status=status, filled_size=filled, base_size=CANARY)


_OTHERS = [
    order_record(
        client_order_id=f"{COID}_stop",
        status="FILLED",
        filled_size=CANARY,
        base_size=CANARY,
        side="SELL",
        stop_price="6",
    ),
    order_record(client_order_id=f"x{COID}", status="FILLED", filled_size=CANARY, base_size=CANARY),
]


@pytest.mark.parametrize(
    ("pages", "status", "filled", "authoritative"),
    [
        ([_page()], "unknown", "0", False),
        ([_page(_own(status="OPEN", filled="0"))], "open", "0", True),
        ([_page(_own(status="PENDING", filled="0"))], "open", "0", True),
        ([_page(_own(status="OPEN", filled="2.5"))], "partial", "2.5", True),
        ([_page(_own(status="FILLED", filled=CANARY))], "filled", CANARY, True),
        ([_page(_own(status="CANCELLED", filled="2.5"))], "filled", "2.5", True),
        ([_page(_own(status="CANCELLED", filled="0"))], "absent", "0", True),
        ([_page(_own(status="EXPIRED", filled="0.00000000"))], "absent", "0", True),
        ([_page(_own(status="FAILED", filled="0"))], "absent", "0", True),
        ([_page(_own(status="CANCELLED", filled=None))], "unknown", "0", False),
        ([_page(_own(status="CANCELLED", filled=""))], "unknown", "0", False),
        ([_page(_own(status="CANCELLED", filled="NaN"))], "unknown", "0", False),
        ([_page(_own(status="CANCELLED", filled="-1"))], "unknown", "0", False),
        ([_page(_own(status="FILLED", filled="0"))], "unknown", "0", False),
        ([_page(_own(status="UNKNOWN_ORDER_STATUS", filled="0"))], "unknown", "0", False),
        ([_page({**_own(status="OPEN", filled="0"), "status": None})], "unknown", "0", False),
        ([_page(*_OTHERS)], "unknown", "0", False),
        ([_page(*_OTHERS, _own(status="CANCELLED", filled="3"))], "filled", "3", True),
        ([_page(*_OTHERS, next_cursor="p1"), _page(_own(status="CANCELLED", filled="3"))], "filled", "3", True),
        (
            [*(_page(next_cursor=f"p{i + 1}") for i in range(5)), _page(_own(status="CANCELLED", filled="0"))],
            "unknown",
            "0",
            False,
        ),
        ([{"orders": "unreadable", "has_next": False}], "unknown", "0", False),
        ([["not", "an", "envelope"]], "unknown", "0", False),
    ],
    ids=[
        "not-listed",
        "open-unexecuted",
        "pending-unexecuted",
        "open-partly-executed",
        "filled",
        "ioc-remainder-cancelled",
        "ioc-cancelled-unexecuted",
        "expired-unexecuted",
        "failed-unexecuted",
        "filled-size-missing",
        "filled-size-empty",
        "filled-size-unreadable",
        "filled-size-negative",
        "filled-status-with-nothing-filled",
        "status-unknown",
        "status-missing",
        "only-its-stop-and-another-order",
        "own-order-beside-its-stop-and-another-order",
        "own-order-on-a-later-page",
        "own-order-beyond-the-five-bounded-pages",
        "orders-unreadable",
        "page-unreadable",
    ],
)
def test_coinbase_order_outcome_reads_filled_size_and_never_sends(pages, status, filled, authoritative):
    transport = FakeCoinbaseTransport(get_responses=list(pages))

    outcome = _venue(transport).order_outcome(COID, pair=PAIR)

    assert isinstance(outcome, OrderOutcome) and isinstance(outcome.filled_qty, Decimal)
    assert (outcome.status, outcome.filled_qty, outcome.authoritative) == (status, Decimal(filled), authoritative)
    assert _posts(transport) == []
    assert all(c.method == "GET" and "/orders/historical/batch?product_ids=BTC-USD" in c.url for c in transport.calls)
    assert len(transport.calls) <= 5  # the five bounded pages, never a sixth


def test_coinbase_order_outcome_follows_the_cursor_through_at_most_five_pages():
    pages = [*(_page(next_cursor=f"p{i + 1}") for i in range(5)), _page(_own(status="FILLED", filled=CANARY))]
    transport = FakeCoinbaseTransport(get_responses=list(pages))

    assert _venue(transport).order_outcome(COID, pair=PAIR).status == "unknown"

    assert [c.url.partition("&cursor=")[2] for c in transport.calls] == ["", "p1", "p2", "p3", "p4"]


def test_coinbase_order_outcome_without_a_product_is_unknown_and_reads_nothing():
    transport = FakeCoinbaseTransport(orders=_page(_own(status="FILLED", filled=CANARY)))

    outcome = _venue(transport, product_id="").order_outcome(COID, pair="")

    assert outcome == OrderOutcome(status="unknown")
    assert transport.calls == []
