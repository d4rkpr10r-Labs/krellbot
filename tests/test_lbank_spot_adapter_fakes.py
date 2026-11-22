"""LBank spot adapter: Venue protocol behind injectable fakes.

CI must never open a network socket to LBank. ``LbankVenue`` requires an
explicit ``Transport``; ``None`` refuses rather than defaulting to
``HttpTransport``. Order mutations fail closed under either release setting.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

import pytest
from fakes.fake_lbank import FakeLbankTransport

from krellbot import release_policy
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
                "clientOrderId": "ordinary-limit-stop",
                "symbol": "btc_usdt",
                "type": "sell",
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
    venue = _venue(transport)
    truth = venue.snapshot()
    assets = {b.asset: b for b in truth.balances}
    assert assets["USDT"].free == Decimal(1000)
    assert len(truth.open_orders) == 1
    order = truth.open_orders[0]
    assert order.coid == "ordinary-limit-stop"
    assert order.pair == WAVE_PAIR
    assert order.side == "sell"
    assert order.stop_price is None
    assert venue.order_by_coid(order.coid) == order
    assert truth.recent_fills[0].side == "buy"
    assert truth.recent_fills[0].pair == WAVE_PAIR


@pytest.mark.parametrize(
    ("paper_only", "expected_error"), [(True, PaperOnlyReleaseError), (False, NotImplementedError)]
)
def test_lbank_order_mutations_refuse_before_transport(monkeypatch, paper_only, expected_error) -> None:
    monkeypatch.setattr(release_policy, "PAPER_ONLY_RELEASE", paper_only)
    transport = FakeLbankTransport()
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


def test_lbank_signed_queries_refuse_order_paths() -> None:
    transport = FakeLbankTransport()
    with pytest.raises(ValueError, match="unsupported account query"):
        _venue(transport)._signed_read("/v2/supplement/create_order.do", {})
    assert transport.calls == []


@pytest.mark.parametrize("can_trade", [True, False])
def test_lbank_check_key_reads_explicit_api_key_permissions(can_trade) -> None:
    transport = FakeLbankTransport(
        permissions={
            "result": True,
            "error_code": 0,
            "data": {"enableSpotTrading": can_trade, "enableWithdrawals": False},
        }
    )
    perms = _venue(transport).check_key()
    assert perms.can_trade is can_trade
    assert perms.can_withdraw is False
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.method == "POST"
    assert call.url == f"{BASE_URL}/v2/supplement/api_Restrictions.do"
    assert call.body == {
        "api_key": "test-key",
        "sign": "bca9b3c1a3d4dc5a19b265a5c100c9085dbfd9922cbe1ad7cf86093d97565fd8",
    }
    assert call.headers == {
        "content-type": "application/x-www-form-urlencoded",
        "timestamp": "1700000000000",
        "signature_method": "HmacSHA256",
        "echostr": ECHO,
    }


def test_lbank_check_key_refuses_withdrawable_api_key() -> None:
    transport = FakeLbankTransport(
        permissions={"result": "true", "error_code": 0, "data": {"enableSpotTrading": True, "enableWithdrawals": True}}
    )
    with pytest.raises(WithdrawCapableError):
        _venue(transport).check_key()


@pytest.mark.parametrize(
    "permissions",
    [
        {},
        {"canTrade": True, "canWithdraw": False},
        {"enableSpotTrading": True},
        {"enableSpotTrading": True, "enableWithdrawals": "false"},
        {"enableSpotTrading": "true", "enableWithdrawals": False},
    ],
)
def test_lbank_check_key_refuses_unknown_permissions(permissions) -> None:
    transport = FakeLbankTransport(permissions={"result": True, "error_code": 0, "data": permissions})
    with pytest.raises(TypeError, match="API key permissions response is incomplete"):
        _venue(transport).check_key()


def test_lbank_snapshot_rejects_account_error() -> None:
    transport = FakeLbankTransport(account={"result": False, "error_code": 10022})
    with pytest.raises(RuntimeError, match="account data request failed"):
        _venue(transport).snapshot()
    assert len(transport.calls) == 1


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
