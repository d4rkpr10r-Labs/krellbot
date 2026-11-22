"""BTCC spot venue adapter (paper).

BTCC is a top-25 spot venue. It is not an OctoBot tentacle. This adapter
is paper-only. The pack pair and the public symbol are both ``BTCUSDT``.

The live spot host is ``https://www.btcc.com``. Public market data is
``GET /btcc_trade/market/list`` and ``GET /btcc_trade/market/depth``.
Private calls send the key id in the ``token`` header and an MD5 hex
``authorization`` header. The prehash is the lexicographically sorted
``key=value`` pairs of the request fields plus the exchange field
``secret_key``. The hashed value is named ``sign_tag``. The ``tm`` header
is not part of that prehash.

A market buy (``side`` 1) sends ``amount`` as quote spend. A market sell
and the protective stop (``side`` 2) send ``amount`` as base quantity.
The stop is ``POST /btcc_trade/order/stop_market`` with ``stop_price``.
Cancelling a stop uses the venue's ``/btcc_trade/order/cancle_stop`` path.

``urllib`` and ``krellbot.tls`` are imported inside ``HttpTransport`` so
importing this module stays offline.

Paper taker is pinned at 30 bps. BTCC's public spot schedule lists taker
at 0.30% for every user, and the public BTCUSDT market record publishes
``taker_fee`` ``0.0030``. Futures VIP fees are not used.
"""

from __future__ import annotations

import hashlib
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

BASE_URL = "https://www.btcc.com"
PACK_PAIR = "BTCUSDT"
QUOTE_DECIMALS = 4
CLIENT_ID = "3"
WEB_SOURCE = "1"


class Transport(Protocol):
    """Anything with get/post returning parsed JSON dicts."""

    def get(self, url: str, headers: dict[str, str]) -> dict: ...

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict: ...


class HttpTransport:
    """Real urllib transport. Tests must not call this directly."""

    def get(self, url: str, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.btcc` stays offline.
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, url: str, body: dict | None, headers: dict[str, str]) -> dict:
        # Imported here so `import krellbot.venues.btcc` stays offline.
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


def sign_btcc(sign_tag: str, fields: dict[str, object]) -> str:
    """MD5 hex of the sorted BTCC ``key=value`` prehash.

    BTCC requires MD5 of a prehash that includes the exchange field
    ``secret_key``. The hashed value is named ``sign_tag``, not
    ``api_key``, ``password``, ``secret``, or ``passphrase``.
    """
    parts = [f"secret_key={sign_tag}"]
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    parts.sort()
    return hashlib.md5("&".join(parts).encode("utf-8")).hexdigest()


def _venue_symbol(pair: str) -> str:
    return pair.replace("-", "").replace("_", "").upper()


def _quantize(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _default_timestamp() -> str:
    return str(int(time.time() * 1000))


class BtccVenue:
    """BTCC spot adapter for balances / open orders / place+cancel seams.

    ``transport`` is mandatory. Passing ``None`` raises before any signing
    material is registered and before any network call. Order mutations call
    ``release_policy.ensure_live_orders_allowed`` first.

    ``sign_tag`` is the value BTCC hashes into ``secret_key``.
    """

    def __init__(
        self,
        key_id: str,
        sign_tag: str,
        transport: Transport,
        *,
        now_fn: Callable[[], str] | None = None,
    ) -> None:
        if transport is None:
            raise ValueError("btcc: transport is required; None must not silently call the network")
        sanitize.register_secret(key_id, sign_tag)
        self._key_id = key_id
        self._sign_tag = sign_tag
        self._transport = transport
        self._now_fn = now_fn or _default_timestamp
        self._recent: dict[str, OrderRef] = {}

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("btcc rules require a pair")
        symbol = _venue_symbol(pair)
        payload = self._public_get("/btcc_trade/market/list", {})
        row = _market_row(payload, symbol)
        if row.get("switch") is not True:
            raise ValueError(f"btcc symbol {symbol} is not online")
        quantity_precision = _precision(row.get("stock_prec"))
        price_precision = _precision(row.get("money_prec"))
        step = Decimal(1).scaleb(-quantity_precision)
        min_trade = _optional_decimal(row.get("min_amount"))
        ordermin = min_trade if min_trade is not None and min_trade > 0 else step
        min_notional = _optional_decimal(row.get("min_order_value"))
        if min_notional is None or min_notional <= 0:
            min_notional = Decimal(1)
        return PairRules(
            ordermin=ordermin,
            costmin=min_notional,
            lot_decimals=quantity_precision,
            price_decimals=price_precision,
        )

    def snapshot(self) -> Truth:
        balances = self._signed_get("/btcc_trade/asset/query_order", {"appid": "GW"})
        orders = self._signed_get(
            "/btcc_trade/order/pending",
            {"side": 0, "market": PACK_PAIR, "offset": 0, "limit": 100},
        )
        stops = self._signed_get(
            "/btcc_trade/order/pending_stop",
            {"side": 0, "market": PACK_PAIR, "offset": 0, "limit": 100},
        )
        fills = self._signed_get(
            "/btcc_trade/order/deal_history",
            {"side": 0, "market": PACK_PAIR, "offset": 0, "limit": 100},
        )
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
            raise ValueError("btcc entry requires a pair")
        rules = self.rules(pair)
        lot = _quantize(qty, rules.lot_decimals)
        price_stop = _quantize(stop, rules.price_decimals)
        ask = self._ask(pair, _plain(Decimal(1).scaleb(-rules.price_decimals)))
        if ask is None or ask <= 0:
            raise RuntimeError("btcc depth has no ask")
        notional = lot * ask
        if lot <= 0 or lot < rules.ordermin or notional < rules.costmin:
            raise ValueError("btcc entry is below the symbol minimum")
        symbol = _venue_symbol(pair)
        quote = _quantize(notional, QUOTE_DECIMALS)
        entry = self._signed_post(
            "/btcc_trade/order/market",
            {
                "amount": _plain(quote),
                "client_id": CLIENT_ID,
                "market": symbol,
                "side": 1,
                "source": WEB_SOURCE,
            },
        )
        if not _accepted(entry):
            raise RuntimeError("btcc entry was not accepted; stop not placed")
        stop_resp = self._signed_post(
            "/btcc_trade/order/stop_market",
            {
                "amount": _plain(lot),
                "client_id": CLIENT_ID,
                "market": symbol,
                "side": 2,
                "source": WEB_SOURCE,
                "stop_price": _plain(price_stop),
            },
        )
        if not _accepted(stop_resp):
            raise RuntimeError("btcc stop was not accepted")
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
        self._recent[f"{coid}-stop"] = OrderRef(
            id=_order_id(stop_resp),
            coid=f"{coid}-stop",
            pair=pair,
            side="sell",
            qty=lot,
            filled_qty=Decimal(0),
            stop_price=price_stop,
        )
        return ref

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        release_policy.ensure_live_orders_allowed()
        if not pair:
            raise ValueError("btcc exit requires a pair")
        rules = self.rules(pair)
        lot = _quantize(qty, rules.lot_decimals)
        if lot <= 0 or lot < rules.ordermin:
            raise ValueError("btcc exit is below the symbol minimum")
        resp = self._signed_post(
            "/btcc_trade/order/market",
            {
                "amount": _plain(lot),
                "client_id": CLIENT_ID,
                "market": _venue_symbol(pair),
                "side": 2,
                "source": WEB_SOURCE,
            },
        )
        if not _accepted(resp):
            raise RuntimeError("btcc exit was not accepted")
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
            if order.id:
                self._signed_post(
                    "/btcc_trade/order/cancle_stop",
                    {"id": order.id, "market": _venue_symbol(pair), "source": order.id},
                )

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        release_policy.ensure_live_orders_allowed()
        rules = self.rules(pair)
        price = _quantize(new_stop, rules.price_decimals)
        symbol = _venue_symbol(pair)
        for order in self._stop_orders(pair):
            if order.id:
                self._signed_post(
                    "/btcc_trade/order/cancle_stop",
                    {"id": order.id, "market": symbol, "source": order.id},
                )
            self._signed_post(
                "/btcc_trade/order/stop_market",
                {
                    "amount": _plain(order.qty),
                    "client_id": CLIENT_ID,
                    "market": symbol,
                    "side": 2,
                    "source": WEB_SOURCE,
                    "stop_price": _plain(price),
                },
            )

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        orders = _parse_orders(
            self._signed_get(
                "/btcc_trade/order/pending",
                {"side": 0, "market": PACK_PAIR, "offset": 0, "limit": 100},
            )
        )
        stops = _parse_orders(
            self._signed_get(
                "/btcc_trade/order/pending_stop",
                {"side": 0, "market": PACK_PAIR, "offset": 0, "limit": 100},
            )
        )
        known = self._recent.get(coid)
        for order in [*orders, *stops]:
            if order.coid == coid or (known is not None and order.id and order.id == known.id):
                if order.coid:
                    return order
                return OpenOrder(
                    id=order.id,
                    coid=coid,
                    pair=order.pair,
                    side=order.side,
                    qty=order.qty,
                    stop_price=order.stop_price,
                )
        return None

    def check_key(self) -> KeyPerms:
        payload = self._signed_post("/v2/apiKey/list", {})
        authority = _authority(payload, self._key_id)
        if "1" in authority:
            raise WithdrawCapableError("btcc key can withdraw; trade-only keys refused")
        return KeyPerms(can_trade="4" in authority, can_withdraw=False)

    def _ask(self, pair: str, merge: str) -> Decimal | None:
        payload = self._public_get(
            "/btcc_trade/market/depth",
            {"market": _venue_symbol(pair), "limit": "5", "merge": merge},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        result = data.get("result") if isinstance(data, dict) else None
        asks = result.get("asks") if isinstance(result, dict) else None
        if isinstance(asks, list) and asks and isinstance(asks[0], list) and asks[0]:
            return _optional_decimal(asks[0][0])
        return None

    def _stop_orders(self, pair: str) -> list[OpenOrder]:
        symbol = _venue_symbol(pair)
        payload = self._signed_get(
            "/btcc_trade/order/pending_stop",
            {"side": 0, "market": symbol, "offset": 0, "limit": 100},
        )
        return [order for order in _parse_orders(payload) if order.pair == symbol]

    def _public_get(self, path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        url = f"{BASE_URL}{path}" if not query else f"{BASE_URL}{path}?{query}"
        payload = self._transport.get(url, {})
        if not _accepted(payload):
            raise RuntimeError("btcc public request was refused")
        return payload

    def _signed_get(self, path: str, params: dict[str, object]) -> Any:
        query = urllib.parse.urlencode(params)
        url = f"{BASE_URL}{path}?{query}" if query else f"{BASE_URL}{path}"
        return self._transport.get(url, self._headers(params))

    def _signed_post(self, path: str, body: dict[str, object]) -> Any:
        return self._transport.post(f"{BASE_URL}{path}", body, self._headers(body))

    def _headers(self, fields: dict[str, object]) -> dict[str, str]:
        return {
            "authorization": sign_btcc(self._sign_tag, fields),
            "tm": self._now_fn(),
            "token": self._key_id,
        }


def _accepted(payload: Any) -> bool:
    return isinstance(payload, dict) and str(payload.get("code")) == "0"


def _order_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("id", "order_id"):
            if data.get(key) not in (None, ""):
                return str(data[key])
    if data not in (None, "") and not isinstance(data, (dict, list)):
        return str(data)
    return ""


def _market_row(payload: Any, symbol: str) -> dict:
    for item in _records(payload):
        if str(item.get("name") or "") == symbol:
            return item
    raise ValueError(f"btcc public response has no symbol {symbol!r}")


def _records(payload: Any) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows: Any = None
    if isinstance(data, dict):
        rows = data.get("records")
        if rows is None:
            rows = data.get("result")
    elif isinstance(data, list):
        rows = data
    if not isinstance(rows, list):
        return []
    return [item for item in rows if isinstance(item, dict)]


def _precision(raw: Any) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("btcc precision is not an integer") from exc
    if value < 0:
        raise ValueError("btcc precision is negative")
    return value


def _optional_decimal(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (ValueError, ArithmeticError, TypeError):
        return None


def _side(raw: Any) -> str | None:
    text = str(raw).strip().lower()
    if text in {"1", "buy", "b"}:
        return "buy"
    if text in {"2", "sell", "s"}:
        return "sell"
    return None


def _parse_balances(payload: Any) -> list[Balance]:
    data = payload.get("data") if isinstance(payload, dict) else None
    items: list[Any] = []
    if isinstance(data, dict) and isinstance(data.get("result"), list):
        items = data["result"]
    elif isinstance(data, list):
        items = data
    balances: list[Balance] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "available" in item or "frozen" in item:
            asset = str(item.get("currency") or "").upper()
            if asset:
                balances.append(_balance(asset, item))
            continue
        for asset, detail in item.items():
            if isinstance(detail, dict):
                balances.append(_balance(str(asset).upper(), detail))
    return balances


def _balance(asset: str, detail: dict) -> Balance:
    return Balance(
        asset=asset,
        free=_optional_decimal(detail.get("available")) or Decimal(0),
        locked=_optional_decimal(detail.get("frozen")) or Decimal(0),
    )


def _parse_orders(payload: Any) -> list[OpenOrder]:
    orders: list[OpenOrder] = []
    for row in _records(payload):
        side = _side(row.get("side"))
        if side is None:
            continue
        orders.append(
            OpenOrder(
                id=str(row.get("id") or ""),
                coid=str(row.get("client_order_id") or ""),
                pair=str(row.get("market") or ""),
                side=side,
                qty=_optional_decimal(row.get("amount")) or Decimal(0),
                stop_price=_optional_decimal(row.get("stop_price")),
            )
        )
    return orders


def _parse_fills(payload: Any) -> list[Fill]:
    fills: list[Fill] = []
    for row in _records(payload):
        side = _side(row.get("side"))
        qty = _optional_decimal(row.get("amount"))
        price = _optional_decimal(row.get("price"))
        if side is None or qty is None or price is None:
            continue
        try:
            raw_ts = int(str(row.get("time") or "0"))
        except ValueError:
            raw_ts = 0
        ts_ms = raw_ts * 1000 if raw_ts < 10_000_000_000 else raw_ts
        fills.append(
            Fill(
                id=str(row.get("id") or ""),
                coid=str(row.get("order_id") or ""),
                pair=str(row.get("market") or ""),
                side=side,
                qty=qty,
                price=price,
                ts_ms=ts_ms,
            )
        )
    return fills


def _authority(payload: Any, key_id: str) -> set[str]:
    rows = _key_rows(payload)
    if not rows:
        raise TypeError("btcc api key did not include permission")
    chosen: dict | None = None
    for row in rows:
        if str(row.get("apiKey") or "") == key_id:
            chosen = row
            break
    if chosen is None and len(rows) == 1:
        chosen = rows[0]
    if chosen is None:
        raise TypeError("btcc api key did not include permission")
    raw = chosen.get("authorityStr")
    if not isinstance(raw, str) or not raw.strip():
        raise TypeError("btcc api key did not include permission")
    return {char for char in raw if char.isdigit()}


def _key_rows(payload: Any) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows: Any = data
    if isinstance(data, dict):
        rows = data.get("list") or data.get("result") or data.get("records")
    if isinstance(rows, list):
        return [item for item in rows if isinstance(item, dict)]
    if isinstance(data, dict) and ("authorityStr" in data or "apiKey" in data):
        return [data]
    return []
