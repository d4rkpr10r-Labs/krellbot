"""Confirm tickets: Krell asks a human before change or heavy work (M38.7).

Krell is never an autopilot. When Krell calls a tool whose effect is
``change``, or whose work is heavy (the tool's cost class or its
:mod:`krellbot.agent.estimate`), the core refuses with ``needs_confirm`` and
issues a :class:`ConfirmTicket` from the engine's :class:`ConfirmLedger`. The
human presses Confirm, and the same call with that ticket runs once.

- A ticket is bound to ``sha256(canonical_receipt_bytes({"tool": tool,
  "params": params}))``, over the params exactly as the agent sent them,
  before any step normalises them. Only plain JSON params can be bound
  (objects with string keys, arrays, strings, finite numbers, booleans and
  null): anything else, such as a ``Decimal``, a tuple or ``NaN``, has no
  exact JSON form the confirm card could show, so :meth:`ConfirmLedger.issue`
  raises :class:`ConfirmParamsError` and :meth:`ConfirmLedger.consume`
  reports ``"mismatch"``.
- A ``paper_arm`` ticket is also bound to the content of the pack it arms:
  the sha256 of the pack bytes the policy step checked and snapshotted
  (``pack_sha256``, shown on the confirm card). A confirmed call whose
  ``pack_path`` now holds other bytes is a mismatch, though its params match.
- A ticket is single use. It is spent only when the core's confirm step
  presents it (:meth:`ConfirmLedger.consume`), and then whatever the
  verdict: ``"ok"`` (the call runs once) or ``"mismatch"`` (another tool,
  other params or other pack bytes; the human confirms again rather than an
  agent retrying with other params). A call refused before the confirm step
  never presents its ticket, which stays live until it expires: a hard
  refusal, bad params, or an operator policy or agent-limit refusal (the
  human may raise the limit and confirm again; the limits are checked again
  on that call). A token sent with a call that needs no confirm, or by an
  MCP actor, is ignored and not spent.
- A ticket expires ``ttl_s`` seconds (600) after it is issued. The core
  passes ``now`` from its own injectable clock to :meth:`ConfirmLedger.issue`
  and :meth:`ConfirmLedger.consume`, so expiry follows the core's clock; the
  ledger's ``clock`` is used only by a caller that passes no ``now``.
- At most :data:`MAX_LIVE_TICKETS_PER_ACTOR` (16) tickets per actor are
  live at once; issuing another evicts that actor's oldest, so repeated
  heavy requests cannot grow the ledger without bound.
- Tickets live in engine memory only: a new ledger (a restarted engine)
  knows none. Ticket ids come from :func:`secrets.token_urlsafe` and are
  matched with :func:`hmac.compare_digest`.
- The ticket id is for the human's confirm UI only and must never be
  returned to the model; the Krell loop (M38.15) strips it from the
  ``needs_confirm`` result before the model sees it.
- Only Krell receives tickets. External MCP actors need no Krellbot-side
  confirm, because their own client asks the human before each call.

The refusal codes live here; :mod:`krellbot.agent.core` re-exports them.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import math
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from krellbot.agent.actors import Actor
from krellbot.agent.limits import DISARM_TOOLS, PAUSE_RESUME_TOOLS, TOOL_PAPER_ARM
from krellbot.api.jobs import canonical_receipt_bytes
from krellbot.application import user_policy
from krellbot.application.user_policy import plain_decimal

CODE_CONFIRM_REQUIRED = "agent_confirm_required"
CODE_CONFIRM_UNKNOWN = "agent_confirm_unknown"
CODE_CONFIRM_MISMATCH = "agent_confirm_mismatch"

MESSAGE_NEEDS_CONFIRM = "Krell needs a human to confirm this call; the same call with the confirm ticket runs it once"
MESSAGE_CONFIRM_UNAVAILABLE = (
    "Krell needs a human to confirm this call, and this engine has no confirm ledger, so it is refused"
)
MESSAGE_CONFIRM_UNKNOWN = "the confirm ticket is unknown, expired or already used; ask the human to confirm again"
MESSAGE_CONFIRM_MISMATCH = (
    "the confirm ticket was issued for another tool, other params or other pack bytes, and is now spent; "
    "ask the human to confirm again"
)
MESSAGE_CONFIRM_PARAMS = (
    "a call that needs a human confirm takes plain JSON params "
    "(objects with string keys, arrays, strings, finite numbers, booleans, null)"
)

TICKET_TTL_S = 600
MAX_LIVE_TICKETS_PER_ACTOR = 16

ConsumeOutcome = Literal["ok", "unknown", "mismatch"]


class ConfirmParamsError(ValueError):
    """The params have no exact plain-JSON form, so no ticket can be bound to them."""


@dataclass(frozen=True)
class ConfirmTicket:
    """One single-use confirm ticket. ``params`` is the ledger's own copy.

    ``pack_sha256`` is the sha256 of the pack bytes a ``paper_arm`` call
    checked (``None`` for a call that arms no pack).
    """

    id: str
    tool: str
    params: dict
    digest: str
    expires_at: float
    pack_sha256: str | None = None


@dataclass(frozen=True)
class _Held:
    """A live ticket and the label of the actor it was issued to."""

    ticket: ConfirmTicket
    actor: str


def _require_plain_json(value: object) -> None:
    """Raise :class:`ConfirmParamsError` unless ``value`` is plain JSON."""
    stack = [value]
    while stack:
        item = stack.pop()
        if item is None or isinstance(item, str | bool | int):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ConfirmParamsError(MESSAGE_CONFIRM_PARAMS)
            continue
        if isinstance(item, list):
            stack.extend(item)
            continue
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ConfirmParamsError(MESSAGE_CONFIRM_PARAMS)
            stack.extend(item.values())
            continue
        raise ConfirmParamsError(MESSAGE_CONFIRM_PARAMS)


def params_digest(tool: str, params: object) -> str:
    """The binding of one call: hex sha256 of its canonical ``{"tool", "params"}`` bytes.

    Raises :class:`ConfirmParamsError` for params that are not plain JSON
    (or too deep or too large to encode).
    """
    if not isinstance(params, dict):
        raise ConfirmParamsError(MESSAGE_CONFIRM_PARAMS)
    _require_plain_json(params)
    try:
        encoded = canonical_receipt_bytes({"tool": str(tool), "params": params})
    except (RecursionError, ValueError, TypeError) as exc:
        raise ConfirmParamsError(MESSAGE_CONFIRM_PARAMS) from exc
    return hashlib.sha256(encoded).hexdigest()


def _same(a: str, b: str) -> bool:
    """Constant-time equality for two ticket ids or digests (any text)."""
    try:
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except UnicodeEncodeError:
        return False


def _same_pack(bound: str | None, presented: str | None) -> bool:
    """Both absent, or the same pack sha256."""
    if bound is None or presented is None:
        return bound is None and presented is None
    return isinstance(presented, str) and _same(bound, presented)


class ConfirmLedger:
    """The engine's in-memory confirm tickets. One per engine process; thread safe.

    ``clock`` is only a fallback for callers that pass no ``now``; the agent
    tool core always passes ``now`` from its own clock.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time, ttl_s: int = TICKET_TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self._clock = clock
        self._ttl_s = ttl_s
        self._tickets: dict[str, _Held] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        actor: Actor,
        tool: str,
        params: dict,
        *,
        now: float | None = None,
        pack_sha256: str | None = None,
    ) -> ConfirmTicket:
        """Issue a ticket for ``tool`` with exactly ``params`` (and pack bytes ``pack_sha256``).

        The ticket expires ``ttl_s`` after ``now``. If ``actor`` already holds
        :data:`MAX_LIVE_TICKETS_PER_ACTOR` live tickets, its oldest is evicted.
        Raises ``ValueError`` for a non-Krell actor, and
        :class:`ConfirmParamsError` for params that are not plain JSON.
        """
        if actor.kind != "krell":
            raise ValueError("only Krell receives confirm tickets")
        if pack_sha256 is not None and not isinstance(pack_sha256, str):
            raise ValueError("pack_sha256 must be a hex digest or None")
        digest = params_digest(tool, params)
        with self._lock:
            at = self._now(now)
            self._drop_expired(at)
            self._evict_oldest(actor.label)
            ticket = ConfirmTicket(
                id=secrets.token_urlsafe(16),
                tool=str(tool),
                params=copy.deepcopy(params),
                digest=digest,
                expires_at=at + self._ttl_s,
                pack_sha256=pack_sha256,
            )
            self._tickets[ticket.id] = _Held(ticket, actor.label)
        return ticket

    def consume(
        self,
        ticket_id: str,
        tool: str,
        params: dict,
        *,
        now: float | None = None,
        pack_sha256: str | None = None,
    ) -> ConsumeOutcome:
        """Spend a ticket on ``tool`` with ``params``: ``"ok"``, ``"unknown"`` or ``"mismatch"``.

        ``"unknown"`` covers an id never issued, expired at ``now``, already
        used, evicted, discarded or cleared. A known, live ticket is spent
        here whatever the verdict: ``"ok"``, or ``"mismatch"`` for another
        tool, other params or other pack bytes (``pack_sha256``).
        """
        if not isinstance(ticket_id, str):
            return "unknown"
        with self._lock:
            self._drop_expired(self._now(now))
            ticket = self._take(ticket_id)
        if ticket is None:
            return "unknown"
        try:
            digest = params_digest(tool, params)
        except ConfirmParamsError:
            return "mismatch"
        if ticket.tool != tool or not _same(ticket.digest, digest) or not _same_pack(ticket.pack_sha256, pack_sha256):
            return "mismatch"
        return "ok"

    def discard(self, ticket_id: str) -> None:
        """Drop one ticket (Not now); an unknown id is a no-op."""
        if not isinstance(ticket_id, str):
            return
        with self._lock:
            self._take(ticket_id)

    def clear(self) -> None:
        """Drop every ticket."""
        with self._lock:
            self._tickets.clear()

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else float(now)

    def _take(self, ticket_id: str) -> ConfirmTicket | None:
        """Remove and return the ticket whose id matches; caller holds the lock."""
        found = None
        for key in self._tickets:
            if _same(key, ticket_id):
                found = key
        return None if found is None else self._tickets.pop(found).ticket

    def _drop_expired(self, now: float) -> None:
        for key in [key for key, held in self._tickets.items() if now >= held.ticket.expires_at]:
            del self._tickets[key]

    def _evict_oldest(self, actor: str) -> None:
        """Make room for one more of ``actor``'s tickets; caller holds the lock.

        Tickets are kept in issue order, so the first of ``actor``'s are its oldest.
        """
        mine = [key for key, held in self._tickets.items() if held.actor == actor]
        for key in mine[: max(0, len(mine) - MAX_LIVE_TICKETS_PER_ACTOR + 1)]:
            del self._tickets[key]


def confirm_limits(home: Path, tool: str) -> dict:
    """The operator's agent limits that apply to a Krell ``tool`` call, for the confirm card."""
    agents = user_policy.load_policy(home).agents
    if tool == TOOL_PAPER_ARM:
        return {
            "max_paper_balance": plain_decimal(agents.max_paper_balance),
            "max_risk_cap_pct": plain_decimal(agents.max_risk_cap_pct),
            "require_daily_loss_limit": agents.require_daily_loss_limit,
            "max_agent_arms": agents.max_agent_arms,
        }
    if tool in PAUSE_RESUME_TOOLS:
        return {"krell_allow_pause_resume": agents.krell_allow_pause_resume}
    if tool in DISARM_TOOLS:
        return {"krell_allow_disarm": agents.krell_allow_disarm}
    return {}


__all__ = [
    "CODE_CONFIRM_MISMATCH",
    "CODE_CONFIRM_REQUIRED",
    "CODE_CONFIRM_UNKNOWN",
    "MAX_LIVE_TICKETS_PER_ACTOR",
    "MESSAGE_CONFIRM_MISMATCH",
    "MESSAGE_CONFIRM_PARAMS",
    "MESSAGE_CONFIRM_UNAVAILABLE",
    "MESSAGE_CONFIRM_UNKNOWN",
    "MESSAGE_NEEDS_CONFIRM",
    "TICKET_TTL_S",
    "ConfirmLedger",
    "ConfirmParamsError",
    "ConfirmTicket",
    "ConsumeOutcome",
    "confirm_limits",
    "params_digest",
]
