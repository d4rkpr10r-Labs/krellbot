"""ETH/USD paper evidence using documented public response shapes and fake transport."""

import json
from decimal import ROUND_DOWN, Decimal

import pytest

from krellbot.data.coinbase_public import parse_coinbase_candles
from krellbot.data.instruments import InstrumentMetadataClient, InstrumentRulesSnapshot, pairrules_from_instrument
from krellbot.data.kraken_public import parse_kraken_ohlc
from krellbot.pack.model import Candle
from krellbot.venues.paper import PaperVenue
from tests.test_paper_venue_acceptance import _arm, _read_position_and_run, _validate_pack

pytest_plugins = ("tests.test_paper_venue_acceptance",)

# Synthetic values in the Kraken AssetPairs and Coinbase public product response formats.
# Sources: https://docs.kraken.com/api/docs/rest-api/get-tradable-asset-pairs/
# and https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product
KRAKEN_PRODUCT = {
    "error": [],
    "result": {
        "XETHZUSD": {
            "altname": "ETHUSD",
            "wsname": "ETH/USD",
            "base": "XETH",
            "quote": "ZUSD",
            "ordermin": "0.0001",
            "costmin": "1",
            "lot_decimals": 6,
            "pair_decimals": 2,
        }
    },
}
COINBASE_PRODUCT = {
    "product_id": "ETH-USD",
    "base_currency_id": "ETH",
    "quote_currency_id": "USD",
    "base_min_size": "0.0001",
    "quote_min_size": "1",
    "base_increment": "0.000001",
    "quote_increment": "0.01",
}


@pytest.mark.parametrize(
    "venue,pair,payload,fee_bps",
    [
        ("kraken", "ETHUSD", KRAKEN_PRODUCT, 40),
        ("coinbase", "ETH-USD", COINBASE_PRODUCT, 120),
    ],
)
def test_eth_usd_public_rules_and_paper_fill(home, workstation, offline_tick, venue, pair, payload, fee_bps):
    class PublicTransport:
        def get(self, url, headers=None):
            assert "/private/" not in url
            return payload

        def post(self, *args, **kwargs):
            raise AssertionError("Paper metadata must be read-only")

    rules = InstrumentMetadataClient(PublicTransport()).fetch_rules(venue, pair)
    assert rules.canonical_pair == pair
    assert Decimal(rules.min_quantity) == Decimal("0.0001")
    assert Decimal(rules.min_notional) == Decimal(1)
    assert Decimal(rules.quantity_increment) == Decimal("0.000001")
    assert Decimal(rules.price_increment) == Decimal("0.01")

    candle = Candle(0, Decimal(13), Decimal(14), Decimal(12), Decimal(13), Decimal(100))
    venue_obj = PaperVenue(
        venue,
        rules_provider=lambda _: pairrules_from_instrument(rules),
        candle_reader=lambda *_: [candle],
        home=home / "minima",
        starting_cash=Decimal(1000),
    )
    before = (home / "minima" / "run" / f"paper-{venue}.json").read_bytes()
    for quantity in (Decimal("0.000001"), Decimal("0.001")):
        with pytest.raises(ValueError, match="below"):
            venue_obj.place_entry_with_stop("too-small", quantity, Decimal(10), pair=pair)
        assert (home / "minima" / "run" / f"paper-{venue}.json").read_bytes() == before

    # The pinned candles mirror the documented public OHLC / candle shapes.
    if venue == "kraken":
        rows = [
            [0, "11", "12", "10", "11", "11", "100", 1],
            [3600, "10", "11", "9", "10", "10", "100", 1],
            [7200, "13", "14", "12", "13", "13", "100", 1],
        ]
        candles = parse_kraken_ohlc({"error": [], "result": {"XETHZUSD": rows, "last": 7200}}, pair=pair)
    else:
        rows = [
            {"start": str(ts), "open": price, "high": high, "low": low, "close": price, "volume": "100"}
            for ts, price, high, low in [(0, "11", "12", "10"), (3600, "10", "11", "9"), (7200, "13", "14", "12")]
        ]
        candles = parse_coinbase_candles({"candles": rows})
    assert [c.close for c in candles] == [Decimal(11), Decimal(10), Decimal(13)]

    revision = _validate_pack(workstation, venue, pair)
    _arm(workstation, venue, pair, revision)
    InstrumentRulesSnapshot(home).write({venue: {pair: rules}})
    assert offline_tick(venue) == 0
    position, run = _read_position_and_run(workstation)
    state = json.loads((home / "run" / f"paper-{venue}.json").read_text())
    [fill] = state["recent_fills"]
    assert fill["pair"] == pair and fill["side"] == "buy"
    assert position["holdings"] == [{"symbol": "ETH", "qty": fill["qty"]}]
    assert run["recent_fills"][0]["side"] == "buy"
    notional = Decimal(fill["qty"]) * Decimal(fill["price"])
    fee = (notional * fee_bps / 10000).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    assert Decimal(position["free_cash"]) == Decimal(1000) - notional - fee


@pytest.mark.parametrize(
    "venue,pair,payload",
    [
        ("kraken", "ETHUSD", KRAKEN_PRODUCT),
        ("coinbase", "ETH-USD", COINBASE_PRODUCT),
    ],
)
@pytest.mark.parametrize("invalid", ["stale", "mismatch", "missing"])
def test_eth_usd_refuses_bad_rules_without_fills(home, workstation, offline_tick, venue, pair, payload, invalid):
    from dataclasses import replace

    from krellbot.data.instruments import METADATA_STALE, METADATA_SYMBOL_MISMATCH, METADATA_UNAVAILABLE

    class PublicTransport:
        def get(self, url, headers=None):
            return payload

    rules = InstrumentMetadataClient(PublicTransport()).fetch_rules(venue, pair)
    if invalid == "stale":
        rules = replace(rules, usable_until="2020-01-01T00:00:00Z")
    elif invalid == "mismatch":
        rules = replace(rules, canonical_pair="OTHER")
    _arm(workstation, venue, pair, _validate_pack(workstation, venue, pair))
    if invalid != "missing":
        InstrumentRulesSnapshot(home).write({venue: {pair: rules}})
    assert offline_tick(venue) == 0
    position, run = _read_position_and_run(workstation)
    assert Decimal(position["free_cash"]) == 1000
    assert position["holdings"] == [] and run["recent_fills"] == []
    assert (
        run["last_refusal_code"]
        == {"stale": METADATA_STALE, "mismatch": METADATA_SYMBOL_MISMATCH, "missing": METADATA_UNAVAILABLE}[invalid]
    )
