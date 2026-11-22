"""MEXC spot venue adapter (paper).

Docs: https://www.mexc.com/api-docs/spot-v3/introduction

MEXC is an OctoBot spot tentacle and a top-25 spot venue. This adapter is
paper-only.

Read requests sign their query parameters with HMAC-SHA256. The
``X-MEXC-APIKEY`` header carries the key id.

The documented spot order API does not place conditional stops.
``STOP_MARKET_ORDER`` is query-only in the spot enum. Order mutations are
disabled pending live qualification. Paper runs use the simulated
PaperVenue and its protective stops.

Paper taker is pinned at 5 bps from public ``takerCommission`` ``0.0005``
on BTCUSDT. The MX deduction is not applied.

Tests inject a fake ``Transport`` and a deterministic ``now_fn``. The
constructor requires ``transport`` — there is no silent ``HttpTransport``
default, so CI cannot accidentally hit the network.

Canonical pack and venue symbol is ``BTCUSDT``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from collections.abc import Callable
from decimal import Decimal
from typing import Any, NoReturn, Protocol

from krellbot import release_policy, sanitize
from krellbot.venues.base import (
    Balance,
    Fill,
    KeyPerms,
    OpenOrder,
    OrderRef,
    PairRules,
    Truth,
)

API_HOST = "api.mexc.com"
BASE_URL = f"https://{API_HOST}"
RECV_WINDOW = "5000"
WAVE_PAIR = "BTCUSDT"


class Transport(Protocol):
    """Read-only transport with explicit request headers."""

    def get(self, url: str, headers: dict[str, str]) -> dict | list: ...


class HttpTransport:
    """Real urllib GET transport. Tests must inject a fake."""

    def get(self, url: str, headers: dict[str, str]) -> dict | list:
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def sign_mexc(secret: str, total_params: str) -> str:
    """Lowercase hex HMAC-SHA256 of Mexc ``totalParams``.

    The signed value is the query or form string. It is not named ``api_key``.
    """
    return hmac.new(secret.encode("utf-8"), total_params.encode("utf-8"), hashlib.sha256).hexdigest()


def _refuse_order_change() -> NoReturn:
    release_policy.ensure_live_orders_allowed()
    raise NotImplementedError("mexc: spot order mutations are disabled pending live qualification")


def _decimals(increment: str) -> int:
    exp = Decimal(increment).as_tuple().exponent
    if not isinstance(exp, int) or exp >= 0:
        return 0
    return -exp


def _optional_decimal(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (ValueError, ArithmeticError, TypeError):
        return None


def _default_now_ms() -> int:
    return int(time.time() * 1000)


class MexcVenue:
    """Read-only MEXC spot adapter for rules, balances, and wave-pair orders.

    An injected transport is required. Order mutation methods fail closed
    even when the global release permits live trading.
    """

    def __init__(
        self,
        key_id: str,
        api_secret: str,
        transport: Transport,
        *,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        if transport is None:
            raise ValueError("mexc: transport is required; None must not silently call the network")
        sanitize.register_secret(key_id, api_secret)
        self._key_id = key_id
        self._api_secret = api_secret
        self._transport = transport
        self._now_fn = now_fn or _default_now_ms

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("mexc rules require a pair")
        payload = self._public_get("/api/v3/exchangeInfo", {"symbol": pair})
        row = _symbol_row(payload, pair)
        step = str(row.get("baseSizePrecision") or "1")
        price_decimals = _price_decimals(row)
        min_qty = Decimal(step)
        min_notional = Decimal(str(row.get("quoteAmountPrecision") or "0"))
        return PairRules(
            ordermin=min_qty,
            costmin=min_notional,
            lot_decimals=_decimals(step),
            price_decimals=price_decimals,
        )

    def snapshot(self) -> Truth:
        account = self._signed_get("/api/v3/account")
        orders = self._wave_orders()
        fills = self._signed_get("/api/v3/myTrades", {"symbol": WAVE_PAIR, "limit": "50"})
        return Truth(
            balances=_parse_balances(account),
            open_orders=orders,
            recent_fills=_parse_fills(fills),
        )

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        _refuse_order_change()

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        _refuse_order_change()

    def cancel_stops(self, pair: str) -> None:
        _refuse_order_change()

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        _refuse_order_change()

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        return next((order for order in self._wave_orders() if order.coid == coid), None)

    def check_key(self) -> KeyPerms:
        raise NotImplementedError("mexc: trade-only key permissions are not qualified")

    def _wave_orders(self) -> list[OpenOrder]:
        return _parse_orders(self._signed_get("/api/v3/openOrders", {"symbol": WAVE_PAIR}))

    def _headers(self) -> dict[str, str]:
        return {"X-MEXC-APIKEY": self._key_id}

    def _signed_params(self, params: dict[str, str] | None = None) -> dict[str, str]:
        body = dict(params or {})
        body["timestamp"] = str(int(self._now_fn()))
        body["recvWindow"] = RECV_WINDOW
        total_params = urllib.parse.urlencode(body)
        body["signature"] = sign_mexc(self._api_secret, total_params)
        return body

    def _public_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"{BASE_URL}{path}"
        if query:
            url = f"{url}?{query}"
        return self._transport.get(url, {})

    def _signed_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        signed = self._signed_params(params)
        query = urllib.parse.urlencode(signed)
        return self._transport.get(f"{BASE_URL}{path}?{query}", self._headers())


def _symbol_row(payload: Any, pair: str) -> dict:
    if isinstance(payload, dict):
        symbols = payload.get("symbols")
        if isinstance(symbols, list):
            for item in symbols:
                if isinstance(item, dict) and str(item.get("symbol") or "") == pair:
                    return item
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and str(item.get("symbol") or "") == pair:
                    return item
        if isinstance(data, dict) and str(data.get("symbol") or "") == pair:
            return data
        if str(payload.get("symbol") or "") == pair:
            return payload
    raise ValueError(f"mexc exchangeInfo has no symbol {pair!r}")


def _price_decimals(row: dict) -> int:
    raw = row.get("quoteAssetPrecision")
    if raw is None:
        raw = row.get("quotePrecision")
    if isinstance(raw, int) and raw >= 0:
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0


def _parse_balances(payload: Any) -> list[Balance]:
    balances = payload.get("balances") if isinstance(payload, dict) else None
    if not isinstance(balances, list):
        return []
    out: list[Balance] = []
    for raw in balances:
        if not isinstance(raw, dict):
            continue
        free = _optional_decimal(raw.get("free"))
        locked = _optional_decimal(raw.get("locked")) or Decimal(0)
        if free is None:
            continue
        if free == 0 and locked == 0:
            continue
        out.append(Balance(asset=str(raw.get("asset") or ""), free=free, locked=locked))
    return out


def _parse_orders(payload: Any) -> list[OpenOrder]:
    orders = payload if isinstance(payload, list) else None
    if not isinstance(orders, list):
        return []
    out: list[OpenOrder] = []
    for raw in orders:
        if not isinstance(raw, dict):
            continue
        qty = _optional_decimal(raw.get("origQty") or raw.get("quantity"))
        if qty is None:
            continue
        coid = str(raw.get("clientOrderId") or "")
        stop = None
        if raw.get("type") == "STOP_MARKET_ORDER":
            trigger = _optional_decimal(raw.get("stopPrice"))
            if trigger is not None and trigger.is_finite() and trigger > 0:
                stop = trigger
        out.append(
            OpenOrder(
                id=str(raw.get("orderId") or ""),
                coid=coid,
                pair=str(raw.get("symbol") or ""),
                side=str(raw.get("side") or "").lower(),
                qty=qty,
                stop_price=stop,
            )
        )
    return out


def _parse_fills(payload: Any) -> list[Fill]:
    fills = payload if isinstance(payload, list) else None
    if not isinstance(fills, list):
        return []
    out: list[Fill] = []
    for raw in fills:
        if not isinstance(raw, dict):
            continue
        qty = _optional_decimal(raw.get("qty"))
        price = _optional_decimal(raw.get("price"))
        if qty is None or price is None:
            continue
        out.append(
            Fill(
                id=str(raw.get("id") or ""),
                coid=str(raw.get("clientOrderId") or raw.get("orderId") or ""),
                pair=str(raw.get("symbol") or ""),
                side="buy" if raw.get("isBuyer") is True else "sell",
                qty=qty,
                price=price,
                ts_ms=int(raw.get("time") or 0),
            )
        )
    return out
