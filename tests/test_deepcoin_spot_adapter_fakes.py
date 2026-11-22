"""Deepcoin spot adapter: Venue protocol behind injectable fakes.

CI must never open a network socket to Deepcoin. ``DeepcoinVenue`` requires
an explicit ``Transport``; ``None`` refuses rather than defaulting to
``HttpTransport``. Live order mutations and trade-only key qualification fail closed.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

import pytest
from fakes.fake_deepcoin import FakeDeepcoinTransport

from krellbot import release_policy
from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.deepcoin import API_HOST, BASE_URL, DeepcoinVenue, HttpTransport

pytestmark = pytest.mark.usefixtures("live_capable_release")

WAVE_PAIR = "BTC-USDT"


def _venue(transport: FakeDeepcoinTransport, **kwargs) -> DeepcoinVenue:
    return DeepcoinVenue(
        key_id="test-key",
        api_secret="DEEPCOIN-SYNTHETIC-SECRET",
        passphrase="test-pass",
        transport=transport,
        now_fn=lambda: 1_700_000_000_000,
        **kwargs,
    )


def test_deepcoin_venue_refuses_none_transport() -> None:
    """No silent HttpTransport default — tests must inject a fake."""
    with pytest.raises(ValueError, match="transport is required"):
        DeepcoinVenue(key_id="k", api_secret="s", passphrase="p", transport=None)  # type: ignore[arg-type]


def test_deepcoin_http_transport_is_not_used_by_adapter_default() -> None:
    """Constructor has no network default; HttpTransport is opt-in only."""
    transport = FakeDeepcoinTransport()
    venue = _venue(transport)
    assert not isinstance(venue._transport, HttpTransport)
    assert venue._transport is transport


def test_deepcoin_rules_btc_usdt_from_public_instruments() -> None:
    transport = FakeDeepcoinTransport()
    rules = _venue(transport).rules(WAVE_PAIR)
    assert rules.ordermin == Decimal("0.0004")
    assert rules.costmin == Decimal(1)
    assert rules.lot_decimals == 8
    assert rules.price_decimals == 1
    gets = [c for c in transport.calls if c.method == "GET"]
    assert gets
    assert "/deepcoin/market/instruments" in gets[0].url
    assert "BTC-USDT" in gets[0].url
    parsed = urlparse(gets[0].url)
    assert parsed.hostname == API_HOST
    assert gets[0].url.startswith(BASE_URL)


def test_deepcoin_snapshot_balances_orders_and_fills() -> None:
    transport = FakeDeepcoinTransport(
        orders=[
            {
                "ordId": "ord-9",
                "clOrdId": "rest-1",
                "instId": WAVE_PAIR,
                "side": "sell",
                "ordType": "limit",
                "px": "89550.0",
                "sz": "0.01",
            }
        ],
        fills=[
            {
                "tradeId": "7",
                "ordId": "1",
                "clOrdId": "fill-coid",
                "instId": WAVE_PAIR,
                "side": "buy",
                "fillSz": "0.01",
                "fillPx": "100000",
                "ts": "1700000000000",
            }
        ],
    )
    venue = _venue(transport)
    truth = venue.snapshot()
    assets = {b.asset: b for b in truth.balances}
    assert assets["USDT"].free == Decimal(1000)
    assert len(truth.open_orders) == 1
    assert truth.open_orders[0].coid == "rest-1"
    assert truth.open_orders[0].stop_price is None
    assert venue.order_by_coid("rest-1") == truth.open_orders[0]
    assert truth.recent_fills[0].side == "buy"
    assert truth.recent_fills[0].coid == "fill-coid"
    for call in transport.calls:
        assert urlparse(call.url).hostname == API_HOST


@pytest.mark.parametrize(
    ("paper_only", "expected_error"),
    [(True, PaperOnlyReleaseError), (False, NotImplementedError)],
)
def test_deepcoin_order_mutations_refuse_before_transport(monkeypatch, paper_only, expected_error) -> None:
    monkeypatch.setattr(release_policy, "PAPER_ONLY_RELEASE", paper_only)
    transport = FakeDeepcoinTransport()
    venue = _venue(transport)
    with pytest.raises(expected_error):
        venue.place_entry_with_stop("entry", Decimal("0.01"), Decimal(90000), pair=WAVE_PAIR)
    with pytest.raises(expected_error):
        venue.place_exit("exit", Decimal("0.01"), pair=WAVE_PAIR)
    with pytest.raises(expected_error):
        venue.cancel_stops(WAVE_PAIR)
    with pytest.raises(expected_error):
        venue.raise_stop(WAVE_PAIR, Decimal(91000))
    assert transport.calls == []


def test_deepcoin_cannot_qualify_trade_only_key_permissions() -> None:
    transport = FakeDeepcoinTransport()
    with pytest.raises(NotImplementedError, match="trade-only key permissions are not qualified"):
        _venue(transport).check_key()
    assert transport.calls == []


def test_deepcoin_snapshot_rejects_account_error() -> None:
    transport = FakeDeepcoinTransport(balance={"code": "100", "data": []})
    with pytest.raises(RuntimeError, match="account data request failed"):
        _venue(transport).snapshot()
    assert len(transport.calls) == 1


def test_deepcoin_signed_read_includes_query_in_documented_prehash() -> None:
    transport = FakeDeepcoinTransport()
    assert _venue(transport).order_by_coid("absent") is None
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.method == "GET"
    assert call.url == f"{BASE_URL}/deepcoin/trade/orders-pending?instType=SPOT"
    assert call.headers == {
        "DC-ACCESS-KEY": "test-key",
        "DC-ACCESS-SIGN": "P4hLocbhYNqwu0X9+NV1xLLhI3mhMQfV2hXYrr5YH0w=",
        "DC-ACCESS-TIMESTAMP": "2023-11-14T22:13:20.000Z",
        "DC-ACCESS-PASSPHRASE": "test-pass",
    }
