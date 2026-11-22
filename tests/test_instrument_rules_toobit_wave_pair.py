"""Toobit wave pair instrument rules from public exchangeInfo.

The production ``ensure_instrument_rules`` path must persist coverage for
the agreed Toobit BTC/USDT symbol (``BTCUSDT``) from public exchangeInfo
(fake transport in tests).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.data.instruments import (
    METADATA_SYMBOL_MISMATCH,
    PAPER_WAVE_TOOBIT_BTC_USDT,
    TOOBIT_EXCHANGE_INFO_URL,
    InstrumentMetadataClient,
    InstrumentMetadataError,
    ensure_instrument_rules,
)

TOOBIT_BTC_USDT_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "minQty": "0.000001", "stepSize": "0.000001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "1"},
            ],
        }
    ]
}


class _ToobitInstrumentsTransport:
    """Serves public exchangeInfo for the wave BTCUSDT pair."""

    def __init__(self, *, payload: dict | None = None) -> None:
        self.payload = dict(payload) if payload is not None else dict(TOOBIT_BTC_USDT_INFO)
        self.calls: list[tuple[str, str]] = []

    def get(self, url, headers=None):
        self.calls.append(("GET", str(url)))
        if "/api/v1/exchangeInfo" in str(url):
            return dict(self.payload)
        return {}


def test_agreed_toobit_wave_pair_spelling() -> None:
    """Wave Toobit BTC/USDT spelling is the concatenated spot symbol."""
    assert PAPER_WAVE_TOOBIT_BTC_USDT == "BTCUSDT"


def test_fetch_rules_toobit_btc_usdt_from_exchange_info() -> None:
    """InstrumentMetadataClient normalizes Toobit exchangeInfo into InstrumentRulesV1."""
    transport = _ToobitInstrumentsTransport()
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)

    record = client.fetch_rules("toobit", PAPER_WAVE_TOOBIT_BTC_USDT)

    assert record.venue == "toobit"
    assert record.canonical_pair == PAPER_WAVE_TOOBIT_BTC_USDT
    assert record.venue_symbol == PAPER_WAVE_TOOBIT_BTC_USDT
    assert record.base_asset == "BTC"
    assert record.quote_asset == "USDT"
    assert Decimal(record.min_quantity) == Decimal("0.000001")
    assert Decimal(record.min_notional) == Decimal(1)
    assert Decimal(record.quantity_increment) == Decimal("0.000001")
    assert Decimal(record.price_increment) == Decimal("0.01")
    assert record.source["endpoint"] == TOOBIT_EXCHANGE_INFO_URL
    assert transport.calls
    assert "exchangeInfo" in transport.calls[0][1]


def test_ensure_instrument_rules_persists_toobit_wave_pair(home: Path) -> None:
    transport = _ToobitInstrumentsTransport()
    path = ensure_instrument_rules(
        home,
        venue="toobit",
        pairs=[PAPER_WAVE_TOOBIT_BTC_USDT],
        transport=transport,
        clock=lambda: 1_700_000_000.0,
    )
    assert path is not None
    body = json.loads(path.read_text(encoding="utf-8"))
    stored = body["records"]["toobit"][PAPER_WAVE_TOOBIT_BTC_USDT]
    assert stored["venue_symbol"] == "BTCUSDT"
    assert stored["quote_asset"] == "USDT"


def test_toobit_exchange_info_missing_symbol_is_metadata_symbol_mismatch() -> None:
    transport = _ToobitInstrumentsTransport(payload={"symbols": []})
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    with pytest.raises(InstrumentMetadataError) as raised:
        client.fetch_rules("toobit", PAPER_WAVE_TOOBIT_BTC_USDT)
    assert raised.value.code == METADATA_SYMBOL_MISMATCH
