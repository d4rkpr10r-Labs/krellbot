"""Mexc spot adapter: Venue protocol behind injectable fakes.

CI must never open a network socket to Mexc. ``MexcVenue`` requires an
explicit ``Transport``; ``None`` refuses rather than defaulting to
``HttpTransport``. Order mutations refuse even when the release allows
live trading.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from fakes.fake_mexc import FakeMexcTransport

from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.mexc import API_HOST, BASE_URL, HttpTransport, MexcVenue

WAVE_PAIR = "BTCUSDT"


def _venue(transport: FakeMexcTransport, **kwargs) -> MexcVenue:
    return MexcVenue(
        key_id="MEXC-SYNTHETIC-KEY",
        api_secret="MEXC-SYNTHETIC-SECRET",
        transport=transport,
        now_fn=lambda: 1_680_885_532_722,
        **kwargs,
    )


def test_mexc_venue_refuses_none_transport() -> None:
    """No silent HttpTransport default — tests must inject a fake."""
    with pytest.raises(ValueError, match="transport is required"):
        MexcVenue(key_id="MEXC-SYNTHETIC-KEY", api_secret="MEXC-SYNTHETIC-SECRET", transport=None)  # type: ignore[arg-type]


def test_mexc_http_transport_is_not_used_by_adapter_default() -> None:
    """Constructor has no network default; HttpTransport is opt-in only."""
    transport = FakeMexcTransport()
    venue = _venue(transport)
    assert not isinstance(venue._transport, HttpTransport)
    assert venue._transport is transport


def test_mexc_rules_btc_usdt_from_public_exchange_info() -> None:
    transport = FakeMexcTransport()
    rules = _venue(transport).rules(WAVE_PAIR)
    assert rules.ordermin == Decimal("0.000001")
    assert rules.costmin == Decimal(1)
    assert rules.lot_decimals == 6
    assert rules.price_decimals == 2
    gets = [c for c in transport.calls if c.method == "GET"]
    assert any("exchangeInfo" in c.url and "BTCUSDT" in c.url for c in gets)
    assert all(urlparse(c.url).hostname == API_HOST for c in gets)
    assert all(c.url.startswith(BASE_URL) for c in gets)


def test_mexc_snapshot_balances_orders_and_fills() -> None:
    transport = FakeMexcTransport(
        orders=[
            {
                "orderId": "limit-9",
                "clientOrderId": "stop-1-stop",
                "symbol": WAVE_PAIR,
                "side": "SELL",
                "origQty": "0.01",
                "price": "89550.00",
                "type": "LIMIT",
            },
            {
                "orderId": "stop-9",
                "clientOrderId": "actual-conditional-stop",
                "symbol": WAVE_PAIR,
                "side": "SELL",
                "origQty": "0.01",
                "type": "STOP_MARKET_ORDER",
                "stopPrice": "90500.00",
            },
        ],
        fills=[
            {
                "id": "7",
                "orderId": "1",
                "clientOrderId": "fill-1",
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
    assert len(truth.open_orders) == 2
    assert truth.open_orders[0].stop_price is None
    assert truth.open_orders[1].stop_price == Decimal("90500.00")
    trades_call = next(call for call in transport.calls if "/api/v3/myTrades" in call.url)
    assert parse_qs(urlparse(trades_call.url).query)["symbol"] == [WAVE_PAIR]
    assert truth.recent_fills[0].side == "buy"
    for call in transport.calls:
        assert urlparse(call.url).hostname == API_HOST


@pytest.mark.parametrize("paper_only", [True, False])
def test_mexc_order_mutations_refuse_before_transport(monkeypatch, paper_only: bool) -> None:
    from krellbot import release_policy

    monkeypatch.setattr(release_policy, "PAPER_ONLY_RELEASE", paper_only)
    transport = FakeMexcTransport()
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


def test_mexc_account_information_does_not_verify_trade_only_keys() -> None:
    transport = FakeMexcTransport()
    with pytest.raises(NotImplementedError, match="trade-only key permissions are not qualified"):
        _venue(transport).check_key()
    assert transport.calls == []


def test_mexc_signed_get_matches_independent_total_params_digest() -> None:
    """Pin the signed pending-order GET request to a separately computed digest."""
    transport = FakeMexcTransport()
    assert _venue(transport).order_by_coid("coid-absent") is None
    (call,) = transport.calls
    assert (
        call.url
        == BASE_URL
        + "/api/v3/openOrders?symbol=BTCUSDT&timestamp=1680885532722&recvWindow=5000&signature=ffac1645e96aece024b93e481085f56c1958289bb097e27c5538c09895d66d85"
    )
    assert call.headers["X-MEXC-APIKEY"] == "MEXC-SYNTHETIC-KEY"
