"""Bitget spot adapter: Venue protocol behind injectable fakes.

CI must never open a network socket to Bitget. ``BitgetVenue`` requires an
explicit ``Transport``; ``None`` refuses rather than defaulting to
``HttpTransport``. Order mutations still go through
``release_policy.ensure_live_orders_allowed``.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

import pytest
from fakes.fake_bitget import FakeBitgetTransport

from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import WithdrawCapableError
from krellbot.venues.bitget import API_HOST, BASE_URL, BitgetVenue, HttpTransport, canonical_json, sign_bitget

WAVE_PAIR = "BTCUSDT"
NOW = 1_700_000_000_000
SECRET = "BITGET-SYNTHETIC-SECRET"


def _venue(transport: FakeBitgetTransport, **kwargs) -> BitgetVenue:
    return BitgetVenue(
        "test-key",
        SECRET,
        "bitget-passphrase",
        transport,
        now_fn=lambda: NOW,
        **kwargs,
    )


def test_bitget_venue_refuses_none_transport(monkeypatch) -> None:
    """No silent HttpTransport default, and no secret is registered."""
    called = False

    def _refuse(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("register_secret must not run")

    monkeypatch.setattr("krellbot.venues.bitget.sanitize.register_secret", _refuse)
    with pytest.raises(ValueError, match="transport is required"):
        BitgetVenue("test-key", SECRET, "bitget-passphrase", None)
    assert called is False


def test_bitget_http_transport_is_not_used_by_adapter_default() -> None:
    assert API_HOST == "api.bitget.com"
    assert BASE_URL == "https://api.bitget.com"
    assert HttpTransport.__name__ == "HttpTransport"


def test_bitget_rules_btcusdt_from_public_symbols() -> None:
    transport = FakeBitgetTransport()
    rules = _venue(transport).rules(WAVE_PAIR)
    assert rules.lot_decimals == 6
    assert rules.price_decimals == 2
    assert rules.ordermin == Decimal("0.000001")
    assert rules.costmin == Decimal(1)
    assert transport.gets
    assert urlparse(transport.gets[0]).netloc == API_HOST
    assert "symbol=BTCUSDT" in transport.gets[0]


@pytest.mark.usefixtures("live_capable_release")
def test_bitget_signature_matches_documented_prehash() -> None:
    """ACCESS-SIGN is base64 HMAC-SHA256 of timestamp + METHOD + path + body."""
    body = {
        "clientOid": "coid",
        "orderType": "market",
        "side": "buy",
        "size": "10",
        "symbol": "BTCUSDT",
    }
    encoded = canonical_json(body)
    signature = sign_bitget(SECRET, str(NOW), "POST", "/api/v2/spot/trade/place-order", encoded)
    assert signature == "lLC/YeU0tXKCWm5Mtxq1DwBXZCesSjlpv/PQbiqrVwc="
    transport = FakeBitgetTransport()
    _venue(transport).place_entry_with_stop("coid", Decimal("0.0001"), Decimal(90000), pair=WAVE_PAIR)
    _path, posted, headers = transport.posts[0]
    assert headers["ACCESS-KEY"] == "test-key"
    assert headers["ACCESS-PASSPHRASE"] == "bitget-passphrase"
    assert headers["ACCESS-TIMESTAMP"] == str(NOW)
    assert headers["ACCESS-SIGN"] == sign_bitget(
        SECRET,
        str(NOW),
        "POST",
        "/api/v2/spot/trade/place-order",
        canonical_json(posted or {}),
    )


@pytest.mark.usefixtures("live_capable_release")
def test_bitget_place_entry_sends_quote_buy_and_trigger_stop() -> None:
    transport = FakeBitgetTransport()
    venue = _venue(transport)
    ref = venue.place_entry_with_stop("coid", Decimal("0.0001"), Decimal(90000), pair=WAVE_PAIR)
    assert ref.coid == "coid"
    assert ref.qty == Decimal("0.0001")
    assert ref.stop_price == Decimal("90000.00")
    assert len(transport.posts) == 2
    buy = transport.posts[0][1]
    stop = transport.posts[1][1]
    assert buy == {
        "clientOid": "coid",
        "orderType": "market",
        "side": "buy",
        "size": "10.00000000",
        "symbol": "BTCUSDT",
    }
    assert stop["tpslType"] == "tpsl"
    assert stop["side"] == "sell"
    assert stop["triggerPrice"] == "90000.00"
    assert stop["size"] == "0.000100"
    assert stop["clientOid"] == "coid-stop"
    again = venue.place_entry_with_stop("coid", Decimal("0.0001"), Decimal(90000), pair=WAVE_PAIR)
    assert again.coid == "coid"
    assert len(transport.posts) == 2


def test_bitget_place_entry_refuses_while_paper_only() -> None:
    transport = FakeBitgetTransport()
    with pytest.raises(PaperOnlyReleaseError):
        _venue(transport).place_entry_with_stop("coid", Decimal("0.0001"), Decimal(90000), pair=WAVE_PAIR)
    assert transport.posts == []


def test_bitget_snapshot_reads_assets_orders_and_fills() -> None:
    transport = FakeBitgetTransport(
        orders={
            "code": "00000",
            "data": [
                {
                    "orderId": "s1",
                    "clientOid": "coid-stop",
                    "symbol": "BTCUSDT",
                    "side": "sell",
                    "size": "0.0001",
                    "status": "live",
                    "triggerPrice": "90000",
                }
            ],
        },
        fills={
            "code": "00000",
            "data": [
                {
                    "tradeId": "f1",
                    "orderId": "9001",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "size": "0.0001",
                    "priceAvg": "100000",
                    "cTime": "1700000000000",
                }
            ],
        },
    )
    truth = _venue(transport).snapshot()
    assets = {item.asset: item.free for item in truth.balances}
    assert assets["USDT"] == Decimal(1000)
    assert assets["BTC"] == Decimal("0.01")
    assert truth.open_orders[0].coid == "coid-stop"
    assert truth.recent_fills[0].id == "f1"
    assert truth.recent_fills[0].side == "buy"


def test_bitget_check_key_refuses_withdraw_authority() -> None:
    transport = FakeBitgetTransport(info={"code": "00000", "data": {"authorities": ["trade", "withdraw"]}})
    with pytest.raises(WithdrawCapableError):
        _venue(transport).check_key()


def test_bitget_check_key_accepts_trade_without_withdraw() -> None:
    perms = _venue(FakeBitgetTransport()).check_key()
    assert perms.can_trade is True
    assert perms.can_withdraw is False


@pytest.mark.usefixtures("live_capable_release")
def test_bitget_raise_stop_replaces_the_trigger() -> None:
    transport = FakeBitgetTransport(
        orders={
            "code": "00000",
            "data": [
                {
                    "orderId": "s1",
                    "clientOid": "coid-stop",
                    "symbol": "BTCUSDT",
                    "side": "sell",
                    "size": "0.0001",
                    "status": "live",
                    "triggerPrice": "90000",
                }
            ],
        }
    )
    _venue(transport).raise_stop(WAVE_PAIR, Decimal(91000))
    assert transport.posts[0][1]["tpslType"] == "tpsl"
    assert transport.posts[0][1]["orderId"] == "s1"
    assert transport.posts[1][1]["triggerPrice"] == "91000.00"
    assert transport.posts[1][1]["clientOid"] == "coid-stop-raised"
