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
- A ticket is single use. Any presentation spends it, a mismatched one
  included, so the human confirms again rather than an agent retrying with
  other params.
- A ticket expires ``ttl_s`` seconds (600) after it is issued, by the clock
  the engine injects into the ledger and the core.
- Tickets live in engine memory only: a new ledger (a restarted engine)
  knows none. Ticket ids come from :func:`secrets.token_urlsafe` and are
  matched with :func:`hmac.compare_digest`.
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
    "the confirm ticket was issued for another tool or other params, and is now spent; ask the human to confirm again"
)
MESSAGE_CONFIRM_PARAMS = (
    "a call that needs a human confirm takes plain JSON params "
    "(objects with string keys, arrays, strings, finite numbers, booleans, null)"
)

TICKET_TTL_S = 600

ConsumeOutcome = Literal["ok", "unknown", "mismatch"]


class ConfirmParamsError(ValueError):
    """The params have no exact plain-JSON form, so no ticket can be bound to them."""


@dataclass(frozen=True)
class ConfirmTicket:
    """One single-use confirm ticket. ``params`` is the ledger's own copy."""

    id: str
    tool: str
    params: dict
    digest: str
    expires_at: float


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


class ConfirmLedger:
    """The engine's in-memory confirm tickets. One per engine process; thread safe."""

    def __init__(self, *, clock: Callable[[], float], ttl_s: int = TICKET_TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self._clock = clock
        self._ttl_s = ttl_s
        self._tickets: dict[str, ConfirmTicket] = {}
        self._lock = threading.Lock()

    def issue(self, actor: Actor, tool: str, params: dict) -> ConfirmTicket:
        """Issue a ticket for ``tool`` with exactly ``params``.

        Raises ``ValueError`` for a non-Krell actor, and
        :class:`ConfirmParamsError` for params that are not plain JSON.
        """
        if actor.kind != "krell":
            raise ValueError("only Krell receives confirm tickets")
        digest = params_digest(tool, params)
        with self._lock:
            now = self._clock()
            self._drop_expired(now)
            ticket = ConfirmTicket(
                id=secrets.token_urlsafe(16),
                tool=str(tool),
                params=copy.deepcopy(params),
                digest=digest,
                expires_at=now + self._ttl_s,
            )
            self._tickets[ticket.id] = ticket
        return ticket

    def consume(self, ticket_id: str, tool: str, params: dict) -> ConsumeOutcome:
        """Spend a ticket on ``tool`` with ``params``: ``"ok"``, ``"unknown"`` or ``"mismatch"``.

        ``"unknown"`` covers an id never issued, expired, already used,
        discarded or cleared. Any known, live ticket is spent here, whether
        it matches or not.
        """
        if not isinstance(ticket_id, str):
            return "unknown"
        with self._lock:
            self._drop_expired(self._clock())
            ticket = self._take(ticket_id)
        if ticket is None:
            return "unknown"
        try:
            digest = params_digest(tool, params)
        except ConfirmParamsError:
            return "mismatch"
        if ticket.tool != tool or not _same(ticket.digest, digest):
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

    def _take(self, ticket_id: str) -> ConfirmTicket | None:
        """Remove and return the ticket whose id matches; caller holds the lock."""
        found = None
        for key in self._tickets:
            if _same(key, ticket_id):
                found = key
        return None if found is None else self._tickets.pop(found)

    def _drop_expired(self, now: float) -> None:
        for key in [key for key, ticket in self._tickets.items() if now >= ticket.expires_at]:
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
