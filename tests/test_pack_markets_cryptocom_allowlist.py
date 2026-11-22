"""Pack markets allow-list admits Crypto.com venue id ``cryptocom``.

Pack validate accepts ``{venue: cryptocom, pair: BTC-USDT}`` within the
existing market-count limits and still rejects an unknown venue id.
"""

from __future__ import annotations

from krellbot.pack.lint import check

_PACK = {
    "schema_version": 1,
    "id": "cryptocom-markets",
    "version": "1.0.0",
    "label": "Crypto.com markets",
    "author": "krellbot tests",
    "timeframe": "1h",
    "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
    "entry": ["close", ">", "sma2"],
    "exit": ["close", "<", "sma2"],
    "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
}


def test_pack_validate_accepts_cryptocom_btc_usdt_market() -> None:
    pack = {**_PACK, "markets": [{"venue": "cryptocom", "pair": "BTC-USDT"}]}
    assert check(pack) == []


def test_pack_validate_rejects_unknown_venue_beside_cryptocom() -> None:
    pack = {**_PACK, "markets": [{"venue": "not_a_venue", "pair": "BTC-USDT"}]}
    errors = check(pack)
    assert errors
