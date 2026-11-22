"""Fake Ourbit spot transport for tests.

Records every GET / POST / DELETE. Never opens a network socket. Default
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

_DEFAULT_BOOK = {
    "symbol": "BTCUSDT",
    "bidPrice": "99990",
    "bidQty": "1",
    "askPrice": "100000",
    "askQty": "1",
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
    body: dict | None = None


@dataclass
class FakeOurbitTransport:
    """Returns canned Ourbit spot payloads; records every call."""

    account: dict = field(default_factory=lambda: dict(_DEFAULT_ACCOUNT))
    orders: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    info: dict = field(default_factory=lambda: dict(_DEFAULT_INFO))
    book: dict = field(default_factory=lambda: dict(_DEFAULT_BOOK))
    calls: list[RecordedCall] = field(default_factory=list)
    _orders_placed: int = 0

    def get(self, url: str, headers: dict[str, str]) -> dict | list:
        self.calls.append(RecordedCall("GET", url, dict(headers)))
        if "exchangeInfo" in url:
            return self.info
        if "bookTicker" in url:
            return self.book
        if "/api/v3/account" in url:
            return self.account
        if "/api/v3/openOrders" in url:
            return self.orders
        if "/api/v3/myTrades" in url:
            return self.fills
        return {}

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall("POST", url, dict(headers), body))
        self._orders_placed += 1
        client = ""
        if isinstance(body, dict):
            client = str(body.get("newClientOrderId") or "")
        return {
            "symbol": "BTCUSDT",
            "orderId": f"ord-{self._orders_placed}",
            "status": "NEW",
            "executedQty": "0",
            "clientOrderId": client,
        }

    def delete(self, url: str, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall("DELETE", url, dict(headers)))
        return {"orderId": "cancelled", "status": "CANCELED"}
