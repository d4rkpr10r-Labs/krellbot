"""HTX spot adapter against a fake transport. No network and no credentials."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fakes.fake_htx import FakeHtxTransport

from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import WithdrawCapableError
from krellbot.venues.htx import API_HOST, HtxVenue, sign_htx

WAVE_PAIR = "BTCUSDT"
SECRET = "HTX-SYNTHETIC-SECRET"
TIMESTAMP = "2026-01-01T00:00:00"


def _venue(transport: FakeHtxTransport) -> HtxVenue:
    return HtxVenue("key", SECRET, transport, now_fn=lambda: TIMESTAMP)


def test_htx_signature_matches_documented_prehash() -> None:
    signature = sign_htx(
        SECRET,
        "POST",
        API_HOST,
        "/v1/order/orders/place",
        {
            "AccessKeyId": "key",
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": TIMESTAMP,
        },
    )
    assert signature == "v5AE1VykqJnsk/muEvvqLDEwJRlMI5FjnhNPK8Nee90="


def test_htx_venue_refuses_none_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _register(*_args: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("krellbot.venues.htx.sanitize.register_secret", _register)
    with pytest.raises(ValueError, match="transport is required"):
        HtxVenue("key", SECRET, None)  # type: ignore[arg-type]
    assert called is False


@pytest.mark.usefixtures("live_capable_release")
def test_htx_place_entry_sends_quote_buy_and_trigger_stop() -> None:
    transport = FakeHtxTransport()
    venue = _venue(transport)
    ref = venue.place_entry_with_stop("coid", Decimal("0.0001"), Decimal(90000), pair=WAVE_PAIR)
    assert ref.coid == "coid"
    assert ref.qty == Decimal("0.0001")
    assert ref.stop_price == Decimal("90000.00")
    assert len(transport.posts) == 2
    buy_url, buy = transport.posts[0]
    stop_url, stop = transport.posts[1]
    assert buy_url.startswith(f"https://{API_HOST}/v1/order/orders/place?")
    assert "Signature=v5AE1VykqJnsk" in buy_url
    assert buy == {
        "account-id": "1001",
        "amount": "10.00000000",
        "client-order-id": "coid",
        "source": "spot-api",
        "symbol": "btcusdt",
        "type": "buy-market",
    }
    assert stop_url.startswith(f"https://{API_HOST}/v2/algo-orders?")
    assert stop == {
        "accountId": "1001",
        "clientOrderId": "coid-stop",
        "orderSide": "sell",
        "orderSize": "0.000100",
        "orderType": "market",
        "stopPrice": "90000.00",
        "symbol": "btcusdt",
        "timeInForce": "ioc",
    }
    again = venue.place_entry_with_stop("coid", Decimal("0.0001"), Decimal(90000), pair=WAVE_PAIR)
    assert again.coid == "coid"
    assert len(transport.posts) == 2


def test_htx_place_entry_refuses_while_paper_only() -> None:
    transport = FakeHtxTransport()
    with pytest.raises(PaperOnlyReleaseError):
        _venue(transport).place_entry_with_stop("coid", Decimal("0.0001"), Decimal(90000), pair=WAVE_PAIR)
    assert transport.posts == []


@pytest.mark.usefixtures("live_capable_release")
def test_htx_raise_stop_replaces_the_trigger() -> None:
    transport = FakeHtxTransport(
        orders=[
            {
                "clientOrderId": "coid-stop",
                "orderId": "9002",
                "orderSide": "sell",
                "orderSize": "0.000100",
                "stopPrice": "90000.00",
                "symbol": "btcusdt",
            }
        ]
    )
    _venue(transport).raise_stop(WAVE_PAIR, Decimal(91000))
    assert any("/v2/algo-orders/cancellation" in url for url, _body in transport.posts)
    placed = [body for url, body in transport.posts if url.split("?", 1)[0].endswith("/v2/algo-orders")]
    assert placed
    assert placed[-1]["clientOrderId"] == "coid-stop-raised"
    assert placed[-1]["stopPrice"] == "91000.00"


def test_htx_check_key_refuses_withdraw_permission() -> None:
    transport = FakeHtxTransport(api_key={"code": 200, "data": [{"accessKey": "key", "permission": "trade,withdraw"}]})
    with pytest.raises(WithdrawCapableError):
        _venue(transport).check_key()


def test_htx_check_key_trade_without_withdraw() -> None:
    perms = _venue(FakeHtxTransport()).check_key()
    assert perms.can_trade is True
    assert perms.can_withdraw is False
