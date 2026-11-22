"""Pack markets allow-list admits Mexc venue id ``mexc``."""

from __future__ import annotations

from krellbot.pack.lint import check

_MINIMAL = {
    "schema_version": 1,
    "id": "mexc-markets",
    "version": "1.0.0",
    "label": "Mexc markets",
    "author": "krellbot tests",
    "timeframe": "1h",
    "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
    "entry": ["close", ">", "sma2"],
    "exit": ["close", "<", "sma2"],
    "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
}


def test_pack_validate_accepts_mexc_btc_usdt_market() -> None:
    pack = {
        **_MINIMAL,
        "markets": [{"venue": "mexc", "pair": "BTCUSDT"}],
    }
    assert check(pack) == []


def test_pack_validate_rejects_unknown_venue_id_with_typed_field_error() -> None:
    pack = {
        **_MINIMAL,
        "markets": [{"venue": "not_a_venue", "pair": "BTCUSDT"}],
    }
    errors = check(pack)
    assert errors
    assert any((item.get("field") or "").startswith("markets") for item in errors), errors
