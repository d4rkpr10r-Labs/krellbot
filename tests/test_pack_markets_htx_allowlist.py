"""Pack markets allow-list admits HTX venue id ``htx``.

Pack validate accepts ``{venue: htx, pair: BTCUSDT}`` within the existing
market-count limits and still rejects an unknown venue id. ``huobi`` is not
a second id.
"""

from __future__ import annotations

from krellbot.pack.lint import check

_PACK = {
    "schema_version": 1,
    "id": "htx-markets",
    "version": "1.0.0",
    "label": "HTX markets",
    "author": "krellbot tests",
    "timeframe": "1h",
    "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
    "entry": ["close", ">", "sma2"],
    "exit": ["close", "<", "sma2"],
    "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
}


def test_pack_validate_accepts_htx_btcusdt_market() -> None:
    pack = {**_PACK, "markets": [{"venue": "htx", "pair": "BTCUSDT"}]}
    assert check(pack) == []


def test_pack_validate_rejects_huobi_alias() -> None:
    pack = {**_PACK, "markets": [{"venue": "huobi", "pair": "BTCUSDT"}]}
    errors = check(pack)
    assert errors
