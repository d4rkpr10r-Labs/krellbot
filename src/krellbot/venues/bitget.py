"""Bitget spot venue adapter (paper).

Docs: https://www.bitget.com/api-doc/spot/intro

Bitget is an OctoBot spot tentacle and a top-25 spot venue. This adapter is
paper-only. Canonical pack and public symbol are ``BTCUSDT``.

Auth is HMAC-SHA256 of ``timestamp + METHOD + requestPath + body``,
base64-encoded, sent as ``ACCESS-SIGN``. The constructor field that holds
the key id is ``key_id``. The passphrase is ``sign_tag`` and is sent as
``ACCESS-PASSPHRASE``; it is not part of the prehash.

Bitget documents spot TP/SL orders. Live order mutations stay disabled
pending fill and order ownership qualification. PaperVenue simulates
protective stops.

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
from decimal import Decimal, InvalidOperation
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

API_HOST = "api.bitget.com"
BASE_URL = f"https://{API_HOST}"
PACK_PAIR = "BTCUSDT"
_SPOT_TRADE_AUTHORITY = "stow"
_SPOT_TRADE_ONLY_AUTHORITIES = frozenset(
    {"coor", "cpor", "stor", "smor", "ttor", "wtor", "taxr", "chor", "p2pr", "pllr", _SPOT_TRADE_AUTHORITY}
)


class Transport(Protocol):
    """Read-only transport with explicit request headers."""

    def get(self, url: str, headers: dict[str, str]) -> dict: ...


class HttpTransport:
    """Real urllib GET transport. Tests must inject a fake."""

    def get(self, url: str, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.bitget` stays offline.
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def sign_bitget(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """Base64 HMAC-SHA256 of the Bitget v2 prehash string."""
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _refuse_order_change() -> NoReturn:
    release_policy.ensure_live_orders_allowed()
    raise NotImplementedError("bitget: spot order mutations are disabled pending live qualification")


def _default_now_ms() -> int:
    return int(time.time() * 1000)


class BitgetVenue:
    """Read-only Bitget spot adapter behind an explicitly supplied transport.

    Order mutations fail closed even when the release permits live trading.
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
        try:
            min_trade = Decimal(str(row.get("minTradeAmount", "0")))
            min_notional = Decimal(str(row.get("minTradeUSDT")))
        except InvalidOperation as exc:
            raise ValueError("bitget symbol minimum is malformed") from exc
        if not min_trade.is_finite() or not min_notional.is_finite() or min_trade < 0 or min_notional <= 0:
            raise ValueError("bitget symbol minimum is invalid")
        return PairRules(
            ordermin=max(step, min_trade),
            costmin=min_notional,
            lot_decimals=quantity_precision,
            price_decimals=price_precision,
        )

    def snapshot(self) -> Truth:
        balances = _parse_balances(self._signed_get("/api/v2/spot/account/assets"))
        orders = self._pending_orders()
        fills = _parse_fills(self._signed_get("/api/v2/spot/trade/fills", {"symbol": PACK_PAIR, "limit": "50"}))
        return Truth(balances=balances, open_orders=orders, recent_fills=fills)

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        _refuse_order_change()

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        _refuse_order_change()

    def cancel_stops(self, pair: str) -> None:
        _refuse_order_change()

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        _refuse_order_change()

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        if not coid:
            return None
        return next((order for order in self._pending_orders() if order.coid == coid), None)

    def check_key(self) -> KeyPerms:
        data = self._signed_get("/api/v2/spot/account/info").get("data")
        authorities = data.get("authorities") if isinstance(data, dict) else None
        if not isinstance(authorities, list) or not all(isinstance(item, str) for item in authorities):
            raise TypeError("bitget account info did not include authorities")
        names = {item.lower() for item in authorities}
        if names & {"wwow", "withdraw"}:
            raise WithdrawCapableError("bitget key can withdraw; trade-only keys refused")
        if names - _SPOT_TRADE_ONLY_AUTHORITIES:
            raise TypeError("bitget key permissions are not limited to spot trading and reads")
        return KeyPerms(can_trade=_SPOT_TRADE_AUTHORITY in names, can_withdraw=False)

    def _pending_orders(self) -> list[OpenOrder]:
        path = "/api/v2/spot/trade/unfilled-orders"
        orders = _parse_orders(self._signed_get(path, {"symbol": PACK_PAIR}))
        conditional_orders = _parse_orders(self._signed_get(path, {"symbol": PACK_PAIR, "tpslType": "tpsl"}))
        return [*orders, *conditional_orders]

    def _public_get(self, path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        return _unwrap(self._transport.get(f"{BASE_URL}{path}?{query}", {}))

    def _signed_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = urllib.parse.urlencode(sorted((params or {}).items()))
        request_path = f"{path}?{query}" if query else path
        timestamp = str(int(self._now_fn()))
        headers = {
            "ACCESS-KEY": self._key_id,
            "ACCESS-PASSPHRASE": self._sign_tag,
            "ACCESS-SIGN": sign_bitget(self._api_secret, timestamp, "GET", request_path),
            "ACCESS-TIMESTAMP": timestamp,
            "locale": "en-US",
        }
        return _unwrap(self._transport.get(f"{BASE_URL}{request_path}", headers))


def _unwrap(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise TypeError("bitget response is not a JSON object")
    if payload.get("code") != "00000":
        raise RuntimeError("bitget request refused")
    return payload


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
    if not 0 <= value <= 28:
        raise ValueError("bitget precision is out of range")
    return value


def _optional_decimal(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
        return value if value.is_finite() else None
    except (ValueError, ArithmeticError, TypeError):
        return None


def _rows(payload: Any) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise TypeError("bitget account response data is not a list")
    return [item for item in data if isinstance(item, dict)]


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
                pair=str(row.get("symbol") or "").upper(),
                side=side,
                qty=qty,
                # The generic TP/SL trigger does not identify a protective stop.
                stop_price=None,
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
                pair=str(row.get("symbol") or "").upper(),
                side=side,
                qty=qty,
                price=price,
                ts_ms=ts_ms,
            )
        )
    return fills
