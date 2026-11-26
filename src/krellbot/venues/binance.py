"""Binance-class spot venue adapter.

Docs:
- https://developers.binance.com/docs/binance-spot-api-docs/rest-api
- https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints
- https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints

Auth: HMAC-SHA256 over the query string; ``X-MBX-APIKEY`` carries the key.
Tests inject a fake ``Transport`` and a deterministic ``now_fn`` for
``timestamp``. The constructor requires ``transport`` — there is no silent
``HttpTransport`` default, so CI cannot accidentally hit the network.

Canonical pack/paper wave pair spelling is ``BTCUSDT`` (concatenated
``BASEQUOTE``; see ``docs/pack-format.md``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.parse
from collections.abc import Callable
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from krellbot import release_policy, sanitize
from krellbot.venues.base import (
    Balance,
    Fill,
    KeyMalformedError,
    KeyPerms,
    KeyTradeOffError,
    KeyUnknownPermissionError,
    OpenOrder,
    OrderRef,
    PairRules,
    ProtectiveStopRejectedError,
    Truth,
    WithdrawCapableError,
)

API_HOST = "api.binance.com"
BASE_URL = f"https://{API_HOST}"
STOP_BUFFER = Decimal("0.005")  # limit = stop * (1 - 0.5%)
# Recent orders scanned when looking up a client order id (allOrders limit).
ORDER_LOOKUP_LIMIT = 100

# GET /sapi/v1/account/apiRestrictions. A trade-only key grants exactly
# these two permissions. Withdrawal and transfer permissions can move funds
# off the spot account and are refused as withdraw-capable. Any other
# ``enable*`` / ``permits*`` flag set to true (margin, futures, options,
# FIX, or a flag Binance adds later) fails closed.
REQUIRED_KEY_PERMISSIONS = ("enableReading", "enableSpotAndMarginTrading")
FUND_MOVING_KEY_PERMISSIONS = ("enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer")
_PERMISSION_PREFIXES = ("enable", "permits")
_FILLED_STATUSES = frozenset({"FILLED", "PARTIALLY_FILLED", "EXPIRED"})
_ACTIVE_STOP_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})


class Transport(Protocol):
    """Anything with get/post/delete returning parsed JSON dicts."""

    def get(self, url: str, headers: dict[str, str]) -> dict: ...

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict: ...

    def delete(self, url: str, headers: dict[str, str]) -> dict: ...


class HttpTransport:
    """Real urllib transport. Tests must not call this directly."""

    def get(self, url: str, headers: dict[str, str]) -> dict:
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict:
        import urllib.request

        from krellbot.tls import urlopen

        data = None
        hdrs = dict(headers)
        if body is not None:
            data = urllib.parse.urlencode(body).encode("utf-8")
            hdrs = {**hdrs, "content-type": "application/x-www-form-urlencoded"}
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def delete(self, url: str, headers: dict[str, str]) -> dict:
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="DELETE")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def sign_query(secret: str, query: str) -> str:
    """HMAC-SHA256 hex digest of ``query`` with the API secret."""
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def _quantize_qty(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def _quantize_price(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


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
    import time as _time

    return int(_time.time() * 1000)


class BinanceVenue:
    """Binance spot adapter for balances / open orders / place+cancel seams.

    ``transport`` is mandatory. Passing ``None`` raises immediately rather than
    falling through to ``HttpTransport``. Order mutations call
    ``release_policy.ensure_live_orders_allowed()`` first.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        transport: Transport,
        *,
        now_fn: Callable[[], int] | None = None,
        recv_window: int = 5_000,
    ) -> None:
        if transport is None:
            raise ValueError("binance: transport is required; None must not silently call the network")
        sanitize.register_secret(api_key, api_secret)
        self._api_key = api_key
        self._api_secret = api_secret
        self._transport = transport
        self._now_fn = now_fn or _default_now_ms
        self._recv_window = int(recv_window)
        self._recent: dict[str, OrderRef] = {}
        self._prices: dict[str, Decimal] = {}

    # ---- public protocol surface ----------------------------------------

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("binance rules require a pair")
        payload = self._public_get("/api/v3/exchangeInfo", {"symbol": pair})
        symbol = _symbol_from_exchange_info(payload, pair)
        filters = symbol.get("filters") if isinstance(symbol.get("filters"), list) else []
        lot = _filter(filters, "LOT_SIZE")
        price = _filter(filters, "PRICE_FILTER")
        notional = _filter(filters, "NOTIONAL") or _filter(filters, "MIN_NOTIONAL")
        if lot is None or price is None:
            raise TypeError("binance exchangeInfo missing LOT_SIZE or PRICE_FILTER")
        min_qty = Decimal(str(lot.get("minQty") or "0"))
        step = str(lot.get("stepSize") or "1")
        tick = str(price.get("tickSize") or "1")
        min_notional = Decimal(str((notional or {}).get("minNotional") or "0"))
        last = _optional_decimal(symbol.get("lastPrice") or symbol.get("price"))
        if last is not None:
            self._prices[pair] = last
        return PairRules(
            ordermin=min_qty,
            costmin=min_notional if min_notional > 0 else Decimal("0.00000001"),
            lot_decimals=_decimals(step),
            price_decimals=_decimals(tick),
        )

    def snapshot(self) -> Truth:
        account = self._signed_get("/api/v3/account")
        orders = self._signed_get("/api/v3/openOrders")
        fills = self._signed_get("/api/v3/myTrades", {"limit": "50"})
        return Truth(
            balances=_parse_balances(account),
            open_orders=_parse_orders(orders),
            recent_fills=_parse_fills(fills),
        )

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        release_policy.ensure_live_orders_allowed()
        if coid in self._recent:
            return self._recent[coid]
        if not pair:
            raise ValueError("binance entry requires a pair")
        rules = self.rules(pair)
        found = self._existing_entry(coid, pair, rules)
        if found is not None:
            self._recent[coid] = found
            return found
        lot = _quantize_qty(qty, rules.lot_decimals)
        price_stop = _quantize_price(stop, rules.price_decimals)
        _reject_size(lot, rules, self._prices.get(pair))
        limit_price = _quantize_price(
            price_stop * (Decimal(1) - STOP_BUFFER),
            rules.price_decimals,
        )
        entry = self._signed_post(
            "/api/v3/order",
            {
                "symbol": pair,
                "side": "BUY",
                "type": "MARKET",
                "quantity": format(lot, "f"),
                "newClientOrderId": coid,
                "newOrderRespType": "FULL",
            },
        )
        filled = _filled_qty(entry, rules.lot_decimals)
        if filled is None:
            raise RuntimeError("binance entry was not filled; stop not placed")
        # The entry filled. From here any failure leaves a position whose
        # protection is unknown, so it is a reconcile case, not a no-op.
        try:
            stop_resp = self._signed_post(
                "/api/v3/order",
                {
                    "symbol": pair,
                    "side": "SELL",
                    "type": "STOP_LOSS_LIMIT",
                    "quantity": format(filled, "f"),
                    "price": format(limit_price, "f"),
                    "stopPrice": format(price_stop, "f"),
                    "timeInForce": "GTC",
                    "newClientOrderId": coid + "_stop",
                },
            )
        except Exception as exc:
            raise ProtectiveStopRejectedError("binance stop outcome unknown after entry fill") from exc
        if not _order_ok(stop_resp):
            raise ProtectiveStopRejectedError("binance stop was not accepted")
        ref = OrderRef(
            id=str(entry.get("orderId") or stop_resp.get("orderId") or ""),
            coid=coid,
            pair=pair,
            side="buy",
            qty=lot,
            filled_qty=filled,
            stop_price=price_stop,
        )
        self._recent[coid] = ref
        return ref

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        release_policy.ensure_live_orders_allowed()
        if not pair:
            raise ValueError("binance exit requires a pair")
        rules = self.rules(pair)
        lot = _quantize_qty(qty, rules.lot_decimals)
        _reject_size(lot, rules, self._prices.get(pair))
        resp = self._signed_post(
            "/api/v3/order",
            {
                "symbol": pair,
                "side": "SELL",
                "type": "MARKET",
                "quantity": format(lot, "f"),
                "newClientOrderId": coid,
            },
        )
        if not _order_ok(resp):
            raise RuntimeError("binance exit was not accepted")
        return OrderRef(
            id=str(resp.get("orderId") or ""),
            coid=coid,
            pair=pair,
            side="sell",
            qty=lot,
            filled_qty=lot,
            stop_price=None,
        )

    def cancel_stops(self, pair: str) -> None:
        release_policy.ensure_live_orders_allowed()
        snapshot = self.snapshot()
        for order in snapshot.open_orders:
            if order.pair == pair and order.stop_price is not None:
                self._signed_delete(
                    "/api/v3/order",
                    {"symbol": pair, "orderId": order.id},
                )

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        release_policy.ensure_live_orders_allowed()
        snapshot = self.snapshot()
        rules = self.rules(pair)
        price = _quantize_price(new_stop, rules.price_decimals)
        limit_price = _quantize_price(
            price * (Decimal(1) - STOP_BUFFER),
            rules.price_decimals,
        )
        for order in snapshot.open_orders:
            if order.pair != pair or order.stop_price is None:
                continue
            self._signed_delete(
                "/api/v3/order",
                {"symbol": pair, "orderId": order.id},
            )
            replaced = self._signed_post(
                "/api/v3/order",
                {
                    "symbol": pair,
                    "side": "SELL",
                    "type": "STOP_LOSS_LIMIT",
                    "quantity": format(order.qty, "f"),
                    "price": format(limit_price, "f"),
                    "stopPrice": format(price, "f"),
                    "timeInForce": "GTC",
                    "newClientOrderId": (order.coid or order.id) + "_raised",
                },
            )
            if not _order_ok(replaced):
                # The old stop is already cancelled. The next tick sees no
                # resting stop and runs the engine's ensure-stop path.
                raise ProtectiveStopRejectedError("binance replacement stop was not accepted")

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        ref = self._recent.get(coid)
        if ref is None:
            for order in self.snapshot().open_orders:
                if order.coid == coid:
                    return order
            return None
        return OpenOrder(
            id=ref.id,
            coid=ref.coid,
            pair=ref.pair,
            side=ref.side,
            qty=ref.qty,
            stop_price=ref.stop_price,
        )

    def check_key(self) -> KeyPerms:
        perms = self._signed_get("/sapi/v1/account/apiRestrictions")
        if not isinstance(perms, dict) or not any(name in perms for name in REQUIRED_KEY_PERMISSIONS):
            raise KeyMalformedError("binance apiRestrictions response shape unknown; refused")
        if any(perms.get(name) is True for name in FUND_MOVING_KEY_PERMISSIONS):
            raise WithdrawCapableError("binance key can move funds; trade-only keys refused")
        missing = [name for name in REQUIRED_KEY_PERMISSIONS if perms.get(name) is not True]
        if missing:
            raise KeyTradeOffError(f"binance key missing required permissions: {missing}; refused")
        extra = sorted(
            name
            for name, value in perms.items()
            if value is True and name.startswith(_PERMISSION_PREFIXES) and name not in REQUIRED_KEY_PERMISSIONS
        )
        if extra:
            raise KeyUnknownPermissionError(f"binance key grants permissions beyond spot trading: {extra}; refused")
        return KeyPerms(can_trade=True, can_withdraw=False)

    # ---- internals ------------------------------------------------------

    def _find_order(self, coid: str, pair: str) -> dict | None:
        """Return the venue's record of ``coid`` on ``pair``, or None.

        Scans recent orders so a client order id sent by an earlier process
        is found without resubmitting. A response that is not a list cannot
        prove absence, so it raises instead of returning None.
        """
        orders = self._signed_get("/api/v3/allOrders", {"symbol": pair, "limit": str(ORDER_LOOKUP_LIMIT)})
        if not isinstance(orders, list):
            # RuntimeError, not TypeError: the tick treats it as a refused send.
            raise RuntimeError("binance allOrders response is not a list; order lookup refused")  # noqa: TRY004
        for raw in orders:
            if isinstance(raw, dict) and str(raw.get("clientOrderId") or "") == coid:
                return raw
        return None

    def _existing_entry(self, coid: str, pair: str, rules: PairRules) -> OrderRef | None:
        """Rebuild the ref for an entry an earlier process already sent."""
        entry = self._find_order(coid, pair)
        if entry is None:
            return None
        filled = _filled_qty(entry, rules.lot_decimals)
        if filled is None:
            raise RuntimeError("binance entry already sent and not filled; reconcile before resending")
        stop = self._find_order(coid + "_stop", pair)
        if stop is None or str(stop.get("status") or "").upper() not in _ACTIVE_STOP_STATUSES:
            raise ProtectiveStopRejectedError("binance entry already filled without an active protective stop")
        return OrderRef(
            id=str(entry.get("orderId") or ""),
            coid=coid,
            pair=pair,
            side="buy",
            qty=_optional_decimal(entry.get("origQty")) or filled,
            filled_qty=filled,
            stop_price=_optional_decimal(stop.get("stopPrice")),
        )

    def _headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._api_key}

    def _signed_params(self, params: dict[str, str] | None = None) -> dict[str, str]:
        body = dict(params or {})
        body["timestamp"] = str(int(self._now_fn()))
        body["recvWindow"] = str(self._recv_window)
        query = urllib.parse.urlencode(body)
        body["signature"] = sign_query(self._api_secret, query)
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

    def _signed_post(self, path: str, params: dict[str, str]) -> Any:
        signed = self._signed_params(params)
        return self._transport.post(f"{BASE_URL}{path}", signed, self._headers())

    def _signed_delete(self, path: str, params: dict[str, str]) -> Any:
        signed = self._signed_params(params)
        query = urllib.parse.urlencode(signed)
        return self._transport.delete(f"{BASE_URL}{path}?{query}", self._headers())


def _symbol_from_exchange_info(payload: Any, pair: str) -> dict:
    if not isinstance(payload, dict):
        raise TypeError("binance exchangeInfo body is not a JSON object")
    symbols = payload.get("symbols")
    if isinstance(symbols, list):
        for item in symbols:
            if isinstance(item, dict) and str(item.get("symbol") or "") == pair:
                return item
    # Some fakes return a single symbol object at the top level.
    if str(payload.get("symbol") or "") == pair:
        return payload
    raise ValueError(f"binance exchangeInfo has no symbol {pair!r}")


def _filter(filters: list, name: str) -> dict | None:
    for item in filters:
        if isinstance(item, dict) and item.get("filterType") == name:
            return item
    return None


def _reject_size(qty: Decimal, rules: PairRules, price: Decimal | None) -> None:
    if qty < rules.ordermin:
        raise ValueError("qty below ordermin")
    if price is not None and qty * price < rules.costmin:
        raise ValueError("notional below costmin")


def _filled_qty(resp: Any, lot_decimals: int) -> Decimal | None:
    """Executed base quantity of a market order, or None when nothing filled.

    Uses ``executedQty`` only. A partial fill (``PARTIALLY_FILLED``, or
    ``EXPIRED`` after the book ran out) protects what actually filled, never
    the requested quantity.
    """
    if not isinstance(resp, dict):
        return None
    status = str(resp.get("status") or "").upper()
    executed = _optional_decimal(resp.get("executedQty"))
    if executed is None or not executed.is_finite() or status not in _FILLED_STATUSES:
        return None
    filled = _quantize_qty(executed, lot_decimals)
    return filled if filled > 0 else None


def _order_ok(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    if resp.get("orderId") not in (None, ""):
        return True
    status = str(resp.get("status") or "").upper()
    return status in {"NEW", "FILLED", "PARTIALLY_FILLED"}


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
    orders = payload if isinstance(payload, list) else (payload.get("orders") if isinstance(payload, dict) else None)
    if not isinstance(orders, list):
        return []
    out: list[OpenOrder] = []
    for raw in orders:
        if not isinstance(raw, dict):
            continue
        qty = _optional_decimal(raw.get("origQty") or raw.get("quantity"))
        if qty is None:
            continue
        stop = _optional_decimal(raw.get("stopPrice"))
        if stop == 0:
            stop = None
        out.append(
            OpenOrder(
                id=str(raw.get("orderId") or ""),
                coid=str(raw.get("clientOrderId") or ""),
                pair=str(raw.get("symbol") or ""),
                side=str(raw.get("side") or "").lower(),
                qty=qty,
                stop_price=stop,
            )
        )
    return out


def _parse_fills(payload: Any) -> list[Fill]:
    fills = payload if isinstance(payload, list) else (payload.get("fills") if isinstance(payload, dict) else None)
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
                id=str(raw.get("id") or raw.get("tradeId") or ""),
                coid=str(raw.get("orderId") or ""),
                pair=str(raw.get("symbol") or ""),
                side="buy" if raw.get("isBuyer") is True else "sell",
                qty=qty,
                price=price,
                ts_ms=int(raw.get("time") or 0),
            )
        )
    return out
