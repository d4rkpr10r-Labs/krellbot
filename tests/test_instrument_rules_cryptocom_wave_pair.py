"""Crypto.com wave pair instrument rules: production snapshot write.

The production ``ensure_instrument_rules`` path must persist
``$KRELLBOT_HOME/run/instrument-rules.json`` coverage for the agreed
Crypto.com BTC/USDT pair (``BTC-USDT``, public symbol ``BTC_USDT``)
from the public instrument list and risk parameters (fake transport).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.data.instruments import (
    CRYPTOCOM_INSTRUMENTS_URL,
    METADATA_SYMBOL_MISMATCH,
    PAPER_WAVE_CRYPTOCOM_BTC_USDT,
    SNAPSHOT_FILENAME,
    InstrumentMetadataClient,
    InstrumentMetadataError,
    ensure_instrument_rules,
)

CRYPTOCOM_BTC_USDT = {
    "code": 0,
    "result": {
        "data": [
            {
                "symbol": "BTC_USDT",
                "inst_type": "CCY_PAIR",
                "base_ccy": "BTC",
                "quote_ccy": "USDT",
                "quote_decimals": 2,
                "quantity_decimals": 5,
                "price_tick_size": "0.01",
                "qty_tick_size": "0.00001",
                "tradable": True,
            }
        ]
    },
}

CRYPTOCOM_RISK = {
    "code": 0,
    "result": {"base_currency_config": [{"instrument_name": "BTC", "min_order_notional_usd": "1.0"}]},
}


class _CryptocomTransport:
    def __init__(self, *, instruments: dict | None = None) -> None:
        self.instruments = dict(instruments) if instruments is not None else dict(CRYPTOCOM_BTC_USDT)
        self.calls: list[str] = []

    def get(self, url, headers=None):
        self.calls.append(str(url))
        if "get-risk-parameters" in str(url):
            return dict(CRYPTOCOM_RISK)
        if "get-instruments" in str(url):
            return dict(self.instruments)
        return {}


def test_agreed_cryptocom_wave_pair_spelling() -> None:
    assert PAPER_WAVE_CRYPTOCOM_BTC_USDT == "BTC-USDT"


def test_fetch_rules_cryptocom_btc_usdt_from_public_instruments() -> None:
    transport = _CryptocomTransport()
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    record = client.fetch_rules("cryptocom", PAPER_WAVE_CRYPTOCOM_BTC_USDT)
    assert record.venue == "cryptocom"
    assert record.canonical_pair == "BTC-USDT"
    assert record.venue_symbol == "BTC_USDT"
    assert record.base_asset == "BTC"
    assert record.quote_asset == "USDT"
    assert Decimal(record.min_quantity) == Decimal("0.00001")
    assert Decimal(record.min_notional) == Decimal(1)
    assert Decimal(record.quantity_increment) == Decimal("0.00001")
    assert Decimal(record.price_increment) == Decimal("0.01")
    assert record.source["endpoint"] == CRYPTOCOM_INSTRUMENTS_URL
    assert any("get-instruments" in url for url in transport.calls)
    assert any("get-risk-parameters" in url for url in transport.calls)


def test_ensure_instrument_rules_persists_cryptocom_wave_pair(home: Path) -> None:
    transport = _CryptocomTransport()
    path = ensure_instrument_rules(
        home,
        venue="cryptocom",
        pairs=[PAPER_WAVE_CRYPTOCOM_BTC_USDT],
        transport=transport,
        clock=lambda: 1_700_000_000.0,
    )
    assert path is not None
    body = json.loads((home / "run" / SNAPSHOT_FILENAME).read_text(encoding="utf-8"))
    stored = body["records"]["cryptocom"][PAPER_WAVE_CRYPTOCOM_BTC_USDT]
    assert stored["venue_symbol"] == "BTC_USDT"
    assert stored["quote_asset"] == "USDT"


def test_cryptocom_instruments_missing_symbol_is_metadata_symbol_mismatch() -> None:
    transport = _CryptocomTransport(instruments={"code": 0, "result": {"data": []}})
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    with pytest.raises(InstrumentMetadataError) as exc:
        client.fetch_rules("cryptocom", PAPER_WAVE_CRYPTOCOM_BTC_USDT)
    assert exc.value.code == METADATA_SYMBOL_MISMATCH
