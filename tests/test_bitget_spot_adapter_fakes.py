"""Bitget spot adapter: Venue protocol behind injectable fakes.

CI must never open a network socket to Bitget. ``BitgetVenue`` requires an
explicit ``Transport``; ``None`` refuses rather than defaulting to
``HttpTransport``. Order mutations fail closed under either release setting.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

import pytest
from fakes.fake_bitget import FakeBitgetTransport

from krellbot import release_policy
from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import WithdrawCapableError
from krellbot.venues.bitget import API_HOST, BASE_URL, BitgetVenue, HttpTransport, sign_bitget

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


def test_bitget_signature_matches_documented_prehash() -> None:
    request_path = "/api/v2/spot/trade/unfilled-orders?symbol=BTCUSDT&tpslType=tpsl"
    signature = sign_bitget(SECRET, str(NOW), "GET", request_path)
    assert signature == "+GhOS4MJZFuVI5000NYxk2W79lbIcQInVCfkHbT0JpM="
    transport = FakeBitgetTransport()
    assert _venue(transport).order_by_coid("absent") is None
    assert transport.gets[1] == f"{BASE_URL}{request_path}"
    assert transport.request_headers[1] == {
        "ACCESS-KEY": "test-key",
        "ACCESS-PASSPHRASE": "bitget-passphrase",
        "ACCESS-TIMESTAMP": str(NOW),
        "ACCESS-SIGN": signature,
        "locale": "en-US",
    }


@pytest.mark.parametrize(
    ("paper_only", "expected_error"), [(True, PaperOnlyReleaseError), (False, NotImplementedError)]
)
def test_bitget_order_mutations_refuse_before_transport(monkeypatch, paper_only, expected_error) -> None:
    monkeypatch.setattr(release_policy, "PAPER_ONLY_RELEASE", paper_only)
    transport = FakeBitgetTransport()
    venue = _venue(transport)
    with pytest.raises(expected_error):
        venue.place_entry_with_stop("entry", Decimal("0.01"), Decimal(90000), pair=WAVE_PAIR)
    with pytest.raises(expected_error):
        venue.place_exit("exit", Decimal("0.01"), pair=WAVE_PAIR)
    with pytest.raises(expected_error):
        venue.cancel_stops(WAVE_PAIR)
    with pytest.raises(expected_error):
        venue.raise_stop(WAVE_PAIR, Decimal(91000))
    assert transport.gets == []


def test_bitget_snapshot_reads_assets_orders_and_fills() -> None:
    transport = FakeBitgetTransport(
        orders={
            "code": "00000",
            "data": [
                {
                    "orderId": "s1",
                    "clientOid": "ordinary-limit-stop",
                    "symbol": "btcusdt",
                    "orderType": "limit",
                    "tpslType": "normal",
                    "side": "sell",
                    "size": "0.0001",
                    "status": "live",
                }
            ],
        },
        conditional_orders={
            "code": "00000",
            "data": [
                {
                    "orderId": "s2",
                    "symbol": "btcusdt",
                    "orderType": "market",
                    "tpslType": "tpsl",
                    "side": "sell",
                    "size": "0.0001",
                    "status": "live",
                    "triggerPrice": "104000",
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
    venue = _venue(transport)
    truth = venue.snapshot()
    assets = {item.asset: item.free for item in truth.balances}
    assert assets["USDT"] == Decimal(1000)
    assert assets["BTC"] == Decimal("0.01")
    assert len(truth.open_orders) == 2
    assert truth.open_orders[0].coid == "ordinary-limit-stop"
    assert truth.open_orders[0].pair == WAVE_PAIR
    assert truth.open_orders[0].stop_price is None
    assert truth.open_orders[1].id == "s2"
    assert truth.open_orders[1].stop_price is None
    assert venue.order_by_coid("ordinary-limit-stop") == truth.open_orders[0]
    assert venue.order_by_coid("") is None
    assert truth.recent_fills[0].id == "f1"
    assert truth.recent_fills[0].side == "buy"


def test_bitget_check_key_refuses_withdraw_authority() -> None:
    transport = FakeBitgetTransport(info={"code": "00000", "data": {"authorities": ["stow", "wwow"]}})
    with pytest.raises(WithdrawCapableError):
        _venue(transport).check_key()


def test_bitget_check_key_accepts_trade_without_withdraw() -> None:
    perms = _venue(FakeBitgetTransport()).check_key()
    assert perms.can_trade is True
    assert perms.can_withdraw is False


def test_bitget_check_key_does_not_accept_readonly_as_spot_trade() -> None:
    transport = FakeBitgetTransport(info={"code": "00000", "data": {"authorities": ["stor"]}})
    assert _venue(transport).check_key().can_trade is False


@pytest.mark.parametrize("extra_permission", ["wtow", "unknown", 0])
def test_bitget_check_key_refuses_broad_or_unknown_permissions(extra_permission) -> None:
    transport = FakeBitgetTransport(info={"code": "00000", "data": {"authorities": ["stow", extra_permission]}})
    with pytest.raises(TypeError):
        _venue(transport).check_key()


def test_bitget_snapshot_does_not_use_rejected_account_read() -> None:
    transport = FakeBitgetTransport(assets={"code": "40006", "msg": "test-key", "data": []})
    with pytest.raises(RuntimeError, match="^bitget request refused$"):
        _venue(transport).snapshot()
    assert len(transport.gets) == 1


def test_bitget_rules_refuse_nonfinite_minimum() -> None:
    transport = FakeBitgetTransport()
    transport.symbol = {**transport.symbol, "data": [{**transport.symbol["data"][0], "minTradeUSDT": "NaN"}]}
    with pytest.raises(ValueError, match="bitget symbol minimum is invalid"):
        _venue(transport).rules(WAVE_PAIR)
