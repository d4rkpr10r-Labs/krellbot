"""Agent tool core: one typed registry and one call path for every agent.

Both front doors (the MCP adapter in ``krellbot.mcp`` and Krell in the engine)
call :meth:`AgentToolCore.call` with an :class:`~krellbot.agent.actors.Actor`,
so refusals, result labels and the audit trail are identical by construction.

``call`` runs these steps in order. Any step can end the call with a typed
result, and every call is audited:

1. Hard key and live markers in the tool name (``mcp_key_read_refused``,
   ``mcp_live_arm_refused``).
2. Unknown tool, or a tool not offered to this actor (``mcp_unknown_tool``).
3. Params: an object with string names. Secret-shaped names are refused
   (``agent_secret_param_refused``) before anything else sees them. Ids that
   become filesystem paths (revision ids, a draft's pack id) must be in their
   exact on-disk format (:func:`path_id_refusal`, ``mcp_invalid_request``).
4. Operator policy and agent limits: :meth:`AgentToolCore._policy_step`
   checks ``paper_arm`` and the paper change tools against the operator's
   agent limits (:mod:`krellbot.agent.limits`, typed ``agent_limit_*`` and
   ``agent_change_not_allowed`` refusals, journaled). ``paper_arm`` reads
   the pack's bytes once, checks them and arms an immutable snapshot of
   them (``<home>/agent-packs/<sha256>.json``). After the arm, the record is
   found by identity (venue, a new key, the snapshot path), checked again as
   stored and counted in ``agent-arms.json`` under a lock
   (:mod:`krellbot.agent.arm_registry`); anything wrong undoes it, or says
   it may still be running (``agent_arm_*`` codes).
5. The tool's own pre-check (:attr:`ToolSpec.precheck`), when it has one:
   a refusal that needs the tool's knowledge, before any ticket is issued.
   The paper change tools refuse a live (or non-paper) target here
   (``agent_live_change_refused``), so Krell never asks a human to confirm
   a call that cannot run. It runs on the asking call and on the confirmed
   call; a refusal on the confirmed call leaves the ticket unspent.
6. Confirm tickets: :meth:`AgentToolCore._confirm_step`. A Krell call whose
   tool changes state or whose work is heavy (cost class or estimate) is
   refused with ``needs_confirm`` and a single-use ticket bound to its exact
   params (:mod:`krellbot.agent.confirm`); the same call with that ticket
   runs once (``agent_confirm_*`` codes). A core without a ledger refuses
   such Krell calls. External MCP actors never get tickets.
7. The tool's handler.
8. One ``kind=agent_action`` journal record (:mod:`krellbot.agent.audit`).

Steps 4-7 run under one guard: an exception there becomes ``agent_tool_error``
(effect ``error``), and the audit step swallows its own failures, so ``call``
never raises.

The secret-name check matches substrings of top-level param names; confirm
tokens travel as the ``confirm_token`` keyword of ``call``, never inside params.

Handlers bind only to ``krellbot.application`` facades. This module does not
import ``run``, ``venues``, ``execution``, ``keys``, ``secrets.get``,
``live_gate`` or ``live_preflight``. The refusal codes below keep their
historic ``mcp_*`` names (``krellbot.mcp.errors`` re-exports them) because
agents and tests already depend on them.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from krellbot import sanitize
from krellbot.agent import arm_registry, limits
from krellbot.agent.actors import ACTOR_KINDS, Actor
from krellbot.agent.audit import append_agent_record, is_secret_param_name
from krellbot.agent.confirm import (
    CODE_CONFIRM_MISMATCH,
    CODE_CONFIRM_REQUIRED,
    CODE_CONFIRM_UNKNOWN,
    MESSAGE_CONFIRM_MISMATCH,
    MESSAGE_CONFIRM_PARAMS,
    MESSAGE_CONFIRM_UNAVAILABLE,
    MESSAGE_CONFIRM_UNKNOWN,
    MESSAGE_NEEDS_CONFIRM,
    ConfirmLedger,
    ConfirmParamsError,
    ConfirmTicket,
    confirm_limits,
)
from krellbot.agent.limits import (
    CODE_AGENT_ARM_CLEANUP_FAILED,
    CODE_AGENT_ARM_UNVERIFIED,
    CODE_AGENT_CHANGE_NOT_ALLOWED,
    CODE_AGENT_LIMIT_BALANCE,
    CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED,
    CODE_AGENT_LIMIT_MAX_ARMS,
    CODE_AGENT_LIMIT_PAIR,
    CODE_AGENT_LIMIT_RISK_CAP,
    CODE_AGENT_LIMIT_VENUE,
)
from krellbot.application import packs as packs_app
from krellbot.application import user_policy
from krellbot.application.paper import (
    CODE_LOSS_LIMIT_ROLLBACK_FAILED,
    CODE_NOT_ARMED,
    PaperService,
    arm_keys,
    arm_terms,
    new_arms_of,
    parse_daily_loss_limit_pct,
)
from krellbot.application.strategy import (
    DraftNotRunnable,
    RevisionNotFound,
    StrategyDraftService,
    StrategyIdMismatch,
)
from krellbot.application.user_policy import PolicyDecision
from krellbot.config import ArmedPack, load_config

# ---- refusal vocabulary -------------------------------------------------
#
# Refusals never carry key material or live grant payloads. Messages come
# from a closed set so an agent transcript cannot leak secrets.

SCHEMA_VERSION = "1"

CODE_LIVE_ARM_REFUSED = "mcp_live_arm_refused"
CODE_KEY_READ_REFUSED = "mcp_key_read_refused"
CODE_UNKNOWN_TOOL = "mcp_unknown_tool"
CODE_INVALID_REQUEST = "mcp_invalid_request"
CODE_POLICY_DENIED = "user_policy_denied"
CODE_SECRET_PARAM_REFUSED = "agent_secret_param_refused"
CODE_TOOL_ERROR = "agent_tool_error"

MESSAGE_LIVE_ARM_REFUSED = "MCP refuses live arm; paper-arm only"
MESSAGE_KEY_READ_REFUSED = "MCP refuses key read or key export"
MESSAGE_UNKNOWN_TOOL = "MCP tool is not in the allowlist"
MESSAGE_SECRET_PARAM_REFUSED = (
    "agent tools refuse parameter names that look like secrets "
    "(key, secret, token, password, credential, authorization)"
)
MESSAGE_TOOL_ERROR = "agent tool failed with an internal error"


@dataclass(frozen=True)
class ToolRefusal:
    """Closed refusal returned by the core (never raised as opaque text)."""

    code: str
    message: str
    tool: str

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "effect": "refused",
            "code": self.code,
            "message": self.message,
            "tool": self.tool,
        }


def live_arm_refused(tool: str) -> ToolRefusal:
    return ToolRefusal(CODE_LIVE_ARM_REFUSED, MESSAGE_LIVE_ARM_REFUSED, tool)


def key_read_refused(tool: str) -> ToolRefusal:
    return ToolRefusal(CODE_KEY_READ_REFUSED, MESSAGE_KEY_READ_REFUSED, tool)


def unknown_tool(tool: str) -> ToolRefusal:
    return ToolRefusal(CODE_UNKNOWN_TOOL, MESSAGE_UNKNOWN_TOOL, tool)


def invalid_request(tool: str, message: str) -> ToolRefusal:
    return ToolRefusal(CODE_INVALID_REQUEST, message, tool)


def policy_denied(tool: str, message: str) -> ToolRefusal:
    return ToolRefusal(CODE_POLICY_DENIED, message, tool)


def secret_param_refused(tool: str) -> ToolRefusal:
    return ToolRefusal(CODE_SECRET_PARAM_REFUSED, MESSAGE_SECRET_PARAM_REFUSED, tool)


# ---- ids that become filesystem paths -----------------------------------
#
# Agents pass untrusted, possibly prompt-injected text. Every id that a store
# joins into a filesystem path is checked against its exact on-disk format in
# step 3, before any handler runs. The refusal message never echoes the value.

# ``StrategyDraftService._revision_id`` is ``sha256(...).hexdigest()``.
REVISION_ID_PATTERN = r"[0-9a-f]{64}"
# ``pack/schema.json`` ``id``; the draft store creates ``drafts/<pack id>/``
# from it before any lint runs.
PACK_ID_PATTERN = r"[a-z0-9][a-z0-9-]{0,31}"
_REVISION_ID_RE = re.compile(REVISION_ID_PATTERN)
_PACK_ID_RE = re.compile(PACK_ID_PATTERN)

# Param names checked for every tool, so a later tool taking one of these
# names is covered without extra wiring.
REVISION_ID_PARAMS: tuple[str, ...] = ("revision_id", "parent_revision_id")
REVISION_ID_LIST_PARAMS: tuple[str, ...] = ("revision_ids",)
PACK_PARAMS: tuple[str, ...] = ("pack",)

MESSAGE_BAD_REVISION_ID = "revision ids are exactly 64 lowercase hex characters"
MESSAGE_BAD_PACK_ID = "pack id must be 1-32 characters of a-z, 0-9 and '-', starting with a letter or digit"


def is_revision_id(value: object) -> bool:
    """True for a draft revision id in its exact on-disk form."""
    return isinstance(value, str) and _REVISION_ID_RE.fullmatch(value) is not None


def is_pack_id(value: object) -> bool:
    """True for a pack id that matches the pack schema's ``id`` pattern."""
    return isinstance(value, str) and _PACK_ID_RE.fullmatch(value) is not None


def path_id_refusal(tool: str, params: dict) -> dict | None:
    """Refuse any filesystem-bound id that is not in its exact on-disk format.

    A falsy value passes, because every handler treats it as "not given" and
    never joins it into a path. Returns the refusal result, or ``None``.
    """
    for name in REVISION_ID_PARAMS:
        value = params.get(name)
        if value and not is_revision_id(value):
            return invalid_request(tool, f"{name}: {MESSAGE_BAD_REVISION_ID}").to_dict()
    for name in REVISION_ID_LIST_PARAMS:
        value = params.get(name)
        if value and not (isinstance(value, list | tuple) and all(is_revision_id(v) for v in value)):
            return invalid_request(tool, f"{name}: {MESSAGE_BAD_REVISION_ID}").to_dict()
    for name in PACK_PARAMS:
        pack = params.get(name)
        if isinstance(pack, dict):
            pack_id = pack.get("id")
            if pack_id and not is_pack_id(pack_id):
                return invalid_request(tool, f"{name}: {MESSAGE_BAD_PACK_ID}").to_dict()
    return None


def limit_refused(tool: str, decision: PolicyDecision) -> dict:
    """The tool result for an agent-limit refusal (already journaled)."""
    return ToolRefusal(decision.code, decision.message, tool).to_dict()


def _tool_error(tool: str, cause: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "effect": "error",
        "code": CODE_TOOL_ERROR,
        "message": f"{MESSAGE_TOOL_ERROR} ({cause})",
        "tool": tool,
    }


# ---- tool model ---------------------------------------------------------

LABEL_SIMULATED = "simulated"
LABEL_NOT_ADVICE = "not_advice"
LABEL_PAPER = "paper"
LABELS: frozenset[str] = frozenset({LABEL_SIMULATED, LABEL_NOT_ADVICE, LABEL_PAPER})

Effect = Literal["read", "research", "change"]
Cost = Literal["light", "heavy"]
EFFECTS: tuple[str, ...] = ("read", "research", "change")
COSTS: tuple[str, ...] = ("light", "heavy")


@dataclass(frozen=True)
class ToolContext:
    """What a handler may use: the home, the caller, the clock and the fetcher.

    ``call_state`` is scratch for one call only, shared by its policy step,
    confirm step and handler (``paper_arm`` hands the pack it checked to the
    handler this way, so the handler arms exactly that).
    """

    home: Path
    actor: Actor
    clock: Callable[[], float]
    fetch: Callable[..., Any] | None
    call_state: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class ToolSpec:
    """One agent tool: its classes, result labels, schema and handler.

    ``effect`` and ``cost`` drive the policy and confirm steps. ``labels`` are
    copied onto every result of the tool, refusals included. ``actors`` names
    the actor kinds the tool is offered to. ``precheck``, when set, runs after
    the policy step and before the confirm step, on a copy of the params: it
    returns a refusal result to stop the call, or ``None`` to continue. It
    must not change state; the handler re-checks whatever it relies on.
    """

    name: str
    effect: Effect
    cost: Cost
    input_schema: dict
    labels: tuple[str, ...]
    description: str
    handler: Callable[[ToolContext, dict], dict]
    actors: frozenset[str] = frozenset({"krell", "mcp"})
    precheck: Callable[[ToolContext, dict], dict | None] | None = None


# ---- hard key / live markers (M22.1) ------------------------------------

_TOOL_ALIASES: dict[str, str] = {
    "paper-arm": "paper_arm",
    "paperarm": "paper_arm",
    "arm_paper": "paper_arm",
}

# Any of these parts / exact names are treated as key or live surfaces and
# refuse with the matching typed code (never dispatched).
_KEY_TOOL_MARKERS: tuple[str, ...] = (
    "key",
    "keys",
    "secret",
    "secrets",
    "credential",
    "credentials",
    "keyring",
    "export_key",
    "export_keys",
    "read_key",
    "read_keys",
)
_LIVE_TOOL_MARKERS: tuple[str, ...] = (
    "live",
    "live_arm",
    "live.arm",
    "arm_live",
)


def normalize_tool_name(name: object) -> str:
    """Lowercase, spaces and dashes to underscores, known aliases resolved."""
    raw = str(name or "").strip()
    lowered = raw.lower().replace(" ", "_")
    if lowered in _TOOL_ALIASES:
        return _TOOL_ALIASES[lowered]
    return lowered.replace("-", "_")


def _is_key_tool(normalized: str) -> bool:
    compact = normalized.replace(".", "_").replace("-", "_")
    parts = [p for p in compact.split("_") if p]
    if any(p in _KEY_TOOL_MARKERS for p in parts):
        return True
    return any(marker in compact for marker in ("secrets", "keyring", "credential"))


def _is_live_tool(normalized: str) -> bool:
    compact = normalized.replace(".", "_").replace("-", "_")
    if compact in _LIVE_TOOL_MARKERS:
        return True
    parts = [p for p in compact.split("_") if p]
    return "live" in parts


# ---- the core -----------------------------------------------------------


class AgentToolCore:
    """Typed tool registry plus the single audited call path.

    The five existing MCP verbs (read, draft, validate, backtest, paper_arm)
    are registered on construction; later tools are added with
    :meth:`register` (``compare`` by
    :func:`krellbot.agent.tools_research.register_research_tools`).
    ``confirm`` is the engine's confirm-ticket ledger (:meth:`_confirm_step`),
    built with the same clock. A core without one (``McpTools``) never issues
    tickets and refuses Krell's change and heavy calls.
    """

    def __init__(
        self,
        home: Path,
        *,
        clock: Callable[[], float] | None = None,
        fetch: Callable[..., Any] | None = None,
        confirm: ConfirmLedger | None = None,
    ) -> None:
        self._home = Path(home)
        self._clock: Callable[[], float] = clock or time.time
        self._fetch = fetch
        self._confirm = confirm
        self._specs: dict[str, ToolSpec] = {}
        for spec in _builtin_specs():
            self.register(spec)

    @property
    def home(self) -> Path:
        return self._home

    def register(self, spec: ToolSpec) -> None:
        """Add a tool. Raises ``ValueError`` for a malformed or unsafe spec."""
        name = spec.name
        if not name or normalize_tool_name(name) != name:
            raise ValueError(f"tool name must be lowercase snake_case: {name!r}")
        if _is_key_tool(name) or _is_live_tool(name):
            raise ValueError(f"tool name looks like a key or live surface: {name!r}")
        if name in self._specs:
            raise ValueError(f"tool already registered: {name!r}")
        if spec.effect not in EFFECTS:
            raise ValueError(f"unknown effect class for {name!r}: {spec.effect!r}")
        if spec.cost not in COSTS:
            raise ValueError(f"unknown cost class for {name!r}: {spec.cost!r}")
        if not set(spec.labels) <= LABELS:
            raise ValueError(f"unknown result label for {name!r}: {spec.labels!r}")
        if not spec.actors or not set(spec.actors) <= ACTOR_KINDS:
            raise ValueError(f"unknown actor kind for {name!r}: {sorted(spec.actors)!r}")
        self._specs[name] = spec

    def has(self, name: str) -> bool:
        """True when a tool named exactly ``name`` is registered (for any actor)."""
        return name in self._specs

    def specs(self, actor: Actor) -> list[ToolSpec]:
        """Tools offered to ``actor``, in registration order."""
        return [spec for spec in self._specs.values() if actor.kind in spec.actors]

    def call(
        self,
        actor: Actor,
        tool: str,
        params: dict,
        *,
        confirm_token: str | None = None,
    ) -> dict:
        """Run one tool call for ``actor``. Never raises; always audited.

        The result keeps the MCP keys (``schema_version``, ``ok``, ``effect``,
        ``tool``, plus ``code``/``message`` on refusal) and adds ``actor``
        (the actor label) and ``labels`` (the tool's result labels).
        """
        normalized = normalize_tool_name(tool)
        spec = self._specs.get(normalized)
        if spec is not None and actor.kind not in spec.actors:
            spec = None
        result, ticket, failed = self._dispatch(actor, normalized, spec, params, confirm_token)
        result["actor"] = actor.label
        result["labels"] = list(spec.labels) if spec is not None else []
        self._audit(actor, normalized, params, result, failed=failed, confirm_ticket=ticket)
        return result

    # ---- call steps -----------------------------------------------------

    def _dispatch(
        self,
        actor: Actor,
        normalized: str,
        spec: ToolSpec | None,
        params: object,
        confirm_token: str | None,
    ) -> tuple[dict, str | None, bool]:
        """Steps 1-7. Returns ``(result, confirm ticket used, handler failed)``."""
        # 1. Hard key and live markers (registration refuses such names too).
        if _is_key_tool(normalized):
            return key_read_refused(normalized).to_dict(), None, False
        if _is_live_tool(normalized):
            return live_arm_refused(normalized).to_dict(), None, False
        # 2. Unknown tool, or one this actor is not offered.
        if spec is None:
            return unknown_tool(normalized).to_dict(), None, False
        # 3. Params shape, then secret-shaped names, before any handler.
        if not isinstance(params, dict):
            return invalid_request(spec.name, "tool params must be an object").to_dict(), None, False
        if any(not isinstance(name, str) for name in params):
            return invalid_request(spec.name, "tool param names must be strings").to_dict(), None, False
        if any(is_secret_param_name(name) for name in params):
            return secret_param_refused(spec.name).to_dict(), None, False
        refusal = path_id_refusal(spec.name, params)
        if refusal is not None:
            return refusal, None, False

        ctx = ToolContext(home=self._home, actor=actor, clock=self._clock, fetch=self._fetch)
        ticket: str | None = None
        # Steps 4-7 share one guard: an exception in a seam or a handler, or a
        # non-dict result from any of them, becomes ``agent_tool_error``.
        try:
            # 4. Operator policy and agent limits.
            result = self._policy_step(ctx, spec, params)
            # 5. The tool's own pre-check, on a copy, before any ticket is issued or spent.
            if result is None and spec.precheck is not None:
                result = spec.precheck(ctx, dict(params))
            # 6. Confirm tickets.
            if result is None:
                result, ticket = self._confirm_step(ctx, spec, params, confirm_token)
            # 7. Handler, on a copy so it cannot change what the audit records.
            if result is None:
                result = spec.handler(ctx, dict(params))
        except Exception as exc:  # noqa: BLE001 — call() never raises; only the class name is kept.
            return _tool_error(spec.name, type(exc).__name__), ticket, True
        if not isinstance(result, dict):
            return _tool_error(spec.name, "malformed result"), ticket, True
        return result, ticket, False

    def _policy_step(self, ctx: ToolContext, spec: ToolSpec, params: dict) -> dict | None:
        """Step 4: operator policy and agent limits (:mod:`krellbot.agent.limits`).

        Return a refusal result to stop the call, or ``None`` to continue.

        ``paper_arm`` (:func:`_prepare_paper_arm`): this step itself refuses a
        live mode, an operator policy that denies paper-arm or is malformed
        (``user_policy.ensure_allowed``, which journals the one refuse row),
        bad input, and anything over the agent limits. It reads the pack's
        bytes once, checks those bytes, writes them to an immutable snapshot
        and leaves the prepared arm in ``ctx.call_state``; the handler arms
        exactly that snapshot, so the pack that was checked is the pack that
        is armed.

        The change tools are checked against the per-actor allow flags.
        """
        if spec.name == limits.TOOL_PAPER_ARM:
            prepared = _prepare_paper_arm(ctx, params)
            if isinstance(prepared, dict):
                return prepared
            ctx.call_state[_PREPARED_ARM] = prepared
            return None
        if spec.name in limits.CHANGE_TOOLS:
            decision = limits.check_agent_call(ctx.home, ctx.actor, spec.name, params, resolved={}, clock=ctx.clock)
            return None if decision is None else limit_refused(spec.name, decision)
        return None

    def _confirm_step(
        self,
        ctx: ToolContext,
        spec: ToolSpec,
        params: dict,
        confirm_token: str | None,
    ) -> tuple[dict | None, str | None]:
        """Step 6: confirm tickets (:mod:`krellbot.agent.confirm`).

        Return ``(stop, ticket_id)``. A non-``None`` ``stop`` (a
        ``needs_confirm`` or refusal result) ends the call before the
        handler; ``ticket_id`` names the consumed ticket for the audit record.

        Only Krell is asked. A Krell call needs a human confirm when the
        tool's effect is ``change`` or its work is heavy: its estimate
        (:func:`krellbot.agent.estimate.estimate`) is heavy, or its cost
        class is ``heavy`` (except ``backtest``, whose class is "heavy when
        long" and whose estimate sizes the call). Then:

        - no ledger on this core: refused, ``agent_confirm_required``, with
          no ticket (fail closed);
        - no token: the confirm card's limits are read, then a ticket is
          issued (so a failing policy read leaves no ticket) and
          ``needs_confirm`` returned. The ticket is bound to the params
          exactly as the agent sent them (params with no plain-JSON form are
          refused, ``mcp_invalid_request``) and, for ``paper_arm``, to the
          sha256 of the pack bytes the policy step checked (``pack_sha256``);
        - a token: the ledger spends it; ``agent_confirm_unknown`` or
          ``agent_confirm_mismatch`` (another tool, other params, or a pack
          file that now holds other bytes) refuse, ``ok`` lets the handler run.

        Expiry follows this core's clock: ``now`` is passed to the ledger.
        The policy step and the tool's pre-check run before this one on every
        call, so a ticketless ``paper_arm`` was already checked (and its
        prepared arm is dropped with this call's context), a live target of a
        change tool is refused before any ticket is issued, and a confirmed
        call is checked again; a refusal there never presents the ticket,
        which stays live. External MCP actors never get or spend tickets:
        their client asks the human before each call.
        """
        if ctx.actor.kind != "krell":
            return None, None
        # These modules import this one, so they are imported here, at call time.
        from krellbot.agent.estimate import estimate
        from krellbot.agent.tools_research import TOOL_BACKTEST

        cost = estimate(spec.name, params, home=ctx.home)
        # A backtest's class is "heavy when long": its estimate measures this
        # call (bars x packs; unknown size is heavy), so it decides. For every
        # other tool a heavy cost class needs a confirm whatever the estimate.
        heavy = cost.cost == "heavy" or (spec.cost == "heavy" and spec.name != TOOL_BACKTEST)
        if spec.effect != "change" and not heavy:
            return None, None
        ledger = self._confirm
        if ledger is None:
            return ToolRefusal(CODE_CONFIRM_REQUIRED, MESSAGE_CONFIRM_UNAVAILABLE, spec.name).to_dict(), None
        pack_sha256 = _prepared_pack_sha256(ctx)
        if confirm_token is None:
            # Read the limits first: a failing policy read must not leave a ticket behind.
            agent_limits = confirm_limits(ctx.home, spec.name)
            try:
                ticket = ledger.issue(ctx.actor, spec.name, params, now=ctx.clock(), pack_sha256=pack_sha256)
            except ConfirmParamsError:
                return invalid_request(spec.name, MESSAGE_CONFIRM_PARAMS).to_dict(), None
            return _needs_confirm(ticket, cost.to_dict(), agent_limits), None
        verdict = ledger.consume(confirm_token, spec.name, params, now=ctx.clock(), pack_sha256=pack_sha256)
        if verdict == "ok":
            return None, confirm_token
        if verdict == "mismatch":
            return ToolRefusal(CODE_CONFIRM_MISMATCH, MESSAGE_CONFIRM_MISMATCH, spec.name).to_dict(), None
        return ToolRefusal(CODE_CONFIRM_UNKNOWN, MESSAGE_CONFIRM_UNKNOWN, spec.name).to_dict(), None

    def _audit(
        self,
        actor: Actor,
        tool: str,
        params: object,
        result: dict,
        *,
        failed: bool,
        confirm_ticket: str | None,
    ) -> None:
        """Step 8: append the ``agent_action`` record (``ts`` in whole seconds).

        Never raises: a failing clock falls back to wall time so the record is
        still written, and any other audit failure is dropped, as a read-only
        journal is for the user-policy receipts.
        """
        if failed:
            outcome = "error"
        elif result.get("effect") == "needs_confirm":
            outcome = "needs_confirm"
        elif result.get("ok") is True:
            outcome = "ok"
        else:
            outcome = "refused"
        try:
            ts = int(self._clock())
        except Exception:  # noqa: BLE001 — an injected clock must not cost the audit record.
            ts = int(time.time())
        try:
            safe_params = params if isinstance(params, dict) else {}
            append_agent_record(
                self._home,
                ts=ts,
                actor=actor,
                tool=sanitize.text(tool, max_len=len(tool)),
                params=safe_params,
                outcome=outcome,
                code=str(result.get("code") or outcome),
                venue=_venue_of(safe_params),
                confirm_ticket=confirm_ticket,
            )
        except Exception:  # noqa: BLE001 — call() never raises; the tool result stands without its record.
            return


def _needs_confirm(ticket: ConfirmTicket, cost: dict, agent_limits: dict) -> dict:
    """The ``needs_confirm`` result: what the confirm card shows, and the ticket to run it once."""
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "effect": "needs_confirm",
        "code": CODE_CONFIRM_REQUIRED,
        "message": MESSAGE_NEEDS_CONFIRM,
        "tool": ticket.tool,
        "confirm": {
            "ticket_id": ticket.id,
            "tool": ticket.tool,
            "params": copy.deepcopy(ticket.params),
            "estimate": cost,
            "limits": agent_limits,
            "expires_at": ticket.expires_at,
            "pack_sha256": ticket.pack_sha256,
        },
    }


def _prepared_pack_sha256(ctx: ToolContext) -> str | None:
    """The sha256 of the pack bytes this call's policy step checked and snapshotted, if it prepared an arm.

    It is the content address of the snapshot ``paper_arm`` arms, so a ticket
    bound to it is bound to the strategy, not to the path it was read from.
    """
    prepared = ctx.call_state.get(_PREPARED_ARM)
    if isinstance(prepared, _ArmRequest):
        return hashlib.sha256(prepared.pack_bytes).hexdigest()
    return None


def _venue_of(params: dict) -> str:
    venue = params.get("venue")
    if not isinstance(venue, str):
        return "-"
    # Redact the whole value before the cut, so a secret cannot straddle it.
    return sanitize.text(sanitize.redact(venue.strip()), max_len=64) or "-"


# ---- built-in verbs (moved from McpTools) -------------------------------


def _read(ctx: ToolContext, params: dict) -> dict:
    drafts = StrategyDraftService(ctx.home, clock=ctx.clock).list_owned_summaries()
    packs = packs_app.list_packs(ctx.home)
    config = load_config(ctx.home)
    armed: list[dict] = []
    for row in config.armed:
        armed.append(
            {
                "venue": row.venue,
                "pair": row.pair,
                "mode": row.mode,
                "pack_id": row.pack_id,
                "pack_version": row.pack_version,
                "entries_paused": bool(getattr(row, "entries_paused", False)),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "effect": "unchanged",
        "tool": "read",
        "drafts": drafts,
        "packs": packs,
        "armed": armed,
        "policy": user_policy.load_policy(ctx.home).to_dict(),
    }


def _draft(ctx: ToolContext, params: dict) -> dict:
    pack = params.get("pack")
    parent_revision_id = params.get("parent_revision_id")
    if not isinstance(pack, dict):
        return invalid_request("draft", "draft requires a pack object").to_dict()
    service = StrategyDraftService(ctx.home, clock=ctx.clock)
    try:
        if parent_revision_id:
            summary = service.edit(str(parent_revision_id), pack, origin=ctx.actor.label)
        else:
            summary = service.create(pack, origin=ctx.actor.label)
    except RevisionNotFound as exc:
        return invalid_request("draft", f"revision not found: {exc}").to_dict()
    except StrategyIdMismatch as exc:
        return invalid_request("draft", str(exc)).to_dict()
    except ValueError as exc:
        return invalid_request("draft", str(exc)).to_dict()
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "effect": "changed" if summary.get("outcome") == "created" else "unchanged",
        "tool": "draft",
        "result": summary,
    }


def _validate(ctx: ToolContext, params: dict) -> dict:
    revision_id = params.get("revision_id")
    if not revision_id:
        return invalid_request("validate", "validate requires revision_id").to_dict()
    service = StrategyDraftService(ctx.home, clock=ctx.clock)
    try:
        summary = service.validate(str(revision_id))
    except RevisionNotFound as exc:
        return invalid_request("validate", f"revision not found: {exc}").to_dict()
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "effect": "changed",
        "tool": "validate",
        "result": summary,
    }


@dataclass(frozen=True)
class _ArmRequest:
    """A ``paper_arm`` call resolved from one read of its pack's bytes.

    ``source_path`` is where the bytes were read (the agent's ``pack_path``
    or the draft revision file); ``snapshot`` is the immutable copy that is
    armed, set once the bytes passed the agent limits.
    """

    venue: str
    source_path: Path
    pack_bytes: bytes
    revision_id: str | None
    balance: Decimal | None
    daily_loss_limit_pct: Decimal | None
    snapshot: Path | None = None


# ``ctx.call_state`` key: the prepared arm the policy step hands the handler.
_PREPARED_ARM = "paper_arm.prepared"

MESSAGE_BAD_LOSS_LIMIT = "daily_loss_limit_pct must be a plain percent above 0 and at most 100, e.g. 5 or 2.5"
MAX_PACK_BYTES = 1024 * 1024
MESSAGE_PACK_UNREADABLE = "the pack must be a readable regular file of at most 1 MiB"


def read_regular_file_bounded(path: Path, max_bytes: int) -> bytes | None:
    """Read an agent-named file, bounded; ``None`` when it is not acceptable.

    Only a regular file (after following symlinks) of at most ``max_bytes``
    is read. The file is opened non-blocking and checked again on the open
    descriptor, so a path swapped for a FIFO or a device after the first
    check cannot block or flood the read, and at most one byte more than the
    limit is ever read.
    """
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, max_bytes + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    if total > max_bytes:
        return None
    return b"".join(chunks)


def _read_pack_bytes(path: Path) -> bytes | None:
    """Read an agent-named pack file of at most :data:`MAX_PACK_BYTES`, or ``None``."""
    return read_regular_file_bounded(path, MAX_PACK_BYTES)


def _arm_request(ctx: ToolContext, params: dict) -> _ArmRequest | dict:
    """Resolve a ``paper_arm`` call, or return its ``mcp_invalid_request`` refusal.

    The pack's bytes are read exactly once here; every check and the armed
    snapshot use those bytes.
    """
    venue = params.get("venue")
    if not venue:
        return invalid_request("paper_arm", "paper_arm requires venue").to_dict()

    revision_id = params.get("revision_id")
    pack_path = params.get("pack_path")
    if revision_id:
        drafts = StrategyDraftService(ctx.home, clock=ctx.clock)
        try:
            resolved_path, _pack = drafts.pack_for_arm(str(revision_id))
        except RevisionNotFound as exc:
            return invalid_request("paper_arm", f"revision not found: {exc}").to_dict()
        except DraftNotRunnable as exc:
            return invalid_request("paper_arm", str(exc)).to_dict()
    elif pack_path is not None:
        resolved_path = Path(pack_path)
    else:
        return invalid_request(
            "paper_arm",
            "paper_arm requires pack_path or revision_id",
        ).to_dict()
    pack_bytes = _read_pack_bytes(resolved_path)
    if pack_bytes is None:
        return invalid_request("paper_arm", MESSAGE_PACK_UNREADABLE).to_dict()

    paper_balance = params.get("paper_balance")
    balance: Decimal | None
    if paper_balance is None:
        balance = None
    else:
        try:
            balance = Decimal(str(paper_balance))
        except (InvalidOperation, ValueError):
            return invalid_request("paper_arm", "paper_balance must be a decimal").to_dict()

    raw_limit = params.get("daily_loss_limit_pct")
    loss_limit: Decimal | None = None
    if raw_limit is not None:
        try:
            loss_limit = parse_daily_loss_limit_pct(raw_limit)
        except ValueError:
            return invalid_request("paper_arm", MESSAGE_BAD_LOSS_LIMIT).to_dict()

    return _ArmRequest(
        venue=str(venue),
        source_path=resolved_path,
        pack_bytes=pack_bytes,
        revision_id=str(revision_id) if revision_id else None,
        balance=balance,
        daily_loss_limit_pct=loss_limit,
    )


def _paper_arm_policy_denied(decision: PolicyDecision) -> dict:
    """The historic ``user_policy_denied`` body for a denied (or malformed) paper-arm policy."""
    body = policy_denied("paper_arm", decision.message).to_dict()
    body["action"] = user_policy.ACTION_PAPER_ARM
    return body


def _prepare_paper_arm(ctx: ToolContext, params: dict) -> _ArmRequest | dict:
    """Check a ``paper_arm`` call and snapshot its pack; a refusal, or the prepared arm.

    Refuses, in the historic order and with the historic bodies: a live mode
    (``mcp_live_arm_refused``), an operator policy that denies paper-arm or is
    malformed (``user_policy.ensure_allowed``, which journals the one refuse
    row), bad input (``mcp_invalid_request``), then the agent limits. The
    limits read the pack bytes ``_arm_request`` read once (``arm_terms``); a
    value that cannot be read is refused, never guessed. Bytes that pass are
    written to ``<home>/agent-packs/<sha256>.json``, which is what is armed.
    """
    if str(params.get("mode", "paper")) != "paper":
        return live_arm_refused("paper_arm").to_dict()
    allowed = user_policy.ensure_allowed(ctx.home, user_policy.ACTION_PAPER_ARM)
    if not allowed.ok:
        return _paper_arm_policy_denied(allowed)
    request = _arm_request(ctx, params)
    if isinstance(request, dict):
        return request
    pair, risk_cap = arm_terms(request.pack_bytes, request.venue)
    resolved = {
        "venue": request.venue,
        "pair": pair or None,
        "paper_balance": request.balance,
        "risk_cap_pct": risk_cap,
        "daily_loss_limit_pct": request.daily_loss_limit_pct,
    }
    decision = limits.check_agent_call(
        ctx.home, ctx.actor, limits.TOOL_PAPER_ARM, params, resolved=resolved, clock=ctx.clock
    )
    if decision is not None:
        return limit_refused(limits.TOOL_PAPER_ARM, decision)
    return replace(request, snapshot=arm_registry.snapshot_pack(ctx.home, request.pack_bytes))


MESSAGE_ARM_NOT_SETTLED = "the agent arm could not be checked, counted or completed"
MESSAGE_ARM_FAILED_AFTER_WRITE = "the agent arm failed after its record was written"


def _shown(venue: str, pair: str | None = None) -> str:
    shown = sanitize.text(venue, max_len=32)
    return f"{shown} {sanitize.text(pair, max_len=32)}" if pair else shown


def _count_anyway(ctx: ToolContext, armed: ArmedPack, request: _ArmRequest) -> None:
    """Best effort: count an agent arm that may still be running, so later pre-checks see it."""
    try:
        arm_registry.record_agent_arm(
            ctx.home,
            venue=armed.venue,
            pair=armed.pair,
            actor=ctx.actor,
            armed_at_ts=armed.armed_at_ts,
            revision_id=request.revision_id,
        )
    except Exception:  # noqa: BLE001 — best effort; the refusal already says it may be running.
        return


def _journal_refusal(ctx: ToolContext, code: str, message: str) -> dict:
    """Journal an agent-limit refusal after an arm; never raises."""
    try:
        decision = limits.refuse(ctx.home, ctx.actor, limits.TOOL_PAPER_ARM, code, message, clock=ctx.clock)
    except Exception:  # noqa: BLE001 — the refusal must reach the agent even if the journal clock fails.
        decision = PolicyDecision(code, False, message, action=limits.TOOL_PAPER_ARM)
    return limit_refused(limits.TOOL_PAPER_ARM, decision)


def _undo_agent_arm(
    ctx: ToolContext, paper: PaperService, armed: ArmedPack, request: _ArmRequest, refusal: limits.Refusal
) -> dict:
    """Disarm this call's agent arm, which must not stand, and refuse with ``refusal``.

    If the disarm fails, the arm is counted anyway (best effort) and the
    refusal becomes ``agent_arm_cleanup_failed``, which says the arm may
    still be running; it never claims a disarm that did not happen.
    """
    code, message = refusal
    try:
        undone = paper.disarm(venue=armed.venue, pair=armed.pair, correlation_id="")
        disarmed = undone.ok or undone.code == CODE_NOT_ARMED
    except Exception:  # noqa: BLE001 — a failed cleanup is reported, never raised.
        disarmed = False
    shown = _shown(armed.venue, armed.pair)
    if disarmed:
        return _journal_refusal(ctx, code, f"{message}; the arm on {shown} was disarmed")
    _count_anyway(ctx, armed, request)
    return _journal_refusal(
        ctx,
        CODE_AGENT_ARM_CLEANUP_FAILED,
        f"{message}; disarming it failed, so the arm on {shown} may still be running",
    )


def _settle_agent_arm(
    ctx: ToolContext,
    request: _ArmRequest,
    paper: PaperService,
    *,
    before: set[tuple[str, str, int]],
    result: Any,
    failure: Exception | None,
) -> dict:
    """Settle an agent arm attempt and return the tool result.

    - A refusal (already armed, config busy, minimum not met, invalid pack,
      kill switch, policy...) wrote nothing, so it is returned unchanged and
      no record is touched. A record of the same snapshot can then only be
      another call's arm of byte-identical pack bytes.
    - Otherwise (success, an exception, or ``loss_limit_rollback_failed``),
      this call's record is identified by identity only: on
      ``request.venue``, a key not armed ``before`` the call, and
      ``pack_path`` equal to the snapshot. A record with any other
      ``pack_path`` is never touched.

      - No record after an exception: nothing was written; the exception is
        re-raised (``agent_tool_error``).
      - No record after a success or a failed rollback, more than one, or an
        unreadable config: ``agent_arm_cleanup_failed`` (may still be
        running); any matched records are counted.
      - One record after an exception or a failed rollback: undone.
      - One record after a success: the stored record is checked and the arm
        counted (:func:`limits.settle_agent_arm`); anything wrong undoes it.
    """
    ok = result is not None and result.ok
    rollback_failed = result is not None and result.code == CODE_LOSS_LIMIT_ROLLBACK_FAILED
    if failure is None and not ok and not rollback_failed:
        return _arm_result(result)
    try:
        mine = new_arms_of(load_config(ctx.home), venue=request.venue, pack_path=request.snapshot, before=before)
    except Exception:  # noqa: BLE001 — an unreadable config is reported below.
        mine = None
    if mine == [] and failure is not None:
        raise failure
    if mine is None or len(mine) != 1:
        for armed in mine or []:
            _count_anyway(ctx, armed, request)
        return _journal_refusal(
            ctx,
            CODE_AGENT_ARM_CLEANUP_FAILED,
            (
                f"the agent arm on {_shown(request.venue)} could not be identified in the stored config, "
                "so it was not checked or disarmed and may still be running; check the armed list"
            ),
        )
    armed = mine[0]
    if not ok:
        # An exception or a failed rollback left this call's record behind.
        return _undo_agent_arm(ctx, paper, armed, request, (CODE_AGENT_ARM_UNVERIFIED, MESSAGE_ARM_FAILED_AFTER_WRITE))
    try:
        refusal = limits.settle_agent_arm(ctx.home, ctx.actor, armed, revision_id=request.revision_id)
        if refusal is None and request.revision_id:
            try:
                StrategyDraftService(ctx.home, clock=ctx.clock).mark_deployed(request.revision_id)
            except (RevisionNotFound, DraftNotRunnable):
                pass
    except Exception:  # noqa: BLE001 — any failure after the arm undoes it.
        refusal = (CODE_AGENT_ARM_UNVERIFIED, MESSAGE_ARM_NOT_SETTLED)
    if refusal is not None:
        return _undo_agent_arm(ctx, paper, armed, request, refusal)
    return _arm_result(result)


def _arm_result(result: Any) -> dict:
    out = result.to_dict()
    out["tool"] = "paper_arm"
    return out


def _paper_arm(ctx: ToolContext, params: dict) -> dict:
    prepared = ctx.call_state.pop(_PREPARED_ARM, None)
    if isinstance(prepared, _ArmRequest):
        # A deny since the policy step still wins.
        decision = user_policy.ensure_allowed(ctx.home, user_policy.ACTION_PAPER_ARM)
        if not decision.ok:
            return _paper_arm_policy_denied(decision)
    else:
        # Called without the policy step: run every check here, on one read.
        prepared = _prepare_paper_arm(ctx, params)
        if isinstance(prepared, dict):
            return prepared
    correlation_id = params.get("correlation_id", "") or ""

    paper = PaperService(home=ctx.home, clock=ctx.clock)
    before = arm_keys(load_config(ctx.home))
    result = None
    failure: Exception | None = None
    try:
        result = paper.arm(
            prepared.snapshot,
            venue=prepared.venue,
            mode="paper",
            paper_balance=prepared.balance,
            correlation_id=correlation_id,
            daily_loss_limit_pct=prepared.daily_loss_limit_pct,
            source_path=prepared.source_path,
        )
    except Exception as exc:  # noqa: BLE001 — settled below by identity, never left running unreported.
        failure = exc
    # Every core caller is an agent: settle the arm by identity.
    return _settle_agent_arm(ctx, prepared, paper, before=before, result=result, failure=failure)


def _builtin_specs() -> tuple[ToolSpec, ...]:
    """The five existing MCP verbs. Schemas are built fresh per core.

    ``backtest`` lives with the other research tools in
    :mod:`krellbot.agent.tools_research`, which imports this module, so it is
    imported here, when a core is built.
    """
    from krellbot.agent.tools_research import backtest_spec

    return (
        ToolSpec(
            name="read",
            effect="read",
            cost="light",
            input_schema={"type": "object", "properties": {}},
            labels=(),
            description=(
                "Read the local inventory: strategy drafts, installed packs, armed strategies "
                "and the operator policy. Changes nothing."
            ),
            handler=_read,
        ),
        ToolSpec(
            name="draft",
            effect="research",
            cost="light",
            input_schema={
                "type": "object",
                "properties": {
                    "pack": {"type": "object", "description": "Strategy pack JSON."},
                    "parent_revision_id": {"type": "string", "description": "Revision to edit."},
                },
                "required": ["pack"],
            },
            labels=(),
            description="Save a strategy pack as a draft revision, or edit one. A draft never trades.",
            handler=_draft,
        ),
        ToolSpec(
            name="validate",
            effect="research",
            cost="light",
            input_schema={
                "type": "object",
                "properties": {"revision_id": {"type": "string"}},
                "required": ["revision_id"],
            },
            labels=(),
            description="Lint a stored draft revision. A clean pack becomes validated and runnable on paper.",
            handler=_validate,
        ),
        backtest_spec(),
        ToolSpec(
            name="paper_arm",
            effect="change",
            cost="heavy",
            input_schema={
                "type": "object",
                "properties": {
                    "revision_id": {"type": "string"},
                    "pack_path": {"type": "string"},
                    "venue": {"type": "string"},
                    "mode": {"type": "string", "enum": ["paper"]},
                    "paper_balance": {"type": ["string", "number"], "description": "Decimal paper balance."},
                    "daily_loss_limit_pct": {
                        "type": ["string", "number"],
                        "description": (
                            "Daily loss limit, a percent of the paper balance above 0 and at most 100. "
                            "The operator's agent limits require one by default."
                        ),
                    },
                    "correlation_id": {"type": "string"},
                },
                "required": ["venue"],
            },
            labels=(LABEL_PAPER,),
            description=(
                "Arm a validated draft or pack on paper (simulated funds) within the operator's agent limits. "
                "Live arm is always refused."
            ),
            handler=_paper_arm,
        ),
    )


__all__ = [
    "CODE_AGENT_ARM_CLEANUP_FAILED",
    "CODE_AGENT_ARM_UNVERIFIED",
    "CODE_AGENT_CHANGE_NOT_ALLOWED",
    "CODE_AGENT_LIMIT_BALANCE",
    "CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED",
    "CODE_AGENT_LIMIT_MAX_ARMS",
    "CODE_AGENT_LIMIT_PAIR",
    "CODE_AGENT_LIMIT_RISK_CAP",
    "CODE_AGENT_LIMIT_VENUE",
    "CODE_CONFIRM_MISMATCH",
    "CODE_CONFIRM_REQUIRED",
    "CODE_CONFIRM_UNKNOWN",
    "CODE_INVALID_REQUEST",
    "CODE_KEY_READ_REFUSED",
    "CODE_LIVE_ARM_REFUSED",
    "CODE_POLICY_DENIED",
    "CODE_SECRET_PARAM_REFUSED",
    "CODE_TOOL_ERROR",
    "CODE_UNKNOWN_TOOL",
    "COSTS",
    "EFFECTS",
    "LABELS",
    "LABEL_NOT_ADVICE",
    "LABEL_PAPER",
    "LABEL_SIMULATED",
    "MAX_PACK_BYTES",
    "MESSAGE_BAD_PACK_ID",
    "MESSAGE_BAD_REVISION_ID",
    "MESSAGE_KEY_READ_REFUSED",
    "MESSAGE_LIVE_ARM_REFUSED",
    "MESSAGE_PACK_UNREADABLE",
    "MESSAGE_SECRET_PARAM_REFUSED",
    "MESSAGE_TOOL_ERROR",
    "MESSAGE_UNKNOWN_TOOL",
    "PACK_ID_PATTERN",
    "PACK_PARAMS",
    "REVISION_ID_LIST_PARAMS",
    "REVISION_ID_PARAMS",
    "REVISION_ID_PATTERN",
    "SCHEMA_VERSION",
    "AgentToolCore",
    "Cost",
    "Effect",
    "ToolContext",
    "ToolRefusal",
    "ToolSpec",
    "invalid_request",
    "is_pack_id",
    "is_revision_id",
    "key_read_refused",
    "limit_refused",
    "live_arm_refused",
    "normalize_tool_name",
    "path_id_refusal",
    "policy_denied",
    "read_regular_file_bounded",
    "secret_param_refused",
    "unknown_tool",
]
