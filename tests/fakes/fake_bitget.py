"""Fake Bitget spot transport for tests.

Records every GET. Never opens a network socket. Default payloads
cover the wave symbol ``BTCUSDT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse


def _ok(data: object) -> dict:
    return {"code": "00000", "msg": "success", "data": data}


_DEFAULT_SYMBOL = _ok(
    [
        {
            "symbol": "BTCUSDT",
            "baseCoin": "BTC",
            "quoteCoin": "USDT",
            "minTradeAmount": "0",
            "takerFeeRate": "0.002",
            "makerFeeRate": "0.002",
            "pricePrecision": "2",
            "quantityPrecision": "6",
            "quotePrecision": "8",
            "status": "online",
            "minTradeUSDT": "1",
        }
    ]
)

_DEFAULT_ASSETS = _ok(
    [
        {"coin": "USDT", "available": "1000", "frozen": "0", "locked": "0"},
        {"coin": "BTC", "available": "0.01", "frozen": "0", "locked": "0"},
    ]
)

_DEFAULT_INFO = _ok({"userId": "1", "authorities": ["stor", "stow"]})


@dataclass
class FakeBitgetTransport:
    """Scripted Bitget responses keyed by path substring."""

    symbol: dict = field(default_factory=lambda: dict(_DEFAULT_SYMBOL))
    assets: dict = field(default_factory=lambda: dict(_DEFAULT_ASSETS))
    info: dict = field(default_factory=lambda: dict(_DEFAULT_INFO))
    orders: dict = field(default_factory=lambda: _ok([]))
    fills: dict = field(default_factory=lambda: _ok([]))
    conditional_orders: dict = field(default_factory=lambda: _ok([]))
    gets: list[str] = field(default_factory=list)
    request_headers: list[dict[str, str]] = field(default_factory=list)

    def get(self, url: str, headers: dict[str, str]) -> dict:
        self.gets.append(url)
        self.request_headers.append(dict(headers))
        path = urlparse(url).path
        if path.endswith("/public/symbols"):
            return self.symbol
        if path.endswith("/account/assets"):
            return self.assets
        if path.endswith("/account/info"):
            return self.info
        if path.endswith("/trade/fills"):
            return self.fills
        if path.endswith("/trade/unfilled-orders"):
            query = parse_qs(urlparse(url).query)
            if "tpsl" in query.get("tpslType", []):
                return self.conditional_orders
            return self.orders
        raise AssertionError(f"unexpected Bitget GET {path}")
