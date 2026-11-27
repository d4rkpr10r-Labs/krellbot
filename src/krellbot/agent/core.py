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
   (``agent_secret_param_refused``) before anything else sees them.
4. Operator policy and agent limits: :meth:`AgentToolCore._policy_step`
   (M38.3 plugs in here).
5. Confirm tickets: :meth:`AgentToolCore._confirm_step` (M38.7 plugs in here).
6. The tool's handler.
7. One ``kind=agent_action`` journal record (:mod:`krellbot.agent.audit`).

Handlers bind only to ``krellbot.application`` facades. This module does not
import ``run``, ``venues``, ``execution``, ``keys``, ``secrets.get``,
``live_gate`` or ``live_preflight``. The refusal codes below keep their
historic ``mcp_*`` names (``krellbot.mcp.errors`` re-exports them) because
agents and tests already depend on them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from krellbot import sanitize
from krellbot.agent.actors import ACTOR_KINDS, Actor
from krellbot.agent.audit import append_agent_record, is_secret_param_name
from krellbot.application import packs as packs_app
from krellbot.application import user_policy
from krellbot.application.paper import PaperService
from krellbot.application.research import ResearchRequest, ResearchService
from krellbot.application.strategy import (
    DraftNotRunnable,
    RevisionNotFound,
    StrategyDraftService,
    StrategyIdMismatch,
)
from krellbot.config import load_config

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

        ctx = ToolContext(home=self._home, actor=actor, clock=self._clock, fetch=self._fetch)
        # 4. Operator policy and agent limits.
        refusal = self._policy_step(ctx, spec, params)
        if refusal is not None:
            return refusal, None, False
        # 5. Confirm tickets.
        stop, ticket = self._confirm_step(ctx, spec, params, confirm_token)
        if stop is not None:
            return stop, ticket, False
        # 6. Handler.
        result, failed = self._run_handler(ctx, spec, params)
        return result, ticket, failed

    def _policy_step(self, ctx: ToolContext, spec: ToolSpec, params: dict) -> dict | None:
        """Step 4: operator policy and agent limits.

        Return a refusal result to stop the call, or ``None`` to continue.
        M38.3 adds the agent limits here. The existing paper-arm policy check
        stays inside the ``paper_arm`` handler (``user_policy.ensure_allowed``).
        """
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

    @staticmethod
    def _run_handler(ctx: ToolContext, spec: ToolSpec, params: dict) -> tuple[dict, bool]:
        try:
            result = spec.handler(ctx, dict(params))
        except Exception as exc:  # noqa: BLE001 — call() never raises; only the class name is kept.
            return _tool_error(spec.name, type(exc).__name__), True
        if not isinstance(result, dict):
            return _tool_error(spec.name, "malformed result"), True
        return result, False

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
        """Step 7: append the ``agent_action`` record (``ts`` in whole seconds)."""
        if failed:
            outcome = "error"
        elif result.get("effect") == "needs_confirm":
            outcome = "needs_confirm"
        elif result.get("ok") is True:
            outcome = "ok"
        else:
            outcome = "refused"
        safe_params = params if isinstance(params, dict) else {}
        append_agent_record(
            self._home,
            ts=int(self._clock()),
            actor=actor,
            tool=sanitize.text(tool, max_len=len(tool)),
            params=safe_params,
            outcome=outcome,
            code=str(result.get("code") or outcome),
            venue=_venue_of(safe_params),
            confirm_ticket=confirm_ticket,
        )


def _venue_of(params: dict) -> str:
    venue = params.get("venue")
    if not isinstance(venue, str):
        return "-"
    return sanitize.text(venue.strip(), max_len=64) or "-"


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


def _paper_arm(ctx: ToolContext, params: dict) -> dict:
    pack_path = params.get("pack_path")
    revision_id = params.get("revision_id")
    venue = params.get("venue")
    mode = params.get("mode", "paper")
    paper_balance = params.get("paper_balance")
    correlation_id = params.get("correlation_id", "")

    # Hard boundary: refuse before any paper/live service call.
    if str(mode) != "paper":
        return live_arm_refused("paper_arm").to_dict()

    decision = user_policy.ensure_allowed(ctx.home, user_policy.ACTION_PAPER_ARM)
    if not decision.ok:
        body = policy_denied("paper_arm", decision.message).to_dict()
        body["action"] = user_policy.ACTION_PAPER_ARM
        return body

    if not venue:
        return invalid_request("paper_arm", "paper_arm requires venue").to_dict()

    resolved_path: Path | None = None
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

    balance: Decimal | None
    if paper_balance is None:
        balance = None
    else:
        try:
            balance = Decimal(str(paper_balance))
        except (InvalidOperation, ValueError):
            return invalid_request("paper_arm", "paper_balance must be a decimal").to_dict()

    paper = PaperService(home=ctx.home, clock=ctx.clock)
    result = paper.arm(
        resolved_path,
        venue=str(venue),
        mode="paper",
        paper_balance=balance,
        correlation_id=correlation_id or "",
    )
    if revision_id and result.ok:
        try:
            StrategyDraftService(ctx.home, clock=ctx.clock).mark_deployed(str(revision_id))
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
                    "correlation_id": {"type": "string"},
                },
                "required": ["venue"],
            },
            labels=(LABEL_PAPER,),
            description="Arm a validated draft or pack on paper (simulated funds). Live arm is always refused.",
            handler=_paper_arm,
        ),
    )


__all__ = [
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
    "MESSAGE_KEY_READ_REFUSED",
    "MESSAGE_LIVE_ARM_REFUSED",
    "MESSAGE_SECRET_PARAM_REFUSED",
    "MESSAGE_TOOL_ERROR",
    "MESSAGE_UNKNOWN_TOOL",
    "SCHEMA_VERSION",
    "AgentToolCore",
    "Cost",
    "Effect",
    "ToolContext",
    "ToolRefusal",
    "ToolSpec",
    "invalid_request",
    "key_read_refused",
    "live_arm_refused",
    "normalize_tool_name",
    "policy_denied",
    "secret_param_refused",
    "unknown_tool",
]
