"""Fake LBank spot transport for tests.

Records every GET / POST. Never opens a network socket. Default accuracy
covers the wave symbol ``btc_usdt``.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

_DEFAULT_ACCURACY = {
    "result": "true",
    "error_code": 0,
    "data": [
        {
            "symbol": "btc_usdt",
            "quantityAccuracy": "5",
            "priceAccuracy": "2",
            "minTranQua": "0.00001",
            "minOrderAmount": "1",
        }
    ],
}

_DEFAULT_ACCOUNT = {
    "result": "true",
    "error_code": 0,
    "data": {
        "balances": [
            {"asset": "usdt", "free": "1000", "locked": "0"},
            {"asset": "btc", "free": "0", "locked": "0"},
        ],
    },
}


@dataclass
class RecordedCall:
    """One HTTP call the venue made through the transport."""

    method: str
    url: str
    headers: dict[str, str]
    body: dict | list | None = None


class FakeLbankTransport:
    """Returns canned LBank spot payloads; records every call."""

    def __init__(
        self,
        *,
        accuracy: dict | None = None,
        account: dict | None = None,
        permissions: dict | None = None,
        orders: list | None = None,
        fills: list | None = None,
    ) -> None:
        self._accuracy = accuracy if accuracy is not None else dict(_DEFAULT_ACCURACY)
        self._account = account if account is not None else dict(_DEFAULT_ACCOUNT)
        self._permissions = permissions
        self._orders = list(orders) if orders is not None else []
        self._fills = list(fills) if fills is not None else []
        self.calls: list[RecordedCall] = []

    def get(self, url: str, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall(method="GET", url=url, headers=dict(headers)))
        path = urlparse(url).path
        if path.endswith("/v2/accuracy.do"):
            return dict(self._accuracy)
        raise AssertionError(f"unexpected LBank GET {path}")

    def post(self, url: str, body: dict | list | None, headers: dict[str, str]) -> dict:
        self.calls.append(RecordedCall(method="POST", url=url, headers=dict(headers), body=body))
        path = urlparse(url).path
        if path.endswith("/v2/supplement/user_info_account.do"):
            return dict(self._account)
        if path.endswith("/v2/supplement/orders_info_no_deal.do"):
            return {"result": "true", "error_code": 0, "data": {"orders": list(self._orders), "current_page": 1}}
        if path.endswith("/v2/supplement/transaction_history.do"):
            return {"result": "true", "error_code": 0, "data": list(self._fills)}
        if path.endswith("/v2/supplement/api_Restrictions.do") and self._permissions is not None:
            return dict(self._permissions)
        raise AssertionError(f"unexpected LBank POST {path}")
