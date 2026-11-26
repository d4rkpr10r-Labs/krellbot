"""Fake Coinbase transport for tests.

Records every GET and POST. `product` is the public product payload. `orders`
is the list-orders payload. `key_permissions` is what `check_key` sees.
`order_record` builds one documented order row for that payload.
"""

from __future__ import annotations

from dataclasses import dataclass

_DEFAULT_PRODUCT = {
    "product_id": "BTC-USD",
    "base_increment": "0.00000001",
    "quote_increment": "0.01",
    "base_min_size": "0.00000001",
    "quote_min_size": "1",
    "price": "100000",
}


def order_record(
    *,
    client_order_id: str,
    status: str,
    filled_size: str | None,
    base_size: str = "1",
    product_id: str = "BTC-USD",
    side: str = "BUY",
    stop_price: str | None = None,
) -> dict:
    """One documented Coinbase order row (List Orders ``orders[]`` / Get Order).

    ``base_size`` is the requested size and ``filled_size`` the executed size;
    ``filled_size=None`` leaves the field out. With ``stop_price`` the row is
    a stop-limit GTC order (a protective stop), otherwise a market IOC.
    """
    if stop_price is None:
        order_type, config = "MARKET", {"market_market_ioc": {"base_size": base_size}}
    else:
        order_type = "STOP_LIMIT"
        config = {
            "stop_limit_stop_limit_gtc": {
                "base_size": base_size,
                "stop_price": stop_price,
                "limit_price": stop_price,
                "stop_direction": "STOP_DIRECTION_STOP_DOWN",
            }
        }
    row: dict = {
        "order_id": f"order-{client_order_id}",
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": side,
        "status": status,
        "order_type": order_type,
        "order_configuration": config,
    }
    if filled_size is not None:
        row["filled_size"] = filled_size
    return row


@dataclass
class RecordedCall:
    """One HTTP call the venue made through the transport."""

    method: str
    url: str
    headers: dict[str, str]
    body: dict | None = None


class FakeCoinbaseTransport:
    """Returns the next response per method."""

    def __init__(
        self,
        post_responses: list[dict] | None = None,
        get_responses: list[dict] | None = None,
        *,
        key_permissions: dict | list | None = None,
        product: dict | None = None,
        orders: dict | None = None,
        accounts: dict | None = None,
        fills: dict | None = None,
        raise_on_get: set[str] | None = None,
    ) -> None:
        self._post_responses = list(post_responses or [])
        self._get_responses = list(get_responses or [])
        self._key_permissions = key_permissions
        self._product = product
        self._orders = orders
        self._accounts = accounts
        self._fills = fills
        self._raise_on_get = set(raise_on_get or ())
        self.calls: list[RecordedCall] = []

    def post(self, url: str, body: dict, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall(method="POST", url=url, headers=dict(headers), body=dict(body)))
        if not self._post_responses:
            return {"success": True}
        return self._post_responses.pop(0)

    def get(self, url: str, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall(method="GET", url=url, headers=dict(headers)))
        for marker in self._raise_on_get:
            if marker in url:
                raise RuntimeError(f"fake coinbase unreachable on {marker}")
        if "key_permissions" in url:
            if self._key_permissions is None:
                return {"scopes": []}
            return self._key_permissions
        if "market/products" in url:
            return self._product or dict(_DEFAULT_PRODUCT)
        if "/brokerage/accounts" in url:
            if self._accounts is not None:
                return self._accounts
            return {"accounts": []}
        if "orders/historical/fills" in url:
            if self._fills is not None:
                return self._fills
            return {"fills": []}
        if "orders/historical/batch" in url:
            if self._orders is not None:
                return self._orders
            if self._get_responses:
                return self._get_responses.pop(0)
            return {"orders": [], "has_next": False}
        if not self._get_responses:
            return {}
        return self._get_responses.pop(0)
