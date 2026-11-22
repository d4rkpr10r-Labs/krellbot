"""BTCC spot adapter against a fake transport. No network and no credentials."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fakes.fake_btcc import FakeBtccTransport

from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import WithdrawCapableError
from krellbot.venues.btcc import BASE_URL, BtccVenue, sign_btcc

WAVE_PAIR = "BTCUSDT"
SIGN_TAG = "BTCC-SYNTHETIC-SECRET"
TIMESTAMP = "1700000000000"


def _venue(transport: FakeBtccTransport) -> BtccVenue:
    return BtccVenue("key", SIGN_TAG, transport, now_fn=lambda: TIMESTAMP)


def test_btcc_signature_matches_sorted_secret_prehash() -> None:
    signature = sign_btcc(
        SIGN_TAG,
        {
            "amount": "100.0000",
            "client_id": "3",
            "market": "BTCUSDT",
            "side": 1,
            "source": "1",
        },
    )
    assert signature == "1489db18414d428b6390a071187e751b"
    assert "tm" not in signature


def test_btcc_venue_refuses_none_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _register(*_args: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("krellbot.venues.btcc.sanitize.register_secret", _register)
    with pytest.raises(ValueError, match="transport is required"):
        BtccVenue("key", SIGN_TAG, None)  # type: ignore[arg-type]
    assert called is False


def test_btcc_rules_read_the_public_btcusdt_market() -> None:
    transport = FakeBtccTransport()
    rules = _venue(transport).rules(WAVE_PAIR)
    assert rules.ordermin == Decimal("0.0003")
    assert rules.costmin == Decimal(10)
    assert rules.lot_decimals == 5
    assert rules.price_decimals == 2
    assert transport.gets
    assert transport.gets[0][0] == f"{BASE_URL}/btcc_trade/market/list"
    assert transport.gets[0][1] == {}


@pytest.mark.usefixtures("live_capable_release")
def test_btcc_place_entry_sends_quote_buy_and_stop_market() -> None:
    transport = FakeBtccTransport()
    venue = _venue(transport)
    ref = venue.place_entry_with_stop("coid", Decimal("0.001"), Decimal(90000), pair=WAVE_PAIR)
    assert ref.coid == "coid"
    assert ref.qty == Decimal("0.00100")
    assert ref.stop_price == Decimal("90000.00")
    assert len(transport.posts) == 2
    buy_url, buy, buy_headers = transport.posts[0]
    stop_url, stop, _stop_headers = transport.posts[1]
    assert buy_url == f"{BASE_URL}/btcc_trade/order/market"
    assert buy_headers["authorization"] == "1489db18414d428b6390a071187e751b"
    assert buy_headers["token"] == "key"
    assert buy_headers["tm"] == TIMESTAMP
    assert "tm" not in (buy or {})
    assert buy == {
        "amount": "100.0000",
        "client_id": "3",
        "market": "BTCUSDT",
        "side": 1,
        "source": "1",
    }
    assert stop_url == f"{BASE_URL}/btcc_trade/order/stop_market"
    assert stop == {
        "amount": "0.00100",
        "client_id": "3",
        "market": "BTCUSDT",
        "side": 2,
        "source": "1",
        "stop_price": "90000.00",
    }
    again = venue.place_entry_with_stop("coid", Decimal("0.001"), Decimal(90000), pair=WAVE_PAIR)
    assert again.coid == "coid"
    assert len(transport.posts) == 2


def test_btcc_place_entry_refuses_while_paper_only() -> None:
    transport = FakeBtccTransport()
    with pytest.raises(PaperOnlyReleaseError):
        _venue(transport).place_entry_with_stop("coid", Decimal("0.001"), Decimal(90000), pair=WAVE_PAIR)
    assert transport.posts == []


@pytest.mark.usefixtures("live_capable_release")
def test_btcc_raise_stop_replaces_the_trigger() -> None:
    transport = FakeBtccTransport(
        orders=[
            {
                "id": "9002",
                "market": "BTCUSDT",
                "side": 2,
                "amount": "0.00100",
                "stop_price": "90000.00",
            }
        ]
    )
    _venue(transport).raise_stop(WAVE_PAIR, Decimal(91000))
    assert any(url.endswith("/btcc_trade/order/cancle_stop") for url, _body, _headers in transport.posts)
    placed = [body for url, body, _headers in transport.posts if url.endswith("/btcc_trade/order/stop_market")]
    assert placed
    assert placed[-1]["stop_price"] == "91000.00"
    assert placed[-1]["side"] == 2


def test_btcc_check_key_refuses_withdraw_permission() -> None:
    transport = FakeBtccTransport(api_keys={"code": 0, "data": [{"apiKey": "key", "authorityStr": "1,4"}]})
    with pytest.raises(WithdrawCapableError):
        _venue(transport).check_key()


def test_btcc_check_key_trade_without_withdraw() -> None:
    perms = _venue(FakeBtccTransport()).check_key()
    assert perms.can_trade is True
    assert perms.can_withdraw is False


def test_btcc_check_key_refuses_missing_permission() -> None:
    transport = FakeBtccTransport(api_keys={"code": 0, "data": [{"apiKey": "key"}]})
    with pytest.raises(TypeError, match="permission"):
        _venue(transport).check_key()
