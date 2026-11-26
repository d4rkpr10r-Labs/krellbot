"""A live send the venue refuses by its documented response records a refusal code.

Kraken answers a refused AddOrder with a documented error list; Coinbase
answers a refused create-order with ``success: false`` and an
``error_response``. When ``classify_documented_response`` maps that body to an
order refusal (Kraken: ``insufficient_funds``, ``order_minimum_not_met``,
``invalid_nonce``; Coinbase: ``insufficient_funds``,
``order_minimum_not_met``), the adapter raises ``VenueRejectedError`` with that
code. ``run.tick`` journals the code as ``intent_refused`` and resolves the
intent as not executed with evidence ``venue_rejected``, which lifts the entry
hold for that venue and mode. The client order id is never sent again.

No response, a transport error, an error the classifier does not recognize, a
documented error that is not a refusal, and an order accepted without fill
evidence prove nothing about execution: the intent stays unresolved and holds
entries until venue evidence or the operator settles it. Only codes are
stored, never the venue's own text.

Runs on fake transports with the release gate patched in-process
(``live_capable_release``); ``PAPER_ONLY_RELEASE`` stays True in source.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fakes.fake_coinbase import FakeCoinbaseTransport
from fakes.fake_kraken import FakeKrakenTransport, order_info
from fakes.live_grant import write_grant

from krellbot import journal as kb_journal
from krellbot import run
from krellbot.config import ArmedPack, Config, save_config
from krellbot.execution.obligations import read_obligations
from krellbot.pack.model import Candle
from krellbot.storage.database import OperationalStore
from krellbot.venues import base as venue_base
from krellbot.venues.coinbase import CoinbaseVenue
from krellbot.venues.kraken import KrakenVenue, RateLimitStop

pytestmark = pytest.mark.usefixtures("live_capable_release")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "venues"
PACK = "refusal-code"
H = 3_600_000
OWNED = Decimal(5)
STOP_REFUSAL = "order_minimum_not_met"


def _fixture(venue: str, name: str) -> Any:
    """A documented response body; the first line is its provenance comment."""
    lines = (FIXTURES / venue / f"{name}.json").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("#")
    return json.loads("\n".join(lines[1:]))


def _candle(ts_ms: int, close: str) -> Candle:
    c = Decimal(close)
    return Candle(ts_ms=ts_ms, open=c, high=c + 1, low=c - 1, close=c, volume=Decimal(100))


ENTRY_BARS = [_candle(0, "10"), _candle(H, "10"), _candle(2 * H, "12")]
# Three bars later the pack signals a fresh entry, under a new client order id.
NEXT_ENTRY_BARS = [*ENTRY_BARS, _candle(3 * H, "10"), _candle(4 * H, "9"), _candle(5 * H, "12")]
EXIT_BARS = [_candle(0, "12"), _candle(H, "12"), _candle(2 * H, "8")]
FLAT_BARS = [_candle(0, "10"), _candle(H, "10"), _candle(2 * H, "10")]


# ---- venues ----------------------------------------------------------------------

KRAKEN_PAIR = "SUIUSD"
KRAKEN_KEY = "FAKE_KEY_FOR_KRAKEN_TESTS"
KRAKEN_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
COINBASE_PAIR = "BTC-USD"
COINBASE_KEY = "organizations/fake-org/apiKeys/fake-coinbase-key-001"
COINBASE_SECRET = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQGKiOPddAnxlf1S2y08ul1yymcJvx2UEhvzdIgBtA9vXA=="


class _KrakenTimesOut(FakeKrakenTransport):
    """AddOrder reaches the venue, then the read times out."""

    def post(self, url: str, form: dict[str, str], headers: dict[str, str]) -> dict:
        reply = super().post(url, form, headers)
        if url.endswith("/AddOrder"):
            raise TimeoutError("read timed out")
        return reply


class _CoinbaseTimesOut(FakeCoinbaseTransport):
    """Create-order reaches the venue, then the read times out."""

    def post(self, url: str, body: dict, headers: dict[str, str]) -> dict:
        reply = super().post(url, body, headers)
        if url.endswith("/brokerage/orders"):
            raise TimeoutError("read timed out")
        return reply


def _kraken_transport(responses=(), *, base_qty=Decimal(0), resting_stop=False, times_out=False):
    transport = (_KrakenTimesOut if times_out else FakeKrakenTransport)(
        responses=list(responses),
        asset_pairs={KRAKEN_PAIR: {"ordermin": "0.01", "costmin": "0.5", "lot_decimals": 5, "pair_decimals": 4}},
        balances={"USD": "100", "SUI": str(base_qty)},
    )
    if resting_stop:
        stop = order_info(
            vol=str(base_qty),
            vol_exec="0",
            status="open",
            pair=KRAKEN_PAIR,
            side="sell",
            ordertype="stop-loss",
            stopprice="6.0000",
        )
        transport.set_order_books(open_rows={"OSTOP-RESTING": stop})
    return transport


def _kraken_adapter(home: Path, transport, cls: type = KrakenVenue):
    return cls(
        KRAKEN_KEY,
        KRAKEN_SECRET,
        transport,
        now_ms=lambda: 1_700_000_000_000,
        min_interval_ms=0,
        home=home,
        sleep=lambda _seconds: None,
    )


def _kraken_sent(transport) -> list[str]:
    return [c.form["cl_ord_id"] for c in transport.calls if c.url.endswith("/AddOrder")]


def _coinbase_transport(responses=(), *, base_qty=Decimal(0), resting_stop=False, times_out=False):
    resting = {
        "order_id": "STOP-RESTING",
        "client_order_id": "",
        "product_id": COINBASE_PAIR,
        "side": "SELL",
        "status": "OPEN",
        "order_configuration": {
            "stop_limit_stop_limit_gtc": {
                "base_size": str(base_qty),
                "limit_price": "5.97",
                "stop_price": "6",
                "stop_direction": "STOP_DIRECTION_STOP_DOWN",
            }
        },
    }
    accounts = [
        {"uuid": "usd", "currency": "USD", "available_balance": {"value": "100", "currency": "USD"}},
        {"uuid": "btc", "currency": "BTC", "available_balance": {"value": str(base_qty), "currency": "BTC"}},
    ]
    return (_CoinbaseTimesOut if times_out else FakeCoinbaseTransport)(
        post_responses=list(responses),
        product={
            "product_id": COINBASE_PAIR,
            "base_increment": "0.00000001",
            "quote_increment": "0.01",
            "base_min_size": "0.00000001",
            "quote_min_size": "1",
            "price": "12",
        },
        accounts={"accounts": accounts},
        orders={"orders": [resting] if resting_stop else [], "has_next": False},
        fills={"fills": []},
    )


def _coinbase_adapter(home: Path, transport, cls: type = CoinbaseVenue):
    del home
    return cls(
        COINBASE_KEY,
        COINBASE_SECRET,
        transport,
        product_id=COINBASE_PAIR,
        now_fn=lambda: 1_700_000_000,
        nonce_fn=lambda: "aa" * 16,
    )


def _coinbase_sent(transport) -> list[str]:
    return [
        c.body["client_order_id"] for c in transport.calls if c.method == "POST" and c.url.endswith("/brokerage/orders")
    ]


@dataclass(frozen=True, eq=False)
class Kit:
    """One venue's fake transport, adapter, order-send log and cancel ack."""

    venue: str
    pair: str
    venue_cls: type
    transport: Callable[..., Any]
    adapter: Callable[..., Any]
    sent: Callable[[Any], list[str]]
    cancel_ack: dict


KRAKEN = Kit(
    "kraken",
    KRAKEN_PAIR,
    KrakenVenue,
    _kraken_transport,
    _kraken_adapter,
    _kraken_sent,
    {"error": [], "result": {"count": 1}},
)
COINBASE = Kit(
    "coinbase",
    COINBASE_PAIR,
    CoinbaseVenue,
    _coinbase_transport,
    _coinbase_adapter,
    _coinbase_sent,
    {"results": [{"success": True, "order_id": "STOP-RESTING"}]},
)
KITS = [pytest.param(KRAKEN, id="kraken"), pytest.param(COINBASE, id="coinbase")]

# Documented responses that prove the venue refused the order. Fixture names
# are the codes the classifier maps them to.
REFUSALS = [
    pytest.param(KRAKEN, "insufficient_funds", id="kraken-insufficient_funds"),
    pytest.param(KRAKEN, "order_minimum_not_met", id="kraken-order_minimum_not_met"),
    pytest.param(KRAKEN, "invalid_nonce", id="kraken-invalid_nonce"),
    pytest.param(COINBASE, "insufficient_funds", id="coinbase-insufficient_funds"),
    pytest.param(COINBASE, "order_minimum_not_met", id="coinbase-order_minimum_not_met"),
]

# Responses that prove nothing about execution. ``None`` is no HTTP body.
NOT_REFUSALS = [
    pytest.param(KRAKEN, None, id="kraken-no-response"),
    pytest.param(KRAKEN, {"error": ["EGeneral:Internal error"]}, id="kraken-unrecognized-error"),
    pytest.param(KRAKEN, {"error": ["EOrder:Rate limit exceeded"]}, id="kraken-documented-error-that-is-no-refusal"),
    pytest.param(
        KRAKEN, {"error": ["EOrder:Insufficient funds", "EService:Unavailable"]}, id="kraken-refusal-mixed-with-unknown"
    ),
    pytest.param(COINBASE, None, id="coinbase-no-response"),
    pytest.param(COINBASE, _fixture("coinbase", "order_accepted"), id="coinbase-accepted-without-fill-evidence"),
    pytest.param(
        COINBASE,
        {"success": False, "error_response": {"error": "UNKNOWN_FAILURE_REASON", "message": "", "error_details": ""}},
        id="coinbase-unrecognized-failure",
    ),
    pytest.param(COINBASE, _fixture("coinbase", "rate_limit"), id="coinbase-rate-limited-request"),
    pytest.param(COINBASE, _fixture("coinbase", "invalid_nonce"), id="coinbase-unauthenticated-request"),
]


# ---- helpers ---------------------------------------------------------------------


def _arm(home: Path, monkeypatch, kit: Kit, *, owned: Decimal = Decimal(0)) -> None:
    """Arm one live pack on ``kit``'s venue, optionally owning ``owned`` already."""
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    write_grant(home, venue=kit.venue, pair=kit.pair)
    pack = {
        "schema_version": 1,
        "id": PACK,
        "version": "1.0.0",
        "label": "Refusal code",
        "author": "krellbot tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 50}},
        "markets": [{"venue": kit.venue, "pair": kit.pair}],
    }
    pack_path = home / f"{PACK}.json"
    pack_path.write_text(json.dumps(pack), encoding="utf-8")
    armed = ArmedPack(
        pack_path=str(pack_path),
        pack_sha256="0" * 64,
        pack_id=PACK,
        pack_version="1.0.0",
        venue=kit.venue,
        pair=kit.pair,
        cap=Decimal(100),
        stop=Decimal(5),
        mode="live",
        starting_cash=Decimal(100),
        requires_license=False,
        armed_at_ts=1,
    )
    save_config(home, Config(armed=[armed]))
    if owned:
        kb_journal.append(
            {
                "ts": 1,
                "kind": "tick",
                "venue": kit.venue,
                "pack": PACK,
                "bar_ts": 0,
                "detail": {"pair": kit.pair, "mode": "live", "reason": "entry", "entry_qty": str(owned)},
            }
        )


def _tick(home: Path, kit: Kit, transport, bars: list[Candle], venue=None) -> None:
    venue = venue if venue is not None else kit.adapter(home, transport)
    assert (
        run.tick(venue=kit.venue, venue_obj=venue, reader=lambda *_: bars, home=home, clock=lambda: 1_700_000_000) == 0
    )


def _coid(kit: Kit, bar_ts: int, intent: str) -> str:
    return run.coid_for(
        pack_id=PACK, pack_version="1.0.0", venue=kit.venue, pair=kit.pair, bar_ts=bar_ts, intent=intent
    )


def _ledger(home: Path) -> list[tuple[str, dict]]:
    return [(kind, json.loads(payload)) for _id, kind, payload in OperationalStore(home / "ops.sqlite").read_ledger()]


def _resolutions(home: Path) -> list[dict]:
    return [payload for kind, payload in _ledger(home) if kind == "intent_resolved"]


def _marked(home: Path) -> list[str]:
    return [payload["coid"] for kind, payload in _ledger(home) if kind == "needs_reconcile"]


def _held(home: Path, kit: Kit) -> bool:
    return read_obligations(home).blocks_entries(venue=kit.venue, mode="live")


def _rejected(coid: str, code: str) -> dict:
    return {"coid": coid, "outcome": "absent", "filled_qty": "0", "evidence": "venue_rejected", "code": code}


def _tick_detail(home: Path, bar_ts: int) -> dict:
    [detail] = [
        r["detail"]
        for r in kb_journal.read_records_lenient(home)
        if r.get("kind") == "tick"
        and r.get("pack") == PACK
        and r.get("bar_ts") == bar_ts
        and r["detail"].get("reason") != run.RECONCILED_REASON
    ]
    return detail


def _venue_text(body: Any) -> set[str]:
    """Every non-empty string the venue sent in ``body``."""
    if isinstance(body, str):
        return {body} if body else set()
    if isinstance(body, dict):
        return {text for value in body.values() for text in _venue_text(value)}
    if isinstance(body, list):
        return {text for value in body for text in _venue_text(value)}
    return set()


def _assert_no_venue_text(home: Path, body: Any) -> None:
    stored = "".join(p.read_text(encoding="utf-8") for p in sorted((home / "journal").glob("*.jsonl")))
    stored += "".join(payload for _id, _kind, payload in OperationalStore(home / "ops.sqlite").read_ledger())
    assert _venue_text(body), "the refusal body must carry venue text for this check to mean anything"
    assert sorted(text for text in _venue_text(body) if text in stored) == []


# ---- the adapters ----------------------------------------------------------------


@pytest.mark.parametrize("send", ["entry", "exit"])
@pytest.mark.parametrize(("kit", "code"), REFUSALS)
def test_documented_refusal_raises_a_typed_rejection_carrying_only_its_code(home, kit, code, send):
    body = _fixture(kit.venue, code)
    transport = kit.transport([body])
    venue = kit.adapter(home, transport)

    with pytest.raises(venue_base.VenueRejectedError) as caught:
        if send == "entry":
            venue.place_entry_with_stop("c0ffee00c0ffee00ab", Decimal(1), Decimal(6), pair=kit.pair)
        else:
            venue.place_exit("c0ffee00c0ffee00ab", Decimal(1), pair=kit.pair)

    assert caught.value.code == code
    assert [text for text in _venue_text(body) if text in str(caught.value)] == []
    assert kit.sent(transport) == ["c0ffee00c0ffee00ab"]


def test_kraken_rate_limit_and_permission_errors_keep_their_own_paths(home):
    pairs = {KRAKEN_PAIR: {"ordermin": "0.01", "costmin": "0.5", "lot_decimals": 5, "pair_decimals": 4}}
    limited = _kraken_adapter(home, FakeKrakenTransport(rate_limit_first_n=3, asset_pairs=pairs))
    denied = _kraken_adapter(home, FakeKrakenTransport(permission_error_on={"AddOrder"}, asset_pairs=pairs))

    with pytest.raises(RateLimitStop) as stopped:
        limited.place_entry_with_stop("c0ffee00c0ffee00ab", Decimal(1), Decimal(6), pair=KRAKEN_PAIR)
    with pytest.raises(RuntimeError) as refused:
        denied.place_entry_with_stop("c0ffee00c0ffee00ab", Decimal(1), Decimal(6), pair=KRAKEN_PAIR)

    assert not isinstance(stopped.value, venue_base.VenueRejectedError)
    assert not isinstance(refused.value, venue_base.VenueRejectedError)


# ---- the run engine --------------------------------------------------------------


@pytest.mark.parametrize(("kit", "code"), REFUSALS)
def test_documented_entry_refusal_is_journaled_resolved_and_never_resent(home, monkeypatch, kit, code):
    _arm(home, monkeypatch, kit)
    body = _fixture(kit.venue, code)
    refused = _coid(kit, 2 * H, "entry")
    transport = kit.transport([body, _fixture(kit.venue, "order_accepted")])

    _tick(home, kit, transport, ENTRY_BARS)

    detail = _tick_detail(home, 2 * H)
    assert detail["intent_refused"] == code
    assert (detail["entry_qty"], detail["owned_qty_after"]) == ("0", "0")
    assert _resolutions(home) == [_rejected(refused, code)]
    assert _marked(home) == []
    assert not _held(home, kit)
    _assert_no_venue_text(home, body)

    _tick(home, kit, transport, ENTRY_BARS)  # the refused bar is journaled: nothing to redo
    _tick(home, kit, transport, NEXT_ENTRY_BARS)  # no hold: a later bar may enter again

    assert kit.sent(transport) == [refused, _coid(kit, 5 * H, "entry")]


@pytest.mark.parametrize(("kit", "body"), NOT_REFUSALS)
def test_entry_send_that_proves_no_refusal_stays_unknown_and_holds_entries(home, monkeypatch, kit, body):
    _arm(home, monkeypatch, kit)
    entry = _coid(kit, 2 * H, "entry")
    transport = kit.transport([body])

    _tick(home, kit, transport, ENTRY_BARS)
    _tick(home, kit, transport, NEXT_ENTRY_BARS)  # the pack wants a new entry on a later bar

    detail = _tick_detail(home, 2 * H)
    assert detail["intent_refused"] is None
    assert detail["entry_qty"] == "0"
    assert _resolutions(home) == []
    assert _held(home, kit)
    assert kit.sent(transport) == [entry]  # held, and the unknown send is never repeated


@pytest.mark.parametrize("kit", KITS)
def test_send_that_times_out_stays_unknown_and_is_never_resent(home, kit):
    transport = kit.transport(times_out=True)
    venue = kit.adapter(home, transport)
    body = json.dumps({"kind": "entry", "qty": "1", "stop": "6", "pair": kit.pair}, separators=(",", ":"))

    with pytest.raises(TimeoutError):
        run._dispatch_place(home=home, mode="live", venue=kit.venue, coid="timed-out", body=body, venue_obj=venue)
    again = run._dispatch_place(home=home, mode="live", venue=kit.venue, coid="timed-out", body=body, venue_obj=venue)

    assert again == ("needs_reconcile", None)
    assert kit.sent(transport) == ["timed-out"]
    assert _resolutions(home) == []
    assert _held(home, kit)


@pytest.mark.parametrize(("kit", "code"), REFUSALS)
def test_documented_exit_refusal_is_journaled_resolved_and_keeps_the_position(home, monkeypatch, kit, code):
    _arm(home, monkeypatch, kit, owned=OWNED)
    body = _fixture(kit.venue, code)
    refused = _coid(kit, 2 * H, "exit")
    transport = kit.transport([kit.cancel_ack, body], base_qty=OWNED, resting_stop=True)

    for _ in range(2):
        _tick(home, kit, transport, EXIT_BARS)

    detail = _tick_detail(home, 2 * H)
    assert (detail["reason"], detail["intent_refused"]) == ("exit", code)
    assert (detail["exit_qty"], detail["owned_qty_after"]) == ("0", str(OWNED))
    assert _resolutions(home) == [_rejected(refused, code)]
    assert not _held(home, kit)
    assert kit.sent(transport) == [refused]
    _assert_no_venue_text(home, body)


@pytest.mark.parametrize(("kit", "code"), REFUSALS)
def test_documented_refusals_on_the_stop_repair_path_are_journaled_and_resolved(home, monkeypatch, kit, code):
    _arm(home, monkeypatch, kit, owned=OWNED)
    body = _fixture(kit.venue, code)
    stop, fallback = _coid(kit, 2 * H, "stop"), _coid(kit, 2 * H, "exit")
    transport = kit.transport([body], base_qty=OWNED)

    def refuse_stop(self, coid, qty, stop, *, pair):
        raise venue_base.VenueRejectedError(kit.venue, STOP_REFUSAL)

    refusing = type("StopRefusingVenue", (kit.venue_cls,), {"place_stop": refuse_stop})
    for _ in range(2):
        _tick(home, kit, transport, FLAT_BARS, kit.adapter(home, transport, refusing))

    detail = _tick_detail(home, 2 * H)
    assert detail["ensure_stop_failed"] is True
    assert detail["intent_refused"] == code  # the market-exit fallback was refused too
    assert _resolutions(home) == [_rejected(stop, STOP_REFUSAL), _rejected(fallback, code)]
    assert not _held(home, kit)
    assert kit.sent(transport) == [fallback]
    _assert_no_venue_text(home, body)
