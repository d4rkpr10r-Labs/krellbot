"""Fake Toobit spot transport for tests.

Records every GET / POST / DELETE. Never opens a network socket. Default
exchangeInfo covers the wave pair ``BTCUSDT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_DEFAULT_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01"},
                {"filterType": "LOT_SIZE", "minQty": "0.000001", "stepSize": "0.000001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "1"},
            ],
        }
    ]
}

_DEFAULT_BOOK = [{"s": "BTCUSDT", "b": "99990", "bq": "1", "a": "100000", "aq": "1"}]

_DEFAULT_ACCOUNT = {
    "balances": [
        {"coin": "USDT", "total": "1000", "free": "1000", "locked": "0"},
        {"coin": "BTC", "total": "0", "free": "0", "locked": "0"},
    ]
}


@dataclass
class RecordedCall:
    """One HTTP call the venue made through the transport."""

    method: str
    url: str
    headers: dict[str, str]
    body: dict | None = None


@dataclass
class FakeToobitTransport:
    """Returns canned Toobit spot payloads; records every call."""

    account: dict = field(default_factory=lambda: dict(_DEFAULT_ACCOUNT))
    orders: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    info: dict = field(default_factory=lambda: dict(_DEFAULT_INFO))
    book: list = field(default_factory=lambda: list(_DEFAULT_BOOK))
    calls: list[RecordedCall] = field(default_factory=list)
    _orders_placed: int = 0

    def get(self, url: str, headers: dict[str, str]) -> dict | list:
        self.calls.append(RecordedCall("GET", url, dict(headers)))
        if "exchangeInfo" in url:
            return self.info
        if "bookTicker" in url:
            return self.book
        if "/api/v1/account/trades" in url:
            return self.fills
        if url.split("?", 1)[0].endswith("/api/v1/account"):
            return self.account
        if "/api/v1/spot/openOrders" in url:
            return self.orders
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
