"""LBank spot venue adapter (paper).

Docs: https://www.lbank.com/docs/

LBank is an OctoBot spot tentacle and a top-25 spot venue. This adapter is
paper-only.

Auth is HmacSHA256. The signed string is the uppercase MD5 hex of the
parameter string (every field except ``sign``, including ``api_key``,
``signature_method``, ``timestamp``, and ``echostr``, sorted by name).
The HMAC is lowercase hex. The constructor field that holds the key id is
``key_id``. The wire field stays ``api_key`` because that is the documented
parameter name.

The documented spot order types are limit and market (plus maker, IOC, and
FOK). There is no placeable stop, so a protective stop is a limit sell at
``stop * (1 - 0.005)`` whose ``custom_id`` ends in ``-stop``. A market buy
sends ``price`` as the quote amount. A market sell sends base ``amount``.

``urllib`` and ``krellbot.tls`` are imported inside ``HttpTransport`` so
importing this module stays offline.

Canonical pack pair is ``BTC-USDT``. The public symbol is ``btc_usdt``.

Paper taker is pinned at 10 bps. LBank's support article on spot fee
vouchers states that BTC/USDT's original maker and taker rate is 0.10%.
Futures fees are not used.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
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

API_HOST = "api.lbank.info"
BASE_URL = f"https://{API_HOST}"
STOP_BUFFER = Decimal("0.005")
PACK_PAIR = "BTC-USDT"
WIRE_SYMBOL = "btc_usdt"


class Transport(Protocol):
    """Anything with get/post returning parsed JSON dicts."""

    def get(self, url: str, headers: dict[str, str]) -> dict: ...

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict: ...


class HttpTransport:
    """Real urllib transport. Tests must not call this directly."""

    def get(self, url: str, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.lbank` stays offline.
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.lbank` stays offline.
        import urllib.request

        from krellbot.tls import urlopen

        data = None
        hdrs = dict(headers)
        if body is not None:
            data = urllib.parse.urlencode(body).encode("utf-8")
            hdrs.setdefault("content-type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def sign_lbank(secret: str, params: dict[str, str]) -> str:
    """Lowercase hex HMAC-SHA256 of the uppercase MD5 of sorted params.

    ``params`` must not include ``sign``.
    """
    prepared = urllib.parse.urlencode(sorted((key, value) for key, value in params.items()))
    digest = hashlib.md5(prepared.encode("utf-8")).hexdigest().upper()
    return hmac.new(secret.encode("utf-8"), digest.encode("utf-8"), hashlib.sha256).hexdigest()


def wire_symbol(pair: str) -> str:
    """Map a pack pair (``BTC-USDT``) to LBank's public symbol (``btc_usdt``)."""
    return pair.replace("-", "_").lower()


def pack_pair(symbol: str) -> str:
    """Map ``btc_usdt`` back to the pack pair ``BTC-USDT``."""
    if "_" not in symbol:
        return symbol.upper()
    base, quote = symbol.split("_", 1)
    return f"{base.upper()}-{quote.upper()}"


def _quantize_qty(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def _quantize_price(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def _default_echostr() -> str:
    return secrets.token_hex(16)


class LbankVenue:
    """LBank spot adapter for balances, open orders, and place/cancel.

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
        echo_fn: Callable[[], str] | None = None,
    ) -> None:
        if transport is None:
            raise ValueError("lbank: transport is required; None must not silently call the network")
        sanitize.register_secret(key_id, api_secret)
        self._key_id = key_id
        self._api_secret = api_secret
        self._transport = transport
        self._now_fn = now_fn or _default_now_ms
        self._echo_fn = echo_fn or _default_echostr
        self._recent: dict[str, OrderRef] = {}

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("lbank rules require a pair")
        symbol = wire_symbol(pair)
        payload = self._public_get("/v2/accuracy.do", {"symbol": symbol})
        row = _accuracy_row(payload, symbol)
        lot_decimals = int(str(row.get("quantityAccuracy") or "0"))
        price_decimals = int(str(row.get("priceAccuracy") or "0"))
        min_qty = Decimal(str(row.get("minTranQua") or "0"))
        min_notional = Decimal(str(row.get("minOrderAmount") or "0"))
        return PairRules(
            ordermin=min_qty,
            costmin=min_notional,
            lot_decimals=lot_decimals,
            price_decimals=price_decimals,
        )

    def snapshot(self) -> Truth:
        account = self._signed_post("/v2/supplement/user_info_account.do", {})
        orders = self._signed_post(
            "/v2/supplement/orders_info_no_deal.do",
            {"symbol": WIRE_SYMBOL, "current_page": "1", "page_length": "200"},
        )
        fills = self._signed_post(
            "/v2/supplement/transaction_history.do",
            {"symbol": WIRE_SYMBOL, "limit": "100"},
        )
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
            raise ValueError("lbank entry requires a pair")
        rules = self.rules(pair)
        lot = _quantize_qty(qty, rules.lot_decimals)
        price_stop = _quantize_price(stop, rules.price_decimals)
        ask = self._ask(wire_symbol(pair))
        if ask is None:
            raise RuntimeError("lbank book ticker did not provide an ask")
        _reject_size(lot, rules, ask)
        quote_spend = _quantize_price(lot * ask, rules.price_decimals)
        symbol = wire_symbol(pair)
        entry = self._signed_post(
            "/v2/supplement/create_order.do",
            {
                "symbol": symbol,
                "type": "buy_market",
                "price": format(quote_spend, "f"),
                "custom_id": coid,
            },
        )
        if not _accepted(entry):
            raise RuntimeError("lbank entry was not accepted; stop not placed")
        limit_price = _quantize_price(price_stop * (Decimal(1) - STOP_BUFFER), rules.price_decimals)
        stop_resp = self._signed_post(
            "/v2/supplement/create_order.do",
            {
                "symbol": symbol,
                "type": "sell",
                "price": format(limit_price, "f"),
                "amount": format(lot, "f"),
                "custom_id": f"{coid}-stop",
            },
        )
        if not _accepted(stop_resp):
            raise RuntimeError("lbank stop was not accepted")
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
            raise ValueError("lbank exit requires a pair")
        rules = self.rules(pair)
        lot = _quantize_qty(qty, rules.lot_decimals)
        _reject_size(lot, rules, None)
        resp = self._signed_post(
            "/v2/supplement/create_order.do",
            {
                "symbol": wire_symbol(pair),
                "type": "sell_market",
                "amount": format(lot, "f"),
                "custom_id": coid,
            },
        )
        if not _accepted(resp):
            raise RuntimeError("lbank exit was not accepted")
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
                "/v2/supplement/cancel_order.do",
                {"symbol": wire_symbol(pair), "orderId": order.id},
            )

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        release_policy.ensure_live_orders_allowed()
        rules = self.rules(pair)
        price = _quantize_price(new_stop, rules.price_decimals)
        limit_price = _quantize_price(price * (Decimal(1) - STOP_BUFFER), rules.price_decimals)
        symbol = wire_symbol(pair)
        for order in self._stop_orders(pair):
            self._signed_post(
                "/v2/supplement/cancel_order.do",
                {"symbol": symbol, "orderId": order.id},
            )
            self._signed_post(
                "/v2/supplement/create_order.do",
                {
                    "symbol": symbol,
                    "type": "sell",
                    "price": format(limit_price, "f"),
                    "amount": format(order.qty, "f"),
                    "custom_id": order.coid if order.coid.endswith("-stop") else f"{order.coid}-stop",
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
        payload = self._signed_post("/v2/supplement/user_info_account.do", {})
        if not _accepted(payload):
            raise TypeError("lbank account response missing")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise TypeError("lbank account response missing")
        if data.get("canWithdraw") is True:
            raise WithdrawCapableError("lbank key can withdraw; trade-only keys refused")
        return KeyPerms(can_trade=data.get("canTrade") is not False, can_withdraw=False)

    def _stop_orders(self, pair: str) -> list[OpenOrder]:
        payload = self._signed_post(
            "/v2/supplement/orders_info_no_deal.do",
            {"symbol": wire_symbol(pair), "current_page": "1", "page_length": "200"},
        )
        return [order for order in _parse_orders(payload) if order.pair == pair and order.coid.endswith("-stop")]

    def _ask(self, symbol: str) -> Decimal | None:
        payload = self._public_get("/v2/supplement/ticker/bookTicker.do", {"symbol": symbol})
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            raw = data.get("askPrice")
            if raw not in (None, ""):
                return Decimal(str(raw))
        return None

    def _public_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"{BASE_URL}{path}" if not query else f"{BASE_URL}{path}?{query}"
        return self._transport.get(url, {})

    def _signed_post(self, path: str, extra: dict[str, str]) -> Any:
        echostr = self._echo_fn()
        if not echostr.isalnum() or not 30 <= len(echostr) <= 40:
            raise ValueError("lbank echostr must be 30 to 40 letters or digits")
        params = {
            "api_key": self._key_id,
            "signature_method": "HmacSHA256",
            "timestamp": str(self._now_fn()),
            "echostr": echostr,
            **extra,
        }
        signature = sign_lbank(self._api_secret, params)
        body = {**params, "sign": signature}
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "timestamp": params["timestamp"],
            "signature_method": params["signature_method"],
            "echostr": echostr,
        }
        return self._transport.post(f"{BASE_URL}{path}", body, headers)


def _accuracy_row(payload: Any, symbol: str) -> dict:
    if not isinstance(payload, dict):
        raise TypeError("lbank accuracy body is not a JSON object")
    if not _accepted(payload):
        raise ValueError("lbank accuracy request was refused")
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("symbol") or "") == symbol:
                return item
    raise ValueError(f"lbank accuracy has no symbol {symbol!r}")


def _reject_size(qty: Decimal, rules: PairRules, price: Decimal | None) -> None:
    if qty < rules.ordermin:
        raise ValueError("qty below ordermin")
    if price is not None and qty * price < rules.costmin:
        raise ValueError("notional below costmin")


def _accepted(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    result = payload.get("result")
    if result not in (True, "true", "True"):
        return False
    code = payload.get("error_code")
    return code in (None, 0, "0")


def _order_id(payload: Any) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        return str(data.get("order_id") or data.get("orderId") or "")
    return ""


def _parse_balances(payload: Any) -> list[Balance]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("balances") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[Balance] = []
    for raw in rows:
        if not isinstance(raw, dict) or "asset" not in raw:
            continue
        free = Decimal(str(raw.get("free") or "0"))
        locked = Decimal(str(raw.get("locked") or "0"))
        if free == 0 and locked == 0:
            continue
        out.append(Balance(asset=str(raw["asset"]).upper(), free=free, locked=locked))
    return out


def _parse_orders(payload: Any) -> list[OpenOrder]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("orders") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[OpenOrder] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        status = raw.get("status")
        if status not in (0, 1, "0", "1"):
            continue
        kind = str(raw.get("type") or "")
        side = "buy" if kind.startswith("buy") else "sell"
        coid = str(raw.get("clientOrderId") or raw.get("custom_id") or "")
        price = Decimal(str(raw.get("price") or "0"))
        out.append(
            OpenOrder(
                id=str(raw.get("orderId") or ""),
                coid=coid,
                pair=pack_pair(str(raw.get("symbol") or "")),
                side=side,
                qty=Decimal(str(raw.get("origQty") or "0")),
                stop_price=price if coid.endswith("-stop") else None,
            )
        )
    return out


def _parse_fills(payload: Any) -> list[Fill]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    out: list[Fill] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        out.append(
            Fill(
                id=str(raw.get("id") or ""),
                coid=str(raw.get("orderId") or ""),
                pair=pack_pair(str(raw.get("symbol") or "")),
                side="buy" if raw.get("isBuyer") is True else "sell",
                qty=Decimal(str(raw.get("qty") or "0")),
                price=Decimal(str(raw.get("price") or "0")),
                ts_ms=int(raw.get("time") or 0),
            )
        )
    return out
