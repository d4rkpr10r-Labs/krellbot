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
one, is *ambiguous*; so is any record with a quantity that is not a finite,
nonnegative number. Ambiguous history is never credited to the identity being
projected, and its identity is never inferred from what the rest of the
journal happens to contain. Readable decreases still count, and an unusable
decrease drops the bound to zero at that point, so uncertainty can only shrink
what the recorded history supports. A projection with ambiguity cannot
establish that the strategy is flat; the tick holds new entries for it and
does not let requested open-order quantity stand in for executed inventory.

The projection describes recorded history only. Fills the engine never
journaled (for example paper stop-outs, which tick records as
``stop_qty: "0"``) are outside it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

_INCREASE_KEY = "entry_qty"
_DECREASE_KEYS = ("exit_qty", "stop_qty")
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
    """What the recorded journal history establishes for one identity.

    ``attributable_qty`` is the quantity the recorded history supports
    without crediting anything uncertain; it is never negative. It is a bound
    over *recorded* history only: fills the engine never journaled are not
    in it. ``ambiguous_records`` counts records for this pack whose identity
    or quantities could not be established and that may belong to it.
    """

    attributable_qty: Decimal
    ambiguous_records: int = 0

    @property
    def ambiguous(self) -> bool:
        return self.ambiguous_records > 0


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


def _qty(detail: dict, key: str) -> Decimal | None:
    """Return a finite, nonnegative quantity; ``None`` when it is unusable.

    An absent field is zero. A present field that is not a finite,
    nonnegative number is unusable: its true value is unknown.
    """
    raw = detail.get(key)
    if raw is None:
        return Decimal(0)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return value


def project_ownership(home: Path, identity: OwnershipIdentity) -> OwnershipProjection:
    """Replay journal ticks for ``identity`` and bound what they establish.

    Records are replayed in journal order. Each quantity field is read on its
    own, so an unusable field never discards a readable one.

    * Increases are credited only from records whose identity is established
      and whose ``entry_qty`` is usable.
    * Readable decreases always lower the bound, including from ambiguous
      records, so uncertainty can only shrink it.
    * An unusable decrease could have consumed everything held at that point,
      so the bound drops to zero there; later established entries still count.
    * The bound never goes below zero.
    """
    bound = Decimal(0)
    ambiguous_records = 0
    expected = (identity.venue, identity.pair, identity.mode)
    for rec in _tick_records(home):
        if rec.get("pack") != identity.pack_id:
            continue
        detail = rec.get("detail")
        if not isinstance(detail, dict):
            continue
        increase = _qty(detail, _INCREASE_KEY)
        decreases = [_qty(detail, key) for key in _DECREASE_KEYS]
        quantities = [increase, *decreases]
        if all(q is not None and q == 0 for q in quantities):
            continue
        recorded = (_text(rec.get("venue")), _text(detail.get("pair")), _record_mode(detail))
        if any(value is not None and value != want for value, want in zip(recorded, expected, strict=True)):
            continue  # provably another identity
        established = None not in recorded
        if not established or None in quantities:
            ambiguous_records += 1
        if established and increase is not None:
            bound += increase
        if any(d is None for d in decreases):
            bound = Decimal(0)
        else:
            bound -= sum(decreases, Decimal(0))
        bound = max(bound, Decimal(0))
    return OwnershipProjection(attributable_qty=bound, ambiguous_records=ambiguous_records)


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
