"""Unresolved order intents read from durable state, with an explicit unknown.

An intent is committed to the Outbox before it is sent. Until the venue's
actual outcome is known it is an *obligation*: the engine may not open new
risk that could stack on top of it, and it may not blindly resend it.

Sources, all in ``<home>/ops.sqlite``:

* ``outbox`` rows still marked ``sent: false`` (committed, outcome unknown);
* ``needs_reconcile`` markers (a redispatch found a committed intent, or a
  protective stop was refused after its entry filled);
* ``intent_resolved`` rows, which retire an obligation once evidence about
  its outcome has been recorded (see ``run._resolve_obligations``);
* ``reconcile_filled`` / ``reconcile_open`` / ``reconcile_absent``
  observation rows, which record what the last lookup saw;
* ``operator_asserted`` rows, which record an outcome the operator asserted
  without venue evidence (see ``run.resolve_order_by_operator``). They are
  evidence for that resolve only, never a venue observation.

The engine and the Operations view read the same interpretation. When the
store cannot be read the result says so (``available=False``) instead of
returning an empty, clean-looking list; the engine then holds new entries.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from krellbot.venues.base import VENUE_REFUSAL_CODES

OPS_DB_FILENAME = "ops.sqlite"
RESOLVED_KIND = "intent_resolved"
OPERATOR_ASSERTION_KIND = "operator_asserted"
OBSERVATION_KINDS = {
    "filled": "reconcile_filled",
    "partial": "reconcile_filled",
    "open": "reconcile_open",
    "absent": "reconcile_absent",
}


@dataclass(frozen=True)
class Obligation:
    """One committed intent whose outcome is not yet resolved.

    Owner context (``pack_id``, ``pack_version``, ``bar_ts``) is read from
    the committed intent body; it is ``None`` for intents committed before
    the engine wrote it there. An obligation with an unknown venue has
    unknown scope and restricts entries on every venue.

    ``observed_status`` and ``observed_qty`` are the status and executed
    quantity the latest observation row recorded (``None`` without one).
    """

    coid: str
    venue: str | None
    mode: str | None
    kind: str | None
    pair: str | None
    qty: Decimal | None
    pack_id: str | None
    pack_version: str | None
    bar_ts: int | None
    last_observation: str | None = None
    marked: bool = False
    observed_status: str | None = None
    observed_qty: Decimal | None = None


@dataclass(frozen=True)
class ObligationsView:
    available: bool
    unresolved: tuple[Obligation, ...] = ()
    error_code: str | None = None

    def blocks_entries(self, *, venue: str, mode: str) -> bool:
        """True when a new entry on ``venue``/``mode`` must wait."""
        if not self.available:
            return True
        return any(
            o.venue is None or (o.venue == venue and (o.mode is None or o.mode == mode)) for o in self.unresolved
        )


class ObligationsUnavailable(RuntimeError):
    """The operational store could not be read; obligations are unknown."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _text(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw.strip() else None


def _decimal(raw: object) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() and value >= 0 else None


def read_obligations(home: Path) -> ObligationsView:
    """Return every unresolved intent, or ``available=False`` on a read failure."""
    path = Path(home) / OPS_DB_FILENAME
    if not path.exists():
        return ObligationsView(available=True)
    from krellbot.storage.database import OperationalStore, StoreError

    try:
        rows = OperationalStore(path).read_ledger()
    except (StoreError, sqlite3.Error, OSError) as exc:
        code = getattr(exc, "code", None)
        return ObligationsView(available=False, error_code=code if isinstance(code, str) else "store_error")

    intents: dict[str, dict] = {}
    unsent: list[str] = []
    marked: list[str] = []
    resolved: set[str] = set()
    observed: dict[str, tuple[str, dict]] = {}
    for _row_id, kind, payload in rows:
        try:
            record = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        coid = _text(record.get("coid"))
        if coid is None:
            continue
        if kind == "outbox":
            intents.setdefault(coid, record)
            if record.get("sent") is not True and coid not in unsent:
                unsent.append(coid)
        elif kind == "needs_reconcile":
            if coid not in marked:
                marked.append(coid)
        elif kind == RESOLVED_KIND:
            resolved.add(coid)
        elif kind in OBSERVATION_KINDS.values():
            observed[coid] = (kind, record)

    out: list[Obligation] = []
    for coid in [*unsent, *(c for c in marked if c not in unsent)]:
        if coid in resolved:
            continue
        record = intents.get(coid, {})
        try:
            body = json.loads(record.get("body") or "{}")
        except (json.JSONDecodeError, TypeError):
            body = {}
        body = body if isinstance(body, dict) else {}
        bar_ts = body.get("bar_ts")
        observed_kind, observation = observed.get(coid, (None, {}))
        out.append(
            Obligation(
                coid=coid,
                venue=_text(record.get("venue")),
                mode=_text(record.get("mode")),
                kind=_text(body.get("kind")),
                pair=_text(body.get("pair")),
                qty=_decimal(body.get("qty")),
                pack_id=_text(body.get("pack_id")),
                pack_version=_text(body.get("pack_version")),
                bar_ts=bar_ts if isinstance(bar_ts, int) and not isinstance(bar_ts, bool) else None,
                last_observation=observed_kind,
                marked=coid in marked,
                observed_status=_observed_status(observed_kind, observation),
                observed_qty=_observed_qty(observed_kind, observation),
            )
        )
    return ObligationsView(available=True, unresolved=tuple(out))


# An observation row without a readable status or quantity still says this
# much: an absent or open order had executed nothing when it was looked up.
_ZERO_EXECUTION_STATUS = {"reconcile_absent": "absent", "reconcile_open": "open"}


def _observed_status(kind: str | None, observation: dict) -> str | None:
    status = _text(observation.get("status"))
    if kind is None:
        return None
    if status is not None and OBSERVATION_KINDS.get(status) == kind:
        return status
    return _ZERO_EXECUTION_STATUS.get(kind)


def _observed_qty(kind: str | None, observation: dict) -> Decimal | None:
    if kind is None:
        return None
    qty = _decimal(observation.get("filled_qty"))
    if qty is not None:
        return qty
    return Decimal(0) if kind in _ZERO_EXECUTION_STATUS else None


def read_resolution(home: Path, coid: str) -> dict | None:
    """The first recorded resolution of ``coid``, or ``None`` while it has none.

    Raises :class:`ObligationsUnavailable` when the store cannot be read.
    """
    path = Path(home) / OPS_DB_FILENAME
    if not path.exists():
        return None
    from krellbot.storage.database import OperationalStore, StoreError

    try:
        rows = OperationalStore(path).read_ledger()
    except (StoreError, sqlite3.Error, OSError) as exc:
        code = getattr(exc, "code", None)
        raise ObligationsUnavailable(code if isinstance(code, str) else "store_error") from None
    for _row_id, kind, payload in rows:
        if kind != RESOLVED_KIND:
            continue
        try:
            record = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(record, dict) and record.get("coid") == coid:
            return record
    return None


def record_observation(store, *, coid: str, status: str, filled_qty: Decimal, previous: str | None) -> None:
    """Record what a lookup saw, once per change of observation."""
    kind = OBSERVATION_KINDS.get(status)
    if kind is None or kind == previous:
        return
    payload = json.dumps({"coid": coid, "status": status, "filled_qty": str(filled_qty)}, sort_keys=True)
    with store.transaction() as conn:
        conn.execute("INSERT INTO ledger (kind, payload) VALUES (?, ?)", (kind, payload))


def record_assertion(store, *, coid: str, outcome: str, filled_qty: Decimal, ts: int) -> None:
    """Record an outcome the operator asserted without venue evidence.

    It is written before any accounting or resolution it leads to, so the
    reason for both stays durable if the resolve is interrupted.
    """
    payload = json.dumps(
        {"coid": coid, "outcome": outcome, "filled_qty": str(filled_qty), "ts": int(ts)},
        sort_keys=True,
    )
    with store.transaction() as conn:
        conn.execute("INSERT INTO ledger (kind, payload) VALUES (?, ?)", (OPERATOR_ASSERTION_KIND, payload))


def record_resolution(store, *, coid: str, outcome: str, filled_qty: Decimal, evidence: str) -> None:
    """Retire one obligation. Callers write it only after its accounting is durable."""
    payload = json.dumps(
        {"coid": coid, "outcome": outcome, "filled_qty": str(filled_qty), "evidence": evidence},
        sort_keys=True,
    )
    with store.transaction() as conn:
        conn.execute("INSERT INTO ledger (kind, payload) VALUES (?, ?)", (RESOLVED_KIND, payload))


VENUE_REJECTED = "venue_rejected"


def record_rejection(store, *, coid: str, code: str) -> None:
    """Retire an intent the venue refused by its documented response: nothing executed.

    ``code`` is the classified refusal code, never the venue's own text. The
    outcome is ``absent`` with no executed quantity, so there is nothing to
    account before it; callers still journal the refusal first.
    """
    if code not in VENUE_REFUSAL_CODES:
        raise ValueError("not a documented venue refusal code")
    payload = json.dumps(
        {"coid": coid, "outcome": "absent", "filled_qty": "0", "evidence": VENUE_REJECTED, "code": code},
        sort_keys=True,
    )
    with store.transaction() as conn:
        conn.execute("INSERT INTO ledger (kind, payload) VALUES (?, ?)", (RESOLVED_KIND, payload))
