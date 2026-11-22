"""In-memory BTCC spot transport. No network."""

from __future__ import annotations

from dataclasses import dataclass, field


def _ok(data: object) -> dict:
    return {"code": 0, "data": data}


@dataclass
class FakeBtccTransport:
    """Records requests and returns scripted public and private payloads."""

    gets: list[tuple[str, dict]] = field(default_factory=list)
    posts: list[tuple[str, dict | None, dict]] = field(default_factory=list)
    markets: dict = field(
        default_factory=lambda: _ok(
            {
                "records": [
                    {
                        "name": "BTCUSDT",
                        "stock": "BTC",
                        "money": "USDT",
                        "min_amount": "0.0003",
                        "min_order_value": "10",
                        "stock_prec": 5,
                        "money_prec": 2,
                        "taker_fee": "0.0030",
                        "switch": True,
                    }
                ]
            }
        )
    )
    depth: dict = field(default_factory=lambda: _ok({"result": {"asks": [["100000", "1"]], "bids": [["99990", "1"]]}}))
    balances: dict = field(
        default_factory=lambda: _ok(
            {
                "result": [
                    {"BTC": {"available": "0.5", "frozen": "0.1"}},
                    {"USDT": {"available": "20", "frozen": "1"}},
                ]
            }
        )
    )
    api_keys: dict = field(default_factory=lambda: _ok([{"apiKey": "key", "authorityStr": "34"}]))
    orders: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)

    def get(self, url: str, headers: dict | None = None) -> dict:
        self.gets.append((str(url), dict(headers or {})))
        if "market/list" in url:
            return dict(self.markets)
        if "market/depth" in url:
            return dict(self.depth)
        if "asset/query" in url:
            return dict(self.balances)
        if "pending_stop" in url:
            return _ok({"records": list(self.orders)})
        if "/order/pending" in url:
            return _ok({"records": []})
        if "deal_history" in url:
            return _ok({"records": list(self.fills)})
        return _ok({"records": []})

    def post(self, url: str, body: dict | None, headers: dict | None = None) -> dict:
        self.posts.append((str(url), body, dict(headers or {})))
        if url.endswith("/btcc_trade/order/stop_market"):
            return _ok({"id": "9002"})
        if url.endswith("/btcc_trade/order/market"):
            return _ok({"id": "9001"})
        if url.endswith("/v2/apiKey/list"):
            return dict(self.api_keys)
        return _ok({})
