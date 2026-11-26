"""Fake Binance spot transport for tests.

Records every GET / POST / DELETE. Never opens a network socket. Default
exchangeInfo covers the M14 wave pair ``BTCUSDT``.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

_DEFAULT_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "lastPrice": "100000",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.00001",
                    "maxQty": "9000",
                    "stepSize": "0.00001",
                },
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0.01",
                    "maxPrice": "1000000",
                    "tickSize": "0.01",
                },
                {
                    "filterType": "NOTIONAL",
                    "minNotional": "5",
                },
            ],
        }
    ]
}

_DEFAULT_ACCOUNT = {
    "balances": [
        {"asset": "USDT", "free": "1000", "locked": "0"},
        {"asset": "BTC", "free": "0", "locked": "0"},
    ]
}

_DEFAULT_RESTRICTIONS = {
    "ipRestrict": False,
    "enableWithdrawals": False,
    "enableInternalTransfer": False,
    "enableReading": True,
    "enableSpotAndMarginTrading": True,
}


@dataclass
class RecordedCall:
    """One HTTP call the venue made through the transport."""

    method: str
    url: str
    headers: dict[str, str]
    body: dict | None = None


class FakeBinanceTransport:
    """Returns canned Binance spot payloads; records every call."""

    def __init__(
        self,
        *,
        exchange_info: dict | None = None,
        account: dict | None = None,
        open_orders: list | None = None,
        fills: list | None = None,
        api_restrictions: dict | None = None,
        post_responses: list[dict] | None = None,
        delete_responses: list[dict] | None = None,
        raise_on: set[str] | None = None,
        all_orders: list | None = None,
    ) -> None:
        self._exchange_info = exchange_info if exchange_info is not None else dict(_DEFAULT_EXCHANGE_INFO)
        self._account = account if account is not None else dict(_DEFAULT_ACCOUNT)
        self._open_orders = list(open_orders) if open_orders is not None else []
        self._fills = list(fills) if fills is not None else []
        self._api_restrictions = dict(api_restrictions) if api_restrictions is not None else dict(_DEFAULT_RESTRICTIONS)
        self._post_responses = list(post_responses or [])
        self._delete_responses = list(delete_responses or [])
        self._raise_on = set(raise_on or ())
        # Orders the venue knows about, newest last, as ``allOrders`` rows.
        # Accepted POSTs are appended so a later lookup finds them.
        self.all_orders: list[dict] = list(all_orders or [])
        self.calls: list[RecordedCall] = []

    def get(self, url: str, headers: dict[str, str]) -> dict | list:
        self.calls.append(RecordedCall(method="GET", url=url, headers=dict(headers)))
        self._maybe_raise(url)
        path = urlparse(url).path
        if path.endswith("/api/v3/exchangeInfo"):
            return dict(self._exchange_info)
        if path.endswith("/api/v3/account"):
            return dict(self._account)
        if path.endswith("/api/v3/openOrders"):
            return list(self._open_orders)
        if path.endswith("/api/v3/myTrades"):
            return list(self._fills)
        if path.endswith("/sapi/v1/account/apiRestrictions"):
            return dict(self._api_restrictions)
        if path.endswith("/api/v3/allOrders"):
            symbol = parse_qs(urlparse(url).query).get("symbol", [""])[0]
            return [dict(o) for o in self.all_orders if o.get("symbol") == symbol]
        return {}

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall(method="POST", url=url, headers=dict(headers), body=dict(body or {})))
        self._maybe_raise(url)
        body = dict(body or {})
        if self._post_responses:
            resp = self._post_responses.pop(0)
        else:
            # Market orders fill at once; stop-limit orders rest as NEW.
            if body.get("type") == "MARKET":
                resp = {"orderId": 1, "status": "FILLED", "executedQty": body.get("quantity", "0")}
            else:
                resp = {"orderId": 2, "status": "NEW", "executedQty": "0"}
        if isinstance(resp, dict) and resp.get("orderId") not in (None, "") and body.get("newClientOrderId"):
            self.all_orders.append(
                {
                    "symbol": body.get("symbol", ""),
                    "orderId": resp["orderId"],
                    "clientOrderId": body["newClientOrderId"],
                    "side": body.get("side", ""),
                    "type": body.get("type", ""),
                    "origQty": body.get("quantity", "0"),
                    "executedQty": resp.get("executedQty", "0"),
                    "stopPrice": body.get("stopPrice", "0"),
                    "status": resp.get("status", "NEW"),
                }
            )
        return resp

    def delete(self, url: str, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall(method="DELETE", url=url, headers=dict(headers)))
        self._maybe_raise(url)
        if self._delete_responses:
            return self._delete_responses.pop(0)
        query = parse_qs(urlparse(url).query)
        order_ids = query.get("orderId", ["0"])
        return {"orderId": int(order_ids[0]), "status": "CANCELED"}

    def _maybe_raise(self, url: str) -> None:
        for marker in self._raise_on:
            if marker in url:
                raise RuntimeError(f"fake binance unreachable on {marker}")
