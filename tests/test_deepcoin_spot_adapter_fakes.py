"""Deepcoin spot adapter: Venue protocol behind injectable fakes.

CI must never open a network socket to Deepcoin. ``DeepcoinVenue`` requires
an explicit ``Transport``; ``None`` refuses rather than defaulting to
``HttpTransport``. Order mutations still go through
``release_policy.ensure_live_orders_allowed``.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

import pytest
from fakes.fake_deepcoin import FakeDeepcoinTransport

from krellbot.release_policy import PaperOnlyReleaseError
from krellbot.venues.base import WithdrawCapableError
from krellbot.venues.deepcoin import API_HOST, BASE_URL, DeepcoinVenue, HttpTransport, sign_deepcoin

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
                "side": "buy",
                "sz": "0.01",
            }
        ],
        fills=[
            {
                "tradeId": "7",
                "ordId": "1",
                "instId": WAVE_PAIR,
                "side": "buy",
                "fillSz": "0.01",
                "fillPx": "100000",
                "ts": "1700000000000",
            }
        ],
    )
    truth = _venue(transport).snapshot()
    assets = {b.asset: b for b in truth.balances}
    assert assets["USDT"].free == Decimal(1000)
    assert len(truth.open_orders) == 1
    assert truth.open_orders[0].coid == "rest-1"
    assert truth.recent_fills[0].side == "buy"
    for call in transport.calls:
        assert urlparse(call.url).hostname == API_HOST


def test_deepcoin_place_entry_with_stop_uses_signed_posts() -> None:
    transport = FakeDeepcoinTransport()
    venue = _venue(transport)
    ref = venue.place_entry_with_stop("coid-entry", Decimal("0.01"), Decimal(90000), pair=WAVE_PAIR)
    assert ref.pair == WAVE_PAIR
    assert ref.side == "buy"
    assert ref.stop_price == Decimal("90000.0")
    posts = [c for c in transport.calls if c.method == "POST"]
    assert len(posts) == 2
    assert isinstance(posts[0].body, dict)
    assert posts[0].body["ordType"] == "market"
    assert posts[0].body["tgtCcy"] == "base_ccy"
    assert posts[0].body["tdMode"] == "cash"
    assert posts[0].body["clOrdId"] == "coid-entry"
    assert posts[0].headers.get("DC-ACCESS-KEY") == "test-key"
    assert posts[0].headers.get("DC-ACCESS-PASSPHRASE") == "test-pass"
    assert posts[0].headers.get("DC-ACCESS-SIGN")
    assert "DC-ACCESS-KEY" not in posts[0].headers.get("DC-ACCESS-SIGN", "")
    assert isinstance(posts[1].body, dict)
    assert posts[1].body["productGroup"] == "Spot"
    assert posts[1].body["side"] == "sell"
    assert posts[1].body["triggerPrice"] == "90000.0"
    assert posts[1].body["tdMode"] == "cash"
    posts_after = len(posts)
    second = venue.place_entry_with_stop("coid-entry", Decimal("0.01"), Decimal(90000), pair=WAVE_PAIR)
    assert second.id == ref.id
    assert len([c for c in transport.calls if c.method == "POST"]) == posts_after


def test_deepcoin_place_exit_cancel_stops_and_raise_stop() -> None:
    transport = FakeDeepcoinTransport(
        triggers=[
            {
                "ordId": "trig-9",
                "instId": WAVE_PAIR,
                "side": "sell",
                "sz": "0.01",
                "triggerPx": "90000",
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
    assert any(body.get("triggerPrice") == "91000.0" for body in (c.body for c in posts if isinstance(c.body, dict)))
    cancels = [c for c in transport.calls if c.method == "POST" and "/cancel-trigger-order" in c.url]
    assert cancels


def test_deepcoin_check_key_trade_only_and_withdraw_refused() -> None:
    ok = _venue(FakeDeepcoinTransport()).check_key()
    assert ok.can_trade is True
    assert ok.can_withdraw is False
    bad = FakeDeepcoinTransport(
        balance={"code": "0", "data": [{"ccy": "USDT", "availBal": "1", "frozenBal": "0", "perm": "trade,withdraw"}]}
    )
    with pytest.raises(WithdrawCapableError):
        _venue(bad).check_key()


def test_deepcoin_place_entry_blocked_when_paper_only_release(monkeypatch) -> None:
    from krellbot import release_policy

    monkeypatch.setattr(release_policy, "PAPER_ONLY_RELEASE", True)
    transport = FakeDeepcoinTransport()
    with pytest.raises(PaperOnlyReleaseError):
        DeepcoinVenue(
            key_id="k",
            api_secret="s",
            passphrase="p",
            transport=transport,
            now_fn=lambda: 1,
        ).place_entry_with_stop("x", Decimal("0.01"), Decimal(1), pair=WAVE_PAIR)
    assert transport.calls == []


def test_deepcoin_sign_matches_documented_prehash() -> None:
    signature = sign_deepcoin(
        "DEEPCOIN-SYNTHETIC-SECRET",
        "2020-12-08T09:08:57.715Z",
        "POST",
        "/deepcoin/trade/order",
        '{"clOrdId":"coid","instId":"BTC-USDT","ordType":"market","side":"buy","sz":"0.01","tdMode":"cash","tgtCcy":"base_ccy"}',
    )
    assert signature == "TaYOdb5CEgdo5AzdnWdjnpWHRFsiK8m5nMyI1n/Gmd4="
