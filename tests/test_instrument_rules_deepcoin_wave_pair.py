"""Deepcoin wave pair instrument rules: production snapshot write.

The production ``ensure_instrument_rules`` path must persist
``$KRELLBOT_HOME/run/instrument-rules.json`` coverage for the agreed
Deepcoin BTC/USDT instId (``BTC-USDT``) from public instruments (fake
transport in tests).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.data.instruments import (
    DEEPCOIN_INSTRUMENTS_URL,
    DEEPCOIN_PAPER_MIN_NOTIONAL,
    METADATA_UNAVAILABLE,
    PAPER_WAVE_DEEPCOIN_BTC_USDT,
    SNAPSHOT_FILENAME,
    InstrumentMetadataClient,
    InstrumentMetadataError,
    ensure_instrument_rules,
)

DEEPCOIN_BTC_USDT_INSTRUMENTS = {
    "code": "0",
    "data": [
        {
            "instType": "SPOT",
            "instId": "BTC-USDT",
            "baseCcy": "BTC",
            "quoteCcy": "USDT",
            "lotSz": "0.00000001",
            "minSz": "0.0004",
            "tickSz": "0.1",
            "state": "live",
        }
    ],
}


class _DeepcoinInstrumentsTransport:
    """Serves public instruments for the wave BTC-USDT pair."""

    def __init__(self, *, payload: dict | None = None) -> None:
        self.payload = dict(payload) if payload is not None else dict(DEEPCOIN_BTC_USDT_INSTRUMENTS)
        self.calls: list[tuple[str, str]] = []

    def get(self, url, headers=None):
        self.calls.append(("GET", str(url)))
        if "/deepcoin/market/instruments" in str(url):
            return dict(self.payload)
        return {}


def test_agreed_deepcoin_wave_pair_spelling() -> None:
    """Wave Deepcoin BTC/USDT spelling is the spot instId."""
    assert PAPER_WAVE_DEEPCOIN_BTC_USDT == "BTC-USDT"


def test_fetch_rules_deepcoin_btc_usdt_from_instruments() -> None:
    """InstrumentMetadataClient normalizes Deepcoin instruments into InstrumentRulesV1."""
    transport = _DeepcoinInstrumentsTransport()
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)

    record = client.fetch_rules("deepcoin", PAPER_WAVE_DEEPCOIN_BTC_USDT)

    assert record.venue == "deepcoin"
    assert record.canonical_pair == PAPER_WAVE_DEEPCOIN_BTC_USDT
    assert record.venue_symbol == PAPER_WAVE_DEEPCOIN_BTC_USDT
    assert record.base_asset == "BTC"
    assert record.quote_asset == "USDT"
    assert Decimal(record.min_quantity) == Decimal("0.0004")
    assert record.min_notional == DEEPCOIN_PAPER_MIN_NOTIONAL
    assert Decimal(record.quantity_increment) == Decimal("0.00000001")
    assert Decimal(record.price_increment) == Decimal("0.1")
    assert record.source["endpoint"] == DEEPCOIN_INSTRUMENTS_URL
    assert any("instruments" in url and "BTC-USDT" in url for _, url in transport.calls)


def test_ensure_instrument_rules_persists_deepcoin_btc_usdt(home: Path) -> None:
    """Production ensure path writes run/instrument-rules.json for BTC-USDT."""
    assert not (home / "run" / SNAPSHOT_FILENAME).exists()
    transport = _DeepcoinInstrumentsTransport()
    path = ensure_instrument_rules(
        home,
        venue="deepcoin",
        pairs=[PAPER_WAVE_DEEPCOIN_BTC_USDT],
        transport=transport,
        clock=lambda: 1_700_000_000.0,
    )
    assert path is not None
    body = json.loads(path.read_text(encoding="utf-8"))
    wave = (body.get("records") or {}).get("deepcoin", {})[PAPER_WAVE_DEEPCOIN_BTC_USDT]
    assert wave["canonical_pair"] == PAPER_WAVE_DEEPCOIN_BTC_USDT
    assert wave["quote_asset"] == "USDT"
    assert Decimal(wave["min_notional"]) == Decimal(DEEPCOIN_PAPER_MIN_NOTIONAL)


def test_fetch_rules_deepcoin_error_code_is_unavailable() -> None:
    transport = _DeepcoinInstrumentsTransport(payload={"code": "51001", "msg": "Instrument ID does not exist."})
    client = InstrumentMetadataClient(transport=transport, clock=lambda: 1_700_000_000.0)
    with pytest.raises(InstrumentMetadataError) as raised:
        client.fetch_rules("deepcoin", PAPER_WAVE_DEEPCOIN_BTC_USDT)
    assert raised.value.code == METADATA_UNAVAILABLE
