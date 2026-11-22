"""Pack markets allow-list admits BTCC venue id ``btcc``.

Pack validate accepts ``{venue: btcc, pair: BTCUSDT}`` within the existing
market-count limits and still rejects an unknown venue id. BTCC is not an
OctoBot tentacle and has no second id.
"""

from __future__ import annotations

from krellbot.pack.lint import check

_PACK = {
    "schema_version": 1,
    "id": "btcc-markets",
    "version": "1.0.0",
    "label": "BTCC markets",
    "author": "krellbot tests",
    "timeframe": "1h",
    "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
    "entry": ["close", ">", "sma2"],
    "exit": ["close", "<", "sma2"],
    "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
}


def test_pack_validate_accepts_btcc_btcusdt_market() -> None:
    pack = {**_PACK, "markets": [{"venue": "btcc", "pair": "BTCUSDT"}]}
    assert check(pack) == []


def test_pack_validate_rejects_unknown_venue_beside_btcc() -> None:
    pack = {**_PACK, "markets": [{"venue": "not_a_venue", "pair": "BTCUSDT"}]}
    errors = check(pack)
    assert errors
