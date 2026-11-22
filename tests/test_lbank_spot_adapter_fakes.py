"""LBank spot adapter: Venue protocol behind injectable fakes.

CI must never open a network socket to LBank. ``LbankVenue`` requires an
explicit ``Transport``; ``None`` refuses rather than defaulting to
``HttpTransport``. Order mutations still go through
``release_policy.ensure_live_orders_allowed``.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

import pytest
from fakes.fake_lbank import FakeLbankTransport

from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import WithdrawCapableError
from krellbot.venues.lbank import API_HOST, BASE_URL, HttpTransport, LbankVenue, sign_lbank

pytestmark = pytest.mark.usefixtures("live_capable_release")

WAVE_PAIR = "BTC-USDT"
ECHO = "LBANKECHOSTR0123456789ABCDEF01"


def _venue(transport: FakeLbankTransport, **kwargs) -> LbankVenue:
    return LbankVenue(
        key_id="test-key",
        api_secret="LBANK-SYNTHETIC-SECRET",
        transport=transport,
        now_fn=lambda: 1_700_000_000_000,
        echo_fn=lambda: ECHO,
        **kwargs,
    )


def test_lbank_venue_refuses_none_transport() -> None:
    """No silent HttpTransport default — tests must inject a fake."""
    with pytest.raises(ValueError, match="transport is required"):
        LbankVenue(key_id="k", api_secret="s", transport=None)  # type: ignore[arg-type]


def test_lbank_http_transport_is_not_used_by_adapter_default() -> None:
    """Constructor has no network default; HttpTransport is opt-in only."""
    transport = FakeLbankTransport()
    venue = _venue(transport)
    assert not isinstance(venue._transport, HttpTransport)
    assert venue._transport is transport


def test_lbank_rules_btc_usdt_from_public_accuracy() -> None:
    transport = FakeLbankTransport()
    rules = _venue(transport).rules(WAVE_PAIR)
    assert rules.ordermin == Decimal("0.00001")
    assert rules.costmin == Decimal(1)
    assert rules.lot_decimals == 5
    assert rules.price_decimals == 2
    gets = [c for c in transport.calls if c.method == "GET"]
    assert gets
    assert "/v2/accuracy.do" in gets[0].url
    assert "btc_usdt" in gets[0].url
    assert urlparse(gets[0].url).hostname == API_HOST
    assert gets[0].url.startswith(BASE_URL)


def test_lbank_snapshot_balances_orders_and_fills() -> None:
    transport = FakeLbankTransport(
        orders=[
            {
                "orderId": "ord-9",
                "clientOrderId": "rest-1",
                "symbol": "btc_usdt",
                "type": "buy",
                "origQty": "0.01",
                "price": "100000",
                "status": 0,
            }
        ],
        fills=[
            {
                "id": "fill-7",
                "orderId": "ord-1",
                "symbol": "btc_usdt",
                "qty": "0.01",
                "price": "100000",
                "time": 1700000000000,
                "isBuyer": True,
            }
        ],
    )
    truth = _venue(transport).snapshot()
    assets = {b.asset: b for b in truth.balances}
    assert assets["USDT"].free == Decimal(1000)
    assert len(truth.open_orders) == 1
    assert truth.open_orders[0].coid == "rest-1"
    assert truth.open_orders[0].pair == WAVE_PAIR
    assert truth.recent_fills[0].side == "buy"
    assert truth.recent_fills[0].pair == WAVE_PAIR


def test_lbank_place_entry_with_stop_uses_signed_posts() -> None:
    transport = FakeLbankTransport()
    venue = _venue(transport)
    ref = venue.place_entry_with_stop("coid", Decimal("0.0001"), Decimal(90000), pair=WAVE_PAIR)
    assert ref.pair == WAVE_PAIR
    assert ref.side == "buy"
    assert ref.stop_price == Decimal("90000.00")
    posts = [c for c in transport.calls if c.method == "POST"]
    assert len(posts) == 2
    assert isinstance(posts[0].body, dict)
    assert posts[0].body["type"] == "buy_market"
    assert posts[0].body["symbol"] == "btc_usdt"
    assert posts[0].body["custom_id"] == "coid"
    assert posts[0].body["price"] == "10.00"
    assert "amount" not in posts[0].body
    assert posts[0].body["api_key"] == "test-key"
    assert posts[0].body["sign"]
    assert posts[0].headers.get("echostr") == ECHO
    assert isinstance(posts[1].body, dict)
    assert posts[1].body["type"] == "sell"
    assert posts[1].body["custom_id"] == "coid-stop"
    assert posts[1].body["price"] == "89550.00"
    posts_after = len(posts)
    second = venue.place_entry_with_stop("coid", Decimal("0.0001"), Decimal(90000), pair=WAVE_PAIR)
    assert second.id == ref.id
    assert len([c for c in transport.calls if c.method == "POST"]) == posts_after


def test_lbank_place_exit_cancel_stops_and_raise_stop() -> None:
    transport = FakeLbankTransport(
        orders=[
            {
                "orderId": "stop-9",
                "clientOrderId": "exit-coid-stop",
                "symbol": "btc_usdt",
                "type": "sell",
                "origQty": "0.0001",
                "price": "89550",
                "status": 0,
            }
        ]
    )
    venue = _venue(transport)
    entry = venue.place_entry_with_stop("exit-coid", Decimal("0.0001"), Decimal(90000), pair=WAVE_PAIR)
    found = venue.order_by_coid("exit-coid")
    assert found is not None
    assert found.id == entry.id
    exit_ref = venue.place_exit("exit-flat", Decimal("0.0001"), pair=WAVE_PAIR)
    assert exit_ref.side == "sell"
    venue.cancel_stops(WAVE_PAIR)
    venue.raise_stop(WAVE_PAIR, Decimal(91000))
    posts = [c for c in transport.calls if c.method == "POST" and isinstance(c.body, dict)]
    assert any(body.get("type") == "sell_market" for body in (c.body for c in posts if isinstance(c.body, dict)))
    assert any("/cancel_order.do" in c.url for c in posts)
    assert any(
        isinstance(c.body, dict) and c.body.get("price") == "90545.00" and c.body.get("type") == "sell" for c in posts
    )


def test_lbank_check_key_trade_only_and_withdraw_refused() -> None:
    ok = _venue(FakeLbankTransport()).check_key()
    assert ok.can_trade is True
    assert ok.can_withdraw is False
    bad = FakeLbankTransport(
        account={
            "result": "true",
            "error_code": 0,
            "data": {"balances": [], "canTrade": True, "canWithdraw": True},
        }
    )
    with pytest.raises(WithdrawCapableError):
        _venue(bad).check_key()


def test_lbank_place_entry_blocked_when_paper_only_release(monkeypatch) -> None:
    from krellbot import release_policy

    monkeypatch.setattr(release_policy, "PAPER_ONLY_RELEASE", True)
    transport = FakeLbankTransport()
    with pytest.raises(PaperOnlyReleaseError):
        LbankVenue(
            key_id="k",
            api_secret="s",
            transport=transport,
            now_fn=lambda: 1,
            echo_fn=lambda: ECHO,
        ).place_entry_with_stop("x", Decimal("0.0001"), Decimal(1), pair=WAVE_PAIR)
    assert transport.calls == []


def test_lbank_sign_matches_documented_prehash() -> None:
    signature = sign_lbank(
        "LBANK-SYNTHETIC-SECRET",
        {
            "api_key": "test-key",
            "custom_id": "coid",
            "echostr": ECHO,
            "price": "100",
            "signature_method": "HmacSHA256",
            "symbol": "btc_usdt",
            "timestamp": "1700000000000",
            "type": "buy_market",
        },
    )
    assert signature == "d2731e2f35cb9570e49be72a388d99fc389650d65a68052e903b8ae724809bc5"
