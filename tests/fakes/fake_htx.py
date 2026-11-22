"""In-memory HTX spot transport. No network."""

from __future__ import annotations

from dataclasses import dataclass, field


def _ok(data: object) -> dict:
    return {"status": "ok", "data": data}


@dataclass
class FakeHtxTransport:
    """Records requests and returns scripted public and private payloads."""

    gets: list[str] = field(default_factory=list)
    posts: list[tuple[str, dict | None]] = field(default_factory=list)
    symbols: dict = field(
        default_factory=lambda: _ok(
            [
                {
                    "symbol": "btcusdt",
                    "bc": "btc",
                    "qc": "usdt",
                    "ap": 6,
                    "pp": 2,
                    "minoa": "0.00001",
                    "minov": "1",
                    "state": "online",
                }
            ]
        )
    )
    ticker: dict = field(default_factory=lambda: {"status": "ok", "tick": {"ask": ["100000", "1"]}})
    accounts: dict = field(default_factory=lambda: _ok([{"id": 1001, "type": "spot", "state": "working"}]))
    balances: dict = field(
        default_factory=lambda: _ok(
            {
                "id": 1001,
                "type": "spot",
                "list": [
                    {"currency": "btc", "type": "trade", "balance": "0.5"},
                    {"currency": "btc", "type": "frozen", "balance": "0.1"},
                    {"currency": "usdt", "type": "trade", "balance": "20"},
                ],
            }
        )
    )
    api_key: dict = field(
        default_factory=lambda: {"code": 200, "data": [{"accessKey": "key", "permission": "readOnly,trade"}]}
    )
    orders: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)

    def get(self, url: str, headers: dict | None = None) -> dict:
        del headers
        self.gets.append(str(url))
        if "market-symbols" in url:
            return dict(self.symbols)
        if "detail/merged" in url:
            return dict(self.ticker)
        if "/v2/user/api-key" in url:
            return dict(self.api_key)
        if "/v2/algo-orders/opening" in url:
            return {"code": 200, "data": list(self.orders)}
        if "/v1/order/openOrders" in url:
            return _ok([])
        if "/v1/order/matchresults" in url:
            return _ok(list(self.fills))
        if "/balance" in url:
            return dict(self.balances)
        if "/v1/account/accounts" in url:
            return dict(self.accounts)
        return _ok([])

    def post(self, url: str, body: dict | None, headers: dict | None = None) -> dict:
        del headers
        self.posts.append((str(url), body))
        if url.split("?", 1)[0].endswith("/v2/algo-orders"):
            return {"code": 200, "data": {"clientOrderId": (body or {}).get("clientOrderId"), "orderId": "9002"}}
        if "/cancellation" in url:
            return {"code": 200, "data": {}}
        return _ok("9001")
