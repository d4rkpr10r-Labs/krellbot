"""BTCC wave pair instrument rules: production snapshot write.

The production ``ensure_instrument_rules`` path must persist
``$KRELLBOT_HOME/run/instrument-rules.json`` coverage for the agreed
BTCC BTC/USDT pair (``BTCUSDT``, public symbol ``BTCUSDT``) from the
public market list (fake transport in tests).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.data.instruments import (
    BTCC_MARKET_LIST_URL,
    METADATA_SYMBOL_MISMATCH,
    PAPER_WAVE_BTCC_BTC_USDT,
    SNAPSHOT_FILENAME,
    InstrumentMetadataClient,
    InstrumentMetadataError,
    ensure_instrument_rules,
)

BTCC_BTCUSDT_MARKETS = {
    "code": 0,
    "data": {
        "records": [
            {
                "name": "BTCUSDT",
                "stock": "BTC",
                "money": "USDT",
                "min_amount": "0.0003",
                "min_order_value": "10",
                "stock_prec": 5,
                "money_prec": 2,
                "switch": True,
            }
        ]
    },
}


class _BtccMarketsTransport:
    def __init__(self, *, payload: dict | None = None) -> None:
        self.payload = dict(payload) if payload is not None else dict(BTCC_BTCUSDT_MARKETS)
        self.calls: list[str] = []

    def get(self, url, headers=None):
        self.calls.append(str(url))
        if "/btcc_trade/market/list" in str(url):
            return dict(self.payload)
        return {}


def test_agreed_btcc_wave_pair_spelling() -> None:
    assert PAPER_WAVE_BTCC_BTC_USDT == "BTCUSDT"


def test_fetch_rules_btcc_btcusdt_from_public_market_list() -> None:
    transport = _BtccMarketsTransport()
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    record = client.fetch_rules("btcc", PAPER_WAVE_BTCC_BTC_USDT)
    assert record.venue == "btcc"
    assert record.canonical_pair == "BTCUSDT"
    assert record.venue_symbol == "BTCUSDT"
    assert record.base_asset == "BTC"
    assert record.quote_asset == "USDT"
    assert Decimal(record.min_quantity) == Decimal("0.0003")
    assert Decimal(record.min_notional) == Decimal(10)
    assert Decimal(record.quantity_increment) == Decimal("0.00001")
    assert Decimal(record.price_increment) == Decimal("0.01")
    assert record.source["endpoint"] == BTCC_MARKET_LIST_URL
    assert any("market/list" in url for url in transport.calls)


def test_ensure_instrument_rules_persists_btcc_wave_pair(home: Path) -> None:
    transport = _BtccMarketsTransport()
    path = ensure_instrument_rules(
        home,
        venue="btcc",
        pairs=[PAPER_WAVE_BTCC_BTC_USDT],
        transport=transport,
        clock=lambda: 1_700_000_000.0,
    )
    assert path is not None
    body = json.loads((home / "run" / SNAPSHOT_FILENAME).read_text(encoding="utf-8"))
    stored = body["records"]["btcc"][PAPER_WAVE_BTCC_BTC_USDT]
    assert stored["venue_symbol"] == "BTCUSDT"
    assert stored["quote_asset"] == "USDT"


def test_btcc_market_list_missing_symbol_is_metadata_symbol_mismatch() -> None:
    transport = _BtccMarketsTransport(payload={"code": 0, "data": {"records": []}})
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    with pytest.raises(InstrumentMetadataError) as exc:
        client.fetch_rules("btcc", PAPER_WAVE_BTCC_BTC_USDT)
    assert exc.value.code == METADATA_SYMBOL_MISMATCH
