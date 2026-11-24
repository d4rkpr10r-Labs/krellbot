"""Crypto.com Exchange spot adapter against a fake transport. No network and no credentials."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fakes.fake_cryptocom import FakeCryptocomTransport

from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import WithdrawCapableError
from krellbot.venues.cryptocom import BASE_URL, CryptocomVenue, sign_cryptocom

WAVE_PAIR = "BTC-USDT"
SECRET = "CRYPTOCOM-SYNTHETIC-SECRET"
NONCE = "1610905028000"
BUY_PARAMS = {
    "client_oid": "coid",
    "instrument_name": "BTC_USDT",
    "notional": "100.00",
    "side": "BUY",
    "spot_margin": "SPOT",
    "type": "MARKET",
}


def _venue(transport: FakeCryptocomTransport) -> CryptocomVenue:
    return CryptocomVenue("key", SECRET, transport, now_fn=lambda: NONCE)


def test_cryptocom_signature_matches_documented_prehash() -> None:
    signature = sign_cryptocom(SECRET, "private/create-order", 1, "key", BUY_PARAMS, NONCE)
    assert signature == "eff830928577d98dff576ea7dfa4e80d228386416bf7395890924afceaecf7d2"


def test_cryptocom_venue_refuses_none_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _register(*_args: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("krellbot.venues.cryptocom.sanitize.register_secret", _register)
    with pytest.raises(ValueError, match="transport is required"):
        CryptocomVenue("key", SECRET, None)  # type: ignore[arg-type]
    assert called is False


def test_cryptocom_rules_read_public_btcusdt() -> None:
    transport = FakeCryptocomTransport()
    rules = _venue(transport).rules(WAVE_PAIR)
    assert rules.ordermin == Decimal("0.00001")
    assert rules.costmin == Decimal(1)
    assert rules.lot_decimals == 5
    assert rules.price_decimals == 2
    assert transport.gets[0] == f"{BASE_URL}/public/get-instruments"
    assert "get-risk-parameters" in transport.gets[1]


@pytest.mark.usefixtures("live_capable_release")
def test_cryptocom_place_entry_sends_quote_buy_and_stop_loss() -> None:
    transport = FakeCryptocomTransport()
    venue = _venue(transport)
    ref = venue.place_entry_with_stop("coid", Decimal("0.001"), Decimal(90000), pair=WAVE_PAIR)
    assert ref.coid == "coid"
    assert ref.qty == Decimal("0.00100")
    assert ref.stop_price == Decimal("90000.00")
    assert len(transport.posts) == 2
    buy_url, buy = transport.posts[0]
    stop_url, stop = transport.posts[1]
    assert buy_url == f"{BASE_URL}/private/create-order"
    assert buy["id"] == 1
    assert buy["nonce"] == int(NONCE)
    assert buy["api_key"] == "key"
    assert buy["sig"] == "eff830928577d98dff576ea7dfa4e80d228386416bf7395890924afceaecf7d2"
    assert buy["params"] == BUY_PARAMS
    assert stop_url == f"{BASE_URL}/private/advanced/create-order"
    assert stop["params"] == {
        "client_oid": "coid-stop",
        "instrument_name": "BTC_USDT",
        "quantity": "0.00100",
        "ref_price": "90000.00",
        "side": "SELL",
        "type": "STOP_LOSS",
    }
    again = venue.place_entry_with_stop("coid", Decimal("0.001"), Decimal(90000), pair=WAVE_PAIR)
    assert again.coid == "coid"
    assert len(transport.posts) == 2


def test_cryptocom_place_entry_refuses_while_paper_only() -> None:
    transport = FakeCryptocomTransport()
    with pytest.raises(PaperOnlyReleaseError):
        _venue(transport).place_entry_with_stop("coid", Decimal("0.001"), Decimal(90000), pair=WAVE_PAIR)
    assert transport.posts == []


@pytest.mark.usefixtures("live_capable_release")
def test_cryptocom_raise_stop_replaces_the_trigger() -> None:
    transport = FakeCryptocomTransport(
        orders=[
            {
                "order_id": "9002",
                "client_oid": "coid-stop",
                "instrument_name": "BTC_USDT",
                "side": "SELL",
                "status": "ACTIVE",
                "quantity": "0.00100",
                "ref_price": "90000.00",
                "order_type": "STOP_LOSS",
            }
        ]
    )
    _venue(transport).raise_stop(WAVE_PAIR, Decimal(91000))
    cancelled = [body for url, body in transport.posts if str(url).endswith("/private/advanced/cancel-order")]
    placed = [body for url, body in transport.posts if str(url).endswith("/private/advanced/create-order")]
    assert cancelled and cancelled[0]["params"]["client_oid"] == "coid-stop"
    assert placed and placed[-1]["params"]["ref_price"] == "91000.00"
    assert placed[-1]["params"]["client_oid"] == "coid-stop-raised"


def test_cryptocom_check_key_refuses_withdraw_permission() -> None:
    transport = FakeCryptocomTransport(accounts={"code": 0, "result": {"permissions": ["Can Trade", "Can Withdraw"]}})
    with pytest.raises(WithdrawCapableError):
        _venue(transport).check_key()


def test_cryptocom_check_key_trade_without_withdraw() -> None:
    perms = _venue(FakeCryptocomTransport()).check_key()
    assert perms.can_trade is True
    assert perms.can_withdraw is False


def test_cryptocom_check_key_refuses_missing_permission() -> None:
    transport = FakeCryptocomTransport(accounts={"code": 0, "result": {"this_account": {"tradable": True}}})
    with pytest.raises(TypeError, match="permission"):
        _venue(transport).check_key()
