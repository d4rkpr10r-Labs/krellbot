"""Bitget wave pair instrument rules: production snapshot write.

The production ``ensure_instrument_rules`` path must persist
``$KRELLBOT_HOME/run/instrument-rules.json`` coverage for the agreed
Bitget BTC/USDT pair (``BTCUSDT``) from the public symbols endpoint
(fake transport in tests).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.data.instruments import (
    BITGET_SYMBOLS_URL,
    METADATA_SYMBOL_MISMATCH,
    PAPER_WAVE_BITGET_BTC_USDT,
    SNAPSHOT_FILENAME,
    InstrumentMetadataClient,
    InstrumentMetadataError,
    ensure_instrument_rules,
)

BITGET_BTC_USDT_SYMBOLS = {
    "code": "00000",
    "msg": "success",
    "data": [
        {
            "symbol": "BTCUSDT",
            "baseCoin": "BTC",
            "quoteCoin": "USDT",
            "minTradeAmount": "0",
            "pricePrecision": "2",
            "quantityPrecision": "6",
            "status": "online",
            "minTradeUSDT": "1",
        }
    ],
}


class _BitgetSymbolsTransport:
    def __init__(self, *, payload: dict | None = None) -> None:
        self.payload = dict(payload) if payload is not None else dict(BITGET_BTC_USDT_SYMBOLS)
        self.calls: list[str] = []

    def get(self, url, headers=None):
        self.calls.append(str(url))
        if "/api/v2/spot/public/symbols" in str(url):
            return dict(self.payload)
        return {}


def test_agreed_bitget_wave_pair_spelling() -> None:
    assert PAPER_WAVE_BITGET_BTC_USDT == "BTCUSDT"


def test_fetch_rules_bitget_btcusdt_from_public_symbols() -> None:
    transport = _BitgetSymbolsTransport()
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    record = client.fetch_rules("bitget", PAPER_WAVE_BITGET_BTC_USDT)
    assert record.venue == "bitget"
    assert record.canonical_pair == "BTCUSDT"
    assert record.venue_symbol == "BTCUSDT"
    assert record.base_asset == "BTC"
    assert record.quote_asset == "USDT"
    assert Decimal(record.min_quantity) == Decimal("0.000001")
    assert Decimal(record.min_notional) == Decimal(1)
    assert Decimal(record.quantity_increment) == Decimal("0.000001")
    assert Decimal(record.price_increment) == Decimal("0.01")
    assert record.source["endpoint"] == BITGET_SYMBOLS_URL
    assert any("BTCUSDT" in url for url in transport.calls)


def test_ensure_instrument_rules_persists_bitget_wave_pair(home: Path) -> None:
    transport = _BitgetSymbolsTransport()
    ensure_instrument_rules(
        home,
        venue="bitget",
        pairs=[PAPER_WAVE_BITGET_BTC_USDT],
        transport=transport,
        clock=lambda: 1_700_000_000.0,
    )
    body = json.loads((home / "run" / SNAPSHOT_FILENAME).read_text(encoding="utf-8"))
    stored = body["records"]["bitget"][PAPER_WAVE_BITGET_BTC_USDT]
    assert stored["venue_symbol"] == "BTCUSDT"
    assert stored["quote_asset"] == "USDT"


def test_bitget_symbols_missing_symbol_is_metadata_symbol_mismatch() -> None:
    transport = _BitgetSymbolsTransport(payload={"code": "00000", "data": []})
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    with pytest.raises(InstrumentMetadataError) as exc:
        client.fetch_rules("bitget", PAPER_WAVE_BITGET_BTC_USDT)
    assert exc.value.code == METADATA_SYMBOL_MISMATCH
