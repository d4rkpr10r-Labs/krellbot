"""MEXC spot venue adapter (paper).

Docs: https://www.mexc.com/api-docs/spot-v3/introduction

MEXC is an OctoBot spot tentacle and a top-25 spot venue. This adapter is
paper-only.

Auth: HMAC-SHA256 of ``totalParams`` (the query string, or the raw body when
the query is empty). The hex digest is lowercase. ``X-MEXC-APIKEY`` carries
the key id and is not part of the prehash. The key id is not named ``api_key``.

The public BTCUSDT symbol lists order types LIMIT, MARKET, and LIMIT_MAKER.
There is no placeable stop, so a protective stop is a limit sell at
``stop * (1 - 0.005)`` whose client id ends in ``-stop``. A market buy sends
``quoteOrderQty`` (quote to spend) sized from the public book ticker. A market
sell sends base ``quantity``.

The live transport sends the signed parameter string with
``Content-Type: application/json``. MEXC rejects
``application/x-www-form-urlencoded`` (error 700013). That header does not
mean the body is a JSON object.

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
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

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

API_HOST = "api.mexc.com"
BASE_URL = f"https://{API_HOST}"
STOP_BUFFER = Decimal("0.005")
RECV_WINDOW = "5000"


class Transport(Protocol):
    """Anything with get/post/delete returning parsed JSON."""

    def get(self, url: str, headers: dict[str, str]) -> dict: ...

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict: ...

    def delete(self, url: str, headers: dict[str, str]) -> dict: ...


class HttpTransport:
    """Real urllib transport. Tests must not call this directly.

    ``urllib`` and ``krellbot.tls`` stay inside the methods so importing this
    module does not load the TLS stack.
    """

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
            hdrs = {**hdrs, "content-type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def delete(self, url: str, headers: dict[str, str]) -> dict:
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="DELETE")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def sign_mexc(secret: str, total_params: str) -> str:
    """Lowercase hex HMAC-SHA256 of Mexc ``totalParams``.

    The signed value is the query or form string. It is not named ``api_key``.
    """
    return hmac.new(secret.encode("utf-8"), total_params.encode("utf-8"), hashlib.sha256).hexdigest()


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
    return int(time.time() * 1000)


class MexcVenue:
    """Mexc spot adapter for balances, open orders, and place/cancel.

    ``transport`` is mandatory. Passing ``None`` raises before any secret is
    registered and before any network call. Order mutations call
    ``release_policy.ensure_live_orders_allowed`` first.
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
        self._recent: dict[str, OrderRef] = {}
        self._prices: dict[str, Decimal] = {}

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("mexc rules require a pair")
        payload = self._public_get("/api/v3/exchangeInfo", {"symbol": pair})
        row = _symbol_row(payload, pair)
        step = str(row.get("baseSizePrecision") or "1")
        price_decimals = _price_decimals(row)
        min_qty = Decimal(step)
        min_notional = Decimal(str(row.get("quoteAmountPrecision") or "0"))
        ask = self._ask(pair)
        if ask is not None:
            self._prices[pair] = ask
        return PairRules(
            ordermin=min_qty,
            costmin=min_notional,
            lot_decimals=_decimals(step),
            price_decimals=price_decimals,
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
            raise ValueError("mexc entry requires a pair")
        rules = self.rules(pair)
        lot = _quantize_qty(qty, rules.lot_decimals)
        price_stop = _quantize_price(stop, rules.price_decimals)
        ask = self._prices.get(pair)
        if ask is None:
            raise RuntimeError("mexc book ticker did not provide an ask")
        _reject_size(lot, rules, ask)
        quote_spend = _quantize_price(lot * ask, rules.price_decimals)
        entry = self._signed_post(
            "/api/v3/order",
            {
                "symbol": pair,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": format(quote_spend, "f"),
                "newClientOrderId": coid,
            },
        )
        if not _order_ok(entry):
            raise RuntimeError("mexc entry was not accepted; stop not placed")
        limit_price = _quantize_price(price_stop * (Decimal(1) - STOP_BUFFER), rules.price_decimals)
        stop_resp = self._signed_post(
            "/api/v3/order",
            {
                "symbol": pair,
                "side": "SELL",
                "type": "LIMIT",
                "quantity": format(lot, "f"),
                "price": format(limit_price, "f"),
                "newClientOrderId": f"{coid}-stop",
            },
        )
        if not _order_ok(stop_resp):
            raise RuntimeError("mexc stop was not accepted")
        ref = OrderRef(
            id=str(entry.get("orderId") or ""),
            coid=coid,
            pair=pair,
            side="buy",
            qty=lot,
            filled_qty=lot,
            stop_price=price_stop,
        )
        self._recent[coid] = ref
        return ref

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        release_policy.ensure_live_orders_allowed()
        if not pair:
            raise ValueError("mexc exit requires a pair")
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
            raise RuntimeError("mexc exit was not accepted")
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
        for order in self.snapshot().open_orders:
            if order.pair == pair and order.stop_price is not None:
                self._signed_delete("/api/v3/order", {"symbol": pair, "orderId": order.id})

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        release_policy.ensure_live_orders_allowed()
        rules = self.rules(pair)
        price = _quantize_price(new_stop, rules.price_decimals)
        limit_price = _quantize_price(price * (Decimal(1) - STOP_BUFFER), rules.price_decimals)
        for order in self.snapshot().open_orders:
            if order.pair != pair or order.stop_price is None:
                continue
            self._signed_delete("/api/v3/order", {"symbol": pair, "orderId": order.id})
            self._signed_post(
                "/api/v3/order",
                {
                    "symbol": pair,
                    "side": "SELL",
                    "type": "LIMIT",
                    "quantity": format(order.qty, "f"),
                    "price": format(limit_price, "f"),
                    "newClientOrderId": f"{order.coid or order.id}-raised",
                },
            )

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        ref = self._recent.get(coid)
        if ref is None:
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
        account = self._signed_get("/api/v3/account")
        if not isinstance(account, dict):
            raise TypeError("mexc account response missing")
        tokens = _permission_tokens(account.get("permissions"))
        if account.get("canWithdraw") is True or any("withdraw" in token for token in tokens):
            raise WithdrawCapableError("mexc key can withdraw; trade-only keys refused")
        return KeyPerms(can_trade=account.get("canTrade") is True, can_withdraw=False)

    def _headers(self) -> dict[str, str]:
        return {"X-MEXC-APIKEY": self._key_id}

    def _signed_params(self, params: dict[str, str] | None = None) -> dict[str, str]:
        body = dict(params or {})
        body["timestamp"] = str(int(self._now_fn()))
        body["recvWindow"] = RECV_WINDOW
        total_params = urllib.parse.urlencode(body)
        body["signature"] = sign_mexc(self._api_secret, total_params)
        return body

    def _ask(self, pair: str) -> Decimal | None:
        payload = self._public_get("/api/v3/ticker/bookTicker", {"symbol": pair})
        row = payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            row = payload["data"]
        if isinstance(payload, list):
            row = next(
                (item for item in payload if isinstance(item, dict) and str(item.get("symbol") or "") == pair),
                None,
            )
        if not isinstance(row, dict):
            return None
        return _optional_decimal(row.get("askPrice"))

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


def _permission_tokens(raw: Any) -> set[str]:
    if isinstance(raw, str):
        return {part.strip().lower() for part in raw.split(",") if part.strip()}
    if isinstance(raw, list):
        return {str(part).strip().lower() for part in raw if str(part).strip()}
    return set()


def _reject_size(qty: Decimal, rules: PairRules, price: Decimal | None) -> None:
    if qty < rules.ordermin:
        raise ValueError("qty below ordermin")
    if price is not None and qty * price < rules.costmin:
        raise ValueError("notional below costmin")


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
        stop = _optional_decimal(raw.get("stopPrice"))
        if stop == 0:
            stop = None
        if stop is None and coid.endswith(("-stop", "-raised")):
            stop = _optional_decimal(raw.get("price"))
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
