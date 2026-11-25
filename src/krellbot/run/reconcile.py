"""Reconcile journal state with venue state for one trading identity.

A strategy's "owned" quantity is `min(base balance on the venue for the pair,
qty this identity's journal says it filled)`. Anything else is the user's
other holdings and is never touched.

Ownership identity is `(pack, venue, pair, mode)`. The same pack and pair
armed on two venues, or in paper and live, owns two separate inventories.
Pack version is not part of the identity: a position bought under 1.0.0 stays
manageable after the pack is re-armed at 1.1.0.

A journal record is attributed only when its identity is supported by what
the record itself durably says:

* ``venue`` (record) and ``pair`` (detail) are written by every engine tick.
* ``mode`` (detail) has been written since 2026-11-25. Paper ticks have also
  carried ``decision_trace_bars`` since 2026-09-28, and the engine writes that
  key only for paper arms, so it is positive evidence of paper mode.

A record for this pack that matches every dimension it does carry, but lacks
one (or has an unreadable quantity), is *ambiguous*. Ambiguous history is
never credited to the identity being projected and is never inferred from
what the rest of the journal happens to contain. Its decreases still lower
the attributable quantity, so ambiguity can only shrink what the engine
believes it may sell, never grow it. A projection with ambiguity also cannot
establish that the strategy is flat; callers hold new entries for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

_QTY_INCREASE = ("entry_qty",)
_QTY_DECREASE = ("exit_qty", "stop_qty")
# Written into tick detail only when ``armed.mode == "paper"`` (run.tick).
_PAPER_ONLY_TRACE_KEY = "decision_trace_bars"


@dataclass(frozen=True)
class OwnershipIdentity:
    """The trading identity a strategy position belongs to."""

    pack_id: str
    venue: str
    pair: str
    mode: str


@dataclass(frozen=True)
class OwnershipProjection:
    """Journal-attributed ownership for one identity.

    ``attributable_qty`` is a conservative lower bound and never negative.
    ``ambiguous_records`` counts quantity-bearing records for this pack whose
    identity could not be established and that might belong to it.
    """

    attributable_qty: Decimal
    ambiguous_records: int = 0

    @property
    def ambiguous(self) -> bool:
        return self.ambiguous_records > 0


class _Unreadable(Exception):
    pass


def _journal_dir(home: Path) -> Path:
    return Path(home) / "journal"


def _tick_records(home: Path):
    journal_dir = _journal_dir(home)
    if not journal_dir.is_dir():
        return
    for path in sorted(journal_dir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("kind") == "tick":
                yield rec


def _text(raw: object) -> str | None:
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _record_mode(detail: dict) -> str | None:
    explicit = _text(detail.get("mode"))
    if explicit is not None:
        return explicit
    if _PAPER_ONLY_TRACE_KEY in detail:
        return "paper"
    return None


def _qty(detail: dict, key: str) -> Decimal:
    raw = detail.get(key)
    if raw is None:
        return Decimal(0)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise _Unreadable from None
    if not value.is_finite():
        raise _Unreadable
    return value


def project_ownership(home: Path, identity: OwnershipIdentity) -> OwnershipProjection:
    """Sum entry - exit - stop over journal ticks attributed to ``identity``."""
    attributable = Decimal(0)
    ambiguous_decrease = Decimal(0)
    ambiguous_records = 0
    expected = (identity.venue, identity.pair, identity.mode)
    for rec in _tick_records(home):
        if rec.get("pack") != identity.pack_id:
            continue
        detail = rec.get("detail")
        if not isinstance(detail, dict):
            continue
        try:
            increase = sum((_qty(detail, key) for key in _QTY_INCREASE), Decimal(0))
            decrease = sum((_qty(detail, key) for key in _QTY_DECREASE), Decimal(0))
            unreadable = False
        except _Unreadable:
            increase = decrease = Decimal(0)
            unreadable = True
        if not unreadable and increase == 0 and decrease == 0:
            continue
        recorded = (_text(rec.get("venue")), _text(detail.get("pair")), _record_mode(detail))
        if any(value is not None and value != want for value, want in zip(recorded, expected, strict=True)):
            continue  # provably another identity
        if unreadable or None in recorded:
            ambiguous_records += 1
            ambiguous_decrease += decrease
            continue
        attributable += increase - decrease
    return OwnershipProjection(
        attributable_qty=max(attributable - ambiguous_decrease, Decimal(0)),
        ambiguous_records=ambiguous_records,
    )


def reconcile_owned_qty(
    home: Path,
    identity: OwnershipIdentity,
    *,
    venue_owned_qty: Decimal,
) -> Decimal:
    """Return the canonical owned qty for ``identity``.

    The strategy owns `min(venue_owned_qty, attributable journal qty)`. Extra
    coins the user holds are not touched.
    """
    return min(venue_owned_qty, project_ownership(home, identity).attributable_qty)
