"""XT spot adapter: Venue protocol behind injectable fakes.

CI must never open a network socket to XT. ``XtVenue`` requires an explicit
``Transport``; ``None`` refuses rather than defaulting to ``HttpTransport``.
Order mutations still go through ``release_policy.ensure_live_orders_allowed``.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

import pytest
from fakes.fake_xt import FakeXtTransport

from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import WithdrawCapableError
from krellbot.venues.xt import API_HOST, BASE_URL, HttpTransport, XtVenue, compact_json, sign_xt

pytestmark = pytest.mark.usefixtures("live_capable_release")

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
                "orderId": "stop-9",
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
    assert truth.open_orders[0].stop_price == Decimal("89550.00")
    assert truth.recent_fills[0].side == "buy"
    assert truth.recent_fills[0].pair == WAVE_PAIR
    for call in transport.calls:
        assert urlparse(call.url).hostname == API_HOST


def test_xt_place_entry_with_stop_uses_quote_buy_and_limit_sell() -> None:
    transport = FakeXtTransport()
    venue = _venue(transport)
    ref = venue.place_entry_with_stop("coid-entry", Decimal("0.01"), Decimal(90000), pair=WAVE_PAIR)
    assert ref.pair == WAVE_PAIR
    assert ref.side == "buy"
    assert ref.stop_price == Decimal("90000.00")
    posts = [c for c in transport.calls if c.method == "POST"]
    assert len(posts) == 2
    assert isinstance(posts[0].body, dict)
    assert posts[0].body["symbol"] == WIRE
    assert posts[0].body["type"] == "MARKET"
    assert posts[0].body["side"] == "BUY"
    assert posts[0].body["quoteQty"] == "1000.00"
    assert posts[0].body["timeInForce"] == "IOC"
    assert "quantity" not in posts[0].body
    assert posts[0].body["clientOrderId"] == "coid-entry"
    assert posts[0].headers.get("xt-validate-appkey") == "XT-SYNTHETIC-KEY"
    signed = sign_xt(
        "XT-SYNTHETIC-SECRET",
        key_id="XT-SYNTHETIC-KEY",
        timestamp="1680885532722",
        method="POST",
        path="/v4/order",
        body=compact_json(posts[0].body),
    )
    assert posts[0].headers.get("xt-validate-signature") == signed
    assert isinstance(posts[1].body, dict)
    assert posts[1].body["type"] == "LIMIT"
    assert posts[1].body["side"] == "SELL"
    assert posts[1].body["timeInForce"] == "GTC"
    assert posts[1].body["price"] == "89550.00"
    assert posts[1].body["quantity"] == "0.01000"
    assert posts[1].body["clientOrderId"] == "coid-entry-stop"
    posts_after = len(posts)
    second = venue.place_entry_with_stop("coid-entry", Decimal("0.01"), Decimal(90000), pair=WAVE_PAIR)
    assert second.id == ref.id
    assert len([c for c in transport.calls if c.method == "POST"]) == posts_after


def test_xt_place_exit_cancel_stops_and_raise_stop() -> None:
    transport = FakeXtTransport(
        orders=[
            {
                "orderId": "stop-9",
                "clientOrderId": "exit-coid-stop",
                "symbol": WIRE,
                "side": "SELL",
                "quantity": "0.01000",
                "price": "89550.00",
                "type": "LIMIT",
            }
        ]
    )
    venue = _venue(transport)
    entry = venue.place_entry_with_stop("exit-coid", Decimal("0.01"), Decimal(90000), pair=WAVE_PAIR)
    found = venue.order_by_coid("exit-coid")
    assert found is not None
    assert found.id == entry.id
    exit_ref = venue.place_exit("exit-flat", Decimal("0.01"), pair=WAVE_PAIR)
    assert exit_ref.side == "sell"
    venue.cancel_stops(WAVE_PAIR)
    venue.raise_stop(WAVE_PAIR, Decimal(91000))
    posts = [c for c in transport.calls if c.method == "POST" and isinstance(c.body, dict)]
    assert any(body.get("price") == "90545.00" for body in (c.body for c in posts if isinstance(c.body, dict)))
    assert any(str(body.get("clientOrderId") or "").endswith("-raised") for body in (c.body for c in posts))
    deletes = [c for c in transport.calls if c.method == "DELETE"]
    assert deletes
    assert any(c.url.endswith("/v4/order/stop-9") for c in deletes)


def test_xt_check_key_trade_only_and_withdraw_refused() -> None:
    ok = _venue(FakeXtTransport()).check_key()
    assert ok.can_trade is True
    assert ok.can_withdraw is False
    bad = FakeXtTransport(balances={"permissions": ["WITHDRAW"], "assets": []})
    with pytest.raises(WithdrawCapableError):
        _venue(bad).check_key()


def test_xt_place_entry_blocked_when_paper_only_release(monkeypatch) -> None:
    from krellbot import release_policy

    monkeypatch.setattr(release_policy, "PAPER_ONLY_RELEASE", True)
    transport = FakeXtTransport()
    with pytest.raises(PaperOnlyReleaseError):
        XtVenue(
            key_id="XT-SYNTHETIC-KEY",
            api_secret="XT-SYNTHETIC-SECRET",
            transport=transport,
            now_fn=lambda: 1,
        ).place_entry_with_stop("x", Decimal("0.01"), Decimal(1), pair=WAVE_PAIR)
    assert transport.calls == []


def test_xt_sign_matches_independent_prehash_digest() -> None:
    """HMAC-SHA256 of the documented validate string, lowercase hex.

    The digest was computed outside this function for secret
    ``XT-SYNTHETIC-SECRET``, key id ``XT-SYNTHETIC-KEY``, timestamp
    ``1680885532722``, ``POST /v4/order``, and the compact body below.
    """
    body = '{"bizType":"SPOT","clientOrderId":"coid","quoteQty":"100","side":"BUY","symbol":"btc_usdt","timeInForce":"IOC","type":"MARKET"}'
    signature = sign_xt(
        "XT-SYNTHETIC-SECRET",
        key_id="XT-SYNTHETIC-KEY",
        timestamp="1680885532722",
        method="POST",
        path="/v4/order",
        body=body,
    )
    assert signature == "00f68e5c43f996e8a3dbc6c127d5c81c7886ab51dd4da173e8ea58a72ca6cfe2"
