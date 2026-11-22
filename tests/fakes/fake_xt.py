"""Fake XT spot transport for tests.

Records every GET / POST / DELETE. Never opens a network socket. Default
public symbol covers pack pair ``BTCUSDT`` as wire symbol ``btc_usdt``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_DEFAULT_INFO = {
    "symbols": [
        {
            "symbol": "btc_usdt",
            "state": "ONLINE",
            "tradingEnabled": True,
            "baseCurrency": "btc",
            "quoteCurrency": "usdt",
            "pricePrecision": 2,
            "quantityPrecision": 5,
            "orderTypes": ["LIMIT", "MARKET"],
            "timeInForces": ["GTC", "IOC"],
            "filters": [{"filter": "QUOTE_QTY", "min": "1"}],
            "takerFeeRate": "0.002",
            "makerFeeRate": "0.002",
        }
    ]
}

_DEFAULT_BOOK = [{"s": "btc_usdt", "ap": "100000", "aq": "1", "bp": "99990", "bq": "1"}]

_DEFAULT_BALANCES = {
    "permissions": ["SPOT"],
    "assets": [
        {"currency": "usdt", "availableAmount": "1000", "frozenAmount": "0"},
        {"currency": "btc", "availableAmount": "0", "frozenAmount": "0"},
    ],
}


def _ok(result):
    return {"rc": 0, "mc": "SUCCESS", "result": result}


@dataclass
class RecordedCall:
    """One HTTP call the venue made through the transport."""

    method: str
    url: str
    headers: dict[str, str]
    body: dict | None = None


@dataclass
class FakeXtTransport:
    """Returns canned XT spot payloads; records every call."""

    balances: dict = field(default_factory=lambda: dict(_DEFAULT_BALANCES))
    orders: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    info: dict = field(default_factory=lambda: dict(_DEFAULT_INFO))
    book: list = field(default_factory=lambda: list(_DEFAULT_BOOK))
    calls: list[RecordedCall] = field(default_factory=list)
    _orders_placed: int = 0

    def get(self, url: str, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall("GET", url, dict(headers)))
        if "/v4/public/symbol" in url:
            return _ok(self.info)
        if "/v4/public/ticker/book" in url:
            return _ok(self.book)
        if "/v4/balances" in url:
            return _ok(self.balances)
        if "/v4/open-order" in url:
            return _ok(self.orders)
        if "/v4/trade" in url:
            return _ok(self.fills)
        return _ok({})

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall("POST", url, dict(headers), body))
        self._orders_placed += 1
        client = ""
        if isinstance(body, dict):
            client = str(body.get("clientOrderId") or "")
        return _ok({"orderId": f"ord-{self._orders_placed}", "clientOrderId": client})

    def delete(self, url: str, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall("DELETE", url, dict(headers)))
        return _ok({"orderId": "cancelled"})
