"""Bitget spot venue adapter (paper).

Docs: https://www.bitget.com/api-doc/spot/intro

Bitget is an OctoBot spot tentacle and a top-25 spot venue. This adapter is
paper-only. Canonical pack and public symbol are ``BTCUSDT``.

Auth is HMAC-SHA256 of ``timestamp + METHOD + requestPath + body``,
base64-encoded, sent as ``ACCESS-SIGN``. The constructor field that holds
the key id is ``key_id``. The passphrase is ``sign_tag`` and is sent as
``ACCESS-PASSPHRASE``; it is not part of the prehash.

A market buy sends ``size`` as quote spend. A market sell sends base
``size``. There is a documented spot TP/SL order (``tpslType=tpsl``), so
the protective stop is a market sell whose ``triggerPrice`` is the stop.
``clientOid`` for that stop ends in ``-stop``.

``urllib`` and ``krellbot.tls`` are imported inside ``HttpTransport`` so
importing this module stays offline.

Paper taker is pinned at 10 bps. Bitget's fee schedule lists VIP 0 spot
maker and taker at 0.10%. The 2026-08-20 trading-fees FAQ states the same
standard spot taker. The BGB payment discount is not applied. The public
symbols payload publishes ``takerFeeRate`` ``0.002``; that field is not
the VIP 0 schedule rate and is not used. Futures fees are not used.
"""

from __future__ import annotations

import base64
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

API_HOST = "api.bitget.com"
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
        # Imported here so `import krellbot.venues.bitget` stays offline.
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.bitget` stays offline.
        import urllib.request

        from krellbot.tls import urlopen

        data = None
        hdrs = dict(headers)
        if body is not None:
            data = canonical_json(body).encode("utf-8")
            hdrs = {**hdrs, "content-type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def canonical_json(body: dict) -> str:
    """Stable JSON for the Bitget signature and the POST body."""
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def sign_bitget(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """Base64 HMAC-SHA256 of the Bitget v2 prehash string."""
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _quantize(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _default_now_ms() -> int:
    return int(time.time() * 1000)


class BitgetVenue:
    """Bitget spot adapter for balances / open orders / place+cancel seams.

    ``transport`` is mandatory. Passing ``None`` raises before any secret is
    registered and before any network call. Order mutations call
    ``release_policy.ensure_live_orders_allowed`` first.
    """

    def __init__(
        self,
        key_id: str,
        api_secret: str,
        sign_tag: str,
        transport: Transport,
        *,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        if transport is None:
            raise ValueError("bitget: transport is required; None must not silently call the network")
        sanitize.register_secret(key_id, api_secret, sign_tag)
        self._key_id = key_id
        self._api_secret = api_secret
        self._sign_tag = sign_tag
        self._transport = transport
        self._now_fn = now_fn or _default_now_ms
        self._recent: dict[str, OrderRef] = {}

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("bitget rules require a pair")
        payload = self._public_get("/api/v2/spot/public/symbols", {"symbol": pair})
        row = _symbol_row(payload, pair)
        if str(row.get("status") or "").lower() != "online":
            raise ValueError(f"bitget symbol {pair} is not online")
        quantity_precision = _precision(row.get("quantityPrecision"))
        price_precision = _precision(row.get("pricePrecision"))
        step = Decimal(1).scaleb(-quantity_precision)
        min_trade = _optional_decimal(row.get("minTradeAmount"))
        ordermin = min_trade if min_trade is not None and min_trade > 0 else step
        min_notional = _optional_decimal(row.get("minTradeUSDT"))
        if min_notional is None or min_notional <= 0:
            min_notional = Decimal(1)
        return PairRules(
            ordermin=ordermin,
            costmin=min_notional,
            lot_decimals=quantity_precision,
            price_decimals=price_precision,
        )

    def snapshot(self) -> Truth:
        balances = self._signed_get("/api/v2/spot/account/assets")
        orders = self._signed_get("/api/v2/spot/trade/unfilled-orders", {"symbol": PACK_PAIR})
        stops = self._signed_get(
            "/api/v2/spot/trade/unfilled-orders",
            {"symbol": PACK_PAIR, "tpslType": "tpsl"},
        )
        fills = self._signed_get("/api/v2/spot/trade/fills", {"symbol": PACK_PAIR, "limit": "50"})
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
            raise ValueError("bitget entry requires a pair")
        rules = self.rules(pair)
        lot = _quantize(qty, rules.lot_decimals)
        price_stop = _quantize(stop, rules.price_decimals)
        ask = self._ask(pair)
        if ask is None or ask <= 0:
            raise RuntimeError("bitget ticker has no ask")
        notional = lot * ask
        if lot <= 0 or lot < rules.ordermin or notional < rules.costmin:
            raise ValueError("bitget entry is below the symbol minimum")
        quote = _quantize(notional, QUOTE_DECIMALS)
        entry = self._signed_post(
            "/api/v2/spot/trade/place-order",
            {
                "clientOid": coid,
                "orderType": "market",
                "side": "buy",
                "size": _plain(quote),
                "symbol": pair,
            },
        )
        if not _accepted(entry):
            raise RuntimeError("bitget entry was not accepted; stop not placed")
        stop_resp = self._signed_post(
            "/api/v2/spot/trade/place-order",
            {
                "clientOid": f"{coid}{STOP_SUFFIX}",
                "orderType": "market",
                "side": "sell",
                "size": _plain(lot),
                "symbol": pair,
                "tpslType": "tpsl",
                "triggerPrice": _plain(price_stop),
            },
        )
        if not _accepted(stop_resp):
            raise RuntimeError("bitget stop was not accepted")
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
            raise ValueError("bitget exit requires a pair")
        rules = self.rules(pair)
        lot = _quantize(qty, rules.lot_decimals)
        if lot <= 0 or lot < rules.ordermin:
            raise ValueError("bitget exit is below the symbol minimum")
        resp = self._signed_post(
            "/api/v2/spot/trade/place-order",
            {
                "clientOid": coid,
                "orderType": "market",
                "side": "sell",
                "size": _plain(lot),
                "symbol": pair,
            },
        )
        if not _accepted(resp):
            raise RuntimeError("bitget exit was not accepted")
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
            self._signed_post(
                "/api/v2/spot/trade/cancel-order",
                {"clientOid": order.coid, "orderId": order.id, "symbol": pair, "tpslType": "tpsl"},
            )

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        release_policy.ensure_live_orders_allowed()
        rules = self.rules(pair)
        price = _quantize(new_stop, rules.price_decimals)
        for order in self._stop_orders(pair):
            self._signed_post(
                "/api/v2/spot/trade/cancel-order",
                {"clientOid": order.coid, "orderId": order.id, "symbol": pair, "tpslType": "tpsl"},
            )
            self._signed_post(
                "/api/v2/spot/trade/place-order",
                {
                    "clientOid": f"{order.coid}-raised",
                    "orderType": "market",
                    "side": "sell",
                    "size": _plain(order.qty),
                    "symbol": pair,
                    "tpslType": "tpsl",
                    "triggerPrice": _plain(price),
                },
            )

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        orders = _parse_orders(self._signed_get("/api/v2/spot/trade/unfilled-orders", {"symbol": PACK_PAIR}))
        stops = _parse_orders(
            self._signed_get("/api/v2/spot/trade/unfilled-orders", {"symbol": PACK_PAIR, "tpslType": "tpsl"})
        )
        return next((order for order in [*orders, *stops] if order.coid == coid), None)

    def check_key(self) -> KeyPerms:
        payload = self._signed_get("/api/v2/spot/account/info")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("authorities"), list):
            raise TypeError("bitget account info did not include authorities")
        authorities = data["authorities"]
        names = {str(item).lower() for item in authorities}
        if "withdraw" in names:
            raise WithdrawCapableError("bitget key can withdraw; trade-only keys refused")
        return KeyPerms(can_trade="trade" in names or "spot_trade" in names, can_withdraw=False)

    def _ask(self, pair: str) -> Decimal | None:
        payload = self._public_get("/api/v2/spot/market/tickers", {"symbol": pair})
        row = _symbol_row(payload, pair)
        return _optional_decimal(row.get("askPr"))

    def _stop_orders(self, pair: str) -> list[OpenOrder]:
        payload = self._signed_get("/api/v2/spot/trade/unfilled-orders", {"symbol": pair, "tpslType": "tpsl"})
        return [order for order in _parse_orders(payload) if order.pair == pair]

    def _public_get(self, path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        return _unwrap(self._transport.get(f"{BASE_URL}{path}?{query}", {}))

    def _signed_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = urllib.parse.urlencode(sorted((params or {}).items()))
        request_path = f"{path}?{query}" if query else path
        headers = self._auth_headers("GET", request_path, body="")
        url = f"{BASE_URL}{request_path}"
        return _unwrap(self._transport.get(url, headers))

    def _signed_post(self, path: str, body: dict) -> Any:
        encoded = canonical_json(body)
        headers = self._auth_headers("POST", path, body=encoded)
        headers["content-type"] = "application/json"
        return _unwrap(self._transport.post(f"{BASE_URL}{path}", body, headers))

    def _auth_headers(self, method: str, request_path: str, *, body: str) -> dict[str, str]:
        timestamp = str(int(self._now_fn()))
        return {
            "ACCESS-KEY": self._key_id,
            "ACCESS-PASSPHRASE": self._sign_tag,
            "ACCESS-SIGN": sign_bitget(self._api_secret, timestamp, method, request_path, body),
            "ACCESS-TIMESTAMP": timestamp,
            "locale": "en-US",
        }


def _unwrap(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise TypeError("bitget response is not a JSON object")
    if str(payload.get("code") or "") not in {"", "00000"}:
        raise RuntimeError(f"bitget request refused: {payload.get('msg') or payload.get('code')}")
    return payload


def _accepted(payload: Any) -> bool:
    return isinstance(payload, dict) and str(payload.get("code") or "") in {"", "00000"}


def _order_id(payload: Any) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and data.get("orderId") is not None:
        return str(data["orderId"])
    return ""


def _symbol_row(payload: Any, symbol: str) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("symbol") or "") == symbol:
                return item
    if isinstance(data, dict) and str(data.get("symbol") or "") == symbol:
        return data
    raise ValueError(f"bitget public response has no symbol {symbol!r}")


def _precision(raw: Any) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("bitget precision is not an integer") from exc
    if value < 0:
        raise ValueError("bitget precision is negative")
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
    return []


def _parse_balances(payload: Any) -> list[Balance]:
    balances: list[Balance] = []
    for row in _rows(payload):
        asset = str(row.get("coin") or "").upper()
        if not asset:
            continue
        free = _optional_decimal(row.get("available")) or Decimal(0)
        frozen = _optional_decimal(row.get("frozen")) or Decimal(0)
        locked = _optional_decimal(row.get("locked")) or Decimal(0)
        balances.append(Balance(asset=asset, free=free, locked=frozen + locked))
    return balances


def _parse_orders(payload: Any) -> list[OpenOrder]:
    orders: list[OpenOrder] = []
    for row in _rows(payload):
        status = str(row.get("status") or "live").lower()
        if status not in {"live", "partially_filled", "new"}:
            continue
        side = str(row.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            continue
        qty = _optional_decimal(row.get("size")) or Decimal(0)
        orders.append(
            OpenOrder(
                id=str(row.get("orderId") or ""),
                coid=str(row.get("clientOid") or ""),
                pair=str(row.get("symbol") or ""),
                side=side,
                qty=qty,
                stop_price=_optional_decimal(row.get("triggerPrice")),
            )
        )
    return orders


def _parse_fills(payload: Any) -> list[Fill]:
    fills: list[Fill] = []
    for row in _rows(payload):
        side = str(row.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            continue
        qty = _optional_decimal(row.get("size"))
        price = _optional_decimal(row.get("priceAvg"))
        if qty is None or price is None:
            continue
        try:
            ts_ms = int(str(row.get("cTime") or "0"))
        except ValueError:
            ts_ms = 0
        fills.append(
            Fill(
                id=str(row.get("tradeId") or ""),
                coid=str(row.get("orderId") or ""),
                pair=str(row.get("symbol") or ""),
                side=side,
                qty=qty,
                price=price,
                ts_ms=ts_ms,
            )
        )
    return fills
