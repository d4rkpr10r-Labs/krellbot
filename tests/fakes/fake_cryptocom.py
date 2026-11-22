"""In-memory Crypto.com Exchange transport. No network."""

from __future__ import annotations

from dataclasses import dataclass, field


def _ok(result: object) -> dict:
    return {"code": 0, "result": result}


@dataclass
class FakeCryptocomTransport:
    """Records requests and returns scripted public and private payloads."""

    gets: list[str] = field(default_factory=list)
    posts: list[tuple[str, dict | None]] = field(default_factory=list)
    instruments: dict = field(
        default_factory=lambda: _ok(
            {
                "data": [
                    {
                        "symbol": "BTC_USDT",
                        "inst_type": "CCY_PAIR",
                        "base_ccy": "BTC",
                        "quote_ccy": "USDT",
                        "quote_decimals": 2,
                        "quantity_decimals": 5,
                        "price_tick_size": "0.01",
                        "qty_tick_size": "0.00001",
                        "tradable": True,
                    }
                ]
            }
        )
    )
    risk: dict = field(
        default_factory=lambda: _ok(
            {"base_currency_config": [{"instrument_name": "BTC", "min_order_notional_usd": "1.0"}]}
        )
    )
    book: dict = field(
        default_factory=lambda: _ok(
            {"instrument_name": "BTC_USDT", "data": [{"asks": [["100000", "1", "1"]], "bids": [["99990", "1", "1"]]}]}
        )
    )
    balances: dict = field(
        default_factory=lambda: _ok(
            {
                "data": [
                    {
                        "position_balances": [
                            {"instrument_name": "BTC", "quantity": "0.5", "reserved_qty": "0.1"},
                            {"instrument_name": "USDT", "quantity": "20", "reserved_qty": "1"},
                        ]
                    }
                ]
            }
        )
    )
    accounts: dict = field(
        default_factory=lambda: _ok({"permissions": ["Can Read", "Can Trade"], "this_account": {"tradable": True}})
    )
    orders: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)

    def get(self, url: str, headers: dict | None = None) -> dict:
        del headers
        self.gets.append(str(url))
        if "get-instruments" in url:
            return dict(self.instruments)
        if "get-risk-parameters" in url:
            return dict(self.risk)
        if "get-book" in url:
            return dict(self.book)
        return _ok({})

    def post(self, url: str, body: dict | None, headers: dict | None = None) -> dict:
        del headers
        self.posts.append((str(url), body))
        method = "" if body is None else str(body.get("method") or "")
        if method == "private/advanced/create-order":
            return _ok({"order_id": "9002", "client_oid": (body or {}).get("params", {}).get("client_oid")})
        if method == "private/create-order":
            return _ok({"order_id": "9001", "client_oid": (body or {}).get("params", {}).get("client_oid")})
        if method == "private/get-accounts":
            return dict(self.accounts)
        if method == "private/user-balance":
            return dict(self.balances)
        if method == "private/advanced/get-open-orders":
            return _ok({"data": list(self.orders)})
        if method == "private/get-open-orders":
            return _ok({"data": []})
        if method == "private/get-trades":
            return _ok({"data": list(self.fills)})
        return _ok({})
