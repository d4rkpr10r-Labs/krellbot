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

The documented spot order types are limit, market, maker, IOC, and FOK.
Live order mutations are disabled. PaperVenue simulates protective stops.

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

API_HOST = "api.lbank.info"
BASE_URL = f"https://{API_HOST}"
PACK_PAIR = "BTC-USDT"
WIRE_SYMBOL = "btc_usdt"
_ACCOUNT_PATH = "/v2/supplement/user_info_account.do"
_PENDING_ORDERS_PATH = "/v2/supplement/orders_info_no_deal.do"
_FILLS_PATH = "/v2/supplement/transaction_history.do"
_KEY_PERMISSIONS_PATH = "/v2/supplement/api_Restrictions.do"


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


def _refuse_order_change() -> NoReturn:
    release_policy.ensure_live_orders_allowed()
    raise NotImplementedError("lbank: spot order mutations are disabled pending live qualification")


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def _default_echostr() -> str:
    return secrets.token_hex(16)


class LbankVenue:
    """Read-only LBank spot adapter behind an explicitly supplied transport.

    Signed account queries use the exchange's POST read endpoints.
    Order mutations fail closed even in a live-capable release.
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
        balances = _parse_balances(self._signed_read(_ACCOUNT_PATH, {}))
        orders = self._pending_orders()
        fills = _parse_fills(self._signed_read(_FILLS_PATH, {"symbol": WIRE_SYMBOL, "limit": "100"}))
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
        return next((order for order in self._pending_orders() if order.coid == coid), None)

    def check_key(self) -> KeyPerms:
        data = self._signed_read(_KEY_PERMISSIONS_PATH, {})
        if not isinstance(data, dict):
            raise TypeError("lbank API key permissions response is incomplete")
        can_withdraw = data.get("enableWithdrawals")
        if can_withdraw is True:
            raise WithdrawCapableError("lbank key can withdraw; trade-only keys refused")
        can_trade = data.get("enableSpotTrading")
        if can_withdraw is not False or not isinstance(can_trade, bool):
            raise TypeError("lbank API key permissions response is incomplete")
        return KeyPerms(can_trade=can_trade, can_withdraw=False)

    def _pending_orders(self) -> list[OpenOrder]:
        data = self._signed_read(
            _PENDING_ORDERS_PATH,
            {"symbol": WIRE_SYMBOL, "current_page": "1", "page_length": "200"},
        )
        return _parse_orders(data)

    def _public_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"{BASE_URL}{path}" if not query else f"{BASE_URL}{path}?{query}"
        return self._transport.get(url, {})

    def _signed_read(self, path: str, extra: dict[str, str]) -> Any:
        if path not in {_ACCOUNT_PATH, _PENDING_ORDERS_PATH, _FILLS_PATH, _KEY_PERMISSIONS_PATH}:
            raise ValueError("lbank: unsupported account query")
        echostr = self._echo_fn()
        if not echostr.isalnum() or not 30 <= len(echostr) <= 40:
            raise ValueError("lbank echostr must be 30 to 40 letters or digits")
        params = {"api_key": self._key_id, **extra}
        auth = {"timestamp": str(self._now_fn()), "signature_method": "HmacSHA256", "echostr": echostr}
        signature = sign_lbank(self._api_secret, {**params, **auth})
        body = {**params, "sign": signature}
        headers = {"content-type": "application/x-www-form-urlencoded", **auth}
        response = self._transport.post(f"{BASE_URL}{path}", body, headers)
        if not _accepted(response):
            raise RuntimeError("lbank: account data request failed")
        return response.get("data")


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


def _accepted(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    result = payload.get("result")
    if result not in (True, "true", "True"):
        return False
    code = payload.get("error_code")
    return code in (None, 0, "0")


def _parse_balances(data: Any) -> list[Balance]:
    rows = data.get("balances") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise TypeError("lbank: malformed account balances")
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


def _parse_orders(data: Any) -> list[OpenOrder]:
    rows = data.get("orders") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise TypeError("lbank: malformed pending orders")
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
        out.append(
            OpenOrder(
                id=str(raw.get("orderId") or ""),
                coid=coid,
                pair=pack_pair(str(raw.get("symbol") or "")),
                side=side,
                qty=Decimal(str(raw.get("origQty") or "0")),
                stop_price=None,
            )
        )
    return out


def _parse_fills(data: Any) -> list[Fill]:
    if not isinstance(data, list):
        raise TypeError("lbank: malformed transaction history")
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
