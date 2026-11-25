"""Bounded MEXC spot klines from the public market-data endpoint.

MEXC documents ``GET /api/v3/klines`` without credentials. Its interval
enum spells one hour ``60m`` (``1h`` is not accepted), the limit caps at
500 rows, and each row has eight fields: open time, open, high, low,
close, volume, close time, quote volume.
"""

from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import urlencode

from krellbot.data.candles import drop_forming, drop_future
from krellbot.pack.model import Candle

KLINES_URL = "https://api.mexc.com/api/v3/klines"
KLINES_LIMIT = 500
ROW_FIELDS = 8
# Krellbot timeframe -> MEXC interval enum.
INTERVALS: dict[str, str] = {"1h": "60m", "4h": "4h", "1d": "1d"}
_SYMBOL = re.compile(r"[A-Z0-9]{4,32}")


class Transport(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> object: ...


def fetch_mexc_candles(
    symbol: str,
    timeframe: str,
    transport: Transport,
    *,
    include_forming: bool = False,
) -> list[Candle]:
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("mexc candles: invalid spot symbol")
    if timeframe not in INTERVALS:
        raise ValueError("mexc candles: unsupported timeframe")
    query = urlencode({"symbol": symbol, "interval": INTERVALS[timeframe], "limit": KLINES_LIMIT})
    cutoff_ms = time.time_ns() // 1_000_000
    payload = transport.get(f"{KLINES_URL}?{query}", {})
    if not isinstance(payload, list) or len(payload) > KLINES_LIMIT:
        raise ValueError("mexc candles: invalid kline response")

    by_start: dict[int, Candle] = {}
    for row in payload:
        if not isinstance(row, list) or len(row) != ROW_FIELDS or type(row[0]) is not int or row[0] < 0:
            raise ValueError("mexc candles: invalid kline row")
        try:
            open_, high, low, close, volume = (Decimal(str(value)) for value in row[1:6])
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("mexc candles: invalid price or volume") from exc
        if not all(value.is_finite() for value in (open_, high, low, close, volume)):
            raise ValueError("mexc candles: non-finite price or volume")
        if not (0 < low <= min(open_, close) <= max(open_, close) <= high and volume >= 0):
            raise ValueError("mexc candles: inconsistent price or volume")
        candle = Candle(row[0], open_, high, low, close, volume)
        if by_start.setdefault(candle.ts_ms, candle) != candle:
            raise ValueError("mexc candles: conflicting duplicate kline")

    candles = sorted(by_start.values(), key=lambda candle: candle.ts_ms)
    if include_forming:
        return drop_future(candles, cutoff_ms)
    return drop_forming(candles, timeframe, cutoff_ms)
