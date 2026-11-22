"""Fake Mexc spot transport for tests.

Records every GET. Never opens a network socket. Default
exchangeInfo covers the wave pair ``BTCUSDT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_DEFAULT_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "ENABLED",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "baseAssetPrecision": 6,
            "quoteAssetPrecision": 2,
            "quotePrecision": 2,
            "baseSizePrecision": "0.000001",
            "quoteAmountPrecision": "1",
            "isSpotTradingAllowed": True,
            "orderTypes": ["LIMIT", "MARKET", "LIMIT_MAKER"],
        }
    ]
}

_DEFAULT_ACCOUNT = {
    "canTrade": True,
    "canWithdraw": False,
    "canDeposit": True,
    "accountType": "SPOT",
    "permissions": ["SPOT"],
    "balances": [
        {"asset": "USDT", "free": "1000", "locked": "0"},
        {"asset": "BTC", "free": "0", "locked": "0"},
    ],
}


@dataclass
class RecordedCall:
    """One HTTP call the venue made through the transport."""

    method: str
    url: str
    headers: dict[str, str]


@dataclass
class FakeMexcTransport:
    """Returns canned Mexc spot payloads; records every call."""

    account: dict = field(default_factory=lambda: dict(_DEFAULT_ACCOUNT))
    orders: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    info: dict = field(default_factory=lambda: dict(_DEFAULT_INFO))
    calls: list[RecordedCall] = field(default_factory=list)

    def get(self, url: str, headers: dict[str, str]) -> dict | list:
        self.calls.append(RecordedCall("GET", url, dict(headers)))
        if "exchangeInfo" in url:
            return self.info
        if "/api/v3/account" in url:
            return self.account
        if "/api/v3/openOrders" in url:
            return self.orders
        if "/api/v3/myTrades" in url:
            return self.fills
        return {}
