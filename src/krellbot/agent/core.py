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
   ``agent_change_not_allowed`` refusals, journaled). A successful agent arm
   is counted in ``agent-arms.json`` (:mod:`krellbot.agent.arm_registry`).
5. Confirm tickets: :meth:`AgentToolCore._confirm_step` (M38.7 plugs in here).
6. The tool's handler.
7. One ``kind=agent_action`` journal record (:mod:`krellbot.agent.audit`).

Steps 4-6 run under one guard: an exception there becomes ``agent_tool_error``
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

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from krellbot import sanitize
from krellbot.agent import arm_registry, limits
from krellbot.agent.actors import ACTOR_KINDS, Actor
from krellbot.agent.audit import append_agent_record, is_secret_param_name
from krellbot.agent.limits import (
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
from krellbot.application.paper import PaperService, arm_terms, parse_daily_loss_limit_pct
from krellbot.application.research import ResearchRequest, ResearchService
from krellbot.application.strategy import (
    DraftNotRunnable,
    RevisionNotFound,
    StrategyDraftService,
    StrategyIdMismatch,
)
from krellbot.application.user_policy import PolicyDecision
from krellbot.config import find_armed, load_config

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
    """What a handler may use: the home, the caller, the clock and the fetcher."""

    home: Path
    actor: Actor
    clock: Callable[[], float]
    fetch: Callable[..., Any] | None


@dataclass(frozen=True)
class ToolSpec:
    """One agent tool: its classes, result labels, schema and handler.

    ``effect`` and ``cost`` drive the policy and confirm steps. ``labels`` are
    copied onto every result of the tool, refusals included. ``actors`` names
    the actor kinds the tool is offered to.
    """

    name: str
    effect: Effect
    cost: Cost
    input_schema: dict
    labels: tuple[str, ...]
    description: str
    handler: Callable[[ToolContext, dict], dict]
    actors: frozenset[str] = frozenset({"krell", "mcp"})


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
    :meth:`register`. ``confirm`` is the confirm-ticket ledger that M38.7
    wires into :meth:`_confirm_step`; until then it is stored and unused.
    """

    def __init__(
        self,
        home: Path,
        *,
        clock: Callable[[], float] | None = None,
        fetch: Callable[..., Any] | None = None,
        confirm: Any | None = None,
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
        """Steps 1-6. Returns ``(result, confirm ticket used, handler failed)``."""
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
        # Steps 4-6 share one guard: an exception in a seam or a handler, or a
        # non-dict result from any of them, becomes ``agent_tool_error``.
        try:
            # 4. Operator policy and agent limits.
            result = self._policy_step(ctx, spec, params)
            # 5. Confirm tickets.
            if result is None:
                result, ticket = self._confirm_step(ctx, spec, params, confirm_token)
            # 6. Handler, on a copy so it cannot change what the audit records.
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
        ``paper_arm`` is checked against the agent limits with the values
        ``PaperService.arm`` will use; the change tools against the per-actor
        allow flags. The existing paper-arm policy check stays inside the
        ``paper_arm`` handler (``user_policy.ensure_allowed``): when it would
        refuse, this step steps aside so that refusal and its single journal
        row are exactly as before.
        """
        if spec.name == limits.TOOL_PAPER_ARM:
            return _paper_arm_limits(ctx, params)
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
        """Step 5: confirm tickets.

        Return ``(stop, ticket_id)``. A non-``None`` ``stop`` (a
        ``needs_confirm`` or refusal result) ends the call; ``ticket_id``
        names the consumed ticket for the audit record. M38.7 implements this
        with ``self._confirm``; until then every call proceeds unticketed.
        """
        return None, None

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
        """Step 7: append the ``agent_action`` record (``ts`` in whole seconds).

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
            summary = service.edit(str(parent_revision_id), pack)
        else:
            summary = service.create(pack)
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


def _backtest(ctx: ToolContext, params: dict) -> dict:
    pack_path = params.get("pack_path")
    dataset_csv = params.get("dataset_csv")
    if pack_path is None:
        return invalid_request("backtest", "backtest requires pack_path").to_dict()
    request = ResearchRequest(
        pack_path=Path(pack_path),
        dataset_csv=Path(dataset_csv) if dataset_csv is not None else None,
        venue=str(params.get("venue", "kraken")),
        pair=params.get("pair"),
        timeframe=params.get("timeframe"),
        allow_gaps=bool(params.get("allow_gaps", False)),
    )
    # Offline by default: no fetch means refuse without network/keyring.
    result = ResearchService(home=ctx.home, fetch=ctx.fetch).run(request)
    if not result.ok:
        refusal = result.refusal or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "effect": "refused",
            "tool": "backtest",
            "code": str(refusal.get("code") or "research_refused"),
            "message": str(refusal.get("message") or "backtest refused"),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "effect": "unchanged",
        "tool": "backtest",
        "legacy_receipt": result.legacy_receipt,
        "detail": result.detail,
    }


@dataclass(frozen=True)
class _ArmRequest:
    """A ``paper_arm`` call resolved to what ``PaperService.arm`` will use."""

    venue: str
    pack_path: Path
    revision_id: str | None
    balance: Decimal | None
    daily_loss_limit_pct: Decimal | None


MESSAGE_BAD_LOSS_LIMIT = "daily_loss_limit_pct must be a plain percent above 0 and at most 100, e.g. 5 or 2.5"


def _arm_request(ctx: ToolContext, params: dict) -> _ArmRequest | dict:
    """Resolve a ``paper_arm`` call, or return its ``mcp_invalid_request`` refusal.

    Shared by the policy step and the handler, so both see the same pack,
    balance and daily loss limit and refuse bad input identically.
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
        pack_path=resolved_path,
        revision_id=str(revision_id) if revision_id else None,
        balance=balance,
        daily_loss_limit_pct=loss_limit,
    )


def _paper_arm_limits(ctx: ToolContext, params: dict) -> dict | None:
    """Policy step for ``paper_arm``: the agent limits, or ``None`` to go on.

    A live mode, an operator policy that denies paper-arm (or is malformed),
    and bad input are left to the handler, which refuses them exactly as it
    did before the limits existed. Values are read from the pack the arm will
    use (``arm_terms``); one that cannot be read is refused, never guessed.
    """
    if str(params.get("mode", "paper")) != "paper":
        return None
    if not user_policy.check(ctx.home, user_policy.ACTION_PAPER_ARM).ok:
        return None
    request = _arm_request(ctx, params)
    if isinstance(request, dict):
        return request
    pair, risk_cap = arm_terms(request.pack_path, request.venue)
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
    return None if decision is None else limit_refused(limits.TOOL_PAPER_ARM, decision)


def _record_agent_arm(ctx: ToolContext, request: _ArmRequest, paper: PaperService, correlation_id: Any) -> dict | None:
    """Count a successful agent arm in ``agent-arms.json``.

    An arm that cannot be counted would slip past ``max_agent_arms``, so it
    is disarmed again and refused (journaled) instead.
    """
    pair, _cap = arm_terms(request.pack_path, request.venue)
    armed = find_armed(load_config(ctx.home), request.venue, pair) if pair else None
    if armed is None or armed.mode != "paper":
        return None
    try:
        arm_registry.record_agent_arm(
            ctx.home,
            venue=armed.venue,
            pair=armed.pair,
            actor=ctx.actor,
            armed_at_ts=armed.armed_at_ts,
            revision_id=request.revision_id,
        )
    except (OSError, arm_registry.AgentArmRegistryUnreadable):
        paper.disarm(venue=armed.venue, pair=armed.pair, correlation_id=correlation_id)
        decision = limits.refuse(
            ctx.home,
            ctx.actor,
            limits.TOOL_PAPER_ARM,
            CODE_AGENT_LIMIT_MAX_ARMS,
            limits.MESSAGE_ARM_NOT_RECORDED,
            clock=ctx.clock,
        )
        return limit_refused(limits.TOOL_PAPER_ARM, decision)
    return None


def _paper_arm(ctx: ToolContext, params: dict) -> dict:
    mode = params.get("mode", "paper")
    correlation_id = params.get("correlation_id", "") or ""

    # Hard boundary: refuse before any paper/live service call.
    if str(mode) != "paper":
        return live_arm_refused("paper_arm").to_dict()

    decision = user_policy.ensure_allowed(ctx.home, user_policy.ACTION_PAPER_ARM)
    if not decision.ok:
        body = policy_denied("paper_arm", decision.message).to_dict()
        body["action"] = user_policy.ACTION_PAPER_ARM
        return body

    request = _arm_request(ctx, params)
    if isinstance(request, dict):
        return request

    paper = PaperService(home=ctx.home, clock=ctx.clock)
    result = paper.arm(
        request.pack_path,
        venue=request.venue,
        mode="paper",
        paper_balance=request.balance,
        correlation_id=correlation_id,
        daily_loss_limit_pct=request.daily_loss_limit_pct,
    )
    if result.ok:
        # Every core caller is an agent: count the arm toward max_agent_arms.
        refused = _record_agent_arm(ctx, request, paper, correlation_id)
        if refused is not None:
            return refused
    if request.revision_id and result.ok:
        try:
            StrategyDraftService(ctx.home, clock=ctx.clock).mark_deployed(request.revision_id)
        except (RevisionNotFound, DraftNotRunnable):
            pass
    out = result.to_dict()
    out["tool"] = "paper_arm"
    return out


def _builtin_specs() -> tuple[ToolSpec, ...]:
    """The five existing MCP verbs. Schemas are built fresh per core."""
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
        ToolSpec(
            name="backtest",
            effect="research",
            cost="heavy",
            input_schema={
                "type": "object",
                "properties": {
                    "pack_path": {"type": "string"},
                    "dataset_csv": {"type": "string", "description": "Local OHLCV CSV."},
                    "venue": {"type": "string", "default": "kraken"},
                    "pair": {"type": "string"},
                    "timeframe": {"type": "string"},
                    "allow_gaps": {"type": "boolean", "default": False},
                },
                "required": ["pack_path"],
            },
            labels=(LABEL_SIMULATED,),
            description=(
                "Backtest a pack offline against a local dataset. Results are simulated, not market performance."
            ),
            handler=_backtest,
        ),
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
    "CODE_AGENT_CHANGE_NOT_ALLOWED",
    "CODE_AGENT_LIMIT_BALANCE",
    "CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED",
    "CODE_AGENT_LIMIT_MAX_ARMS",
    "CODE_AGENT_LIMIT_PAIR",
    "CODE_AGENT_LIMIT_RISK_CAP",
    "CODE_AGENT_LIMIT_VENUE",
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
    "MESSAGE_BAD_PACK_ID",
    "MESSAGE_BAD_REVISION_ID",
    "MESSAGE_KEY_READ_REFUSED",
    "MESSAGE_LIVE_ARM_REFUSED",
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
    "secret_param_refused",
    "unknown_tool",
]
