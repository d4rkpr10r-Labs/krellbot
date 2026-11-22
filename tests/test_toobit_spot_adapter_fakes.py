"""Toobit spot adapter: Venue protocol behind injectable fakes.

CI must never open a network socket to Toobit. ``ToobitVenue`` requires an
explicit ``Transport``; ``None`` refuses rather than defaulting to
``HttpTransport``. Order mutations still go through
``release_policy.ensure_live_orders_allowed``.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from fakes.fake_toobit import FakeToobitTransport

from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import WithdrawCapableError
from krellbot.venues.toobit import API_HOST, BASE_URL, HttpTransport, ToobitVenue, sign_toobit

pytestmark = pytest.mark.usefixtures("live_capable_release")

WAVE_PAIR = "BTCUSDT"


def _venue(transport: FakeToobitTransport, **kwargs) -> ToobitVenue:
    return ToobitVenue(
        key_id="TOOBIT-SYNTHETIC-KEY",
        api_secret="TOOBIT-SYNTHETIC-SECRET",
        transport=transport,
        now_fn=lambda: 1_680_885_532_722,
        **kwargs,
    )


def test_toobit_venue_refuses_none_transport() -> None:
    """No silent HttpTransport default — tests must inject a fake."""
    with pytest.raises(ValueError, match="transport is required"):
        ToobitVenue(key_id="TOOBIT-SYNTHETIC-KEY", api_secret="TOOBIT-SYNTHETIC-SECRET", transport=None)  # type: ignore[arg-type]


def test_toobit_http_transport_is_not_used_by_adapter_default() -> None:
    """Constructor has no network default; HttpTransport is opt-in only."""
    transport = FakeToobitTransport()
    venue = _venue(transport)
    assert not isinstance(venue._transport, HttpTransport)
    assert venue._transport is transport


def test_toobit_rules_btc_usdt_from_public_exchange_info() -> None:
    transport = FakeToobitTransport()
    rules = _venue(transport).rules(WAVE_PAIR)
    assert rules.ordermin == Decimal("0.000001")
    assert rules.costmin == Decimal(1)
    assert rules.lot_decimals == 6
    assert rules.price_decimals == 2
    gets = [c for c in transport.calls if c.method == "GET"]
    assert any("exchangeInfo" in c.url for c in gets)
    assert any("bookTicker" in c.url and "BTCUSDT" in c.url for c in gets)
    assert all(urlparse(c.url).hostname == API_HOST for c in gets)
    assert all(c.url.startswith(BASE_URL) for c in gets)


def test_toobit_snapshot_balances_orders_and_fills() -> None:
    transport = FakeToobitTransport(
        orders=[
            {
                "orderId": "stop-9",
                "clientOrderId": "stop-1-stop",
                "symbol": WAVE_PAIR,
                "side": "SELL",
                "origQty": "0.01",
                "price": "89550.00",
                "type": "LIMIT",
            }
        ],
        fills=[
            {
                "id": "7",
                "orderId": "1",
                "symbol": WAVE_PAIR,
                "isBuyer": True,
                "qty": "0.01",
                "price": "100000",
                "time": 1700000000000,
            }
        ],
    )
    truth = _venue(transport).snapshot()
    assets = {b.asset: b for b in truth.balances}
    assert assets["USDT"].free == Decimal(1000)
    assert len(truth.open_orders) == 1
    assert truth.open_orders[0].stop_price == Decimal("89550.00")
    assert truth.recent_fills[0].side == "buy"
    for call in transport.calls:
        assert urlparse(call.url).hostname == API_HOST


def test_toobit_place_entry_with_stop_uses_quote_buy_and_limit_sell() -> None:
    transport = FakeToobitTransport()
    venue = _venue(transport)
    ref = venue.place_entry_with_stop("coid-entry", Decimal("0.01"), Decimal(90000), pair=WAVE_PAIR)
    assert ref.pair == WAVE_PAIR
    assert ref.side == "buy"
    assert ref.stop_price == Decimal("90000.00")
    posts = [c for c in transport.calls if c.method == "POST"]
    assert len(posts) == 2
    assert isinstance(posts[0].body, dict)
    assert posts[0].body["type"] == "MARKET"
    assert posts[0].body["side"] == "BUY"
    assert posts[0].body["quantity"] == "1000.00"
    assert posts[0].body["newClientOrderId"] == "coid-entry"
    assert posts[0].headers.get("X-BB-APIKEY") == "TOOBIT-SYNTHETIC-KEY"
    assert isinstance(posts[1].body, dict)
    assert posts[1].body["type"] == "LIMIT"
    assert posts[1].body["side"] == "SELL"
    assert posts[1].body["timeInForce"] == "GTC"
    assert posts[1].body["price"] == "89550.00"
    assert posts[1].body["quantity"] == "0.010000"
    assert posts[1].body["newClientOrderId"] == "coid-entry-stop"
    signed = {k: v for k, v in posts[0].body.items() if k != "signature"}
    assert posts[0].body["signature"] == sign_toobit("TOOBIT-SYNTHETIC-SECRET", urlencode(signed))
    posts_after = len(posts)
    second = venue.place_entry_with_stop("coid-entry", Decimal("0.01"), Decimal(90000), pair=WAVE_PAIR)
    assert second.id == ref.id
    assert len([c for c in transport.calls if c.method == "POST"]) == posts_after


def test_toobit_place_exit_cancel_stops_and_raise_stop() -> None:
    transport = FakeToobitTransport(
        orders=[
            {
                "orderId": "stop-9",
                "clientOrderId": "exit-coid-stop",
                "symbol": WAVE_PAIR,
                "side": "SELL",
                "origQty": "0.01",
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
    assert any(str(body.get("newClientOrderId") or "").endswith("-raised") for body in (c.body for c in posts))
    deletes = [c for c in transport.calls if c.method == "DELETE"]
    assert deletes
    assert all("/api/v1/spot/order" in c.url for c in deletes)


def test_toobit_check_key_trade_only_and_withdraw_refused() -> None:
    ok = _venue(FakeToobitTransport()).check_key()
    assert ok.can_trade is True
    assert ok.can_withdraw is False
    bad = FakeToobitTransport(
        account={
            "canWithdraw": True,
            "permissions": ["SPOT"],
            "balances": [],
        }
    )
    with pytest.raises(WithdrawCapableError):
        _venue(bad).check_key()
    flagged = FakeToobitTransport(
        account={
            "canWithdraw": False,
            "permissions": ["SPOT", "WITHDRAW"],
            "balances": [],
        }
    )
    with pytest.raises(WithdrawCapableError):
        _venue(flagged).check_key()


def test_toobit_place_entry_blocked_when_paper_only_release(monkeypatch) -> None:
    from krellbot import release_policy

    monkeypatch.setattr(release_policy, "PAPER_ONLY_RELEASE", True)
    transport = FakeToobitTransport()
    with pytest.raises(PaperOnlyReleaseError):
        ToobitVenue(
            key_id="TOOBIT-SYNTHETIC-KEY",
            api_secret="TOOBIT-SYNTHETIC-SECRET",
            transport=transport,
            now_fn=lambda: 1,
        ).place_entry_with_stop("x", Decimal("0.01"), Decimal(1), pair=WAVE_PAIR)
    assert transport.calls == []


def test_toobit_sign_matches_independent_total_params_digest() -> None:
    """HMAC-SHA256 of the documented totalParams string, lowercase hex.

    The digest was computed with the standard library outside this function
    for secret ``TOOBIT-SYNTHETIC-SECRET`` and
    ``symbol=BTCUSDT&side=BUY&type=MARKET&quantity=100&recvWindow=5000&timestamp=1680885532722``.
    """
    signature = sign_toobit(
        "TOOBIT-SYNTHETIC-SECRET",
        "symbol=BTCUSDT&side=BUY&type=MARKET&quantity=100&recvWindow=5000&timestamp=1680885532722",
    )
    assert signature == "54464e66d4e7b4af6c6d0593fc3896daefd959cf5d402c64ad694e9850ccf067"
    query = parse_qs("symbol=BTCUSDT&side=BUY&type=MARKET&quantity=100&recvWindow=5000&timestamp=1680885532722")
    assert query["symbol"] == ["BTCUSDT"]
