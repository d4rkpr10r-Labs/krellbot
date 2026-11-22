"""XT wave pair instrument rules from the public symbol endpoint.

The production ``ensure_instrument_rules`` path must persist coverage for
the agreed XT BTC/USDT pack pair (``BTCUSDT``). The public symbol is
``btc_usdt``. Tests use a fake transport.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.data.instruments import (
    METADATA_SYMBOL_MISMATCH,
    PAPER_WAVE_XT_BTC_USDT,
    XT_SYMBOLS_URL,
    InstrumentMetadataClient,
    InstrumentMetadataError,
    ensure_instrument_rules,
)

XT_BTC_USDT_INFO = {
    "rc": 0,
    "mc": "SUCCESS",
    "result": {
        "symbols": [
            {
                "symbol": "btc_usdt",
                "state": "ONLINE",
                "tradingEnabled": True,
                "baseCurrency": "btc",
                "quoteCurrency": "usdt",
                "pricePrecision": 2,
                "quantityPrecision": 5,
                "filters": [{"filter": "QUOTE_QTY", "min": "1"}],
            }
        ]
    },
}


class _XtInstrumentsTransport:
    """Serves the public symbol payload for the wave BTCUSDT pair."""

    def __init__(self, *, payload: dict | None = None) -> None:
        self.payload = dict(payload) if payload is not None else dict(XT_BTC_USDT_INFO)
        self.calls: list[tuple[str, str]] = []

    def get(self, url, headers=None):
        self.calls.append(("GET", str(url)))
        if "/v4/public/symbol" in str(url):
            return dict(self.payload)
        return {}


def test_agreed_xt_wave_pair_spelling() -> None:
    """Wave XT pack pair is BTCUSDT. The public symbol is btc_usdt."""
    assert PAPER_WAVE_XT_BTC_USDT == "BTCUSDT"


def test_fetch_rules_xt_btc_usdt_from_public_symbol() -> None:
    """InstrumentMetadataClient normalizes the public symbol into InstrumentRulesV1."""
    transport = _XtInstrumentsTransport()
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)

    record = client.fetch_rules("xt", PAPER_WAVE_XT_BTC_USDT)

    assert record.venue == "xt"
    assert record.canonical_pair == PAPER_WAVE_XT_BTC_USDT
    assert record.venue_symbol == "btc_usdt"
    assert record.base_asset == "BTC"
    assert record.quote_asset == "USDT"
    assert Decimal(record.min_quantity) == Decimal("0.00001")
    assert Decimal(record.min_notional) == Decimal(1)
    assert Decimal(record.quantity_increment) == Decimal("0.00001")
    assert Decimal(record.price_increment) == Decimal("0.01")
    assert record.source["endpoint"] == XT_SYMBOLS_URL
    assert transport.calls
    assert "symbol=btc_usdt" in transport.calls[0][1]


def test_ensure_instrument_rules_persists_xt_wave_pair(home: Path) -> None:
    transport = _XtInstrumentsTransport()
    path = ensure_instrument_rules(
        home,
        venue="xt",
        pairs=[PAPER_WAVE_XT_BTC_USDT],
        transport=transport,
        clock=lambda: 1_700_000_000.0,
    )
    assert path is not None
    body = json.loads(path.read_text(encoding="utf-8"))
    stored = body["records"]["xt"][PAPER_WAVE_XT_BTC_USDT]
    assert stored["venue_symbol"] == "btc_usdt"
    assert stored["quote_asset"] == "USDT"


def test_xt_public_symbol_missing_symbol_is_metadata_symbol_mismatch() -> None:
    transport = _XtInstrumentsTransport(payload={"rc": 0, "mc": "SUCCESS", "result": {"symbols": []}})
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    with pytest.raises(InstrumentMetadataError) as raised:
        client.fetch_rules("xt", PAPER_WAVE_XT_BTC_USDT)
    assert raised.value.code == METADATA_SYMBOL_MISMATCH
