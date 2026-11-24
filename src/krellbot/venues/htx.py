"""HTX spot venue adapter (paper).

Docs: https://www.htx.com/en-us/opend/newApiPages/

HTX is an OctoBot spot tentacle and a top-25 spot venue. ``huobi`` is the
legacy alias of this same family, not a second venue id. This adapter is
paper-only. The pack pair is ``BTCUSDT``. The public symbol is ``btcusdt``.

Auth is Huobi Signature Version 2: base64 HMAC-SHA256 of
``METHOD\\nhost\\npath\\nsorted-query``, sent as the ``Signature`` query
parameter. The key id is ``AccessKeyId``. The API secret is the HMAC key.
The request body is not part of the prehash.

A market buy sends ``amount`` as quote spend. The protective stop is a
conditional market sell (``POST /v2/algo-orders``) whose ``stopPrice`` is
the stop and whose ``clientOrderId`` ends in ``-stop``.

``urllib`` and ``krellbot.tls`` are imported inside ``HttpTransport`` so
importing this module stays offline.

Paper taker is pinned at 20 bps. HTX's public fee page lists Prime0 spot
maker and taker at 0.2000%. The 25% HTX-token discount is not applied.
Futures fees are not used.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
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

API_HOST = "api.htx.com"
BASE_URL = f"https://{API_HOST}"
STOP_SUFFIX = "-stop"
PACK_PAIR = "BTCUSDT"
QUOTE_DECIMALS = 8


class Transport(Protocol):
    """Anything with get/post returning parsed JSON dicts."""

    def get(self, url: str, headers: dict[str, str]) -> dict: ...

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict: ...


class HttpTransport:
    """Real urllib transport. Tests must not call this directly."""

    def get(self, url: str, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.htx` stays offline.
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.htx` stays offline.
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


def sign_htx(api_secret: str, method: str, host: str, path: str, params: dict[str, str]) -> str:
    """Authenticate the canonical request using HTX Signature Version 2."""
    encoded = urllib.parse.urlencode(sorted(params.items()))
    prehash = f"{method.upper()}\n{host}\n{path}\n{encoded}"
    # HTX requires a keyed HMAC over the request. The API secret is the
    # key, and this digest is a request authenticator rather than a password hash.
    # codeql[py/weak-sensitive-data-hashing]
    digest = hmac.new(api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _venue_symbol(pair: str) -> str:
    return pair.replace("-", "").lower()


def _quantize(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _default_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class HtxVenue:
    """HTX spot adapter for balances / open orders / place+cancel seams.

    ``transport`` is mandatory. Passing ``None`` raises before any signing
    material is registered and before any network call. Order mutations call
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
            raise ValueError("htx: transport is required; None must not silently call the network")
        sanitize.register_secret(key_id, api_secret)
        self._key_id = key_id
        self._api_secret = api_secret
        self._transport = transport
        self._now_fn = now_fn or _default_timestamp
        self._recent: dict[str, OrderRef] = {}
        self._account_id: str | None = None

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("htx rules require a pair")
        symbol = _venue_symbol(pair)
        payload = self._public_get("/v1/settings/common/market-symbols", {"symbols": symbol})
        row = _symbol_row(payload, symbol)
        if str(row.get("state") or "").lower() != "online":
            raise ValueError(f"htx symbol {symbol} is not online")
        quantity_precision = _precision(row.get("ap"))
        price_precision = _precision(row.get("pp"))
        step = Decimal(1).scaleb(-quantity_precision)
        min_trade = _optional_decimal(row.get("minoa"))
        ordermin = min_trade if min_trade is not None and min_trade > 0 else step
        min_notional = _optional_decimal(row.get("minov"))
        if min_notional is None or min_notional <= 0:
            min_notional = Decimal(1)
        return PairRules(
            ordermin=ordermin,
            costmin=min_notional,
            lot_decimals=quantity_precision,
            price_decimals=price_precision,
        )

    def snapshot(self) -> Truth:
        account_id = self._spot_account_id()
        balances = self._signed_get(f"/v1/account/accounts/{account_id}/balance")
        orders = self._signed_get(
            "/v1/order/openOrders",
            {"account-id": account_id, "symbol": _venue_symbol(PACK_PAIR)},
        )
        stops = self._signed_get("/v2/algo-orders/opening", {"symbol": _venue_symbol(PACK_PAIR)})
        fills = self._signed_get("/v1/order/matchresults", {"symbol": _venue_symbol(PACK_PAIR)})
        return Truth(
            balances=_parse_balances(balances),
            open_orders=[*_parse_orders(orders), *_parse_algo(stops)],
            recent_fills=_parse_fills(fills),
        )

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        release_policy.ensure_live_orders_allowed()
        if coid in self._recent:
            return self._recent[coid]
        if not pair:
            raise ValueError("htx entry requires a pair")
        rules = self.rules(pair)
        lot = _quantize(qty, rules.lot_decimals)
        price_stop = _quantize(stop, rules.price_decimals)
        ask = self._ask(pair)
        if ask is None or ask <= 0:
            raise RuntimeError("htx ticker has no ask")
        notional = lot * ask
        if lot <= 0 or lot < rules.ordermin or notional < rules.costmin:
            raise ValueError("htx entry is below the symbol minimum")
        symbol = _venue_symbol(pair)
        account_id = self._spot_account_id()
        quote = _quantize(notional, QUOTE_DECIMALS)
        entry = self._signed_post(
            "/v1/order/orders/place",
            {
                "account-id": account_id,
                "amount": _plain(quote),
                "client-order-id": coid,
                "source": "spot-api",
                "symbol": symbol,
                "type": "buy-market",
            },
        )
        if not _v1_ok(entry):
            raise RuntimeError("htx entry was not accepted; stop not placed")
        stop_resp = self._signed_post(
            "/v2/algo-orders",
            {
                "accountId": account_id,
                "clientOrderId": f"{coid}{STOP_SUFFIX}",
                "orderSide": "sell",
                "orderSize": _plain(lot),
                "orderType": "market",
                "stopPrice": _plain(price_stop),
                "symbol": symbol,
                "timeInForce": "ioc",
            },
        )
        if not _v2_ok(stop_resp):
            raise RuntimeError("htx stop was not accepted")
        ref = OrderRef(
            id=_v1_order_id(entry),
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
            raise ValueError("htx exit requires a pair")
        rules = self.rules(pair)
        lot = _quantize(qty, rules.lot_decimals)
        if lot <= 0 or lot < rules.ordermin:
            raise ValueError("htx exit is below the symbol minimum")
        resp = self._signed_post(
            "/v1/order/orders/place",
            {
                "account-id": self._spot_account_id(),
                "amount": _plain(lot),
                "client-order-id": coid,
                "source": "spot-api",
                "symbol": _venue_symbol(pair),
                "type": "sell-market",
            },
        )
        if not _v1_ok(resp):
            raise RuntimeError("htx exit was not accepted")
        return OrderRef(
            id=_v1_order_id(resp),
            coid=coid,
            pair=pair,
            side="sell",
            qty=lot,
            filled_qty=lot,
            stop_price=None,
        )

    def cancel_stops(self, pair: str) -> None:
        release_policy.ensure_live_orders_allowed()
        client_ids = [order.coid for order in self._stop_orders(pair) if order.coid]
        if client_ids:
            self._signed_post("/v2/algo-orders/cancellation", {"clientOrderIds": client_ids})

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        release_policy.ensure_live_orders_allowed()
        rules = self.rules(pair)
        price = _quantize(new_stop, rules.price_decimals)
        symbol = _venue_symbol(pair)
        account_id = self._spot_account_id()
        for order in self._stop_orders(pair):
            if order.coid:
                self._signed_post("/v2/algo-orders/cancellation", {"clientOrderIds": [order.coid]})
            self._signed_post(
                "/v2/algo-orders",
                {
                    "accountId": account_id,
                    "clientOrderId": f"{order.coid}-raised",
                    "orderSide": "sell",
                    "orderSize": _plain(order.qty),
                    "orderType": "market",
                    "stopPrice": _plain(price),
                    "symbol": symbol,
                    "timeInForce": "ioc",
                },
            )

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        orders = _parse_orders(
            self._signed_get(
                "/v1/order/openOrders",
                {"account-id": self._spot_account_id(), "symbol": _venue_symbol(PACK_PAIR)},
            )
        )
        stops = _parse_algo(self._signed_get("/v2/algo-orders/opening", {"symbol": _venue_symbol(PACK_PAIR)}))
        return next((order for order in [*orders, *stops] if order.coid == coid), None)

    def check_key(self) -> KeyPerms:
        payload = self._signed_get("/v2/user/api-key", {"accessKey": self._key_id})
        names = _permission_tokens(payload, self._key_id)
        if "withdraw" in names:
            raise WithdrawCapableError("htx key can withdraw; trade-only keys refused")
        return KeyPerms(can_trade="trade" in names, can_withdraw=False)

    def _ask(self, pair: str) -> Decimal | None:
        payload = self._public_get("/market/detail/merged", {"symbol": _venue_symbol(pair)})
        tick = payload.get("tick") if isinstance(payload, dict) else None
        if not isinstance(tick, dict):
            return None
        ask = tick.get("ask")
        if isinstance(ask, list) and ask:
            return _optional_decimal(ask[0])
        return _optional_decimal(ask)

    def _stop_orders(self, pair: str) -> list[OpenOrder]:
        symbol = _venue_symbol(pair)
        payload = self._signed_get("/v2/algo-orders/opening", {"symbol": symbol})
        return [order for order in _parse_algo(payload) if order.pair == symbol]

    def _spot_account_id(self) -> str:
        if self._account_id:
            return self._account_id
        payload = self._signed_get("/v1/account/accounts")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise TypeError("htx accounts response did not include a list")
        for row in data:
            if not isinstance(row, dict):
                continue
            if str(row.get("type") or "") == "spot" and str(row.get("state") or "") == "working":
                self._account_id = str(row.get("id") or "")
                break
        if not self._account_id:
            raise RuntimeError("htx spot account was not found")
        return self._account_id

    def _public_get(self, path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        payload = self._transport.get(f"{BASE_URL}{path}?{query}", {})
        if not isinstance(payload, dict) or str(payload.get("status") or "") != "ok":
            raise RuntimeError("htx public request was refused")
        return payload

    def _signed_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = self._signed_url("GET", path, params or {})
        return self._transport.get(url, {})

    def _signed_post(self, path: str, body: dict) -> Any:
        url = self._signed_url("POST", path, {})
        return self._transport.post(url, body, {"content-type": "application/json"})

    def _signed_url(self, method: str, path: str, params: dict[str, str]) -> str:
        query = {key: str(value) for key, value in params.items()}
        query.update(
            {
                "AccessKeyId": self._key_id,
                "SignatureMethod": "HmacSHA256",
                "SignatureVersion": "2",
                "Timestamp": self._now_fn(),
            }
        )
        query["Signature"] = sign_htx(self._api_secret, method, API_HOST, path, query)
        return f"{BASE_URL}{path}?{urllib.parse.urlencode(sorted(query.items()))}"


def _v1_ok(payload: Any) -> bool:
    return isinstance(payload, dict) and str(payload.get("status") or "") == "ok"


def _v2_ok(payload: Any) -> bool:
    return isinstance(payload, dict) and str(payload.get("code") or "") == "200"


def _v1_order_id(payload: Any) -> str:
    if (
        isinstance(payload, dict)
        and payload.get("data") is not None
        and not isinstance(payload.get("data"), (dict, list))
    ):
        return str(payload["data"])
    return ""


def _symbol_row(payload: Any, symbol: str) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("symbol") or item.get("sc") or "") == symbol:
                return item
    raise ValueError(f"htx public response has no symbol {symbol!r}")


def _precision(raw: Any) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("htx precision is not an integer") from exc
    if value < 0:
        raise ValueError("htx precision is negative")
    return value


def _optional_decimal(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (ValueError, ArithmeticError, TypeError):
        return None


def _rows(payload: Any) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        nested = data.get("list") or data.get("orders")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    return []


def _parse_balances(payload: Any) -> list[Balance]:
    grouped: dict[str, dict[str, Decimal]] = {}
    for row in _rows(payload):
        asset = str(row.get("currency") or "").upper()
        if not asset:
            continue
        bucket = grouped.setdefault(asset, {"free": Decimal(0), "locked": Decimal(0)})
        amount = _optional_decimal(row.get("balance")) or Decimal(0)
        if str(row.get("type") or "") == "trade":
            bucket["free"] += amount
        else:
            bucket["locked"] += amount
    return [Balance(asset=asset, free=parts["free"], locked=parts["locked"]) for asset, parts in grouped.items()]


def _parse_orders(payload: Any) -> list[OpenOrder]:
    orders: list[OpenOrder] = []
    for row in _rows(payload):
        state = str(row.get("state") or "submitted").lower()
        if state not in {"submitted", "partial-filled", "created"}:
            continue
        kind = str(row.get("type") or "")
        side = kind.split("-", 1)[0].lower()
        if side not in {"buy", "sell"}:
            continue
        orders.append(
            OpenOrder(
                id=str(row.get("id") or ""),
                coid=str(row.get("client-order-id") or ""),
                pair=str(row.get("symbol") or ""),
                side=side,
                qty=_optional_decimal(row.get("amount")) or Decimal(0),
                stop_price=_optional_decimal(row.get("stop-price")),
            )
        )
    return orders


def _parse_algo(payload: Any) -> list[OpenOrder]:
    orders: list[OpenOrder] = []
    for row in _rows(payload):
        side = str(row.get("orderSide") or "").lower()
        if side not in {"buy", "sell"}:
            continue
        orders.append(
            OpenOrder(
                id=str(row.get("orderId") or ""),
                coid=str(row.get("clientOrderId") or ""),
                pair=str(row.get("symbol") or ""),
                side=side,
                qty=_optional_decimal(row.get("orderSize")) or Decimal(0),
                stop_price=_optional_decimal(row.get("stopPrice")),
            )
        )
    return orders


def _parse_fills(payload: Any) -> list[Fill]:
    fills: list[Fill] = []
    for row in _rows(payload):
        kind = str(row.get("type") or "")
        side = kind.split("-", 1)[0].lower()
        if side not in {"buy", "sell"}:
            continue
        qty = _optional_decimal(row.get("filled-amount"))
        price = _optional_decimal(row.get("price"))
        if qty is None or price is None:
            continue
        try:
            ts_ms = int(str(row.get("created-at") or "0"))
        except ValueError:
            ts_ms = 0
        fills.append(
            Fill(
                id=str(row.get("id") or ""),
                coid=str(row.get("order-id") or ""),
                pair=str(row.get("symbol") or ""),
                side=side,
                qty=qty,
                price=price,
                ts_ms=ts_ms,
            )
        )
    return fills


def _permission_tokens(payload: Any, key_id: str) -> set[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        raise TypeError("htx api key did not include permission")
    chosen = rows[0]
    for row in rows:
        if str(row.get("accessKey") or "") == key_id:
            chosen = row
            break
    raw = chosen.get("permission")
    if not isinstance(raw, str) or not raw.strip():
        raise TypeError("htx api key did not include permission")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}
