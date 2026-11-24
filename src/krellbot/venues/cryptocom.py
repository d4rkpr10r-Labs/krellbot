"""Crypto.com Exchange spot venue adapter (paper).

Crypto.com Exchange is an OctoBot spot tentacle and a top-25 spot venue.
This adapter is paper-only. The pack pair is ``BTC-USDT``. The public
symbol is ``BTC_USDT``.

The public host is ``https://api.crypto.com/exchange/v1``. Market metadata
is ``GET /public/get-instruments``. The minimum quote notional is
``GET /public/get-risk-parameters`` (``min_order_notional_usd`` for the
base currency). The ask is the first book level from
``GET /public/get-book``.

Private calls are JSON POSTs. The hex HMAC-SHA256 signature is
``method + id + key id + sorted parameter string + nonce``. The parameter
string concatenates each key with its value and has no separators. The
secret is the HMAC key and is not sent. There is no passphrase.

A market buy sends ``notional`` as quote spend. A market sell and the
protective stop send ``quantity`` as base size. The stop is
``private/advanced/create-order`` with ``type`` ``STOP_LOSS`` and
``ref_price`` as the trigger. Cancelling that stop uses
``private/advanced/cancel-order``.

``urllib`` and ``krellbot.tls`` are imported inside ``HttpTransport`` so
importing this module stays offline.

Paper taker is pinned at 50 bps. The Exchange fee schedule lists Level 1
spot taker at 0.500% without a CRO balance. The CRO-balance discount is
not applied. Derivatives fees are not used.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
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

BASE_URL = "https://api.crypto.com/exchange/v1"
PACK_PAIR = "BTC-USDT"
STOP_SUFFIX = "-stop"


class Transport(Protocol):
    """Anything with get/post returning parsed JSON dicts."""

    def get(self, url: str, headers: dict[str, str]) -> dict: ...

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict: ...


class HttpTransport:
    """Real urllib transport. Tests must not call this directly."""

    def get(self, url: str, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.cryptocom` stays offline.
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.cryptocom` stays offline.
        import urllib.request

        from krellbot.tls import urlopen

        data = None
        hdrs = dict(headers)
        if body is not None:
            data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
            hdrs = {**hdrs, "content-type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def params_to_str(obj: object, level: int = 0) -> str:
    """Crypto.com parameter string: sorted keys concatenated with their values."""
    if level >= 3 or not isinstance(obj, dict):
        return str(obj)
    rendered = ""
    for key in sorted(obj):
        rendered += key
        value = obj[key]
        if value is None:
            rendered += "null"
        elif isinstance(value, list):
            for item in value:
                rendered += params_to_str(item, level + 1) if isinstance(item, dict) else str(item)
        else:
            rendered += str(value)
    return rendered


def sign_cryptocom(secret: str, method: str, req_id: int, key_id: str, params: dict, nonce: str) -> str:
    """Hex HMAC-SHA256 of ``method + id + key id + params + nonce``."""
    payload = f"{method}{req_id}{key_id}{params_to_str(params)}{nonce}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _venue_symbol(pair: str) -> str:
    return pair.replace("-", "_").upper()


def _quantize(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _default_nonce() -> str:
    return str(int(time.time() * 1000))


class CryptocomVenue:
    """Crypto.com Exchange spot adapter.

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
        now_fn: Callable[[], str] | None = None,
    ) -> None:
        if transport is None:
            raise ValueError("cryptocom: transport is required; None must not silently call the network")
        sanitize.register_secret(key_id, api_secret)
        self._key_id = key_id
        self._api_secret = api_secret
        self._transport = transport
        self._now_fn = now_fn or _default_nonce
        self._recent: dict[str, OrderRef] = {}
        self._next_id = 1

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("cryptocom rules require a pair")
        symbol = _venue_symbol(pair)
        row = _instrument_row(self._public_get("/public/get-instruments"), symbol)
        if row.get("tradable") is not True or str(row.get("inst_type") or "") != "CCY_PAIR":
            raise ValueError(f"cryptocom symbol {symbol} is not a tradable spot pair")
        quantity_precision = _precision(row.get("quantity_decimals"))
        price_precision = _precision(row.get("quote_decimals"))
        step = _optional_decimal(row.get("qty_tick_size"))
        if step is None or step <= 0:
            raise ValueError("cryptocom quantity tick is missing")
        min_notional = _min_notional(self._public_get("/public/get-risk-parameters"), str(row.get("base_ccy") or ""))
        return PairRules(
            ordermin=step,
            costmin=min_notional,
            lot_decimals=quantity_precision,
            price_decimals=price_precision,
        )

    def snapshot(self) -> Truth:
        balances = self._private("private/user-balance", {})
        orders = self._private("private/get-open-orders", {"instrument_name": _venue_symbol(PACK_PAIR)})
        stops = self._private("private/advanced/get-open-orders", {"instrument_name": _venue_symbol(PACK_PAIR)})
        fills = self._private("private/get-trades", {"instrument_name": _venue_symbol(PACK_PAIR), "limit": 100})
        return Truth(
            balances=_parse_balances(balances),
            open_orders=[*_parse_orders(orders), *_parse_orders(stops)],
            recent_fills=_parse_fills(fills),
        )

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        release_policy.ensure_live_orders_allowed()
        if coid in self._recent:
            return self._recent[coid]
        if not pair:
            raise ValueError("cryptocom entry requires a pair")
        rules = self.rules(pair)
        lot = _quantize(qty, rules.lot_decimals)
        price_stop = _quantize(stop, rules.price_decimals)
        ask = self._ask(pair)
        if ask is None or ask <= 0:
            raise RuntimeError("cryptocom book has no ask")
        notional = lot * ask
        if lot <= 0 or lot < rules.ordermin or notional < rules.costmin:
            raise ValueError("cryptocom entry is below the symbol minimum")
        symbol = _venue_symbol(pair)
        quote = _quantize(notional, rules.price_decimals)
        entry = self._private(
            "private/create-order",
            {
                "client_oid": coid,
                "instrument_name": symbol,
                "notional": _plain(quote),
                "side": "BUY",
                "spot_margin": "SPOT",
                "type": "MARKET",
            },
        )
        if not _accepted(entry):
            raise RuntimeError("cryptocom entry was not accepted; stop not placed")
        stop_resp = self._private(
            "private/advanced/create-order",
            {
                "client_oid": f"{coid}{STOP_SUFFIX}",
                "instrument_name": symbol,
                "quantity": _plain(lot),
                "ref_price": _plain(price_stop),
                "side": "SELL",
                "type": "STOP_LOSS",
            },
        )
        if not _accepted(stop_resp):
            raise RuntimeError("cryptocom stop was not accepted")
        ref = OrderRef(
            id=_order_id(entry),
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
            raise ValueError("cryptocom exit requires a pair")
        rules = self.rules(pair)
        lot = _quantize(qty, rules.lot_decimals)
        if lot <= 0 or lot < rules.ordermin:
            raise ValueError("cryptocom exit is below the symbol minimum")
        resp = self._private(
            "private/create-order",
            {
                "client_oid": coid,
                "instrument_name": _venue_symbol(pair),
                "quantity": _plain(lot),
                "side": "SELL",
                "spot_margin": "SPOT",
                "type": "MARKET",
            },
        )
        if not _accepted(resp):
            raise RuntimeError("cryptocom exit was not accepted")
        return OrderRef(
            id=_order_id(resp),
            coid=coid,
            pair=pair,
            side="sell",
            qty=lot,
            filled_qty=lot,
            stop_price=None,
        )

    def cancel_stops(self, pair: str) -> None:
        release_policy.ensure_live_orders_allowed()
        for order in self._stop_orders(pair):
            if order.coid:
                self._private("private/advanced/cancel-order", {"client_oid": order.coid})

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        release_policy.ensure_live_orders_allowed()
        rules = self.rules(pair)
        price = _quantize(new_stop, rules.price_decimals)
        symbol = _venue_symbol(pair)
        for order in self._stop_orders(pair):
            if order.coid:
                self._private("private/advanced/cancel-order", {"client_oid": order.coid})
            self._private(
                "private/advanced/create-order",
                {
                    "client_oid": f"{order.coid}-raised",
                    "instrument_name": symbol,
                    "quantity": _plain(order.qty),
                    "ref_price": _plain(price),
                    "side": "SELL",
                    "type": "STOP_LOSS",
                },
            )

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        orders = _parse_orders(self._private("private/get-open-orders", {"instrument_name": _venue_symbol(PACK_PAIR)}))
        stops = _parse_orders(
            self._private("private/advanced/get-open-orders", {"instrument_name": _venue_symbol(PACK_PAIR)})
        )
        return next((order for order in [*orders, *stops] if order.coid == coid), None)

    def check_key(self) -> KeyPerms:
        payload = self._private("private/get-accounts", {})
        names = _permission_tokens(payload)
        if any("withdraw" in name for name in names):
            raise WithdrawCapableError("cryptocom key can withdraw; trade-only keys refused")
        return KeyPerms(can_trade=any("trade" in name for name in names), can_withdraw=False)

    def _ask(self, pair: str) -> Decimal | None:
        symbol = _venue_symbol(pair)
        payload = self._public_get(f"/public/get-book?instrument_name={symbol}&depth=5")
        result = payload.get("result") if isinstance(payload, dict) else None
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return None
        asks = data[0].get("asks")
        if isinstance(asks, list) and asks and isinstance(asks[0], list) and asks[0]:
            return _optional_decimal(asks[0][0])
        return None

    def _stop_orders(self, pair: str) -> list[OpenOrder]:
        symbol = _venue_symbol(pair)
        payload = self._private("private/advanced/get-open-orders", {"instrument_name": symbol})
        return [order for order in _parse_orders(payload) if order.pair == symbol]

    def _public_get(self, path: str) -> Any:
        payload = self._transport.get(f"{BASE_URL}{path}", {})
        if not _accepted(payload):
            raise RuntimeError("cryptocom public request was refused")
        return payload

    def _private(self, method: str, params: dict[str, object]) -> Any:
        req_id = self._next_id
        self._next_id += 1
        nonce = self._now_fn()
        body: dict[str, object] = {
            "id": req_id,
            "method": method,
            "api_key": self._key_id,
            "params": params,
            "nonce": int(nonce) if str(nonce).isdigit() else nonce,
        }
        body["sig"] = sign_cryptocom(self._api_secret, method, req_id, self._key_id, params, str(nonce))
        return self._transport.post(
            f"{BASE_URL}/{method}",
            body,
            {"content-type": "application/json"},
        )


def _accepted(payload: Any) -> bool:
    return isinstance(payload, dict) and str(payload.get("code")) == "0"


def _order_id(payload: Any) -> str:
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict) and result.get("order_id") not in (None, ""):
        return str(result["order_id"])
    return ""


def _instrument_row(payload: Any, symbol: str) -> dict:
    result = payload.get("result") if isinstance(payload, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("symbol") or "") == symbol:
                return item
    raise ValueError(f"cryptocom public response has no symbol {symbol!r}")


def _min_notional(payload: Any, base: str) -> Decimal:
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = result.get("base_currency_config") if isinstance(result, dict) else None
    if isinstance(rows, list):
        for item in rows:
            if isinstance(item, dict) and str(item.get("instrument_name") or "") == base.upper():
                amount = _optional_decimal(item.get("min_order_notional_usd"))
                if amount is not None and amount > 0:
                    return amount
    raise ValueError(f"cryptocom risk parameters have no minimum for {base!r}")


def _precision(raw: Any) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("cryptocom precision is not an integer") from exc
    if value < 0:
        raise ValueError("cryptocom precision is negative")
    return value


def _optional_decimal(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (ValueError, ArithmeticError, TypeError):
        return None


def _rows(payload: Any) -> list[dict]:
    result = payload.get("result") if isinstance(payload, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _parse_balances(payload: Any) -> list[Balance]:
    balances: list[Balance] = []
    for wallet in _rows(payload):
        positions = wallet.get("position_balances")
        if not isinstance(positions, list):
            continue
        for row in positions:
            if not isinstance(row, dict):
                continue
            asset = str(row.get("instrument_name") or "").upper()
            quantity = _optional_decimal(row.get("quantity")) or Decimal(0)
            reserved = _optional_decimal(row.get("reserved_qty")) or Decimal(0)
            if asset:
                balances.append(Balance(asset=asset, free=quantity - reserved, locked=reserved))
    return balances


def _parse_orders(payload: Any) -> list[OpenOrder]:
    orders: list[OpenOrder] = []
    for row in _rows(payload):
        if str(row.get("status") or "ACTIVE").upper() not in {"ACTIVE", "NEW"}:
            continue
        side = str(row.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            continue
        orders.append(
            OpenOrder(
                id=str(row.get("order_id") or ""),
                coid=str(row.get("client_oid") or ""),
                pair=str(row.get("instrument_name") or ""),
                side=side,
                qty=_optional_decimal(row.get("quantity")) or Decimal(0),
                stop_price=_optional_decimal(row.get("ref_price")),
            )
        )
    return orders


def _parse_fills(payload: Any) -> list[Fill]:
    fills: list[Fill] = []
    for row in _rows(payload):
        side = str(row.get("side") or "").lower()
        qty = _optional_decimal(row.get("traded_quantity"))
        price = _optional_decimal(row.get("traded_price"))
        if side not in {"buy", "sell"} or qty is None or price is None:
            continue
        try:
            ts_ms = int(str(row.get("create_time") or "0"))
        except ValueError:
            ts_ms = 0
        fills.append(
            Fill(
                id=str(row.get("trade_id") or ""),
                coid=str(row.get("client_oid") or ""),
                pair=str(row.get("instrument_name") or ""),
                side=side,
                qty=qty,
                price=price,
                ts_ms=ts_ms,
            )
        )
    return fills


def _permission_tokens(payload: Any) -> set[str]:
    result = payload.get("result") if isinstance(payload, dict) else None
    raw: Any = None
    if isinstance(result, dict):
        raw = result.get("permissions")
        account = result.get("this_account")
        if raw is None and isinstance(account, dict):
            raw = account.get("permissions")
    if isinstance(raw, str):
        tokens = {part.strip().lower() for part in raw.split(",") if part.strip()}
    elif isinstance(raw, list):
        tokens = {str(part).strip().lower() for part in raw if str(part).strip()}
    else:
        raise TypeError("cryptocom api key did not include permission")
    if not tokens:
        raise TypeError("cryptocom api key did not include permission")
    return tokens
