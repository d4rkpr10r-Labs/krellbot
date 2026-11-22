"""Deepcoin spot venue adapter (paper).

Docs:
- https://www.deepcoin.com/docs/DeepCoinTrade/order
- https://www.deepcoin.com/docs/DeepCoinTrade/triggerOrder
- https://www.deepcoin.com/docs/DeepCoinAccount/getAccountBalance
- https://api.deepcoin.com/deepcoin/market/instruments

Auth: HMAC-SHA256 of ``timestamp + METHOD + requestPath + body``, base64,
sent as ``DC-ACCESS-SIGN``. The key id is header ``DC-ACCESS-KEY`` and is
not part of the prehash. The passphrase is header ``DC-ACCESS-PASSPHRASE``
and is not part of the prehash. Timestamp is ISO-8601 UTC with milliseconds.
Query parameters are part of ``requestPath``.

``urllib`` and ``krellbot.tls`` are imported inside ``HttpTransport`` so
importing this module stays offline.

Canonical pack pair is ``BTC-USDT`` (Deepcoin ``instId``). The venue
documents spot trigger orders. Live mutation stays disabled until entry
fills and owned trigger-order reconciliation are qualified. Paper runs use
the simulated PaperVenue and its protective stops.

Paper taker is pinned at 10 bps. The public spot schedule lists a 0.10%
taker, and the 2025-11-26 fee notice keeps BTC/USDT at 0.10%. Futures
fees are not used.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
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

API_HOST = "api.deepcoin.com"
BASE_URL = f"https://{API_HOST}"
# Public instruments omit a quote minimum. Paper uses 1 quote unit.
PAPER_MIN_NOTIONAL = Decimal(1)


class Transport(Protocol):
    """Read-only transport with explicit request headers."""

    def get(self, url: str, headers: dict[str, str]) -> dict: ...


class HttpTransport:
    """Real urllib GET transport. Tests must inject a fake."""

    def get(self, url: str, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.deepcoin` stays offline.
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def sign_deepcoin(secret: str, timestamp: str, method: str, request_path: str, body: str) -> str:
    """Base64 HMAC-SHA256 of ``timestamp + METHOD + requestPath + body``."""
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _refuse_order_change() -> NoReturn:
    release_policy.ensure_live_orders_allowed()
    raise NotImplementedError("deepcoin: spot order mutations are disabled pending live qualification")


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


def _iso_millis(ms: int) -> str:
    seconds, millis = divmod(int(ms), 1000)
    base = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{millis:03d}Z"


def _default_now_ms() -> int:
    return int(time.time() * 1000)


class DeepcoinVenue:
    """Read-only Deepcoin spot adapter for rules, balances, orders, and fills.

    An injected transport is required. Order mutations fail closed even
    when the global release permits live trading.
    """

    def __init__(
        self,
        key_id: str,
        api_secret: str,
        passphrase: str,
        transport: Transport,
        *,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        if transport is None:
            raise ValueError("deepcoin: transport is required; None must not silently call the network")
        sanitize.register_secret(key_id, api_secret, passphrase)
        self._key_id = key_id
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._transport = transport
        self._now_fn = now_fn or _default_now_ms

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("deepcoin rules require a pair")
        payload = self._public_get(
            "/deepcoin/market/instruments",
            {"instType": "SPOT", "instId": pair},
        )
        row = _instrument_row(payload, pair)
        if str(row.get("state") or "").lower() != "live":
            raise ValueError(f"deepcoin symbol {pair} is not trading")
        lot = str(row.get("lotSz") or "1")
        tick = str(row.get("tickSz") or "1")
        min_qty = Decimal(str(row.get("minSz") or "0"))
        return PairRules(
            ordermin=min_qty,
            costmin=PAPER_MIN_NOTIONAL,
            lot_decimals=_decimals(lot),
            price_decimals=_decimals(tick),
        )

    def snapshot(self) -> Truth:
        balances = self._signed_get("/deepcoin/account/balances", {"instType": "SPOT"})
        orders = self._pending_orders()
        fills = self._signed_get("/deepcoin/trade/fills", {"instType": "SPOT"})
        return Truth(
            balances=_parse_balances(balances),
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
        return next((order for order in self._pending_orders() if order.coid == coid), None)

    def check_key(self) -> KeyPerms:
        raise NotImplementedError("deepcoin: trade-only key permissions are not qualified")

    def _pending_orders(self) -> list[OpenOrder]:
        return _parse_orders(self._signed_get("/deepcoin/trade/orders-pending", {"instType": "SPOT"}))

    def _headers(self, timestamp: str, signature: str) -> dict[str, str]:
        return {
            "DC-ACCESS-KEY": self._key_id,
            "DC-ACCESS-SIGN": signature,
            "DC-ACCESS-TIMESTAMP": timestamp,
            "DC-ACCESS-PASSPHRASE": self._passphrase,
        }

    def _public_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        request_path = _request_path(path, params)
        return self._transport.get(f"{BASE_URL}{request_path}", {})

    def _signed_get(self, path: str, params: dict[str, str] | None = None) -> dict:
        request_path = _request_path(path, params)
        timestamp = _iso_millis(self._now_fn())
        signature = sign_deepcoin(self._api_secret, timestamp, "GET", request_path, "")
        result = self._transport.get(f"{BASE_URL}{request_path}", self._headers(timestamp, signature))
        if not isinstance(result, dict) or str(result.get("code")) != "0" or not isinstance(result.get("data"), list):
            raise RuntimeError("deepcoin: account data request failed")
        return result


def _request_path(path: str, params: dict[str, str] | None) -> str:
    if not params:
        return path
    return f"{path}?{urllib.parse.urlencode(params)}"


def _instrument_row(payload: Any, pair: str) -> dict:
    if not isinstance(payload, dict):
        raise TypeError("deepcoin instruments body is not a JSON object")
    if str(payload.get("code") or "") not in {"", "0"}:
        raise ValueError("deepcoin instruments request was refused")
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("instId") or "") == pair:
                return item
    if str(payload.get("instId") or "") == pair:
        return payload
    raise ValueError(f"deepcoin instruments has no instId {pair!r}")


def _parse_balances(payload: Any) -> list[Balance]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    out: list[Balance] = []
    for raw in data:
        if not isinstance(raw, dict) or "ccy" not in raw:
            continue
        free = _optional_decimal(raw.get("availBal"))
        locked = _optional_decimal(raw.get("frozenBal")) or Decimal(0)
        if free is None or (free == 0 and locked == 0):
            continue
        out.append(Balance(asset=str(raw.get("ccy") or ""), free=free, locked=locked))
    return out


def _parse_orders(payload: Any) -> list[OpenOrder]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    out: list[OpenOrder] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        qty = _optional_decimal(raw.get("sz"))
        if qty is None:
            continue
        out.append(
            OpenOrder(
                id=str(raw.get("ordId") or ""),
                coid=str(raw.get("clOrdId") or ""),
                pair=str(raw.get("instId") or ""),
                side=str(raw.get("side") or "").lower(),
                qty=qty,
                stop_price=None,
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
        qty = _optional_decimal(raw.get("fillSz"))
        price = _optional_decimal(raw.get("fillPx"))
        if qty is None or price is None:
            continue
        out.append(
            Fill(
                id=str(raw.get("tradeId") or ""),
                coid=str(raw.get("clOrdId") or raw.get("ordId") or ""),
                pair=str(raw.get("instId") or ""),
                side=str(raw.get("side") or "").lower(),
                qty=qty,
                price=price,
                ts_ms=int(raw.get("ts") or 0),
            )
        )
    return out
