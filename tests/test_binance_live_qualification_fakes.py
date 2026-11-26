"""Binance live qualification path, exercised only through fake transports.

Binance is the first candidate beyond Kraken and Coinbase (M27.4). These
tests pin the behavior a qualified venue needs: trade-only keys, refusal of
fund-moving permissions, venue-backed preflight, protective stops sized to
what filled, cross-process deduplication, reconcile-never-resubmit on an
ambiguous submit, cancel and exit, and the release gate on every write path.
Binance stays out of the qualified registry until the owner approves it.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fakes.fake_binance import FakeBinanceTransport

from krellbot import release_policy
from krellbot.application import live_preflight
from krellbot.application.distribution_venues import QUALIFIED_LIVE_VENUES
from krellbot.application.live_preflight import (
    CODE_KEY_UNKNOWN_PERMISSION,
    CODE_KEY_WITHDRAW_CAPABLE,
    CODE_OK,
    CODE_PREFLIGHT_BLOCKED,
    CODE_VENUE_UNREACHABLE,
    VenueBackedPreflightTransport,
    build_venue_preflight_transport,
    evaluate,
)
from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import (
    KeyMalformedError,
    KeyTradeOffError,
    KeyUnknownPermissionError,
    ProtectiveStopRejectedError,
    WithdrawCapableError,
)
from krellbot.venues.binance import BinanceVenue

PAIR = "BTCUSDT"
TRADE_ONLY = {
    "ipRestrict": True,
    "createTime": 1_700_000_000_000,
    "enableReading": True,
    "enableSpotAndMarginTrading": True,
    "enableWithdrawals": False,
    "enableInternalTransfer": False,
    "permitsUniversalTransfer": False,
    "enableMargin": False,
    "enableFutures": False,
    "enableVanillaOptions": False,
    "enableFixApiTrade": False,
    "enableFixReadOnly": False,
    "enablePortfolioMarginTrading": False,
}


def _venue(transport: FakeBinanceTransport) -> BinanceVenue:
    return BinanceVenue("test-key", "test-secret", transport, now_fn=lambda: 1_700_000_000_000)


def _posts(transport: FakeBinanceTransport) -> list:
    return [c for c in transport.calls if c.method == "POST"]


def _restrictions(**overrides) -> dict:
    return {**TRADE_ONLY, **overrides}


# ---- keys -----------------------------------------------------------------


def test_documented_trade_only_key_is_accepted():
    perms = _venue(FakeBinanceTransport(api_restrictions=TRADE_ONLY)).check_key()
    assert perms.can_trade is True
    assert perms.can_withdraw is False


@pytest.mark.parametrize("flag", ["enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer"])
def test_fund_moving_permission_is_refused_as_withdraw_capable(flag):
    with pytest.raises(WithdrawCapableError) as caught:
        _venue(FakeBinanceTransport(api_restrictions=_restrictions(**{flag: True}))).check_key()
    assert type(caught.value) is WithdrawCapableError


@pytest.mark.parametrize("flag", ["enableReading", "enableSpotAndMarginTrading"])
def test_key_without_spot_trading_or_reading_is_refused(flag):
    with pytest.raises(KeyTradeOffError):
        _venue(FakeBinanceTransport(api_restrictions=_restrictions(**{flag: False}))).check_key()


@pytest.mark.parametrize(
    "flag",
    ["enableMargin", "enableFutures", "enableVanillaOptions", "enableFixApiTrade", "enableFutureProductX"],
)
def test_permission_beyond_spot_trading_fails_closed(flag):
    with pytest.raises(KeyUnknownPermissionError):
        _venue(FakeBinanceTransport(api_restrictions=_restrictions(**{flag: True}))).check_key()


@pytest.mark.parametrize("payload", [{}, {"code": -2015, "msg": "Invalid API-key"}])
def test_unrecognized_restrictions_shape_is_refused(payload):
    with pytest.raises(KeyMalformedError):
        _venue(FakeBinanceTransport(api_restrictions=payload)).check_key()


# ---- preflight --------------------------------------------------------------


def _preflight(transport: FakeBinanceTransport):
    return evaluate(
        account_id="binance",
        revision_id="rev-1",
        mode="sandbox",
        transport=VenueBackedPreflightTransport(account="binance", _venue=_venue(transport)),
    )


@pytest.mark.parametrize(
    ("restrictions", "raise_on", "code"),
    [
        (TRADE_ONLY, set(), CODE_OK),
        (_restrictions(enableWithdrawals=True), set(), CODE_KEY_WITHDRAW_CAPABLE),
        (_restrictions(enableFutures=True), set(), CODE_KEY_UNKNOWN_PERMISSION),
        (_restrictions(enableSpotAndMarginTrading=False), set(), CODE_PREFLIGHT_BLOCKED),
        (TRADE_ONLY, {"apiRestrictions"}, CODE_VENUE_UNREACHABLE),
        (TRADE_ONLY, {"/api/v3/account"}, CODE_VENUE_UNREACHABLE),
    ],
    ids=["trade-only", "withdraw", "futures", "trade-off", "permissions-down", "balances-down"],
)
def test_binance_preflight_codes(restrictions, raise_on, code):
    transport = FakeBinanceTransport(api_restrictions=restrictions, raise_on=raise_on)
    result = _preflight(transport)
    assert result.code == code, result
    assert _posts(transport) == []
    assert [c for c in transport.calls if c.method == "DELETE"] == []


def test_binance_preflight_ok_reports_balances():
    result = _preflight(FakeBinanceTransport(api_restrictions=TRADE_ONLY))
    assert result.ok is True
    assert result.balances == {"USDT": "1000"}


def test_binance_stays_out_of_the_qualified_registry():
    assert "binance" not in QUALIFIED_LIVE_VENUES
    assert "binance" not in live_preflight.SUPPORTED_PREFLIGHT_VENUES
    blocked = build_venue_preflight_transport(venue="binance", http_transport=FakeBinanceTransport())
    result = evaluate(account_id="binance", revision_id="rev-1", mode="sandbox", transport=blocked)
    assert result.code == CODE_PREFLIGHT_BLOCKED


# ---- release gate -----------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda v: v.place_entry_with_stop("c1", Decimal("0.01"), Decimal(90000), pair=PAIR),
        lambda v: v.place_exit("c2", Decimal("0.01"), pair=PAIR),
        lambda v: v.cancel_stops(PAIR),
        lambda v: v.raise_stop(PAIR, Decimal(91000)),
    ],
    ids=["entry", "exit", "cancel", "raise-stop"],
)
def test_paper_only_release_refuses_every_write_before_any_request(call):
    assert release_policy.PAPER_ONLY_RELEASE is True
    transport = FakeBinanceTransport()
    with pytest.raises(PaperOnlyReleaseError):
        call(_venue(transport))
    assert transport.calls == []


# ---- live mechanics (simulated live-capable release) ------------------------

live = pytest.mark.usefixtures("live_capable_release")


@live
@pytest.mark.parametrize("status", ["PARTIALLY_FILLED", "EXPIRED"])
def test_partial_fill_protects_only_the_filled_quantity(status):
    transport = FakeBinanceTransport(
        post_responses=[
            {"orderId": 11, "status": status, "executedQty": "0.00400000"},
            {"orderId": 12, "status": "NEW"},
        ]
    )
    ref = _venue(transport).place_entry_with_stop("partial", Decimal("0.01"), Decimal(90000), pair=PAIR)
    entry, stop = (c.body for c in _posts(transport))
    assert entry["quantity"] == "0.01000"
    assert entry["newOrderRespType"] == "FULL"
    assert stop["quantity"] == "0.00400"
    assert stop["type"] == "STOP_LOSS_LIMIT"
    assert stop["stopPrice"] == "90000.00"
    assert ref.qty == Decimal("0.01000")
    assert ref.filled_qty == Decimal("0.00400")


@live
@pytest.mark.parametrize(
    "entry_response",
    [
        {"orderId": 11, "status": "EXPIRED", "executedQty": "0"},
        {"orderId": 11, "status": "NEW", "executedQty": "0"},
        {"orderId": 11, "status": "FILLED"},
    ],
    ids=["expired-empty", "resting", "no-executed-qty"],
)
def test_unfilled_entry_places_no_stop(entry_response):
    transport = FakeBinanceTransport(post_responses=[entry_response])
    with pytest.raises(RuntimeError, match="not filled") as caught:
        _venue(transport).place_entry_with_stop("nofill", Decimal("0.01"), Decimal(90000), pair=PAIR)
    assert not isinstance(caught.value, ProtectiveStopRejectedError)
    assert len(_posts(transport)) == 1


@live
def test_rejected_stop_after_fill_requires_reconcile():
    transport = FakeBinanceTransport(
        post_responses=[
            {"orderId": 11, "status": "FILLED", "executedQty": "0.01000"},
            {"code": -2010, "msg": "Stop price would trigger immediately."},
        ]
    )
    with pytest.raises(ProtectiveStopRejectedError):
        _venue(transport).place_entry_with_stop("stoprej", Decimal("0.01"), Decimal(90000), pair=PAIR)


@live
def test_stop_with_unknown_outcome_after_fill_requires_reconcile():
    class StopTimesOut(FakeBinanceTransport):
        def post(self, url, body, headers):
            if (body or {}).get("type") == "STOP_LOSS_LIMIT":
                self.calls.append(type(self.calls[0])(method="POST", url=url, headers=dict(headers), body=dict(body)))
                raise TimeoutError("read timed out")
            return super().post(url, body, headers)

    transport = StopTimesOut()
    with pytest.raises(ProtectiveStopRejectedError):
        _venue(transport).place_entry_with_stop("stoptimeout", Decimal("0.01"), Decimal(90000), pair=PAIR)
    assert [c.body["type"] for c in _posts(transport)] == ["MARKET", "STOP_LOSS_LIMIT"]


@live
def test_entry_sent_by_an_earlier_process_is_not_resubmitted():
    transport = FakeBinanceTransport()
    first = _venue(transport).place_entry_with_stop("dedup", Decimal("0.01"), Decimal(90000), pair=PAIR)
    sent = len(_posts(transport))

    second = _venue(transport).place_entry_with_stop("dedup", Decimal("0.01"), Decimal(90000), pair=PAIR)

    assert len(_posts(transport)) == sent
    assert second.filled_qty == first.filled_qty
    assert second.stop_price == Decimal(90000)


@live
def test_earlier_entry_without_an_active_stop_is_not_resubmitted():
    transport = FakeBinanceTransport(
        all_orders=[
            {
                "symbol": PAIR,
                "orderId": 5,
                "clientOrderId": "orphan",
                "origQty": "0.01",
                "executedQty": "0.01",
                "status": "FILLED",
            },
            {
                "symbol": PAIR,
                "orderId": 6,
                "clientOrderId": "orphan_stop",
                "origQty": "0.01",
                "executedQty": "0",
                "stopPrice": "90000",
                "status": "CANCELED",
            },
        ]
    )
    with pytest.raises(ProtectiveStopRejectedError):
        _venue(transport).place_entry_with_stop("orphan", Decimal("0.01"), Decimal(90000), pair=PAIR)
    assert _posts(transport) == []


@live
def test_order_lookup_that_cannot_prove_absence_sends_nothing():
    class BrokenLookup(FakeBinanceTransport):
        def get(self, url, headers):
            if "/api/v3/allOrders" in url:
                self.calls.append(type("C", (), {"method": "GET"})())
                return {"code": -1003, "msg": "Too many requests"}
            return super().get(url, headers)

    transport = BrokenLookup()
    with pytest.raises(RuntimeError, match="order lookup refused"):
        _venue(transport).place_entry_with_stop("lookup", Decimal("0.01"), Decimal(90000), pair=PAIR)
    assert _posts(transport) == []


@live
def test_ambiguous_entry_submit_is_reconciled_never_resubmitted(tmp_path):
    from krellbot.run import _dispatch_place

    class EntryTimesOut(FakeBinanceTransport):
        def post(self, url, body, headers):
            super().post(url, body, headers)
            raise TimeoutError("read timed out after the venue accepted the order")

    transport = EntryTimesOut()
    venue = _venue(transport)
    body = json.dumps({"kind": "entry", "qty": "0.01", "stop": "90000", "pair": PAIR}, sort_keys=True)

    with pytest.raises(TimeoutError):
        _dispatch_place(home=tmp_path, mode="live", venue="binance", coid="amb", body=body, venue_obj=venue)
    state, ref = _dispatch_place(home=tmp_path, mode="live", venue="binance", coid="amb", body=body, venue_obj=venue)

    assert state == "needs_reconcile"
    assert ref is None
    assert len(_posts(transport)) == 1


@live
def test_stop_rejection_through_the_outbox_is_audited_for_reconcile(tmp_path):
    from krellbot.execution.reservations import should_block_for_reconcile
    from krellbot.run import _dispatch_place

    transport = FakeBinanceTransport(
        post_responses=[
            {"orderId": 11, "status": "FILLED", "executedQty": "0.01000"},
            {"code": -2010, "msg": "Account has insufficient balance for requested action."},
        ]
    )
    body = json.dumps({"kind": "entry", "qty": "0.01", "stop": "90000", "pair": PAIR}, sort_keys=True)
    with pytest.raises(ProtectiveStopRejectedError):
        _dispatch_place(
            home=tmp_path, mode="live", venue="binance", coid="audited", body=body, venue_obj=_venue(transport)
        )
    assert should_block_for_reconcile(tmp_path)


@live
def test_exit_sells_at_market_and_cancel_touches_only_stops_on_the_pair():
    resting = [
        {
            "orderId": 21,
            "clientOrderId": "a_stop",
            "symbol": PAIR,
            "side": "SELL",
            "origQty": "0.01",
            "stopPrice": "90000",
        },
        {"orderId": 22, "clientOrderId": "limit", "symbol": PAIR, "side": "SELL", "origQty": "0.01", "stopPrice": "0"},
        {
            "orderId": 23,
            "clientOrderId": "b_stop",
            "symbol": "ETHUSDT",
            "side": "SELL",
            "origQty": "1",
            "stopPrice": "3000",
        },
    ]
    transport = FakeBinanceTransport(open_orders=resting)
    venue = _venue(transport)

    exit_ref = venue.place_exit("flat", Decimal("0.01"), pair=PAIR)
    venue.cancel_stops(PAIR)

    (exit_body,) = (c.body for c in _posts(transport))
    assert (exit_body["side"], exit_body["type"], exit_body["quantity"]) == ("SELL", "MARKET", "0.01000")
    assert exit_ref.side == "sell"
    deletes = [c.url for c in transport.calls if c.method == "DELETE"]
    assert len(deletes) == 1
    assert "orderId=21" in deletes[0]


@live
def test_rejected_replacement_stop_is_reported():
    transport = FakeBinanceTransport(
        open_orders=[
            {
                "orderId": 21,
                "clientOrderId": "a_stop",
                "symbol": PAIR,
                "side": "SELL",
                "origQty": "0.01",
                "stopPrice": "90000",
            }
        ],
        post_responses=[{"code": -2010, "msg": "Stop price would trigger immediately."}],
    )
    with pytest.raises(ProtectiveStopRejectedError):
        _venue(transport).raise_stop(PAIR, Decimal(99000))
    assert [c.method for c in transport.calls if c.method in {"DELETE", "POST"}] == ["DELETE", "POST"]


@live
def test_resting_stop_is_found_by_client_order_id_in_a_new_process():
    transport = FakeBinanceTransport(
        open_orders=[
            {
                "orderId": 21,
                "clientOrderId": "x_stop",
                "symbol": PAIR,
                "side": "SELL",
                "origQty": "0.01",
                "stopPrice": "90000",
            }
        ]
    )
    found = _venue(transport).order_by_coid("x_stop")
    assert found is not None
    assert found.stop_price == Decimal(90000)
    assert _venue(transport).order_by_coid("absent") is None
