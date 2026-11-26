"""MEXC public candles feed the scheduled MEXC paper tick."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from krellbot.cli import cmd_tick
from krellbot.data.candles import TF_MS
from krellbot.data.instruments import MEXC_EXCHANGE_INFO_URL, SNAPSHOT_FILENAME
from krellbot.data.mexc_public import INTERVALS, KLINES_LIMIT, KLINES_URL, fetch_mexc_candles
from tests.test_instrument_rules_mexc_wave_pair import MEXC_BTC_USDT_INFO
from tests.test_mexc_paper_arm import _arm_paper, _tick_journal_records, _write_wave_pack

CLOSED = 1_797_000_000_000
HOUR = 3_600_000
NOW = CLOSED + 3 * HOUR + 1_000


def _row(timestamp: int, price: str) -> list:
    # MEXC documents eight fields per kline row.
    return [timestamp, price, price, price, price, "100", timestamp + HOUR, "1000"]


class FakePublicTransport:
    def __init__(self, rows: object):
        self.rows = rows
        self.requests: list[str] = []

    def get(self, url, headers):
        assert headers == {}
        self.requests.append(url)
        if url.startswith(MEXC_EXCHANGE_INFO_URL + "?"):
            return MEXC_BTC_USDT_INFO
        assert url.startswith(KLINES_URL + "?")
        return self.rows


def test_every_engine_timeframe_maps_to_a_mexc_interval():
    assert INTERVALS.keys() == TF_MS.keys()
    assert INTERVALS["1h"] == "60m"


def test_public_klines_sort_dedupe_and_distinguish_forming_bar(monkeypatch):
    monkeypatch.setattr("krellbot.data.mexc_public.time.time_ns", lambda: NOW * 1_000_000)
    rows = [_row(CLOSED + i * HOUR, "10") for i in range(5)]
    transport = FakePublicTransport([*reversed(rows), rows[0]])

    closed = fetch_mexc_candles("BTCUSDT", "1h", transport)
    pricing = fetch_mexc_candles("BTCUSDT", "1h", transport, include_forming=True)

    assert [bar.ts_ms for bar in closed] == [CLOSED + i * HOUR for i in range(3)]
    assert [bar.ts_ms for bar in pricing] == [CLOSED + i * HOUR for i in range(4)]
    assert pricing[-1].close == Decimal(10)
    assert parse_qs(urlsplit(transport.requests[0]).query) == {
        "symbol": ["BTCUSDT"],
        "interval": ["60m"],
        "limit": [str(KLINES_LIMIT)],
    }


@pytest.mark.parametrize(
    "rows",
    [
        [_row(CLOSED, "NaN")],
        [_row(CLOSED, "10"), _row(CLOSED, "11")],
        [_row(CLOSED, "10")[:6]],
        [[str(CLOSED), "10", "10", "10", "10", "100", CLOSED + HOUR, "1000"]],
        [[CLOSED, "10", "9", "11", "10", "100", CLOSED + HOUR, "1000"]],
        {"code": -1121, "msg": "Invalid symbol."},
        [_row(CLOSED + i * HOUR, "10") for i in range(KLINES_LIMIT + 1)],
    ],
    ids=["nan", "conflicting-duplicate", "short-row", "string-time", "high-below-low", "error-body", "over-limit"],
)
def test_malformed_market_data_is_refused(rows):
    with pytest.raises(ValueError, match="mexc candles:"):
        fetch_mexc_candles("BTCUSDT", "1h", FakePublicTransport(rows))


@pytest.mark.parametrize(("symbol", "timeframe"), [("BTC-USDT", "1h"), ("BTCUSDT", "15m")])
def test_bad_request_is_refused_before_any_network_call(symbol, timeframe):
    transport = FakePublicTransport([])
    with pytest.raises(ValueError, match="mexc candles:"):
        fetch_mexc_candles(symbol, timeframe, transport)
    assert transport.requests == []


def test_scheduled_paper_tick_uses_mexc_public_data(home, fresh_keyring, monkeypatch):
    from krellbot import run

    monkeypatch.setattr("krellbot.data.mexc_public.time.time_ns", lambda: NOW * 1_000_000)
    monkeypatch.setattr(run, "_now_seconds", lambda: NOW / 1000)
    pack = _write_wave_pack(home)
    _arm_paper(home, pack)
    transport = FakePublicTransport(
        [_row(CLOSED + i * HOUR, price) for i, price in enumerate(("11", "10", "13", "14"))]
    )

    assert cmd_tick([], transport=transport) == 0
    assert (home / "run" / SNAPSHOT_FILENAME).is_file()
    records = _tick_journal_records(home)
    assert records[-1]["bar_ts"] == CLOSED + 2 * HOUR
    assert records[-1]["detail"].get("metadata_refusal") is None
    assert any(url.startswith(KLINES_URL + "?") for url in transport.requests)
