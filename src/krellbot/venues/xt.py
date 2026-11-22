"""XT spot venue adapter (paper).

Public spot order schema: https://doc.xt.com/docs/spot/Order/SubmitOrder.

The pack pair is ``BTCUSDT``. The public symbol is ``btc_usdt``. Orders,
books, and instrument lookups use the public spelling.

Auth headers are ``xt-validate-algorithms``, ``xt-validate-appkey``,
``xt-validate-recvwindow``, ``xt-validate-timestamp``, and
``xt-validate-signature``. The signed string is the four validation headers
sorted by name, then ``#METHOD#PATH`` plus the sorted query when present.
The digest is lowercase hex
HMAC-SHA256. The appkey value is inside that string. The constructor field
that holds it is ``key_id``.

The documented spot order API exposes plain LIMIT and MARKET orders. Order
mutation is disabled until a conditional spot order API is qualified. Paper
runs use the simulated PaperVenue and its protective stops.

Tests inject a fake ``Transport`` and a deterministic ``now_fn``. The
constructor requires ``transport``.
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
    WithdrawCapableError,
)

API_HOST = "sapi.xt.com"
BASE_URL = f"https://{API_HOST}"
RECV_WINDOW = "5000"
PACK_PAIR = "BTCUSDT"
WIRE_SYMBOL = "btc_usdt"


class Transport(Protocol):
    """Read-only transport with explicit request headers."""

    def get(self, url: str, headers: dict[str, str]) -> dict: ...


class HttpTransport:
    """Real urllib GET transport. Tests must inject a fake."""

    def get(self, url: str, headers: dict[str, str]) -> dict:
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def sign_xt(
    secret: str,
    *,
    key_id: str,
    timestamp: str,
    method: str,
    path: str,
    query: str = "",
    recv_window: str = RECV_WINDOW,
) -> str:
    """Lowercase hex HMAC-SHA256 of the XT validate header string plus path.

    ``key_id`` is the ``xt-validate-appkey`` value and is part of the
    prehash. It is not named ``api_key``.
    """
    headers = {
        "xt-validate-algorithms": "HmacSHA256",
        "xt-validate-appkey": key_id,
        "xt-validate-recvwindow": recv_window,
        "xt-validate-timestamp": timestamp,
    }
    signed_headers = "&".join(f"{key}={headers[key]}" for key in sorted(headers))
    parts = [method.upper(), path]
    if query:
        parts.append(query)
    message = signed_headers + "#" + "#".join(parts)
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def wire_symbol(pair: str) -> str:
    """Map the pack pair ``BTCUSDT`` onto the public symbol ``btc_usdt``."""
    if pair == PACK_PAIR:
        return WIRE_SYMBOL
    if pair.lower() == WIRE_SYMBOL:
        return WIRE_SYMBOL
    return pair


def pack_pair(symbol: str) -> str:
    """Map the public symbol back to the pack pair."""
    if str(symbol).lower() == WIRE_SYMBOL:
        return PACK_PAIR
    return str(symbol)


def _refuse_order_change() -> NoReturn:
    release_policy.ensure_live_orders_allowed()
    raise NotImplementedError("xt: spot order mutations are disabled pending live qualification")


def _default_now_ms() -> int:
    return int(time.time() * 1000)


class XtVenue:
    """Read-only XT spot adapter for rules, balances, orders, and fills.

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
            raise ValueError("xt: transport is required; None must not silently call the network")
        sanitize.register_secret(key_id, api_secret)
        self._key_id = key_id
        self._api_secret = api_secret
        self._transport = transport
        self._now_fn = now_fn or _default_now_ms

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("xt rules require a pair")
        symbol = wire_symbol(pair)
        payload = self._public_get("/v4/public/symbol", {"symbol": symbol})
        row = _symbol_row(payload, symbol)
        if str(row.get("state") or "").upper() not in {"ONLINE", "TRADING"} or row.get("tradingEnabled") is False:
            raise ValueError(f"xt symbol {symbol} is not enabled for spot")
        quantity_precision = _precision(row.get("quantityPrecision"))
        price_precision = _precision(row.get("pricePrecision"))
        step = Decimal(1).scaleb(-quantity_precision)
        min_notional = _quote_min(row)
        return PairRules(
            ordermin=step,
            costmin=min_notional,
            lot_decimals=quantity_precision,
            price_decimals=price_precision,
        )

    def snapshot(self) -> Truth:
        balances = self._signed_get("/v4/balances")
        orders = self._signed_get("/v4/open-order", {"bizType": "SPOT"})
        fills = self._signed_get("/v4/trade", {"bizType": "SPOT"})
        return Truth(
            balances=_parse_balances(balances),
            open_orders=_parse_orders(orders),
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
        # Only finds pending orders. This adapter does not submit orders.
        orders = _parse_orders(self._signed_get("/v4/open-order", {"bizType": "SPOT"}))
        return next((order for order in orders if order.coid == coid), None)

    def check_key(self) -> KeyPerms:
        payload = self._signed_get("/v4/balances")
        if _withdraw_capable(payload):
            raise WithdrawCapableError("xt key can withdraw; trade-only keys refused")
        raise RuntimeError("xt: balance access does not establish trade-only key permissions")

    def _public_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"{BASE_URL}{path}"
        if query:
            url = f"{url}?{query}"
        return _unwrap(self._transport.get(url, {}))

    def _signed_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = "&".join(f"{key}={params[key]}" for key in sorted(params or {}))
        headers = self._auth_headers("GET", path, query=query)
        url = f"{BASE_URL}{path}"
        if query:
            url = f"{url}?{query}"
        return _unwrap(self._transport.get(url, headers))

    def _auth_headers(self, method: str, path: str, *, query: str = "") -> dict[str, str]:
        timestamp = str(int(self._now_fn()))
        signature = sign_xt(
            self._api_secret,
            key_id=self._key_id,
            timestamp=timestamp,
            method=method,
            path=path,
            query=query,
        )
        return {
            "xt-validate-algorithms": "HmacSHA256",
            "xt-validate-appkey": self._key_id,
            "xt-validate-recvwindow": RECV_WINDOW,
            "xt-validate-timestamp": timestamp,
            "xt-validate-signature": signature,
        }


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "rc" in payload:
        if payload.get("rc") not in (0, "0"):
            raise RuntimeError(f"xt request refused: {payload.get('mc') or payload.get('rc')}")
        return payload.get("result")
    return payload


def _symbol_row(payload: Any, symbol: str) -> dict:
    rows = None
    if isinstance(payload, dict):
        rows = payload.get("symbols")
        if rows is None and isinstance(payload.get("result"), dict):
            rows = payload["result"].get("symbols")
    if isinstance(rows, list):
        for item in rows:
            if isinstance(item, dict) and str(item.get("symbol") or "") == symbol:
                return item
    raise ValueError(f"xt public symbol has no symbol {symbol!r}")


def _precision(raw: Any) -> int:
    if isinstance(raw, int) and raw >= 0:
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0


def _quote_min(row: dict) -> Decimal:
    filters = row.get("filters")
    if isinstance(filters, list):
        for item in filters:
            if isinstance(item, dict) and item.get("filter") == "QUOTE_QTY":
                parsed = _optional_decimal(item.get("min"))
                if parsed is not None:
                    return parsed
    parsed = _optional_decimal(row.get("minQuoteQty"))
    if parsed is None:
        raise ValueError("xt symbol payload missing quote minimum")
    return parsed


def _optional_decimal(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (ValueError, ArithmeticError, TypeError):
        return None


def _permission_tokens(raw: Any) -> set[str]:
    if isinstance(raw, str):
        return {part.strip().lower() for part in raw.split(",") if part.strip()}
    if isinstance(raw, list):
        return {str(part).strip().lower() for part in raw if str(part).strip()}
    return set()


def _withdraw_capable(payload: Any) -> bool:
    if isinstance(payload, dict):
        if payload.get("canWithdraw") is True or payload.get("enableWithdraw") is True:
            return True
        tokens = _permission_tokens(payload.get("permissions") or payload.get("permission"))
        if any("withdraw" in token for token in tokens):
            return True
    return False


def _asset_rows(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        assets = payload.get("assets")
        if assets is None:
            assets = payload.get("balances")
        if isinstance(assets, list):
            return assets
    return []


def _parse_balances(payload: Any) -> list[Balance]:
    out: list[Balance] = []
    for raw in _asset_rows(payload):
        if not isinstance(raw, dict):
            continue
        free = _optional_decimal(raw.get("availableAmount") or raw.get("free"))
        locked = _optional_decimal(raw.get("frozenAmount") or raw.get("locked")) or Decimal(0)
        if free is None:
            continue
        if free == 0 and locked == 0:
            continue
        asset = str(raw.get("currency") or raw.get("asset") or "").upper()
        out.append(Balance(asset=asset, free=free, locked=locked))
    return out


def _order_rows(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "orders"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
    return []


def _parse_orders(payload: Any) -> list[OpenOrder]:
    out: list[OpenOrder] = []
    for raw in _order_rows(payload):
        if not isinstance(raw, dict):
            continue
        qty = _optional_decimal(raw.get("quantity") or raw.get("origQty"))
        if qty is None:
            continue
        coid = str(raw.get("clientOrderId") or "")
        out.append(
            OpenOrder(
                id=str(raw.get("orderId") or ""),
                coid=coid,
                pair=pack_pair(str(raw.get("symbol") or "")),
                side=str(raw.get("side") or "").lower(),
                qty=qty,
            )
        )
    return out


def _parse_fills(payload: Any) -> list[Fill]:
    out: list[Fill] = []
    for raw in _order_rows(payload):
        if not isinstance(raw, dict):
            continue
        qty = _optional_decimal(raw.get("quantity") or raw.get("qty"))
        price = _optional_decimal(raw.get("price"))
        if qty is None or price is None:
            continue
        side = str(raw.get("side") or "").lower()
        if not side and raw.get("isBuyer") is True:
            side = "buy"
        out.append(
            Fill(
                id=str(raw.get("tradeId") or raw.get("id") or ""),
                coid=str(raw.get("clientOrderId") or raw.get("orderId") or ""),
                pair=pack_pair(str(raw.get("symbol") or "")),
                side=side,
                qty=qty,
                price=price,
                ts_ms=int(raw.get("time") or raw.get("timestamp") or 0),
            )
        )
    return out
