"""Daily-loss entry gate: per-arm equity and each UTC day's opening value.

An arm may carry ``daily_loss_limit_pct`` (``config.ArmedPack``): a percent of
its starting cash. Off by default. Every tick that acts on a bar measures the
arm's equity just before its entry decision, with or without a limit, and
persists it with the UTC day's opening value in
``<home>/run/daily-loss-<venue>.json`` (written under the venue's tick lock).

Equity of the ownership identity ``(pack, venue, pair, mode)``::

    starting cash
    + the net cash of its attributed fills: each sell's notional x (1 - fee),
      less each buy's notional x (1 + fee), at the venue's taker fee
    + its attributed quantity x the latest close

Quantities are what the journal records attributed to the identity say
executed, the same records ``reconcile.project_ownership`` credits
(reconciled records included), never requested sizes. A resting stop fill
the tick has seen but not journaled yet counts as a sell of that quantity.
Prices come from the venue fills that name a record's orders: by fill id
(``stop_fill_ids``), by client order id (``resolved_coid``, ``entry_coid``,
or the Outbox exit intent of a tick record's bar), or by ``order_coid``.
Live adapters name sell fills only (Kraken trades carry ``ordertxid``,
Coinbase List Fills no client order id), so a buy that no fill names is
priced at the close of the bar its entry was decided on. A sell that no
fill names cannot be priced.

Each record's net cash is cached by a digest of the record the first time it
is priced, so a live fill that later leaves the venue's recent window still
counts. External inventory and other identities' records never count.
Equity spans the identity's whole journal history, as ownership does; the
gate only reads changes within one UTC day.

The day's opening value is the first equity measured in that UTC day of the
tick clock, recorded once; ``day_low`` is the lowest equity measured since.
When ``day_open - day_low`` reaches the limit (``starting_cash x pct / 100``),
entries are refused with ``daily_loss_limit`` for the rest of the day.
Equity that cannot be computed (no usable limit or starting cash, ambiguous
ownership, a quantity with no price, an unreadable state file) refuses them
with ``loss_limit_unavailable``. Exits and stops are never gated here.
Decimal throughout, in a fixed context, so the same inputs give the same
result.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Context, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from krellbot import paths as kb_paths
from krellbot.storage.database import OperationalStore

from .reconcile import OwnershipIdentity, OwnershipProjection, _qty, _record_mode, _text, _tick_records

CODE_DAILY_LOSS_LIMIT = "daily_loss_limit"
CODE_LOSS_LIMIT_UNAVAILABLE = "loss_limit_unavailable"
SCHEMA_VERSION = 1
# Each quantity field a tick record carries, and the side of the fill behind it.
_FIELDS = (("entry_qty", "buy"), ("exit_qty", "sell"), ("stop_qty", "sell"))
# Fixed so the result never depends on a caller's decimal context.
_CONTEXT = Context(prec=28)


@dataclass(frozen=True)
class Measure:
    """One equity measurement: the value, or why there is none, plus newly priced records."""

    equity: Decimal | None
    reason: str | None = None
    priced: dict[str, str] = field(default_factory=dict)
    # Digests of every attributed record measured; None when measuring stopped early.
    keys: frozenset[str] | None = None


def state_path(home: Path, venue: str) -> Path:
    return Path(home) / "run" / f"daily-loss-{venue}.json"


def arm_key(identity: OwnershipIdentity) -> str:
    """The arm's key in its venue's state file."""
    return f"{identity.pack_id}|{identity.pair}|{identity.mode}"


def utc_day(now: float) -> str:
    return datetime.datetime.fromtimestamp(int(now), tz=datetime.timezone.utc).date().isoformat()


def read_states(home: Path, venue: str) -> dict[str, dict] | None:
    """Per-arm states for ``venue``: empty without a file, None when the file cannot be read."""
    try:
        raw = state_path(home, venue).read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    arms = document.get("arms") if isinstance(document, dict) else None
    if not isinstance(arms, dict):
        return None
    return {key: value for key, value in arms.items() if isinstance(key, str) and isinstance(value, dict)}


def write_states(home: Path, venue: str, arms: dict[str, dict]) -> None:
    payload = {"schema_version": SCHEMA_VERSION, "arms": dict(sorted(arms.items()))}
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    kb_paths.atomic_write(state_path(home, venue), data.encode("utf-8"))


def cached_cash_flows(state: dict | None) -> dict[str, str]:
    flows = state.get("cash_flows") if isinstance(state, dict) else None
    if not isinstance(flows, dict):
        return {}
    return {key: value for key, value in flows.items() if isinstance(key, str) and isinstance(value, str)}


def measure(
    home: Path,
    identity: OwnershipIdentity,
    *,
    ownership: OwnershipProjection,
    starting_cash: Decimal | None,
    fills: Iterable[Any],
    candles: list,
    cached: dict[str, str],
    fee_bps: int,
    hold_coid: Callable[[str], str],
    stop_fill_ids: Iterable[str] = (),
    stop_fill_qty: Decimal = Decimal(0),
) -> Measure:
    """The identity's equity now; see the module docstring for the formula.

    ``hold_coid`` maps a venue fill id to the coid of the exit hold
    (``run._unlinked_sell_coid``) an operator resolve of that sell names.
    ``StoreError`` from reading the Outbox propagates.
    """
    with localcontext(_CONTEXT):
        if not _usable_cash(starting_cash):
            return Measure(None, "starting_cash_unknown")
        if ownership.ambiguous:
            return Measure(None, "ownership_ambiguous")
        if not candles:
            return Measure(None, "no_close")
        last_close = _decimal(getattr(candles[-1], "close", None))
        if last_close is None:
            return Measure(None, "no_close")
        fills = list(fills)
        fee = Decimal(fee_bps) / Decimal(10_000)
        exits: dict[int, set[str]] | None = None

        def exit_coids(bar_ts: object) -> set[str]:
            nonlocal exits
            if exits is None:
                exits = _exit_coids(home, identity)
            return exits.get(bar_ts, set()) if isinstance(bar_ts, int) else set()

        cash = Decimal(0)
        priced: dict[str, str] = {}
        keys: set[str] = set()
        unpriced = False
        for key, record in attributed_records(home, identity):
            keys.add(key)
            value = _decimal(cached.get(key))
            if value is None:
                value = _record_cash(
                    identity, record, fills=fills, candles=candles, fee=fee, hold_coid=hold_coid, exit_coids=exit_coids
                )
                if value is None:
                    unpriced = True  # keep pricing the rest, so their fills are cached while the venue shows them
                    continue
                priced[key] = str(value)
            cash += value
        qty = ownership.attributable_qty
        if stop_fill_qty > 0:
            pending = _cash(identity.pair, "sell", stop_fill_qty, fills, set(), set(stop_fill_ids), hold_coid, fee)
            if pending is None:
                unpriced = True
            else:
                cash += pending
                qty = max(qty - stop_fill_qty, Decimal(0))
        if unpriced:
            return Measure(None, "fill_price_unknown", priced, frozenset(keys))
        return Measure(starting_cash + cash + qty * last_close, None, priced, frozenset(keys))


def attributed_records(home: Path, identity: OwnershipIdentity) -> list[tuple[str, dict]]:
    """``identity``'s journal records with a quantity, in journal order, each with its digest.

    A record counts when it names the identity's pack and every one of its
    venue, pair and mode, exactly as ``project_ownership`` credits it. The
    digest covers the whole record; identical records are told apart by
    occurrence.
    """
    expected = (identity.venue, identity.pair, identity.mode)
    seen: dict[str, int] = {}
    out: list[tuple[str, dict]] = []
    for record in _tick_records(home):
        detail = record.get("detail")
        if record.get("pack") != identity.pack_id or not isinstance(detail, dict):
            continue
        if (_text(record.get("venue")), _text(detail.get("pair")), _record_mode(detail)) != expected:
            continue
        if all(_qty(detail, name) == 0 for name, _side in _FIELDS):
            continue
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        occurrence = seen.get(digest, 0)
        seen[digest] = occurrence + 1
        out.append((digest if occurrence == 0 else f"{digest}:{occurrence}", record))
    return out


def observe(previous: dict | None, measured: Measure, *, now: float) -> dict:
    """The arm's state after ``measured``: equity, the day's opening value (once) and its low."""
    previous = previous if isinstance(previous, dict) else {}
    today = utc_day(now)
    flows = cached_cash_flows(previous)
    if measured.keys is not None:
        flows = {key: value for key, value in flows.items() if key in measured.keys}
    flows.update(measured.priced)
    same_day = previous.get("day") == today
    day_open = _decimal(previous.get("day_open")) if same_day else None
    day_low = _decimal(previous.get("day_low")) if same_day else None
    equity = measured.equity
    if equity is not None:
        if day_open is None:
            day_open = equity
        day_low = equity if day_low is None else min(day_low, equity)
    state: dict = {
        "cash_flows": dict(sorted(flows.items())),
        "day": today,
        "day_open": None if day_open is None else str(day_open),
        "day_low": None if day_low is None else str(day_low),
        "equity": None if equity is None else str(equity),
        "equity_ts": int(now),
    }
    if equity is None:
        state["unavailable"] = measured.reason or "unknown"
    return state


def assess(
    limit_pct: Decimal | None, starting_cash: Decimal | None, state: dict | None, *, now: float
) -> tuple[str | None, str | None]:
    """``(refusal code, reason)`` for an arm's entries; ``(None, None)`` when nothing holds them.

    ``state`` is the arm's state for the tick clock's day. A state from an
    earlier day holds nothing: the next tick opens the new day.
    """
    if limit_pct is None:
        return None, None
    if not _usable_pct(limit_pct):
        return CODE_LOSS_LIMIT_UNAVAILABLE, "limit_invalid"
    if not _usable_cash(starting_cash):
        return CODE_LOSS_LIMIT_UNAVAILABLE, "starting_cash_unknown"
    if not isinstance(state, dict) or state.get("day") != utc_day(now):
        return None, None
    with localcontext(_CONTEXT):
        day_open, day_low = _decimal(state.get("day_open")), _decimal(state.get("day_low"))
        if (
            day_open is not None
            and day_low is not None
            and day_open - day_low >= limit_amount(starting_cash, limit_pct)
        ):
            return CODE_DAILY_LOSS_LIMIT, None
    if _decimal(state.get("equity")) is None:
        reason = state.get("unavailable")
        return CODE_LOSS_LIMIT_UNAVAILABLE, reason if isinstance(reason, str) else "unknown"
    return None, None


def limit_amount(starting_cash: Decimal, limit_pct: Decimal) -> Decimal:
    with localcontext(_CONTEXT):
        return starting_cash * limit_pct / Decimal(100)


def project(home: Path, armed: Any, *, now: float) -> dict:
    """What the Workstation shows for ``armed``: its limit and the gate as of its last tick."""
    identity = OwnershipIdentity(pack_id=armed.pack_id, venue=armed.venue, pair=armed.pair, mode=armed.mode)
    states = read_states(home, armed.venue)
    state = (states or {}).get(arm_key(identity)) or {}
    limit, cash = armed.daily_loss_limit_pct, armed.starting_cash
    if limit is not None and states is None:
        code, reason = CODE_LOSS_LIMIT_UNAVAILABLE, "state_unreadable"
    else:
        code, reason = assess(limit, cash, state, now=now)
    usable = _usable_pct(limit) and _usable_cash(cash)
    return {
        "venue": armed.venue,
        "pair": armed.pair,
        "mode": armed.mode,
        "daily_loss_limit_pct": plain(limit),
        "starting_cash": plain(cash),
        "limit_amount": plain(limit_amount(cash, limit)) if usable else None,
        "day": state.get("day") if isinstance(state.get("day"), str) else None,
        "day_open": plain(_decimal(state.get("day_open"))),
        "day_low": plain(_decimal(state.get("day_low"))),
        "equity": plain(_decimal(state.get("equity"))),
        "code": code,
        "reason": reason,
    }


def plain(value: Decimal | None) -> str | None:
    """A Decimal as plain text without exponent or trailing zeros."""
    if value is None:
        return None
    if not value.is_finite():
        return str(value)
    if value.is_zero():
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


# ---- internals ---------------------------------------------------------------------------


def _record_cash(
    identity: OwnershipIdentity,
    record: dict,
    *,
    fills: list,
    candles: list,
    fee: Decimal,
    hold_coid: Callable[[str], str],
    exit_coids: Callable[[object], set[str]],
) -> Decimal | None:
    """The net cash of one attributed record, or None when a quantity in it has no price."""
    detail = record["detail"]
    bar_ts = record.get("bar_ts")
    resolved = _text(detail.get("resolved_coid"))
    raw_ids = detail.get("stop_fill_ids")
    ids = {i for i in raw_ids if isinstance(i, str) and i} if isinstance(raw_ids, list) else set()
    total = Decimal(0)
    for name, side in _FIELDS:
        qty = _qty(detail, name)
        if qty is None:
            return None
        if qty == 0:
            continue
        if resolved is not None:
            # A reconciled record: the intent's own coid names its fills (an
            # exit hold's coid names the sell it holds), plus any stop fill ids.
            coids, fill_ids = {resolved}, ids
        elif name == "entry_qty":
            entry = _text(detail.get("entry_coid"))
            coids, fill_ids = ({entry} if entry else set()), set()
        elif name == "exit_qty":
            coids, fill_ids = exit_coids(bar_ts), set()
        else:
            coids, fill_ids = set(), ids
        value = _cash(identity.pair, side, qty, fills, coids, fill_ids, hold_coid, fee)
        if value is None and side == "buy":
            close = _close_at(candles, bar_ts)
            if close is not None:
                value = -(qty * close * (1 + fee))
        if value is None:
            return None
        total += value
    return total


def _cash(
    pair: str,
    side: str,
    qty: Decimal,
    fills: list,
    coids: set[str],
    ids: set[str],
    hold_coid: Callable[[str], str],
    fee: Decimal,
) -> Decimal | None:
    """Signed net cash of ``qty`` at the volume-weighted price of the fills that name it."""
    coids = {coid for coid in coids if coid}
    if not coids and not ids:
        return None
    matched: dict[object, tuple[Decimal, Decimal]] = {}
    for fill in fills:
        if getattr(fill, "pair", None) != pair or getattr(fill, "side", None) != side:
            continue
        fill_id = getattr(fill, "id", "") or ""
        named = (
            (fill_id and fill_id in ids)
            or getattr(fill, "coid", None) in coids
            or getattr(fill, "order_coid", None) in coids
            or (fill_id and coids and hold_coid(fill_id) in coids)
        )
        if not named:
            continue
        fill_qty, price = _decimal(getattr(fill, "qty", None)), _decimal(getattr(fill, "price", None))
        if fill_qty is None or price is None or fill_qty <= 0 or price <= 0:
            continue
        matched[fill_id or (getattr(fill, "coid", None), getattr(fill, "ts_ms", None), fill_qty, price)] = (
            fill_qty,
            price,
        )
    volume = sum((q for q, _p in matched.values()), Decimal(0))
    if volume <= 0:
        return None
    notional = qty * sum((q * p for q, p in matched.values()), Decimal(0)) / volume
    return notional * (1 - fee) if side == "sell" else -(notional * (1 + fee))


def _close_at(candles: list, bar_ts: object) -> Decimal | None:
    if not isinstance(bar_ts, int) or isinstance(bar_ts, bool):
        return None
    for candle in candles:
        if int(getattr(candle, "ts_ms", -1)) == bar_ts:
            close = _decimal(getattr(candle, "close", None))
            return close if close is not None and close > 0 else None
    return None


def _exit_coids(home: Path, identity: OwnershipIdentity) -> dict[int, set[str]]:
    """The coids of ``identity``'s exit intents by bar, from the Outbox (committed before any send)."""
    path = Path(home) / "ops.sqlite"
    if not path.exists():
        return {}
    out: dict[int, set[str]] = {}
    for _row_id, kind, payload in OperationalStore(path).read_ledger():
        if kind != "outbox":
            continue
        try:
            record = json.loads(payload)
            body = json.loads(record["body"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if not isinstance(record, dict) or not isinstance(body, dict):
            continue
        if (record.get("venue"), record.get("mode")) != (identity.venue, identity.mode):
            continue
        if body.get("kind") != "exit" or body.get("pair") != identity.pair or body.get("pack_id") != identity.pack_id:
            continue
        bar_ts, coid = body.get("bar_ts"), record.get("coid")
        if isinstance(bar_ts, int) and not isinstance(bar_ts, bool) and isinstance(coid, str) and coid:
            out.setdefault(bar_ts, set()).add(coid)
    return out


def _decimal(raw: object) -> Decimal | None:
    """A finite Decimal from a Decimal, an int or a decimal string; None otherwise."""
    if isinstance(raw, bool) or not isinstance(raw, (Decimal, int, str)):
        return None
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _usable_pct(pct: Decimal | None) -> bool:
    return isinstance(pct, Decimal) and pct.is_finite() and Decimal(0) < pct <= Decimal(100)


def _usable_cash(cash: Decimal | None) -> bool:
    return isinstance(cash, Decimal) and cash.is_finite() and cash > 0
