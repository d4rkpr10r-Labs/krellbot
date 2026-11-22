"""LBank wave pair instrument rules: production snapshot write.

The production ``ensure_instrument_rules`` path must persist
``$KRELLBOT_HOME/run/instrument-rules.json`` coverage for the agreed
LBank BTC/USDT pair (``BTC-USDT``, public symbol ``btc_usdt``) from public
accuracy (fake transport in tests).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.data.instruments import (
    LBANK_ACCURACY_URL,
    METADATA_UNAVAILABLE,
    PAPER_WAVE_LBANK_BTC_USDT,
    SNAPSHOT_FILENAME,
    InstrumentMetadataClient,
    InstrumentMetadataError,
    ensure_instrument_rules,
)

LBANK_BTC_USDT_ACCURACY = {
    "result": "true",
    "error_code": 0,
    "data": [
        {
            "symbol": "btc_usdt",
            "quantityAccuracy": "5",
            "priceAccuracy": "2",
            "minTranQua": "0.00001",
            "minOrderAmount": "1",
        }
    ],
}


class _LbankAccuracyTransport:
    """Serves public accuracy for the wave btc_usdt symbol."""

    def __init__(self, *, payload: dict | None = None) -> None:
        self.payload = dict(payload) if payload is not None else dict(LBANK_BTC_USDT_ACCURACY)
        self.calls: list[tuple[str, str]] = []

    def get(self, url, headers=None):
        self.calls.append(("GET", str(url)))
        if "/v2/accuracy.do" in str(url):
            return dict(self.payload)
        return {}


def test_agreed_lbank_wave_pair_spelling() -> None:
    """Wave LBank BTC/USDT spelling is the hyphenated pack pair."""
    assert PAPER_WAVE_LBANK_BTC_USDT == "BTC-USDT"


def test_fetch_rules_lbank_btc_usdt_from_accuracy() -> None:
    """InstrumentMetadataClient normalizes LBank accuracy into InstrumentRulesV1."""
    transport = _LbankAccuracyTransport()
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)

    record = client.fetch_rules("lbank", PAPER_WAVE_LBANK_BTC_USDT)

    assert record.venue == "lbank"
    assert record.canonical_pair == PAPER_WAVE_LBANK_BTC_USDT
    assert record.venue_symbol == "btc_usdt"
    assert record.base_asset == "BTC"
    assert record.quote_asset == "USDT"
    assert Decimal(record.min_quantity) == Decimal("0.00001")
    assert Decimal(record.min_notional) == Decimal(1)
    assert Decimal(record.quantity_increment) == Decimal("0.00001")
    assert Decimal(record.price_increment) == Decimal("0.01")
    assert record.source["endpoint"] == LBANK_ACCURACY_URL
    assert any("accuracy.do" in url and "btc_usdt" in url for _, url in transport.calls)


def test_ensure_instrument_rules_persists_lbank_btc_usdt(home: Path) -> None:
    """Production ensure path writes run/instrument-rules.json for BTC-USDT."""
    assert not (home / "run" / SNAPSHOT_FILENAME).exists()
    transport = _LbankAccuracyTransport()
    path = ensure_instrument_rules(
        home,
        venue="lbank",
        pairs=[PAPER_WAVE_LBANK_BTC_USDT],
        transport=transport,
        clock=lambda: 1_700_000_000.0,
    )
    assert path is not None
    body = json.loads(path.read_text(encoding="utf-8"))
    wave = (body.get("records") or {}).get("lbank", {})[PAPER_WAVE_LBANK_BTC_USDT]
    assert wave["canonical_pair"] == PAPER_WAVE_LBANK_BTC_USDT
    assert wave["venue_symbol"] == "btc_usdt"
    assert wave["quote_asset"] == "USDT"


def test_fetch_rules_lbank_error_code_is_unavailable() -> None:
    transport = _LbankAccuracyTransport(payload={"result": "false", "error_code": 10008, "msg": "invalid symbol"})
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    with pytest.raises(InstrumentMetadataError) as raised:
        client.fetch_rules("lbank", PAPER_WAVE_LBANK_BTC_USDT)
    assert raised.value.code == METADATA_UNAVAILABLE
