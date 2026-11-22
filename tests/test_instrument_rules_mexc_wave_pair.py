"""Mexc wave pair instrument rules from public exchangeInfo.

The production ``ensure_instrument_rules`` path must persist coverage for
the agreed Mexc BTC/USDT symbol (``BTCUSDT``) from public exchangeInfo
(fake transport in tests).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.data.instruments import (
    METADATA_SYMBOL_MISMATCH,
    MEXC_EXCHANGE_INFO_URL,
    PAPER_WAVE_MEXC_BTC_USDT,
    InstrumentMetadataClient,
    InstrumentMetadataError,
    ensure_instrument_rules,
)

MEXC_BTC_USDT_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "ENABLED",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "baseAssetPrecision": 6,
            "quoteAssetPrecision": 2,
            "baseSizePrecision": "0.000001",
            "quoteAmountPrecision": "1",
            "isSpotTradingAllowed": True,
        }
    ]
}


class _MexcInstrumentsTransport:
    """Serves public exchangeInfo for the wave BTCUSDT pair."""

    def __init__(self, *, payload: dict | None = None) -> None:
        self.payload = dict(payload) if payload is not None else dict(MEXC_BTC_USDT_INFO)
        self.calls: list[tuple[str, str]] = []

    def get(self, url, headers=None):
        self.calls.append(("GET", str(url)))
        if "/api/v3/exchangeInfo" in str(url):
            return dict(self.payload)
        return {}


def test_agreed_mexc_wave_pair_spelling() -> None:
    """Wave Mexc BTC/USDT spelling is the concatenated spot symbol."""
    assert PAPER_WAVE_MEXC_BTC_USDT == "BTCUSDT"


def test_fetch_rules_mexc_btc_usdt_from_exchange_info() -> None:
    """InstrumentMetadataClient normalizes Mexc exchangeInfo into InstrumentRulesV1."""
    transport = _MexcInstrumentsTransport()
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)

    record = client.fetch_rules("mexc", PAPER_WAVE_MEXC_BTC_USDT)

    assert record.venue == "mexc"
    assert record.canonical_pair == PAPER_WAVE_MEXC_BTC_USDT
    assert record.venue_symbol == PAPER_WAVE_MEXC_BTC_USDT
    assert record.base_asset == "BTC"
    assert record.quote_asset == "USDT"
    assert Decimal(record.min_quantity) == Decimal("0.000001")
    assert Decimal(record.min_notional) == Decimal(1)
    assert Decimal(record.quantity_increment) == Decimal("0.000001")
    assert Decimal(record.price_increment) == Decimal("0.01")
    assert record.source["endpoint"] == MEXC_EXCHANGE_INFO_URL
    assert transport.calls
    assert "symbol=BTCUSDT" in transport.calls[0][1]


def test_ensure_instrument_rules_persists_mexc_wave_pair(home: Path) -> None:
    transport = _MexcInstrumentsTransport()
    path = ensure_instrument_rules(
        home,
        venue="mexc",
        pairs=[PAPER_WAVE_MEXC_BTC_USDT],
        transport=transport,
        clock=lambda: 1_700_000_000.0,
    )
    assert path is not None
    body = json.loads(path.read_text(encoding="utf-8"))
    stored = body["records"]["mexc"][PAPER_WAVE_MEXC_BTC_USDT]
    assert stored["venue_symbol"] == "BTCUSDT"
    assert stored["quote_asset"] == "USDT"


def test_mexc_exchange_info_missing_symbol_is_metadata_symbol_mismatch() -> None:
    transport = _MexcInstrumentsTransport(payload={"symbols": []})
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    with pytest.raises(InstrumentMetadataError) as raised:
        client.fetch_rules("mexc", PAPER_WAVE_MEXC_BTC_USDT)
    assert raised.value.code == METADATA_SYMBOL_MISMATCH
