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
  observation rows, which record what the last lookup saw.

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

OPS_DB_FILENAME = "ops.sqlite"
RESOLVED_KIND = "intent_resolved"
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
    observed: dict[str, str] = {}
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
            observed[coid] = kind

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
                last_observation=observed.get(coid),
                marked=coid in marked,
            )
        )
    return ObligationsView(available=True, unresolved=tuple(out))


def record_observation(store, *, coid: str, status: str, filled_qty: Decimal, previous: str | None) -> None:
    """Record what a lookup saw, once per change of observation."""
    kind = OBSERVATION_KINDS.get(status)
    if kind is None or kind == previous:
        return
    payload = json.dumps({"coid": coid, "status": status, "filled_qty": str(filled_qty)}, sort_keys=True)
    with store.transaction() as conn:
        conn.execute("INSERT INTO ledger (kind, payload) VALUES (?, ?)", (kind, payload))


def record_resolution(store, *, coid: str, outcome: str, filled_qty: Decimal, evidence: str) -> None:
    """Retire one obligation. Callers write it only after its accounting is durable."""
    payload = json.dumps(
        {"coid": coid, "outcome": outcome, "filled_qty": str(filled_qty), "evidence": evidence},
        sort_keys=True,
    )
    with store.transaction() as conn:
        conn.execute("INSERT INTO ledger (kind, payload) VALUES (?, ?)", (RESOLVED_KIND, payload))
