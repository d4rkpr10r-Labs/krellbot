"""HTX wave pair instrument rules: production snapshot write.

The production ``ensure_instrument_rules`` path must persist
``$KRELLBOT_HOME/run/instrument-rules.json`` coverage for the agreed
HTX BTC/USDT pair (``BTCUSDT``, public symbol ``btcusdt``) from the
public market-symbols endpoint (fake transport in tests).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.data.instruments import (
    HTX_MARKET_SYMBOLS_URL,
    METADATA_SYMBOL_MISMATCH,
    PAPER_WAVE_HTX_BTC_USDT,
    SNAPSHOT_FILENAME,
    InstrumentMetadataClient,
    InstrumentMetadataError,
    ensure_instrument_rules,
)

HTX_BTCUSDT_SYMBOLS = {
    "status": "ok",
    "data": [
        {
            "symbol": "btcusdt",
            "bc": "btc",
            "qc": "usdt",
            "ap": 6,
            "pp": 2,
            "minoa": "0.00001",
            "minov": "1",
            "state": "online",
        }
    ],
}


class _HtxSymbolsTransport:
    def __init__(self, *, payload: dict | None = None) -> None:
        self.payload = dict(payload) if payload is not None else dict(HTX_BTCUSDT_SYMBOLS)
        self.calls: list[str] = []

    def get(self, url, headers=None):
        self.calls.append(str(url))
        if "/v1/settings/common/market-symbols" in str(url):
            return dict(self.payload)
        return {}


def test_agreed_htx_wave_pair_spelling() -> None:
    assert PAPER_WAVE_HTX_BTC_USDT == "BTCUSDT"


def test_fetch_rules_htx_btcusdt_from_public_market_symbols() -> None:
    transport = _HtxSymbolsTransport()
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    record = client.fetch_rules("htx", PAPER_WAVE_HTX_BTC_USDT)
    assert record.venue == "htx"
    assert record.canonical_pair == "BTCUSDT"
    assert record.venue_symbol == "btcusdt"
    assert record.base_asset == "BTC"
    assert record.quote_asset == "USDT"
    assert Decimal(record.min_quantity) == Decimal("0.00001")
    assert Decimal(record.min_notional) == Decimal(1)
    assert Decimal(record.quantity_increment) == Decimal("0.000001")
    assert Decimal(record.price_increment) == Decimal("0.01")
    assert record.source["endpoint"] == HTX_MARKET_SYMBOLS_URL
    assert any("btcusdt" in url for url in transport.calls)


def test_ensure_instrument_rules_persists_htx_wave_pair(home: Path) -> None:
    transport = _HtxSymbolsTransport()
    path = ensure_instrument_rules(
        home,
        venue="htx",
        pairs=[PAPER_WAVE_HTX_BTC_USDT],
        transport=transport,
        clock=lambda: 1_700_000_000.0,
    )
    assert path is not None
    body = json.loads((home / "run" / SNAPSHOT_FILENAME).read_text(encoding="utf-8"))
    stored = body["records"]["htx"][PAPER_WAVE_HTX_BTC_USDT]
    assert stored["venue_symbol"] == "btcusdt"
    assert stored["quote_asset"] == "USDT"


def test_htx_market_symbols_missing_symbol_is_metadata_symbol_mismatch() -> None:
    transport = _HtxSymbolsTransport(payload={"status": "ok", "data": []})
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    with pytest.raises(InstrumentMetadataError) as exc:
        client.fetch_rules("htx", PAPER_WAVE_HTX_BTC_USDT)
    assert exc.value.code == METADATA_SYMBOL_MISMATCH
