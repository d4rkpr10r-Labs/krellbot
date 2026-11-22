"""XT spot adapter: Venue protocol behind injectable fakes.

CI must never open a network socket to XT. ``XtVenue`` requires an explicit
``Transport``; ``None`` refuses rather than defaulting to ``HttpTransport``.
Order mutations refuse even when the release allows live trading.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

import pytest
from fakes.fake_xt import FakeXtTransport

from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import WithdrawCapableError
from krellbot.venues.xt import API_HOST, BASE_URL, HttpTransport, XtVenue

WAVE_PAIR = "BTCUSDT"
WIRE = "btc_usdt"


def _venue(transport: FakeXtTransport, **kwargs) -> XtVenue:
    return XtVenue(
        key_id="XT-SYNTHETIC-KEY",
        api_secret="XT-SYNTHETIC-SECRET",
        transport=transport,
        now_fn=lambda: 1_680_885_532_722,
        **kwargs,
    )


def test_xt_venue_refuses_none_transport() -> None:
    """No silent HttpTransport default — tests must inject a fake."""
    with pytest.raises(ValueError, match="transport is required"):
        XtVenue(key_id="XT-SYNTHETIC-KEY", api_secret="XT-SYNTHETIC-SECRET", transport=None)  # type: ignore[arg-type]


def test_xt_http_transport_is_not_used_by_adapter_default() -> None:
    """Constructor has no network default; HttpTransport is opt-in only."""
    transport = FakeXtTransport()
    venue = _venue(transport)
    assert not isinstance(venue._transport, HttpTransport)
    assert venue._transport is transport


def test_xt_rules_btc_usdt_from_public_symbol() -> None:
    transport = FakeXtTransport()
    rules = _venue(transport).rules(WAVE_PAIR)
    assert rules.ordermin == Decimal("0.00001")
    assert rules.costmin == Decimal(1)
    assert rules.lot_decimals == 5
    assert rules.price_decimals == 2
    gets = [c for c in transport.calls if c.method == "GET"]
    assert any("/v4/public/symbol" in c.url and "symbol=btc_usdt" in c.url for c in gets)
    assert all(urlparse(c.url).hostname == API_HOST for c in gets)
    assert all(c.url.startswith(BASE_URL) for c in gets)


def test_xt_snapshot_balances_orders_and_fills() -> None:
    transport = FakeXtTransport(
        orders=[
            {
                "orderId": "limit-9",
                "clientOrderId": "stop-1-stop",
                "symbol": WIRE,
                "side": "SELL",
                "quantity": "0.01000",
                "price": "89550.00",
                "type": "LIMIT",
            }
        ],
        fills=[
            {
                "tradeId": "7",
                "orderId": "1",
                "clientOrderId": "fill-1",
                "symbol": WIRE,
                "side": "BUY",
                "quantity": "0.01",
                "price": "100000",
                "time": 1700000000000,
            }
        ],
    )
    truth = _venue(transport).snapshot()
    assets = {b.asset: b for b in truth.balances}
    assert assets["USDT"].free == Decimal(1000)
    assert len(truth.open_orders) == 1
    assert truth.open_orders[0].pair == WAVE_PAIR
    assert truth.open_orders[0].stop_price is None
    assert _venue(transport).order_by_coid("stop-1-stop") == truth.open_orders[0]
    assert truth.recent_fills[0].side == "buy"
    assert truth.recent_fills[0].pair == WAVE_PAIR
    for call in transport.calls:
        assert urlparse(call.url).hostname == API_HOST


@pytest.mark.parametrize("paper_only", [True, False])
def test_xt_order_mutations_refuse_before_transport(monkeypatch, paper_only: bool) -> None:
    from krellbot import release_policy

    monkeypatch.setattr(release_policy, "PAPER_ONLY_RELEASE", paper_only)
    transport = FakeXtTransport()
    venue = _venue(transport)
    denied = PaperOnlyReleaseError if paper_only else NotImplementedError
    with pytest.raises(denied):
        venue.place_entry_with_stop("coid-entry", Decimal("0.01"), Decimal(90000), pair=WAVE_PAIR)
    with pytest.raises(denied):
        venue.place_exit("coid-exit", Decimal("0.01"), pair=WAVE_PAIR)
    with pytest.raises(denied):
        venue.cancel_stops(WAVE_PAIR)
    with pytest.raises(denied):
        venue.raise_stop(WAVE_PAIR, Decimal(91000))
    assert transport.calls == []


def test_xt_check_key_does_not_infer_trade_only_permissions_from_balances() -> None:
    for payload in ({"assets": []}, {"permissions": ["SPOT"], "assets": []}):
        with pytest.raises(RuntimeError, match="does not establish trade-only key permissions"):
            _venue(FakeXtTransport(balances=payload)).check_key()
    bad = FakeXtTransport(balances={"permissions": ["WITHDRAW"], "assets": []})
    with pytest.raises(WithdrawCapableError):
        _venue(bad).check_key()


def test_xt_signed_get_matches_independent_prehash_digest() -> None:
    """Pin the signed pending-order GET request to a separately computed digest."""
    transport = FakeXtTransport()
    assert _venue(transport).order_by_coid("coid-absent") is None
    (call,) = transport.calls
    assert call.url == BASE_URL + "/v4/open-order?bizType=SPOT"
    assert call.headers["xt-validate-appkey"] == "XT-SYNTHETIC-KEY"
    assert call.headers["xt-validate-signature"] == "9dc019f4d9af36b212aa15e5e4fa43b47aa545c2c3e4e9e42503b45ff188baa2"
