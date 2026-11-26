"""Kraken spot venue adapter.

Docs:
- https://docs.kraken.com/api/docs/rest-api/add-order
- https://docs.kraken.com/api/docs/guides/spot-rest-auth
- https://docs.kraken.com/api/docs/rest-api/get-withdrawal-methods

Auth: HMAC-SHA512 over `path + SHA256(nonce + postdata)`, key =
base64decode(secret); the signature is base64 and goes in `API-Sign`. Tests
inject the epoch-microsecond clock (`now_ms`, a historical name) that feeds
the monotonic nonces, a `sleep`, and `min_interval_ms` for the 1-per-second
private-call budget; the journal is appended only on the third rate-limit
failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Protocol

from krellbot import journal, paths, release_policy, sanitize
from krellbot.venues.base import (
    UNKNOWN_OUTCOME,
    Balance,
    Fill,
    KeyMalformedError,
    KeyPerms,
    KeyTradeOffError,
    KeyUnverifiableError,
    KrakenKeyUnknownPermissionError,
    OpenOrder,
    OrderOutcome,
    OrderRef,
    PairRules,
    Truth,
    VenueRejectedError,
    WithdrawCapableError,
)

BASE_URL = "https://api.kraken.com"
PUBLIC_PATH = "/0/public"
PRIVATE_PATH = "/0/private"
GET_API_KEY_INFO = "GetApiKeyInfo"
ADD_ORDER = "AddOrder"
QUERY_ORDERS = "QueryOrders"
# Query Orders Info takes a comma-delimited list of at most 50 txids.
QUERY_ORDERS_MAX_TXIDS = 50
# Orders followed through ``refid`` from a sell's own order towards the order
# that created it. An entry's conditional close is one hop from the entry.
MAX_REFERRAL_HOPS = 3
NS_PER_US = 1_000
US_PER_MS = 1_000
US_PER_S = 1_000_000

# `GetApiKeyInfo` permissions as documented at
# https://docs.kraken.com/api-reference/account-data/get-api-key-info.
# The endpoint requires no extra permission: it returns the calling key's
# own permission set. The engine accepts the key only when all four trade
# permissions are present and no withdrawal/address-management rights are
# granted.
REQUIRED_PERMISSIONS = frozenset({"modify-trades", "close-trades", "query-funds", "query-open-trades"})
FORBIDDEN_PERMISSIONS = frozenset(
    {
        "withdraw-funds",
        "add-funds",  # explicit deposit rights; conservative
        "earn-funds",
        "add-withdraw-address",
        "update-withdraw-address",
    }
)

# Tokens Kraken publishes in the API keys guide that grant read-only /
# ancillary access (no funds movement). Anything outside this set AND
# `REQUIRED_PERMISSIONS` AND `FORBIDDEN_PERMISSIONS` is treated as an
# unrecognized scope and the key is refused. This is the documented set
# only — not a claim of exhaustiveness.
#
# Sources:
#   * https://docs.kraken.com/api-reference/account-data/get-api-key-info
#     schema enum (response-side spellings), which is authoritative for
#     any token Kraken returns in `GetApiKeyInfo.permissions`.
#   * https://docs.kraken.com/exchange/guides/rest/api-keys (UI-side
#     permission names shown in Settings → API). Tokens from this guide
#     are included when their GetApiKeyInfo spelling is unambiguous.
#
# If Kraken later adds a new permission token that is not in this set,
# the venue refuses it (see `KrakenKeyUnknownPermissionError`); the
# allowlist is updated only after confirming the token is published in
# the API reference or the guide above.
DOCUMENTED_HARMLESS_EXTRAS = frozenset(
    {
        "query-closed-trades",
        "query-ledger",
        "export-data",
        "create-ws-token",
        "query-affiliate-participants",
    }
)

ALL_KNOWN_PERMISSIONS = REQUIRED_PERMISSIONS | FORBIDDEN_PERMISSIONS | DOCUMENTED_HARMLESS_EXTRAS


def _default_now_us() -> int:
    """Wall clock in epoch microseconds: the nonce scale Kraken has accepted."""
    return time.time_ns() // NS_PER_US


NONCE_FILENAME = "kraken.nonce"
RATE_LIMIT_MARKER = "EAPI:Rate limit exceeded"
DEFAULT_LOT_DECIMALS = 8
DEFAULT_PRICE_DECIMALS = 5


class KrakenDocumentedOutcome:
    """Closed taxonomy for documented Kraken REST response shapes (M1.6).

    Fixtures under ``tests/fixtures/venues/kraken/`` are derived from Kraken's
    published REST docs, not recorded from a real account. Each fixture maps
    to exactly one of these outcomes via ``classify_documented_response``.
    """

    ORDER_ACCEPTED = "order_accepted"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    ORDER_MINIMUM_NOT_MET = "order_minimum_not_met"
    RATE_LIMIT = "rate_limit"
    INVALID_NONCE = "invalid_nonce"
    NO_RESPONSE = "no_response"
    PARTIALLY_FILLED = "partially_filled"
    STOP_TRIGGERED = "stop_triggered"
    CANCEL_ACCEPTED = "cancel_accepted"

    _ALL = frozenset(
        {
            ORDER_ACCEPTED,
            INSUFFICIENT_FUNDS,
            ORDER_MINIMUM_NOT_MET,
            RATE_LIMIT,
            INVALID_NONCE,
            NO_RESPONSE,
            PARTIALLY_FILLED,
            STOP_TRIGGERED,
            CANCEL_ACCEPTED,
        }
    )

    @classmethod
    def values(cls) -> frozenset[str]:
        return cls._ALL


def classify_documented_response(payload: object) -> str:
    """Map a documented Kraken REST body (or timeout) to one typed outcome.

    ``None`` means the transport got no HTTP body (timeout / no response).
    Error strings match the published error guide. Success shapes match
    AddOrder / CancelOrder / OpenOrders / ClosedOrders examples.
    """
    if payload is None:
        return KrakenDocumentedOutcome.NO_RESPONSE
    if not isinstance(payload, dict):
        raise TypeError("kraken documented response must be a dict or None")

    errors = payload.get("error")
    if isinstance(errors, list) and errors:
        joined = " ".join(str(item) for item in errors)
        if "Insufficient funds" in joined:
            return KrakenDocumentedOutcome.INSUFFICIENT_FUNDS
        if "Order minimum not met" in joined:
            return KrakenDocumentedOutcome.ORDER_MINIMUM_NOT_MET
        if "Rate limit exceeded" in joined:
            return KrakenDocumentedOutcome.RATE_LIMIT
        if "Invalid nonce" in joined:
            return KrakenDocumentedOutcome.INVALID_NONCE
        raise ValueError(f"unrecognized documented kraken error: {errors!r}")

    result = payload.get("result")
    if not isinstance(result, dict):
        raise TypeError("kraken documented response missing result object")

    if "txid" in result:
        return KrakenDocumentedOutcome.ORDER_ACCEPTED
    if "count" in result:
        return KrakenDocumentedOutcome.CANCEL_ACCEPTED

    for bucket in ("open", "closed"):
        book = result.get(bucket)
        if not isinstance(book, dict) or not book:
            continue
        for raw in book.values():
            if not isinstance(raw, dict):
                continue
            misc_tokens = {part.strip() for part in str(raw.get("misc") or "").split(",") if part.strip()}
            if "stopped" in misc_tokens:
                return KrakenDocumentedOutcome.STOP_TRIGGERED
            if "partial" in misc_tokens:
                return KrakenDocumentedOutcome.PARTIALLY_FILLED
            vol = _optional_decimal(raw.get("vol"))
            vol_exec = _optional_decimal(raw.get("vol_exec"))
            if vol is not None and vol_exec is not None and Decimal(0) < vol_exec < vol:
                return KrakenDocumentedOutcome.PARTIALLY_FILLED

    raise ValueError("unrecognized documented kraken response shape")


# Documented AddOrder errors that prove Kraken refused the order.
_ORDER_REFUSALS = frozenset(
    {
        KrakenDocumentedOutcome.INSUFFICIENT_FUNDS,
        KrakenDocumentedOutcome.ORDER_MINIMUM_NOT_MET,
        KrakenDocumentedOutcome.INVALID_NONCE,
    }
)


def _documented_refusal(errors: list) -> str | None:
    """The refusal code when every error Kraken returned is one documented order refusal.

    An error ``classify_documented_response`` does not recognize, a documented
    error that is not a refusal (a rate limit), or a mix of codes proves
    nothing about execution: ``None``, and the send's outcome stays unknown.
    """
    codes: set[str] = set()
    for item in errors:
        try:
            codes.add(classify_documented_response({"error": [item]}))
        except (TypeError, ValueError):
            return None
    if len(codes) != 1 or not codes <= _ORDER_REFUSALS:
        return None
    return codes.pop()


class Transport(Protocol):
    """Anything with a `post(url, form)` returning a parsed JSON dict."""

    def post(self, url: str, form: dict[str, str], headers: dict[str, str]) -> dict: ...

    def get(self, url: str, headers: dict[str, str] | None = None) -> dict: ...


class HttpTransport:
    """Real urllib transport. Tests must not call this directly."""

    def post(self, url: str, form: dict[str, str], headers: dict[str, str]) -> dict:
        import urllib.request

        from krellbot.tls import urlopen

        body = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={**headers, "content-type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get(self, url: str, headers: dict[str, str] | None = None) -> dict:
        import urllib.request

        from krellbot.tls import urlopen

        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def sign(secret_b64: str, path: str, nonce: str, postdata: str) -> str:
    """Produce the value of `API-Sign` per Kraken's documented auth recipe.

    `path` is the request path beginning with `/0/private`. `postdata` is the
    URL-encoded body, exactly as sent. Returns base64(HMAC-SHA512).
    """
    secret = base64.b64decode(secret_b64)
    sha256 = hashlib.sha256((nonce + postdata).encode("utf-8")).digest()
    msg = path.encode("utf-8") + sha256
    digest = hmac.new(secret, msg, hashlib.sha512).digest()
    return base64.b64encode(digest).decode("utf-8")


def coid_userref(coid: str) -> int:
    """Derive Kraken's `userref` integer from a coid.

    First four bytes of SHA256(coid) as a big-endian unsigned 32-bit int,
    masked with `0x7FFFFFFF` so the wire value is non-negative and fits
    Kraken's int32-ish field.
    """
    digest = hashlib.sha256(coid.encode("utf-8")).digest()
    value = int.from_bytes(digest[:4], "big")
    return value & 0x7FFFFFFF


def build_entry_with_stop_form(
    coid: str,
    qty: Decimal,
    stop: Decimal,
    *,
    pair: str,
    lot_decimals: int,
    price_decimals: int,
) -> dict[str, str]:
    """Build the AddOrder form for a market buy with an attached stop-loss.

    Same field set the live engine posts via ``place_entry_with_stop``. Does
    not include ``validate`` or ``nonce``; callers that must never place an
    order set ``validate=true`` before send.
    """
    lot = _quantize_qty(qty, lot_decimals)
    price = _quantize_price(stop, price_decimals)
    return {
        "pair": pair,
        "ordertype": "market",
        "type": "buy",
        "volume": format(lot, "f"),
        "cl_ord_id": coid,
        "userref": str(coid_userref(coid)),
        "close[ordertype]": "stop-loss",
        "close[price]": format(price, "f"),
    }


def build_stop_form(coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> dict[str, str]:
    """Build the AddOrder form for a standalone sell stop-loss (a repaired protective stop).

    Per the AddOrder reference (https://docs.kraken.com/api-reference/trading/add-order),
    ``ordertype=stop-loss`` is a market order triggered at ``price``, the same order
    type as the entry's conditional close. The coid goes in ``cl_ord_id`` and no
    ``userref`` is sent: the reference documents the two as mutually exclusive.
    ``qty`` and ``stop`` are already quantized by the caller.
    """
    return {
        "pair": pair,
        "type": "sell",
        "ordertype": "stop-loss",
        "price": format(stop, "f"),
        "volume": format(qty, "f"),
        "cl_ord_id": coid,
    }


def require_validate_true(body: dict[str, str]) -> None:
    """Refuse any request body that is not explicitly validate-only."""
    if body.get("validate") != "true":
        raise ValueError("refusing to send request body without validate=true")


def _quantize_qty(value: Decimal, lot_decimals: int) -> Decimal:
    """Round quantity DOWN to the venue's lot precision."""
    if value.is_nan() or not value.is_finite():
        raise ValueError("qty must be finite")
    return value.quantize(Decimal(1).scaleb(-lot_decimals), rounding=ROUND_DOWN)


def _quantize_price(value: Decimal, price_decimals: int) -> Decimal:
    """Round price DOWN to the venue's price precision."""
    return value.quantize(Decimal(1).scaleb(-price_decimals), rounding=ROUND_DOWN)


@dataclass
class _NonceStore:
    """Monotonic nonce counter, persisted at `paths.home()/run/kraken.nonce`.

    `next(now_us)` takes the current time in epoch microseconds. Nonces stay
    on that scale so a fresh home never sends one below a nonce Kraken
    already accepted for the key; tests inject a clock so the nonce never
    reuses across simulated restarts.
    """

    home: Path

    def _path(self) -> Path:
        run = self.home / "run"
        run.mkdir(parents=True, exist_ok=True)
        return run / NONCE_FILENAME

    def _read_last(self) -> int:
        p = self._path()
        if not p.exists():
            return 0
        raw = p.read_text(encoding="utf-8").strip()
        return int(raw) if raw else 0

    def _write(self, nonce: int) -> None:
        target = self._path()
        paths.atomic_write(target, f"{nonce}\n".encode())

    def next(self, now_us: int) -> int:
        last = self._read_last()
        nonce = max(last + 1, now_us)
        self._write(nonce)
        return nonce


class RateLimitStop(RuntimeError):
    """Third consecutive rate-limit failure; the adapter stops retrying."""


class KrakenVenue:
    """Kraken spot adapter.

    `transport` is injected; tests pass a fake. `now_ms` is the clock and,
    despite its historical name, returns epoch microseconds: it feeds the
    nonce, the private-call spacing and the journal's seconds. Private calls
    are spaced `min_interval_ms` real milliseconds apart through `sleep`
    (seconds); tests inject all three.
    """

    def __init__(
        self,
        api_key: str,
        secret_b64: str,
        transport: Transport,
        now_ms: Callable[[], int] | None = None,
        min_interval_ms: int = 1000,
        home: Path | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        sanitize.register_secret(api_key, secret_b64)
        self._api_key = api_key
        self._secret = secret_b64
        self._transport = transport
        self._now_us = now_ms or _default_now_us
        self._min_interval_ms = min_interval_ms
        self._nonce = _NonceStore(home if home is not None else paths.home())
        self._last_call_us: int | None = None
        self._rate_failures: int = 0
        self._sleep = sleep or time.sleep
        # Query Orders Info rows read for sell fills, by txid (None: not shown).
        self._order_rows: dict[str, dict | None] = {}

    # ---- public protocol surface ----------------------------------------

    def rules(self, pair: str) -> PairRules:
        if not pair:
            raise ValueError("kraken rules require a pair")
        payload = self._public("AssetPairs", {"pair": pair})
        result = _result(payload)
        row = result.get(pair)
        if not isinstance(row, dict):
            raise TypeError("kraken AssetPairs did not return the pair")
        return PairRules(
            ordermin=_required_decimal(row, "ordermin"),
            costmin=_required_decimal(row, "costmin"),
            lot_decimals=int(row["lot_decimals"]),
            price_decimals=int(row["pair_decimals"]),
        )

    def snapshot(self) -> Truth:
        balances_data, open_orders_data, recent_trades_data = (
            self._private("Balance", {}),
            self._private("OpenOrders", {}),
            self._private("TradesHistory", {"trades": True}),
        )
        return Truth(
            balances=_parse_balances(balances_data),
            open_orders=_parse_open_orders(open_orders_data),
            recent_fills=self._link_sell_fills(_parse_recent_trades(recent_trades_data)),
        )

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        release_policy.ensure_live_orders_allowed()
        if not pair:
            raise ValueError("kraken entry requires a pair")
        existing = self._existing(coid)
        if existing is not None:
            return existing
        rules = self.rules(pair)
        lot = _quantize_qty(qty, rules.lot_decimals)
        price = _quantize_price(stop, rules.price_decimals)
        _reject_size(lot, rules, self._last_price(pair))
        form = build_entry_with_stop_form(
            coid,
            lot,
            price,
            pair=pair,
            lot_decimals=rules.lot_decimals,
            price_decimals=rules.price_decimals,
        )
        return self._place(ADD_ORDER, form, coid=coid, side="buy", qty=lot, stop=price)

    def validate_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> Any:
        """Post the engine's entry-with-stop AddOrder shape with ``validate=true``.

        Kraken's validate flag checks inputs and never submits the order, so
        this path does not call ``ensure_live_orders_allowed``. Callers must
        still refuse any body that lacks ``validate=true`` before send.
        """
        if not pair:
            raise ValueError("kraken validate requires a pair")
        rules = self.rules(pair)
        lot = _quantize_qty(qty, rules.lot_decimals)
        price = _quantize_price(stop, rules.price_decimals)
        _reject_size(lot, rules, self._last_price(pair))
        form = build_entry_with_stop_form(
            coid,
            lot,
            price,
            pair=pair,
            lot_decimals=rules.lot_decimals,
            price_decimals=rules.price_decimals,
        )
        form["validate"] = "true"
        require_validate_true(form)
        return self._private(ADD_ORDER, form)

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        release_policy.ensure_live_orders_allowed()
        if not pair:
            raise ValueError("kraken exit requires a pair")
        existing = self._existing(coid)
        if existing is not None:
            return existing
        rules = self.rules(pair)
        lot = _quantize_qty(qty, rules.lot_decimals)
        _reject_size(lot, rules, self._last_price(pair))
        form = {
            "pair": pair,
            "ordertype": "market",
            "type": "sell",
            "volume": format(lot, "f"),
            "cl_ord_id": coid,
            "userref": str(coid_userref(coid)),
        }
        return self._place(ADD_ORDER, form, coid=coid, side="sell", qty=lot, stop=None)

    def place_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        """Rest a sell stop-loss for ``qty`` triggered at ``stop``, under ``cl_ord_id=coid``.

        See ``build_stop_form`` for the request. An order Kraken already lists
        under the coid's ``cl_ord_id`` is returned and nothing is sent. Its
        fills name the coid through ``_link_sell_fills`` (the order carries
        ``cl_ord_id``), and ``stop_outcome`` finds it again. Only ``ordermin``
        is checked here: whether the stop's cost is acceptable is Kraken's to
        say. The send goes through ``_place``, so a documented refusal raises
        ``VenueRejectedError`` and any other error ``RuntimeError``.
        """
        release_policy.ensure_live_orders_allowed()
        if not pair:
            raise ValueError("kraken stop requires a pair")
        found = self._lookup_client_order(coid)
        if found is not None:
            _bucket, order_id, raw = found
            order = _open_order(order_id, raw)
            return OrderRef(
                id=order_id,
                coid=coid,
                pair=order.pair if order is not None else pair,
                side="sell",
                qty=order.qty if order is not None else Decimal(0),
                filled_qty=_executed_qty(raw) or Decimal(0),
                stop_price=order.stop_price if order is not None else None,
            )
        rules = self.rules(pair)
        lot = _quantize_qty(qty, rules.lot_decimals)
        price = _quantize_price(stop, rules.price_decimals)
        _reject_size(lot, rules, None)
        form = build_stop_form(coid, lot, price, pair=pair)
        return self._place(ADD_ORDER, form, coid=coid, side="sell", qty=lot, stop=price)

    def cancel_stops(self, pair: str) -> None:
        release_policy.ensure_live_orders_allowed()
        # Refuse to cancel the whole account. Cancel open orders tagged as stop
        # for this pair, by txid; the doc lists cancelOrder for one txid.
        snapshot = self.snapshot()
        for order in snapshot.open_orders:
            if order.pair == pair and order.stop_price is not None:
                self._private("CancelOrder", {"txid": order.id})

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        release_policy.ensure_live_orders_allowed()
        rules = self.rules(pair)
        price = _quantize_price(new_stop, rules.price_decimals)
        snapshot = self.snapshot()
        for order in snapshot.open_orders:
            if order.pair == pair and order.stop_price is not None:
                self._private(
                    "EditOrder",
                    {
                        "txid": order.id,
                        "ordertype": "stop-loss",
                        "price": format(price, "f"),
                    },
                )

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        snapshot = self.snapshot()
        for order in snapshot.open_orders:
            if order.coid == coid:
                return order
        return None

    def order_outcome(self, coid: str, *, pair: str) -> OrderOutcome:
        """Report what Kraken's own record of ``coid``'s order says executed.

        Finds the order through the same userref lookup that stops ``place_*``
        from resending a coid, and reads its executed volume (``vol_exec``),
        never its requested ``vol``. Still on the book: ``open`` with nothing
        executed, else ``partial``. Closed, cancelled or expired: ``filled``
        at the executed volume, or ``absent`` when nothing executed. No match,
        or an unreadable executed volume, is ``unknown``: the open and closed
        lists are not a complete record, so a miss proves nothing and the
        caller keeps its hold. Never sends an order.
        """
        del pair  # the coid's own cl_ord_id / userref identify the order
        return _outcome_of(self._lookup(coid, own_only=True))

    def stop_outcome(self, coid: str, *, pair: str) -> OrderOutcome:
        """``order_outcome`` for a stop ``place_stop`` rested, found by its ``cl_ord_id``.

        ``place_stop`` sends no ``userref``, so the userref lookup cannot find
        it; this reads the same open and closed lists through their documented
        ``cl_ord_id`` filter (``_lookup_client_order``). The answer is mapped as
        in ``order_outcome``: resting with nothing executed is ``open``, and a
        miss is ``unknown``. Never sends an order.
        """
        del pair  # the cl_ord_id identifies the order
        return _outcome_of(self._lookup_client_order(coid))

    def check_key(self) -> KeyPerms:
        # Strictly validate the key against `GetApiKeyInfo` permissions.
        # See https://docs.kraken.com/api-reference/account-data/get-api-key-info.
        #
        # A key that cannot probe its own permissions (invalid key,
        # permission denied) cannot be verified — refuse. A key whose
        # permission set is missing, malformed, lacks the four required
        # trade permissions, or grants withdrawal/address-management
        # rights is also refused. Only a documented trade-only permission
        # set is accepted.
        #
        # Unknown permission strings fail closed so a future Kraken-added
        # token (e.g. `transfer-funds`) cannot silently make a verified
        # key partially-trusted. Each refusal subclass is still
        # `isinstance(WithdrawCapableError)` so the engine never weakens.
        try:
            data = self._private(GET_API_KEY_INFO, {})
        except _PermissionDenied:
            raise KeyUnverifiableError("kraken GetApiKeyInfo denied; key cannot be verified as trade-only") from None
        result = _result(data)
        if not isinstance(result, dict):
            raise KeyMalformedError("kraken GetApiKeyInfo shape unknown; refused")
        permissions = result.get("permissions")
        if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
            raise KeyMalformedError("kraken GetApiKeyInfo permissions missing or malformed; refused")
        perm_set = set(permissions)
        forbidden = FORBIDDEN_PERMISSIONS & perm_set
        if forbidden:
            raise WithdrawCapableError(f"kraken key grants forbidden permissions: {sorted(forbidden)}; refused")
        missing = REQUIRED_PERMISSIONS - perm_set
        if missing:
            raise KeyTradeOffError(f"kraken key missing required trade permissions: {sorted(missing)}; refused")
        unknown = sorted(perm_set - ALL_KNOWN_PERMISSIONS)
        if unknown:
            raise KrakenKeyUnknownPermissionError(f"kraken key grants unrecognized permissions: {unknown}; refused")
        return KeyPerms(can_trade=True, can_withdraw=False)

    # ---- internals ------------------------------------------------------

    def _place(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        coid: str,
        side: str,
        qty: Decimal,
        stop: Decimal | None,
    ) -> OrderRef:
        payload = self._private(endpoint, form, order_send=True)
        order_id = ""
        if isinstance(payload, dict):
            result = payload.get("result")
            if isinstance(result, dict):
                txid = result.get("txid")
                if isinstance(txid, list) and txid:
                    order_id = str(txid[0])
                elif txid:
                    order_id = str(txid)
        pair = ""
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            pair = str(payload["result"].get("descr", {}).get("pair", ""))
        if not pair:
            pair = str(form.get("pair", ""))
        return OrderRef(
            id=order_id,
            coid=coid,
            pair=pair,
            side=side,
            qty=qty,
            filled_qty=Decimal(0),
            stop_price=stop,
        )

    def _existing(self, coid: str) -> OrderRef | None:
        # Any order on the coid's userref suppresses a resend. Its executed
        # volume, not its requested one, is what the caller may credit.
        found = self._lookup(coid, own_only=False)
        if found is None:
            return None
        _bucket, order_id, raw = found
        order = _open_order(order_id, raw)
        if order is None:
            return None
        return OrderRef(
            id=order.id,
            coid=coid,
            pair=order.pair,
            side=order.side,
            qty=order.qty,
            filled_qty=_executed_qty(raw) or Decimal(0),
            stop_price=order.stop_price,
        )

    def _lookup(self, coid: str, *, own_only: bool) -> tuple[str, str, dict] | None:
        """Find ``coid``'s order in Kraken's open, then closed, orders.

        Returns ``(bucket, order_id, row)``. Queries by the coid's userref;
        ``own_only`` is as in ``_match_userref``.
        """
        userref = coid_userref(coid)
        for endpoint, bucket in (("OpenOrders", "open"), ("ClosedOrders", "closed")):
            payload = self._private(endpoint, {"userref": str(userref)})
            found = _match_userref(payload, bucket, coid, userref, own_only=own_only)
            if found is not None:
                return bucket, found[0], found[1]
        return None

    def _lookup_client_order(self, coid: str) -> tuple[str, str, dict] | None:
        """Find the order whose own ``cl_ord_id`` is ``coid`` in the open, then closed, orders.

        Queries by the ``cl_ord_id`` filter of Get Open Orders
        (https://docs.kraken.com/api-reference/account-data/get-open-orders) and
        Get Closed Orders
        (https://docs.kraken.com/api-reference/account-data/get-closed-orders).
        Only a row that carries ``coid`` as its ``cl_ord_id`` counts. Returns
        ``(bucket, order_id, row)``.
        """
        for endpoint, bucket in (("OpenOrders", "open"), ("ClosedOrders", "closed")):
            book = _result(self._private(endpoint, {"cl_ord_id": coid})).get(bucket)
            if not isinstance(book, dict):
                continue
            for order_id, raw in book.items():
                if isinstance(raw, dict) and str(raw.get("cl_ord_id") or "") == coid:
                    return bucket, str(order_id), raw
        return None

    def _link_sell_fills(self, fills: list[Fill]) -> list[Fill]:
        """Name the client order behind each sell fill, from documented fields only.

        A TradesHistory trade names the order that executed it, ``ordertxid``
        (https://docs.kraken.com/api-reference/account-data/get-trades-history).
        Query Orders Info shows that order's ``cl_ord_id`` and its ``refid``,
        the "referral order transaction ID that created this order"
        (https://docs.kraken.com/api-reference/account-data/query-orders-info).
        An entry's protective stop is its conditional close
        (``close[ordertype]=stop-loss``, triggered by execution of the primary
        order: https://docs.kraken.com/api-reference/trading/add-order). It
        has no ``cl_ord_id`` of its own, so ``refid`` leads to the entry, whose
        ``cl_ord_id`` is the entry coid: that is the fill's ``order_coid``.

        * An order that carries a ``cl_ord_id`` is named by it.
        * A sell that is not a stop order (market, limit, ...) and carries no
          ``cl_ord_id`` is no protective stop: krellbot's Kraken stops are
          only ever stop-loss orders. Its ``order_coid`` stays None.
        * Otherwise ``refid`` is followed, at most ``MAX_REFERRAL_HOPS``
          orders, to the first order with a ``cl_ord_id``.
        * Anything else is ``order_unknown``: an order Query Orders does not
          show, a chain that ends without a ``cl_ord_id``, or a stop order
          with neither field. EditOrder, which ``raise_stop`` uses, replaces a
          stop with a new order and documents that userref is not retained
          (https://docs.kraken.com/api-reference/trading/edit-order); it
          documents no link to the old order, so such a fill is not guessed at.

        Buys are never a protective stop and are not looked up. Each order is
        read once per adapter instance. A rejected read raises, as any other
        snapshot read does, and nothing is inferred from it.
        """
        if not any(f.side == "sell" for f in fills):
            return fills
        self._read_orders({f.coid for f in fills if f.side == "sell" and f.coid})
        linked: list[Fill] = []
        for fill in fills:
            if fill.side != "sell":
                linked.append(fill)
                continue
            order_coid, unknown = self._order_origin(fill.coid) if fill.coid else (None, True)
            linked.append(replace(fill, order_coid=order_coid, order_unknown=unknown))
        return linked

    def _read_orders(self, txids: set[str]) -> None:
        """Read Query Orders Info for ``txids`` and, hop by hop, the orders that created them."""
        pending = sorted(txid for txid in txids if txid not in self._order_rows)
        for _hop in range(MAX_REFERRAL_HOPS + 1):  # the sells' own orders, then their creators
            if not pending:
                return
            for start in range(0, len(pending), QUERY_ORDERS_MAX_TXIDS):
                chunk = pending[start : start + QUERY_ORDERS_MAX_TXIDS]
                rows = _result(self._private(QUERY_ORDERS, {"txid": ",".join(chunk)}))
                for txid in chunk:
                    row = rows.get(txid)
                    self._order_rows[txid] = row if isinstance(row, dict) else None
            referrers = {_referrer(self._order_rows.get(txid)) for txid in pending}
            pending = sorted(ref for ref in referrers if ref and ref not in self._order_rows)

    def _order_origin(self, txid: str) -> tuple[str | None, bool]:
        """``(order_coid, order_unknown)`` for the order ``txid``; see ``_link_sell_fills``."""
        row = self._order_rows.get(txid)
        for hop in range(MAX_REFERRAL_HOPS + 1):
            if row is None:
                return None, True
            client = _text(row.get("cl_ord_id"))
            if client is not None:
                return client, False
            if hop == 0 and _known_non_stop(row):
                return None, False
            referrer = _referrer(row)
            if referrer is None:
                return None, True
            row = self._order_rows.get(referrer)
        return None, True

    def _last_price(self, pair: str) -> Decimal:
        payload = self._public("Ticker", {"pair": pair})
        result = _result(payload)
        row = result.get(pair)
        if not isinstance(row, dict):
            raise TypeError("kraken Ticker did not return the pair")
        last = row.get("c")
        if not isinstance(last, list) or not last:
            raise RuntimeError("kraken Ticker last price missing")
        return Decimal(str(last[0]))

    def _public(self, endpoint: str, query: dict[str, str]) -> Any:
        url = f"{BASE_URL}{PUBLIC_PATH}/{endpoint}?{urllib.parse.urlencode(query)}"
        return self._transport.get(url, {})

    def _private(self, endpoint: str, form: dict[str, str], *, order_send: bool = False) -> Any:
        """POST one private call; ``order_send`` marks the call that places an order.

        Only an order send turns a documented refusal into ``VenueRejectedError``.
        """
        for _attempt in range(3):
            payload = self._send_once(endpoint, form)
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, list) and err:
                if any(RATE_LIMIT_MARKER in str(item) for item in err):
                    self._note_rate_limit()
                    continue
                if any("Permission" in str(item) or "Invalid key" in str(item) for item in err):
                    raise _PermissionDenied("kraken permission denied")
                code = _documented_refusal(err) if order_send else None
                if code is not None:
                    raise VenueRejectedError("kraken", code)
                raise RuntimeError(f"kraken {endpoint} rejected the request")
            self._rate_failures = 0
            return payload
        self._journal_rate_limit()
        raise RateLimitStop("kraken rate limit: stopped after 3 failures")

    def _send_once(self, endpoint: str, form: dict[str, str]) -> Any:
        # Wait for the slot before drawing the nonce, so a nonce never sits
        # through the wait while another process on this home sends a higher
        # one. One POST per call: throttling never resends.
        self._wait_for_private_slot()
        nonce_str = str(self._nonce.next(int(self._now_us())))
        body = dict(form)
        body["nonce"] = nonce_str
        path = f"{PRIVATE_PATH}/{endpoint}"
        postdata = urllib.parse.urlencode(body)
        signature = sign(self._secret, path, nonce_str, postdata)
        headers = {"API-Key": self._api_key, "API-Sign": signature}
        return self._transport.post(f"{BASE_URL}{path}", body, headers)

    def _wait_for_private_slot(self) -> None:
        # Space private calls `min_interval_ms` real milliseconds apart. The
        # clock is in microseconds, so the interval is converted to it before
        # comparing, and `sleep` takes seconds. A wall clock that steps back
        # waits one interval, never longer.
        interval_us = self._min_interval_ms * US_PER_MS
        now_us = int(self._now_us())
        if self._last_call_us is not None:
            wait_us = min(interval_us - (now_us - self._last_call_us), interval_us)
            if wait_us > 0:
                self._sleep(wait_us / US_PER_S)
                now_us = int(self._now_us())
        self._last_call_us = now_us

    def _note_rate_limit(self) -> None:
        self._rate_failures += 1
        if self._rate_failures >= 3:
            self._journal_rate_limit()
            raise RateLimitStop("kraken rate limit: stopped after 3 failures")
        self._sleep(300.0 if self._rate_failures == 1 else 600.0)

    def _journal_rate_limit(self) -> None:
        journal.append(
            {
                "ts": int(self._now_us()) // US_PER_S,
                "kind": "rate_limit",
                "venue": "kraken",
                "pack": "",
                "bar_ts": 0,
                "detail": "rate limit",
            }
        )


class _PermissionDenied(RuntimeError):
    """Internal marker: the venue rejected a private call as not authorized."""


def _result(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def _required_decimal(row: dict, key: str) -> Decimal:
    if key not in row:
        raise RuntimeError("kraken pair rules missing a required field")
    return Decimal(str(row[key]))


def _optional_decimal(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (ValueError, ArithmeticError, TypeError):
        return None


def _reject_size(qty: Decimal, rules: PairRules, price: Decimal | None) -> None:
    if qty < rules.ordermin:
        raise ValueError("qty below ordermin")
    if price is not None and qty * price < rules.costmin:
        raise ValueError("notional below costmin")


def _parse_balances(payload: Any) -> list[Balance]:
    out: list[Balance] = []
    for asset, raw in _result(payload).items():
        free = _optional_decimal(raw)
        if free is None:
            continue
        out.append(Balance(asset=str(asset), free=free))
    return out


def _parse_open_orders(payload: Any) -> list[OpenOrder]:
    book = _result(payload).get("open")
    if not isinstance(book, dict):
        return []
    out: list[OpenOrder] = []
    for order_id, raw in book.items():
        order = _open_order(str(order_id), raw)
        if order is not None:
            out.append(order)
    return out


def _open_order(order_id: str, raw: Any) -> OpenOrder | None:
    if not isinstance(raw, dict):
        return None
    descr_raw = raw.get("descr")
    descr = descr_raw if isinstance(descr_raw, dict) else {}
    qty = _optional_decimal(raw.get("vol"))
    if qty is None:
        return None
    ordertype = str(descr.get("ordertype", ""))
    stop = _optional_decimal(raw.get("stopprice"))
    if "stop" not in ordertype and (stop is None or stop <= 0):
        stop = None
    elif stop is None or stop <= 0:
        stop = _optional_decimal(descr.get("price"))
    return OpenOrder(
        id=order_id,
        coid=str(raw.get("cl_ord_id") or ""),
        pair=str(descr.get("pair", "")),
        side=str(descr.get("type", "")),
        qty=qty,
        stop_price=stop,
    )


def _match_userref(payload: Any, bucket: str, coid: str, userref: int, *, own_only: bool) -> tuple[str, dict] | None:
    """Return ``(order_id, row)`` of the listed order for ``coid``, if any.

    A row whose ``cl_ord_id`` is ``coid`` wins; otherwise the first row with
    the coid's ``userref``. With ``own_only``, a userref row counts only when
    nothing says it is another order: no other ``cl_ord_id`` and no ``refid``
    (an order created by another one, such as an entry's conditional close).
    Without it (the resend guard), a row ``_open_order`` cannot read is
    skipped and the scan goes on, so a readable listed order still blocks a
    second send.
    """
    book = _result(payload).get(bucket)
    if not isinstance(book, dict):
        return None
    by_userref: tuple[str, dict] | None = None
    for order_id, raw in book.items():
        if not isinstance(raw, dict):
            continue
        if not own_only and _open_order(str(order_id), raw) is None:
            continue
        stored = str(raw.get("cl_ord_id") or "")
        if stored == coid:
            return str(order_id), raw
        try:
            stored_ref = int(raw.get("userref") or 0)
        except (TypeError, ValueError):
            stored_ref = 0
        if by_userref is not None or stored_ref != userref:
            continue
        if own_only and (stored or raw.get("refid")):
            continue
        by_userref = (str(order_id), raw)
    return by_userref


def _text(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw.strip() else None


def _referrer(row: dict | None) -> str | None:
    """The ``refid`` to follow from an order with no ``cl_ord_id`` of its own."""
    if row is None or _text(row.get("cl_ord_id")) is not None:
        return None
    return _text(row.get("refid"))


def _known_non_stop(row: dict) -> bool:
    """True when the order's documented ``descr.ordertype`` is readable and no stop type."""
    descr = row.get("descr")
    ordertype = _text(descr.get("ordertype")) if isinstance(descr, dict) else None
    return ordertype is not None and "stop" not in ordertype


def _executed_qty(raw: dict) -> Decimal | None:
    """The order's executed volume (``vol_exec``), or None when unreadable."""
    executed = _optional_decimal(raw.get("vol_exec"))
    if executed is None or not executed.is_finite() or executed < 0:
        return None
    return executed


def _outcome_of(found: tuple[str, str, dict] | None) -> OrderOutcome:
    """Map a looked-up ``(bucket, order_id, row)`` to its outcome; see ``KrakenVenue.order_outcome``."""
    if found is None:
        return UNKNOWN_OUTCOME
    bucket, _order_id, raw = found
    executed = _executed_qty(raw)
    if executed is None:
        return UNKNOWN_OUTCOME
    if bucket == "open":
        if executed > 0:
            return OrderOutcome(status="partial", filled_qty=executed, authoritative=True)
        return OrderOutcome(status="open", authoritative=True)
    if executed > 0:
        return OrderOutcome(status="filled", filled_qty=executed, authoritative=True)
    return OrderOutcome(status="absent", authoritative=True)


def _parse_recent_trades(payload: Any) -> list[Fill]:
    trades = _result(payload).get("trades")
    if not isinstance(trades, dict):
        return []
    out: list[Fill] = []
    for trade_id, raw in trades.items():
        if not isinstance(raw, dict):
            continue
        qty = _optional_decimal(raw.get("vol"))
        price = _optional_decimal(raw.get("price"))
        stamp = _optional_decimal(raw.get("time"))
        if qty is None or price is None or stamp is None:
            continue
        out.append(
            Fill(
                id=str(trade_id),
                coid=str(raw.get("ordertxid", "")),
                pair=str(raw.get("pair", "")),
                side=str(raw.get("type", "")),
                qty=qty,
                price=price,
                ts_ms=int(stamp * 1000),
            )
        )
    return out
