"""Loopback HTTP API factory and routes."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import re
import sys
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from krellbot import config as kb_config
from krellbot import release_policy
from krellbot.agent import audit as agent_audit
from krellbot.api.jobs import (
    JOB_ERROR_COSTS_UNAVAILABLE,
    JOB_ERROR_FILLS_UNAVAILABLE,
    JOB_ERROR_NOT_FOUND,
    JOB_ERROR_RESULT_UNAVAILABLE,
    JOB_ERROR_RUN_UNAVAILABLE,
    JOB_KIND_RESEARCH_BACKTEST,
    JobManager,
    QueueFull,
)
from krellbot.api.security import (
    ReconnectOffer,
    generate_csrf_token,
    generate_session_token,
    is_loopback_host,
    is_loopback_origin,
    issue_reconnect_offer,
    reconnect_offer_valid,
    reconnect_redeem_path,
)
from krellbot.api.tailnet import REMOTE_SCOPES, TailnetAccess, read_remote_scope, write_remote_scope
from krellbot.application import alerts as alerts_app
from krellbot.application import live_gate, live_guardrails
from krellbot.application import operations as operations_view_app
from krellbot.application import packs as app_packs
from krellbot.application import user_policy as user_policy_app
from krellbot.application.live_preflight import build_venue_preflight_transport
from krellbot.application.live_preflight import evaluate as live_preflight_evaluate
from krellbot.application.paper import (
    project_forward_paper_ledger,
    project_owned_position,
    project_paper_assumptions,
    project_paper_pnl_position_chart,
    project_portfolio_history,
)
from krellbot.application.strategy import (
    DraftNotRunnable,
    RevisionNotFound,
    StrategyDraftService,
    StrategyIdMismatch,
)
from krellbot.trust import trust_snapshot

SCHEMA_VERSION = "1"
SESSION_COOKIE = "krellbot_session"
CSRF_HEADER = "X-Krellbot-CSRF"


def _now_seconds() -> int:
    return int(time.time())


PAPER_COMMANDS = (
    "paper.arm",
    "paper.disarm",
    "paper.pause_entries",
    "paper.resume_entries",
    "paper.raise_stop",
    "paper.export",
)


@dataclass
class _AppState:
    home: Path
    port: int
    bootstrap_token: str
    bootstrap_used: bool = False
    sessions: dict[str, str] = field(default_factory=dict)
    # At most one outstanding short-lived reconnect offer (second browser).
    reconnect_offer: ReconnectOffer | None = None
    # Tests inject a fake clock; production uses wall time via `_now_seconds`.
    clock: Callable[[], int] | None = None
    jobs: JobManager = field(default=None)  # type: ignore[assignment]
    draft_service: object = field(default=None)  # StrategyDraftService singleton per home
    paper_service: object = field(default=None)  # PaperService singleton per home
    rehearsal_service: object = field(default=None)
    live_transport: object = field(default=None)  # test spy override for live.preflight
    # Test hook for ``positions.close``: ``armed_list -> (venue_obj, reader)``;
    # None builds the scheduler's own venue (``cli.venue_for_tick``).
    close_venue_opener: object = field(default=None)
    keys_transport: object = field(default=None)  # injectable venue transport for keys routes
    catalogue_transport: object = field(default=None)  # injectable signed-catalogue fetch
    checkout_transport: object = field(default=None)  # injectable payment transport (fake in CI)
    community_transport: object = field(default=None)  # injectable community index/pack bytes
    telemetry_transport: object = field(default=None)  # injectable telemetry POST (spy in CI)
    keys_allow_fake_backend: bool = False  # tests-only FakeKeyring opt-in
    scheduler_supervisor: object = None  # PlatformSupervisor; tests inject FakeSupervisor
    scheduler_write_root: Path | None = None  # defaults to Path.home()
    scheduler_executable: str | None = None  # defaults to sys.executable
    alert_notifier: object = None  # Notifier; tests inject FakeNotifier
    referral_transport: object = None  # referral.Transport; tests inject FakeTransport
    ai_transport: object = None  # ai.http.HttpTransport; tests inject FakeAiTransport
    # Sign-in flows (ai.oauth.SigninRegistry), in engine memory only; built on first use.
    ai_signins: object = None
    ai_signin_listener_factory: object = None  # ai.oauth.ListenerFactory; tests inject a socket-free fake


def _app_now(s: _AppState) -> int:
    """Return the app clock in unix seconds (injectable for reconnect TTL tests)."""

    if s.clock is not None:
        return int(s.clock())
    return _now_seconds()


def _state(app: FastAPI) -> _AppState:
    return app.state.krellbot


def _draft_service(s: _AppState) -> StrategyDraftService:
    """Lazily build the strategy-draft service bound to the app's home."""

    if s.draft_service is None:
        s.draft_service = StrategyDraftService(home=s.home)
    return s.draft_service


def _paper_service(s: _AppState):
    """Lazily build the paper service bound to the app's home."""

    if s.paper_service is None:
        from krellbot.application.paper import PaperService

        s.paper_service = PaperService(home=s.home)
    return s.paper_service


def _rehearsal_service(s: _AppState):
    if s.rehearsal_service is None:
        from krellbot.application.rehearsal import RehearsalService

        s.rehearsal_service = RehearsalService(home=s.home)
    return s.rehearsal_service


def _rehearsal_wire(body: dict) -> dict:
    """Adapt durable service truth to the existing rehearsalClient contract."""
    # `exited` describes the trading outcome, not a terminal Stop. The
    # existing UI disables Stop for `exited`; expose its true paused control
    # phase so the user can explicitly Stop after the rule closes ownership.
    return {
        **body,
        "position": body["position"]["qty"],
        "status": "paused" if body["status"] == "exited" else body["status"],
        "outcome": "exited" if body["status"] == "exited" else None,
    }


def _live_transport(s: _AppState, *, venue: str = "kraken"):
    """Resolve the live-preflight transport for ``venue``.

    Tests may pre-set ``s.live_transport`` to a spy. Production builds a
    venue-backed adapter over the injectable ``keys_transport`` (or the
    real HTTP transport) — never the legacy zero-USD ``SandboxTransport``.
    """

    if s.live_transport is not None:
        return s.live_transport
    return build_venue_preflight_transport(venue=venue, http_transport=s.keys_transport)


def _live_arm_gate_refusal(
    result: live_gate.LiveGateResult,
    *,
    home: Path | None = None,
    venue: str = "kraken",
    pair: str | None = None,
    pack_id: str = "-",
) -> JSONResponse:
    """Honest 403 body for a refused web ``live.arm`` gate check.

    When ``home`` is provided, journal the refusal with a closed reason
    code so the Deploy guardrails / journal surfaces can show it.
    """
    if home is not None and result.code:
        live_guardrails.journal_refusal(
            home,
            venue=venue,
            pack_id=pack_id,
            pair=pair,
            code=result.code,
            message=result.message,
            now=_now_seconds(),
        )
    return JSONResponse(
        {
            "schema_version": SCHEMA_VERSION,
            "code": result.code,
            "ok": False,
            "effect": "refused",
            "message": result.message,
            "detail": result.message,
        },
        status_code=403,
    )


# Typed web ``live.arm`` refusals for non-trade-only keys (M20.2).
# Distinct from the opaque ``live_arm_refused`` catch-all used for other
# arm_pack failures (missing stop/cap, etc.).
CODE_LIVE_ARM_KEY_WITHDRAW_CAPABLE = "live_arm_key_withdraw_capable"
CODE_LIVE_ARM_KEY_UNKNOWN_PERMISSION = "live_arm_key_unknown_permission"
CODE_LIVE_ARM_KEY_TRADE_OFF = "live_arm_key_trade_off"
CODE_LIVE_ARM_KEY_INVALID = "live_arm_key_invalid"
CODE_LIVE_ARM_KEY_MALFORMED = "live_arm_key_malformed"
CODE_LIVE_ARM_KEY_UNREACHABLE = "live_arm_key_unreachable"
CODE_LIVE_ARM_KEY_MISSING = "live_arm_key_missing"

_LIVE_ARM_KEY_REFUSALS: dict[str, tuple[str, str]] = {
    "withdraw_capable": (
        CODE_LIVE_ARM_KEY_WITHDRAW_CAPABLE,
        "key has withdraw rights; trade-only keys refused",
    ),
    "unknown_permission": (
        CODE_LIVE_ARM_KEY_UNKNOWN_PERMISSION,
        "key grants unrecognized permission; refused",
    ),
    "trade_off": (
        CODE_LIVE_ARM_KEY_TRADE_OFF,
        "key lacks required trade permission; refused",
    ),
    "invalid": (
        CODE_LIVE_ARM_KEY_INVALID,
        "key cannot be verified; refused",
    ),
    "malformed": (
        CODE_LIVE_ARM_KEY_MALFORMED,
        "venue response shape unknown; refused",
    ),
    "unreachable": (
        CODE_LIVE_ARM_KEY_UNREACHABLE,
        "venue unreachable; cannot verify key",
    ),
}


def _live_arm_key_probe(s: _AppState, venue: str) -> object | None:
    """Probe the stored key for ``venue`` via the injectable transport.

    Returns a ``KeyProbeResult`` when a key pair is readable, or ``None``
    when no usable key is stored. Never echoes credentials.
    """
    from krellbot import secrets as kb_secrets
    from krellbot.cli_keys import _probe

    try:
        api_key, api_secret = kb_secrets.get(venue)
    except (FileNotFoundError, ValueError, PermissionError):
        return None
    return _probe(venue, api_key, api_secret, transport=s.keys_transport)


def _live_arm_key_refusal(
    *,
    venue: str,
    pair: str,
    correlation_id: str,
    probe: object | None,
) -> JSONResponse | None:
    """Return a typed 403 when the live-arm key probe is not trade-only."""
    from krellbot.venues.base import KeyProbeResult

    if probe is None:
        msg = f"no key stored for {venue}"
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "correlation_id": correlation_id,
                "code": CODE_LIVE_ARM_KEY_MISSING,
                "ok": False,
                "effect": "refused",
                "message": msg,
                "detail": msg,
                "venue": venue,
                "pair": pair,
            },
            status_code=403,
        )
    if not isinstance(probe, KeyProbeResult):
        msg = "key check failed; refused"
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "correlation_id": correlation_id,
                "code": CODE_LIVE_ARM_KEY_MALFORMED,
                "ok": False,
                "effect": "refused",
                "message": msg,
                "detail": msg,
                "venue": venue,
                "pair": pair,
            },
            status_code=403,
        )
    if probe.is_trade_only:
        return None
    code, message = _LIVE_ARM_KEY_REFUSALS.get(
        probe.outcome,
        (CODE_LIVE_ARM_KEY_MALFORMED, "key refused"),
    )
    return JSONResponse(
        {
            "schema_version": SCHEMA_VERSION,
            "correlation_id": correlation_id,
            "code": code,
            "ok": False,
            "effect": "refused",
            "message": message,
            "detail": message,
            "venue": venue,
            "pair": pair,
        },
        status_code=403,
    )


def _live_arm_key_check(s: _AppState) -> Callable[[str], object]:
    """Build a key_check callable that uses the injectable keys transport.

    Defense-in-depth for ``arm_pack`` after the typed API probe. Returns
    ``KeyPerms`` (or ``None`` when no key is stored).
    """
    from krellbot.venues.base import KeyProbeResult

    def key_check(venue_arg: str) -> object:
        probe = _live_arm_key_probe(s, venue_arg)
        if probe is None:
            return None
        if isinstance(probe, KeyProbeResult):
            return probe.to_key_perms()
        return None

    return key_check


def _client_pack_basename(pack_path: str) -> str | None:
    """Return a safe ``*.json`` basename from a client pack_path, or None.

    Uses ``os.path.basename`` so directory components never reach a path
    expression; the name is joined only onto the trusted data home.
    """
    import os.path

    if type(pack_path) is not str or not pack_path:
        return None
    # CodeQL-recognized sanitizer: basename strips directory components.
    name = os.path.basename(pack_path)
    if not name or name in {".", ".."}:
        return None
    if not name.endswith(".json") or name.startswith("."):
        return None
    if os.path.basename(name) != name:
        return None
    return name


def _pair_from_pack_data(pack_data: object, venue: str) -> str | None:
    """Return the market pair for ``venue`` from an in-memory pack object."""
    if type(pack_data) is not dict:
        return None
    markets = pack_data.get("markets") or []
    if type(markets) is not list:
        return None
    for market in markets:
        if type(market) is dict and market.get("venue") == venue:
            pair = market.get("pair")
            if type(pair) is str and pair:
                return pair
    return None


def _scheduler_supervisor(s: _AppState):
    """Return the platform supervisor, creating the production default.

    Tests pre-set ``s.scheduler_supervisor`` to a ``FakeSupervisor`` so
    no ``systemctl`` / ``launchctl`` / ``schtasks`` process is spawned.
    """

    if s.scheduler_supervisor is None:
        from krellbot.service import supervise as kb_supervise

        s.scheduler_supervisor = kb_supervise.platform_supervisor()
    return s.scheduler_supervisor


def _scheduler_write_root(s: _AppState) -> Path:
    """Where unit files are written; defaults to the process user home."""

    if s.scheduler_write_root is None:
        return Path.home()
    return Path(s.scheduler_write_root)


def _emit_funnel(s: _AppState, event: str, *, sku: str = "") -> None:
    """Best-effort opt-in funnel telemetry. No-op when consent is off."""

    from krellbot import telemetry as kb_telemetry

    transport = s.telemetry_transport
    if transport is None:
        transport = kb_telemetry.resolve_transport(s.home, None)
    if transport is None:
        return
    kb_telemetry.maybe_send_funnel(s.home, event, sku=sku, transport=transport)


def _scheduler_executable(s: _AppState) -> str:
    """Executable path baked into rendered unit files."""

    if s.scheduler_executable:
        return s.scheduler_executable
    return sys.executable


def _stored_deployment_mode(home: Path) -> str | None:
    """Return the mode of the first armed pack, or None when no record exists.

    Used by ``live.preflight`` to honor the symmetric stored-live
    refusal (``stored_mode_not_sandbox``). The full-mode-only check is
    a deliberate read of the existing config bytes; the helper never
    mutates the config.
    """

    config = kb_config.load_config(home)
    if not config.armed:
        return None
    return config.armed[0].mode


def _resolve_dist_dir(dist_dir: Path | None) -> Path:
    """Resolve the frontend ``dist`` directory.

    Resolution order:

    1. An explicit ``dist_dir`` wins; callers (tests, operators) get
       exactly what they passed.
    2. A frozen process always resolves ``<sys._MEIPASS>/frontend/dist``.
       A missing ``index.html`` stays missing so the static route returns
       ``shell_not_built``. It does not fall back to a checkout, which
       would hide a bundle that omitted the shell.
    3. A non-frozen run returns the checkout ``<repo>/frontend/dist``.
       A missing build is handled by the existing static route. The
       resolver never synthesises HTML.
    """

    if dist_dir is not None:
        return Path(dist_dir)
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None) or ""
        return Path(meipass) / "frontend" / "dist"
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    return repo_root / "frontend" / "dist"


def _gate_state_change(request: Request, s: _AppState) -> Response | None:
    """Apply the session-cookie + CSRF + loopback-origin gate for state changes."""

    origin = request.headers.get("origin", "")
    if not is_loopback_origin(origin, port=s.port):
        return JSONResponse({"detail": "origin not loopback"}, status_code=403)

    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return JSONResponse({"detail": "session required"}, status_code=403)
    expected_csrf = s.sessions.get(session_id)
    presented_csrf = request.headers.get(CSRF_HEADER, "")
    if not expected_csrf or not hmac.compare_digest(expected_csrf, presented_csrf):
        return JSONResponse({"detail": "csrf required"}, status_code=403)

    return None


def _gate_get(request: Request, s: _AppState) -> Response | None:
    """Apply the session-cookie + loopback-origin gate for read endpoints.

    The cookie must be a key in ``s.sessions``: presence alone is not
    sufficient. A missing or invented cookie, or one that has been
    revoked (popped from the map) is 403 with body
    ``{"detail": "session required"}``. Browsers do not send ``Origin``
    on same-origin GET, so a missing ``Origin`` with a loopback ``Host``
    is allowed; a present non-loopback ``Origin`` is still 403.

    POST routes use ``_gate_state_change`` instead, which keeps the
    loopback-Origin + cookie-present + CSRF triad unchanged.
    """

    origin = request.headers.get("origin", "")
    if origin and not is_loopback_origin(origin, port=s.port):
        # A missing Origin is accepted (same-origin GETs ship without
        # one). A present non-loopback Origin is the only Origin case
        # we refuse; a present loopback Origin still passes through.
        return JSONResponse({"detail": "origin not loopback"}, status_code=403)
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id or session_id not in s.sessions:
        return JSONResponse({"detail": "session required"}, status_code=403)
    return None


def _measured_bind(request: Request) -> str | None:
    """Return the local address this request actually arrived on.

    Read from the ASGI ``server`` tuple, which uvicorn fills from the
    accepted socket's own address, so it is measured rather than the
    literal ``trust.bind``. Behind Tailscale Serve it is still the
    loopback listener. Anything that is not an IP literal (a test
    client's placeholder, a Unix socket path, no tuple) is ``None``.
    """

    server = request.scope.get("server")
    if not server:
        return None
    try:
        return str(ipaddress.ip_address(str(server[0])))
    except (ValueError, IndexError, TypeError):
        return None


def _remote_access_view(tailnet_access: TailnetAccess | None) -> dict[str, object]:
    """Describe opt-in Tailscale Serve access; never the owner login."""

    if tailnet_access is None:
        return {"mode": "off", "origin": None}
    return {"mode": "tailscale_serve", "origin": tailnet_access.origin}


def _paper_details_decimal(value: object) -> str | None:
    """Render a finite Decimal without exponent, padding, or precision loss."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if number.is_zero():
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _select_armed_for_request(
    config: kb_config.Config,
    request: Request,
) -> tuple[kb_config.ArmedPack | None, int]:
    """Pick the armed record a paper GET should project, with multi-deploy clarity.

    Without ``venue``+``pair`` query params, returns the first armed record
    (legacy single-projection behaviour) together with ``armed_count`` so
    the workstation can say when several deployments are armed. With both
    params, returns that exact record or ``None`` when it is not armed.
    A partial selector (only one of venue/pair) never guesses — returns
    ``None`` with the real count.
    """

    count = len(config.armed)
    if count == 0:
        return None, 0
    venue = (request.query_params.get("venue") or "").strip()
    pair = (request.query_params.get("pair") or "").strip()
    if venue and pair:
        return kb_config.find_armed(config, venue, pair), count
    if venue or pair:
        return None, count
    return config.armed[0], count


# The optional per-arm daily loss limit, a percent of the arm's starting cash
# (run/daily_loss.py). ``pct`` is a plain decimal string in (0, 100], or null
# to turn the limit off. It only ever holds new entries, so paper and live arms
# may both carry one.
DAILY_LOSS_LIMIT_COMMAND = "risk.set_daily_loss_limit"
_LOSS_LIMIT_PCT = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_LOSS_LIMIT_MESSAGES = {
    "bad_input": "Send venue, pair and pct: a percent as text, or null to turn the limit off.",
    "invalid_loss_limit": "Enter the daily loss limit as a plain percent above 0 and at most 100, e.g. 5 or 2.5.",
    "not_armed": "No strategy is armed on this venue and pair.",
    "starting_cash_unknown": "This run has no starting cash to take a percent of.",
    "config_busy": "Another command is updating this run. Try again in a moment.",
}


def _set_daily_loss_limit(s: _AppState, inner: dict) -> Response:
    """Handle ``risk.set_daily_loss_limit``; refusals are 200 bodies with ``ok: false``."""

    from krellbot.run import daily_loss as kb_daily_loss

    venue, pair, raw = inner.get("venue"), inner.get("pair"), inner.get("pct")
    pct: Decimal | None = None
    if not isinstance(venue, str) or not venue or not isinstance(pair, str) or not pair or "pct" not in inner:
        code = "bad_input"
    elif raw is not None and (
        not isinstance(raw, str) or not _LOSS_LIMIT_PCT.fullmatch(raw) or not Decimal(0) < Decimal(raw) <= 100
    ):
        code = "invalid_loss_limit"
    else:
        pct = None if raw is None else Decimal(raw)
        outcome = kb_config.set_daily_loss_limit(s.home, venue=venue, pair=pair, pct=pct)
        code = {"busy": "config_busy"}.get(outcome, outcome)
    if code in ("changed", "unchanged"):
        shown = None if pct is None else kb_daily_loss.plain(pct)
        message = (
            f"daily loss limit off: {venue} {pair}"
            if shown is None
            else f"daily loss limit set: {venue} {pair} -> {shown}% of starting cash"
        )
        body = {
            "code": "daily_loss_limit_off" if pct is None else "daily_loss_limit_set",
            "ok": True,
            "effect": code,
            "message": message,
            "daily_loss_limit_pct": shown,
        }
    else:
        body = {
            "code": code,
            "ok": False,
            "effect": "refused",
            "message": _LOSS_LIMIT_MESSAGES.get(code, code),
            "daily_loss_limit_pct": None,
        }
    return JSONResponse(
        {
            "schema_version": SCHEMA_VERSION,
            **body,
            "venue": venue if isinstance(venue, str) else None,
            "pair": pair if isinstance(pair, str) else None,
        },
        status_code=200,
        headers={"Cache-Control": "no-store"},
    )


def _with_armed_count(body: dict, armed_count: int) -> dict:
    """Attach ``armed_count`` only when more than one pack is armed.

    Single-armed and unarmed closed shapes stay byte-compatible with
    existing clients; multi-deploy clarity appears only when it matters.
    """

    if armed_count > 1:
        body["armed_count"] = armed_count
    return body


# ---- paper history projection (read-only) ---------------------------------

PAPER_HISTORY_MAX_RUNS = 20
PAPER_HISTORY_MAX_FILLS = 50


def _read_journal_records(home: Path) -> list[dict]:
    """Return every valid tick record under ``<home>/journal/*.jsonl``.

    Malformed lines and unreadable files are skipped, never fatal: the
    history view is read-only evidence over an append-only journal, so a
    torn last line must not take the panel down.
    """

    journal_dir = Path(home) / "journal"
    if not journal_dir.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(journal_dir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


# Closed feed failure codes the tick journals (cli.FEED_FAILURE_CODES).
_FEED_FAILURE_CODES = frozenset(
    {"timeout", "dns_or_network", "tls", "region_or_permission", "rate_limited", "venue_error"}
)


def _feed_failure_code(detail: dict) -> str | None:
    """Return the stored feed failure code if it is in the closed set."""
    value = detail.get("failure_code")
    return value if isinstance(value, str) and value in _FEED_FAILURE_CODES else None


def _tick_refusal_code(record: dict) -> str | None:
    """Return the refusal code a tick record carries, else ``None``.

    Refusal paths in the engine write a literal code: the live gate and
    store refusals carry ``detail.code``, per-pack send refusals carry
    ``detail.intent_refused``, and an instrument-metadata refusal carries
    ``detail.metadata_refusal``. A plain decision ``reason`` is NOT a
    refusal, so it is never reported as one.
    """

    detail = record.get("detail")
    if not isinstance(detail, dict):
        return None
    for key in ("code", "intent_refused", "metadata_refusal"):
        value = detail.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---- diagnostic Operations section (M28.15) --------------------------------

_DIAGNOSTIC_STORE_CODES = frozenset({"store_error", "store_busy", "store_corrupt", "store_full"})

# The closed alert vocabulary of the diagnostic Operations section. Each kind
# maps to the codes it may export; any other code exports as "other". ``None``
# marks a kind whose code is an identifier or free text (a client order id, a
# scheduler error message), so its code is never exported. ``live_refused`` is
# left out on purpose: live events stay out until a privacy review (D20).
_DIAGNOSTIC_ALERT_CODES: dict[str, frozenset[str] | None] = {
    "kill_switch": frozenset({"kill_switch_engaged"}),
    "restore_fence": frozenset({"restore_fence_engaged"}),
    "store_fault": _DIAGNOSTIC_STORE_CODES,
    "store_refused": _DIAGNOSTIC_STORE_CODES,
    "intent_refused": _DIAGNOSTIC_STORE_CODES | {"ownership_ambiguous"},
    "metadata_refusal": frozenset(
        {"metadata_unavailable", "metadata_stale", "metadata_symbol_mismatch", "metadata_invalid", "minimum_not_met"}
    ),
    "scheduler_stale": frozenset({"tick_stale"}),
    "feed_stale": frozenset({"candle_stale"}),
    "needs_reconcile": None,
    "unresolved_order_aging": None,
    "tick_failure": None,
}
_DIAGNOSTIC_ALERT_ID = re.compile(r"[0-9a-f]{16}")
_DIAGNOSTIC_PAIR = re.compile(r"[A-Z0-9-]{3,24}")
_DIAGNOSTIC_PACK_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_DIAGNOSTIC_PACK_VERSION = re.compile(r"[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}")
_DIAGNOSTIC_ROW_LIMIT = 100
_UNAVAILABLE = "unavailable"


def _diagnostic_paper_scope(
    armed: list[kb_config.ArmedPack], records: list[dict]
) -> tuple[set[str], set[tuple[str, str]]]:
    """Return the venues with a paper arm and the feeds with paper-only evidence.

    A feed (venue, pair) is paper-only when a paper arm or a paper tick names
    it and no other arm or tick does. Ticks without a mode prove nothing.
    """

    paper_feeds = {(record.venue, record.pair) for record in armed if record.mode == "paper"}
    other_feeds = {(record.venue, record.pair) for record in armed if record.mode != "paper"}
    for record in records:
        venue = record.get("venue")
        detail = record.get("detail")
        if record.get("kind") != "tick" or not isinstance(venue, str) or not isinstance(detail, dict):
            continue
        pair = detail.get("pair")
        mode = detail.get("mode")
        if not isinstance(pair, str) or mode is None:
            continue
        (paper_feeds if mode == "paper" else other_feeds).add((venue, pair))
    paper_venues = {record.venue for record in armed if record.mode == "paper"}
    return paper_venues, paper_feeds - other_feeds


def _diagnostic_alerts(home: Path, *, now: int, armed: list[kb_config.ArmedPack] | None) -> list[dict] | str:
    """Project current alerts onto the closed diagnostic alert row.

    An alert tied to a feed or venue is kept only when that feed has
    paper-only evidence or the venue has a paper arm; without a readable
    config no such alert is kept.
    """

    from krellbot import secrets as kb_secrets

    try:
        collected = alerts_app.collect(home, now=now)
    except Exception:  # noqa: BLE001 — diagnostics must survive an unreadable source.
        return _UNAVAILABLE
    paper_venues, paper_feeds = _diagnostic_paper_scope(armed or [], _read_journal_records(home))
    rows: list[dict] = []
    for alert in collected:
        if alert.kind not in _DIAGNOSTIC_ALERT_CODES:
            continue
        if alert.pair is not None or alert.venue is not None:
            if armed is None or alert.venue not in kb_secrets.VENUES:
                continue
            if alert.pair is not None and (alert.venue, alert.pair) not in paper_feeds:
                continue
            if alert.pair is None and alert.venue not in paper_venues:
                continue
        if not isinstance(alert.id, str) or _DIAGNOSTIC_ALERT_ID.fullmatch(alert.id) is None:
            continue
        if alert.severity not in {"critical", "warning"}:
            continue
        numbers = (alert.count, alert.first_ts, alert.last_ts)
        if any(type(value) is not int or value < 0 for value in numbers):
            continue
        allowed = _DIAGNOSTIC_ALERT_CODES[alert.kind]
        if allowed is None or not alert.code:
            code = None
        else:
            code = alert.code if alert.code in allowed else "other"
        rows.append(
            {
                "id": alert.id,
                "kind": alert.kind,
                "severity": alert.severity,
                "code": code,
                "count": alert.count,
                "first_ts": alert.first_ts,
                "last_ts": alert.last_ts,
            }
        )
    return rows[:_DIAGNOSTIC_ROW_LIMIT]


def _diagnostic_unresolved_orders(home: Path, *, now: int) -> dict | str:
    """Return how many orders await reconcile and their ages, oldest first."""

    try:
        rows = operations_view_app.list_unresolved_orders(home, now=now)
    except Exception:  # noqa: BLE001 — a store failure reads "unavailable", never its text.
        return _UNAVAILABLE
    ages = []
    for row in rows:
        age = row.get("age_seconds")
        ages.append(age if type(age) is int and age >= 0 else None)
    return {"count": len(rows), "ages_seconds": ages[:_DIAGNOSTIC_ROW_LIMIT]}


def _diagnostic_paper_runs(armed: list[kb_config.ArmedPack] | None) -> list[dict] | str:
    """Return pack id, version and mode of every paper arm, per venue and pair."""

    from krellbot import secrets as kb_secrets

    if armed is None:
        return _UNAVAILABLE
    rows = []
    for record in armed:
        if record.mode != "paper" or record.venue not in kb_secrets.VENUES:
            continue
        if _DIAGNOSTIC_PAIR.fullmatch(record.pair) is None:
            continue
        rows.append(
            {
                "venue": record.venue,
                "pair": record.pair,
                "pack_id": record.pack_id if _DIAGNOSTIC_PACK_ID.fullmatch(record.pack_id) else None,
                "pack_version": record.pack_version
                if _DIAGNOSTIC_PACK_VERSION.fullmatch(record.pack_version)
                else None,
                "mode": "paper",
            }
        )
    rows.sort(key=lambda row: (row["venue"], row["pair"]))
    return rows[:_DIAGNOSTIC_ROW_LIMIT]


def _diagnostic_operations_section(home: Path, *, now: int) -> dict:
    """Build the allowlisted Operations section of a diagnostic report.

    Only named fields with closed value sets are copied: no keys, balances,
    quantities, paths, client order ids, free text or live events. Each part
    that cannot be read is the string ``"unavailable"``.
    """

    try:
        armed: list[kb_config.ArmedPack] | None = kb_config.load_config(home).armed
    except Exception:  # noqa: BLE001 — an unreadable config reads "unavailable".
        armed = None
    return {
        "schema_version": SCHEMA_VERSION,
        "alerts": _diagnostic_alerts(home, now=now, armed=armed),
        "unresolved_orders": _diagnostic_unresolved_orders(home, now=now),
        "paper_runs": _diagnostic_paper_runs(armed),
    }


def _paper_state_fills(home: Path, venue: str) -> list[dict]:
    """Return the paper state's ``recent_fills`` for ``venue``, with pair.

    The returned items are internal: they keep ``pair`` so the caller can
    bucket them per run. Exposure rule for the wire: only ``coid``,
    ``side`` and ``ts_ms`` survive the projection — fill ``qty``/``price``
    belong to the money plane the brief keeps off the wire, and the state
    file's balances (owned cash) are never read here at all.
    """

    path = Path(home) / "run" / f"paper-{venue}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    fills_raw = raw.get("recent_fills")
    if not isinstance(fills_raw, list):
        return []
    fills: list[dict] = []
    for item in fills_raw:
        if not isinstance(item, dict):
            continue
        pair = item.get("pair")
        if not isinstance(pair, str) or not pair:
            continue
        fills.append(
            {
                "coid": str(item.get("coid", "")),
                "side": str(item.get("side", "")),
                "pair": pair,
                "ts_ms": int(item.get("ts_ms", 0) or 0),
            }
        )
    return fills


def _paper_position_run_id(home: Path, venue: str, pair: str) -> str | None:
    """Return the most recent journal run id for ``(venue, pair)``, or ``None``.

    A run id is the ``"<venue>:<pack>:<pair>"`` triple of the latest tick
    record the journal holds for the pair — the same run identity
    ``_paper_history_view`` keys its runs by. ``None`` means the journal
    has no tick record for the pair: the engine has never evaluated it,
    so the persisted paper state cannot yet carry engine-produced money.
    """

    best: tuple[int, str] | None = None
    for record in _read_journal_records(home):
        if record.get("kind") != "tick":
            continue
        if record.get("venue") != venue:
            continue
        detail = record.get("detail")
        if not isinstance(detail, dict) or detail.get("pair") != pair:
            continue
        pack_id = record.get("pack")
        if not isinstance(pack_id, str) or not pack_id:
            continue
        ts = record.get("ts")
        ts_value = int(ts) if isinstance(ts, (int, float)) else 0
        if best is None or ts_value >= best[0]:
            best = (ts_value, f"{venue}:{pack_id}:{pair}")
    return best[1] if best is not None else None


def _paper_history_view(home: Path) -> dict:
    """Build the closed-shape read-only paper history body.

    A "run" is the journal's evidence for one ``(venue, pack, pair)``
    triple — the identity the tick journal actually records. Runs sort
    most-recent-first and cap at ``PAPER_HISTORY_MAX_RUNS``. ``status``
    is ``"active"`` when the config still arms that triple, else
    ``"closed"``. No cash, quantity, stop, cap, pack path, or pack
    digest is read into or returned by this view.
    """

    home = Path(home)
    runs: dict[tuple[str, str, str], dict] = {}
    for record in _read_journal_records(home):
        if record.get("kind") != "tick":
            continue
        venue = record.get("venue")
        pack_id = record.get("pack")
        if not isinstance(venue, str) or not isinstance(pack_id, str):
            continue
        detail = record.get("detail")
        pair = detail.get("pair") if isinstance(detail, dict) else None
        if not isinstance(pair, str) or not pair:
            continue
        ts = record.get("ts")
        ts_ms = int(ts) * 1000 if isinstance(ts, (int, float)) else 0
        key = (venue, pack_id, pair)
        run = runs.setdefault(
            key,
            {
                "venue": venue,
                "pair": pair,
                "pack_id": pack_id,
                "pack_version": None,
                "first_ts_ms": ts_ms,
                "last_ts_ms": ts_ms,
                "ticks_total": 0,
                "last_refusal_code": None,
                "mode": "paper",
                "status": "closed",
                "summary": "",
                "recent_fills": [],
                "_refusals": [],
            },
        )
        run["first_ts_ms"] = min(run["first_ts_ms"], ts_ms)
        run["last_ts_ms"] = max(run["last_ts_ms"], ts_ms)
        run["ticks_total"] += 1
        refusal = _tick_refusal_code(record)
        if refusal is not None:
            run["_refusals"].append((ts_ms, refusal))

    try:
        config = kb_config.load_config(home)
    except (OSError, ValueError):
        config = None
    if config is not None:
        for armed in config.armed:
            key = (armed.venue, armed.pack_id, armed.pair)
            run = runs.get(key)
            if run is not None:
                run["status"] = "active"
                run["mode"] = armed.mode
                run["pack_version"] = armed.pack_version

    venue_fills: dict[str, list[dict]] = {}
    body_runs: list[dict] = []
    for run in runs.values():
        # Paper fills carry no pack id; the state file is per venue, so
        # bucket the venue's fills by pair and hand this run its bucket.
        if run["venue"] not in venue_fills:
            venue_fills[run["venue"]] = _paper_state_fills(home, run["venue"])
        run_fills = [fill for fill in venue_fills[run["venue"]] if fill["pair"] == run["pair"]]
        refusal_code = None
        if run["_refusals"]:
            refusal_code = max(run["_refusals"], key=lambda item: item[0])[1]
        parts = [
            f"{run['ticks_total']} tick{'s' if run['ticks_total'] != 1 else ''}",
            f"{len(run_fills)} fill{'s' if len(run_fills) != 1 else ''}",
        ]
        if refusal_code is not None:
            parts.append(f"last refusal {refusal_code}")
        else:
            parts.append("no refusal")
        parts.append(run["status"])
        exposed_run: dict = {
            "venue": run["venue"],
            "pair": run["pair"],
            "pack_id": run["pack_id"],
            "first_ts_ms": run["first_ts_ms"],
            "last_ts_ms": run["last_ts_ms"],
            "ticks_total": run["ticks_total"],
            "last_refusal_code": refusal_code,
            "mode": run["mode"],
            "status": run["status"],
            "summary": ", ".join(parts),
            "recent_fills": [
                {
                    "coid": fill["coid"],
                    "side": fill["side"],
                    "ts_ms": fill["ts_ms"],
                }
                for fill in run_fills[-PAPER_HISTORY_MAX_FILLS:]
            ],
        }
        if run["pack_version"]:
            exposed_run["pack_version"] = run["pack_version"]
        body_runs.append(exposed_run)

    body_runs.sort(key=lambda run: run["last_ts_ms"], reverse=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "runs": body_runs[:PAPER_HISTORY_MAX_RUNS],
    }


def _fleet_tick_index(home: Path) -> dict[tuple[str, str, str], dict]:
    """Index the journal's latest tick evidence per ``(venue, pack, pair)``.

    Used by the armed-fleet management view so each armed row can carry
    last-tick time/result without inventing cash, quantity, stop, or cap.
    Malformed journal lines are skipped the same way as paper history.
    """

    ticks: dict[tuple[str, str, str], dict] = {}
    for record in _read_journal_records(home):
        if record.get("kind") != "tick":
            continue
        venue = record.get("venue")
        pack_id = record.get("pack")
        if not isinstance(venue, str) or not isinstance(pack_id, str):
            continue
        detail = record.get("detail")
        pair = detail.get("pair") if isinstance(detail, dict) else None
        if not isinstance(pair, str) or not pair:
            continue
        ts = record.get("ts")
        ts_ms = int(ts) * 1000 if isinstance(ts, (int, float)) else 0
        key = (venue, pack_id, pair)
        entry = ticks.setdefault(
            key,
            {
                "last_tick_ts_ms": ts_ms,
                "ticks_total": 0,
                "_refusals": [],
            },
        )
        entry["last_tick_ts_ms"] = max(entry["last_tick_ts_ms"], ts_ms)
        entry["ticks_total"] += 1
        refusal = _tick_refusal_code(record)
        if refusal is not None:
            entry["_refusals"].append((ts_ms, refusal))
    return ticks


def _fleet_last_tick_result(*, ticks_total: int, last_refusal_code: str | None) -> str:
    """Closed last-tick result sentence for one armed fleet row."""

    parts = [
        f"{ticks_total} tick{'s' if ticks_total != 1 else ''}",
    ]
    if last_refusal_code is not None:
        parts.append(f"last refusal {last_refusal_code}")
    else:
        parts.append("no refusal")
    return ", ".join(parts)


def _fleet_detail_href(*, venue: str, pair: str) -> str:
    """Relative deep link into the Workstation status detail for one market."""

    return "/?" + urlencode({"view": "workstation", "venue": venue, "pair": pair})


def _fleet_row_for_record(record: kb_config.ArmedPack, tick_index: dict) -> dict:
    """Closed fleet row for one armed record + optional journal tick evidence."""

    key = (record.venue, record.pack_id, record.pair)
    tick = tick_index.get(key)
    if tick is None:
        last_tick_ts_ms = None
        last_refusal_code = None
        last_tick_result = "no tick yet"
    else:
        last_tick_ts_ms = int(tick["last_tick_ts_ms"])
        last_refusal_code = None
        if tick["_refusals"]:
            last_refusal_code = max(tick["_refusals"], key=lambda item: item[0])[1]
        last_tick_result = _fleet_last_tick_result(
            ticks_total=int(tick["ticks_total"]),
            last_refusal_code=last_refusal_code,
        )
    return {
        "venue": record.venue,
        "pair": record.pair,
        "mode": record.mode,
        "pack_id": record.pack_id,
        "entries_paused": kb_config.entries_held(record),
        "last_tick_ts_ms": last_tick_ts_ms,
        "last_tick_result": last_tick_result,
        "last_refusal_code": last_refusal_code,
        "detail_href": _fleet_detail_href(venue=record.venue, pair=record.pair),
    }


def _fleet_group_rows(deployments: list[dict]) -> list[dict]:
    """Group fleet rows by ``(venue, pair)`` for multi-pair supervision.

    One-pack-per-(venue,pair) means each group normally has a single
    deployment; the grouping still surfaces pair/venue as the fleet's
    primary axis so the UI never collapses to ``armed[0]``.
    """

    buckets: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for row in deployments:
        key = (str(row["venue"]), str(row["pair"]))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)
    return [
        {
            "venue": venue,
            "pair": pair,
            "deployments": buckets[(venue, pair)],
        }
        for venue, pair in order
    ]


def _fleet_armed_view(
    home: Path,
    *,
    venue: str | None = None,
    pair: str | None = None,
) -> dict:
    """Build the closed-shape armed fleet list with last tick time/result.

    Every armed config record becomes one row. Last-tick fields come from
    the journal when evidence exists; an armed market that has never
    ticked reports ``last_tick_ts_ms: null`` and
    ``last_tick_result: "no tick yet"``. ``detail_href`` is the relative
    deep link the workstation uses to open that market's status detail.
    Cash, quantity, stop, cap, and pack path stay off the wire.

    Optional ``venue`` / ``pair`` filters narrow ``deployments`` and
    ``groups`` without collapsing the fleet to ``armed[0]``.
    ``armed_count`` always reports the unfiltered armed total so a
    filtered miss still discloses that other markets are armed.
    """

    home = Path(home)
    venue_filter = (venue or "").strip() or None
    pair_filter = (pair or "").strip() or None
    try:
        config = kb_config.load_config(home)
    except (OSError, ValueError):
        return {
            "schema_version": SCHEMA_VERSION,
            "armed": False,
            "armed_count": 0,
            "deployments": [],
            "groups": [],
        }

    armed_count = len(config.armed)
    tick_index = _fleet_tick_index(home) if config.armed else {}
    deployments: list[dict] = []
    for record in config.armed:
        if venue_filter is not None and record.venue != venue_filter:
            continue
        if pair_filter is not None and record.pair != pair_filter:
            continue
        deployments.append(_fleet_row_for_record(record, tick_index))

    return {
        "schema_version": SCHEMA_VERSION,
        "armed": bool(deployments),
        "armed_count": armed_count,
        "deployments": deployments,
        "groups": _fleet_group_rows(deployments),
    }


def _fleet_freshness_view(home: Path) -> dict:
    """Candle evidence for every armed feed, one closed row per record.

    Each row is read from the current arm's latest tick with the stale rule
    ``/api/v1/paper/health`` uses (``alerts.candle_is_fresh``):
    ``last_bar_ts_ms`` is the candle that tick read, ``candle_age_s`` its
    age when read, and ``candle_stale`` whether it was older than two
    timeframes. Missing evidence is ``null``. The fleet row stays closed;
    the workstation joins the two on ``(venue, pack_id, pair)``.
    """

    try:
        config = kb_config.load_config(Path(home))
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_VERSION, "feeds": []}
    records = _read_journal_records(Path(home)) if config.armed else []
    feeds: list[dict] = []
    for record in config.armed:
        freshness = alerts_app.feed_freshness(
            alerts_app.current_arm_ticks(records, record),
            timeframe_seconds=alerts_app.pack_timeframe_seconds(record.pack_path),
        )
        feeds.append(
            {
                "venue": record.venue,
                "pack_id": record.pack_id,
                "pair": record.pair,
                "last_bar_ts_ms": freshness.last_bar_ts_ms,
                "candle_age_s": freshness.candle_age_s,
                "candle_stale": freshness.candle_stale,
            }
        )
    return {"schema_version": SCHEMA_VERSION, "feeds": feeds}


# Settings 'Trust & billing' copy for ``GET /api/v1/entitlements`` (M28.14,
# decision D10). Each key states what the code enforces today and is checked
# against it in tests/test_entitlement_disclosure_matches_gate.py: the tick
# gate (``license.entries_allowed`` on armed records whose
# ``requires_license`` came from ``catalog.requires_license_for``), the
# paid-install check (``checkout.has_entitlement``), and the export and
# diagnostics routes, which never read the license. No price or plan is named
# here until the owner decides one (M28.34).
_ENTITLEMENT_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "paid_catalogue_entries_need_license",
        "gated",
        (
            "In the tick, the license decides one thing: whether packs installed from the paid catalogue "
            "(packs/catalog/) may open new positions."
        ),
    ),
    (
        "grace_keeps_entries",
        "gated",
        (
            "While the license is active or past due, those packs keep opening positions until the grace date "
            "last verified on this computer. The tick does not go online to check."
        ),
    ),
    (
        "after_grace_entries_stop",
        "gated",
        (
            "After that grace date, or if the license has any other status or was never verified on this "
            "computer, paid catalogue packs open no new positions."
        ),
    ),
    (
        "exits_and_stops_continue",
        "continues",
        "Exits and protective stops keep running for every pack, paid catalogue packs included.",
    ),
    (
        "nothing_force_closed",
        "continues",
        (
            "A lapse never closes a position. Positions are sold only by the pack's exit rule or its stop "
            "protection, as before the lapse."
        ),
    ),
    (
        "export_continues",
        "continues",
        "Export does not read the license. Home export and the journal keep working after a lapse.",
    ),
    (
        "diagnostics_continue",
        "continues",
        "Diagnostics do not read the license. The diagnostic report keeps working after a lapse.",
    ),
    (
        "own_and_community_packs_never_gated",
        "never_gated",
        "Packs you write and community packs never need a license. A lapse does not change how they run.",
    ),
    (
        "paid_install_needs_sku_entitlement",
        "records",
        (
            "Installing a paid catalogue entry from Packs needs its purchase recorded on this computer as a SKU "
            "entitlement. Checkout records it; a refund removes it."
        ),
    ),
    (
        "records_can_disagree",
        "records",
        (
            "The license status (new entries) and the SKU entitlements (paid installs) are separate records on "
            "this computer and can disagree today. A refund removes the SKU entitlement but does not change the "
            "license status."
        ),
    ),
)

# License cache statuses reported as-is; anything else reads ``other``.
_LICENSE_STATUSES = frozenset({"active", "past_due", "lapsed", "dead", "revoked"})


def _license_int(value: object) -> int | None:
    """Parse a cache timestamp the way ``license.entries_allowed`` does, or None."""

    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _entitlements_body(home: Path, *, now: int) -> dict:
    """Read-only entitlement disclosure: license cache, SKU records, gated packs."""

    from krellbot import catalog as kb_catalog
    from krellbot import license as kb_license
    from krellbot.application import checkout as kb_checkout

    cache = kb_license.read_cache(home)
    if cache is None:
        status = "missing"
    else:
        raw_status = cache.get("status")
        status = raw_status if isinstance(raw_status, str) and raw_status in _LICENSE_STATUSES else "other"
    gated: list[str] = []
    for root in (home / "packs", home / "packs" / "community", home / "packs" / "catalog"):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            if path.is_file() and kb_catalog.requires_license_for(path, home) and path.stem not in gated:
                gated.append(path.stem)
    return {
        "schema_version": SCHEMA_VERSION,
        "license": {
            "status": status,
            "period_end": None if cache is None else _license_int(cache.get("period_end")),
            "grace_until": None if cache is None else _license_int(cache.get("grace_until")),
            "paid_catalogue_entries_allowed": kb_license.entries_allowed(cache, now=int(now)),
        },
        "license_gated_packs": sorted(gated),
        "sku_entitlements": kb_checkout.read_entitlements(home),
        "records_can_disagree": True,
        "rules": [{"key": key, "group": group, "text": text} for key, group, text in _ENTITLEMENT_RULES],
    }


def _format_sse(event_id: str, event_name: str, data_payload: dict) -> bytes:
    """Render a single SSE frame. JSON-encode the payload as ``data:``."""

    data = json.dumps(data_payload, sort_keys=True, separators=(",", ":"))
    lines = [
        f"id: {event_id}",
        f"event: {event_name}",
        f"data: {data}",
        "",
        "",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _format_resync_marker(last_event_id: str) -> bytes:
    """Render a ``resync_required`` SSE marker with no body."""

    lines = ["event: resync_required", f"id: {last_event_id}", "data: ", "", ""]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def create_app(
    home: Path,
    *,
    port: int,
    bootstrap_token: str,
    runner: Callable | None = None,
    dist_dir: Path | None = None,
    tailnet_access: TailnetAccess | None = None,
) -> FastAPI:
    state = _AppState(
        home=Path(home),
        port=port,
        bootstrap_token=bootstrap_token,
        jobs=JobManager(home=Path(home), runner=runner),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        from krellbot.application.rehearsal import DRIVER_CADENCE_SECONDS

        async def drive_rehearsal():
            while True:
                await asyncio.sleep(DRIVER_CADENCE_SECONDS)
                # Deliberately synchronous on the API event loop. Commands,
                # reads and driving cannot interleave inside the engine's
                # scoped environment override; no thread executor is used.
                _rehearsal_service(state).drive()

        task = asyncio.create_task(drive_rehearsal())
        print(
            f"Synthetic rehearsal cadence: one bar every {DRIVER_CADENCE_SECONDS:g}s while running; paused/stopped do not evaluate",
            flush=True,
        )
        try:
            yield
        finally:
            try:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            finally:
                # Sign-in listeners close even if the rehearsal task ended with an error.
                if state.ai_signins is not None:
                    state.ai_signins.close_all()  # type: ignore[attr-defined]

    app = FastAPI(
        title="krellbot.api",
        version=SCHEMA_VERSION,
        lifespan=lifespan,
    )
    app.state.krellbot = state

    from krellbot.api import static as _static

    _static.register(
        app,
        _resolve_dist_dir(dist_dir),
        bootstrap_token=lambda: None if state.bootstrap_used or tailnet_access else bootstrap_token,
    )

    @app.middleware("http")
    async def _enforce_loopback_host(request: Request, call_next):
        host = request.headers.get("host", "")
        if not is_loopback_host(host, port=port):
            return JSONResponse({"detail": "loopback only"}, status_code=403)
        return await call_next(request)

    if tailnet_access is not None:
        tailnet_access.install(
            app,
            state.sessions,
            port=port,
            dist_dir=_resolve_dist_dir(dist_dir),
            home=state.home,
        )

    @app.post("/api/v1/session/bootstrap")
    async def bootstrap(request: Request) -> Response:
        s = _state(request.app)
        origin = request.headers.get("origin", "")
        if not is_loopback_origin(origin, port=s.port):
            return JSONResponse({"detail": "origin not loopback"}, status_code=403)

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        submitted = payload.get("token", "")
        if not isinstance(submitted, str) or not hmac.compare_digest(submitted, s.bootstrap_token):
            return JSONResponse({"detail": "wrong token"}, status_code=403)
        if s.bootstrap_used:
            return JSONResponse({"detail": "token already redeemed"}, status_code=403)
        s.bootstrap_used = True

        session_id = generate_session_token()
        csrf_token = generate_csrf_token()
        s.sessions[session_id] = csrf_token

        body = {"schema_version": SCHEMA_VERSION, "csrf_token": csrf_token}
        response = JSONResponse(body, status_code=200)
        response.set_cookie(
            key=SESSION_COOKIE,
            value=session_id,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/v1/session/csrf")
    async def session_csrf(request: Request) -> Response:
        """Return the CSRF token for the caller's existing session.

        A page reload loses the module-memory CSRF while the session
        cookie stays valid; this read endpoint lets the reloaded shell
        recover the token. It runs ``_gate_get`` (session membership
        plus the loopback Origin/Host checks), never mints a session,
        and never returns the bootstrap token.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        session_id = request.cookies.get(SESSION_COOKIE)
        csrf_token = s.sessions[session_id]
        return JSONResponse(
            {"schema_version": SCHEMA_VERSION, "csrf_token": csrf_token},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/v1/session/reconnect/issue")
    async def reconnect_issue(request: Request) -> Response:
        """Issue a short-lived reconnect token for a second browser/profile.

        Local re-pair: an already-authenticated session (cookie + CSRF +
        loopback Origin) mints a one-time token the primary UI (or a
        terminal that holds the same session) can show as text or QR.
        The outstanding offer is replaced on each successful issue.
        Host middleware still refuses non-loopback Host headers.
        """

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        now = _app_now(s)
        offer = issue_reconnect_offer(now=now)
        s.reconnect_offer = offer
        path = reconnect_redeem_path(offer.token)
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "token": offer.token,
                "expires_at": offer.expires_at,
                "expires_in": max(0, offer.expires_at - now),
                "redeem_path": path,
                "qr_payload": f"http://127.0.0.1:{s.port}{path}",
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/v1/session/reconnect/redeem")
    async def reconnect_redeem(request: Request) -> Response:
        """Redeem a reconnect token into a new authenticated session.

        Same loopback-Origin gate as bootstrap (no prior session required).
        A matching unexpired token is single-use: the offer is cleared
        before the new session cookie is set. Expired, wrong, or already-
        spent tokens are 403. Does not revive the one-time bootstrap
        token and does not weaken Host/Origin/CSRF checks for later
        state changes.
        """

        s = _state(request.app)
        origin = request.headers.get("origin", "")
        if not is_loopback_origin(origin, port=s.port):
            return JSONResponse({"detail": "origin not loopback"}, status_code=403)

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        submitted = payload.get("token", "")
        now = _app_now(s)
        offer = s.reconnect_offer
        if not reconnect_offer_valid(offer, submitted=submitted if isinstance(submitted, str) else "", now=now):
            detail = "token expired" if offer is not None and int(now) >= int(offer.expires_at) else "wrong token"
            # Clear a spent/expired offer so a stale QR cannot be retried
            # after the clock advances past expiry (wrong token leaves a
            # still-valid offer for the rightful redeem).
            if offer is not None and int(now) >= int(offer.expires_at):
                s.reconnect_offer = None
            return JSONResponse({"detail": detail}, status_code=403)

        s.reconnect_offer = None
        session_id = generate_session_token()
        csrf_token = generate_csrf_token()
        s.sessions[session_id] = csrf_token

        body = {"schema_version": SCHEMA_VERSION, "csrf_token": csrf_token}
        response = JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            key=SESSION_COOKIE,
            value=session_id,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/v1/commands")
    async def commands(request: Request) -> Response:
        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        command = payload.get("command", "")
        inner = payload.get("payload") or {}
        if not isinstance(command, str) or not isinstance(inner, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        correlation_id = payload.get("correlation_id") or ""

        if command.startswith("rehearsal."):
            from krellbot.application.rehearsal import COMMANDS

            if inner.get("mode") == "live" or payload.get("mode") == "live":
                return JSONResponse({"detail": "live orders are disabled"}, status_code=403)
            if command not in COMMANDS:
                return JSONResponse({"detail": f"unknown command {command}"}, status_code=403)
            if inner:
                return JSONResponse({"detail": "invalid body"}, status_code=400)
            result = _rehearsal_service(s).handle(command, inner)
            # Keep the durable service envelope and supply the actual UI
            # client's `status` readback. No synthetic success or fills.
            return JSONResponse({**result, "status": _rehearsal_wire(result["rehearsal"])}, status_code=200)

        if command == "live.guardrails":
            # Deploy preflight surface: active rails before arm + journal
            # refusals. Never places an order. Optional revision_id loads
            # pack risk (stop / max_account_pct) from the draft store.
            venue = inner.get("venue") or "kraken"
            revision_id = inner.get("revision_id")
            journal_flag = inner.get("journal", True)
            if not isinstance(venue, str):
                return JSONResponse({"detail": "invalid body"}, status_code=400)
            if revision_id is not None and not isinstance(revision_id, str):
                return JSONResponse({"detail": "invalid body"}, status_code=400)
            if not isinstance(journal_flag, bool):
                return JSONResponse({"detail": "invalid body"}, status_code=400)
            pack_for_rails = None
            if revision_id:
                try:
                    _trusted, pack_for_rails = _draft_service(s).pack_for_arm(revision_id)
                except RevisionNotFound:
                    return JSONResponse(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "code": "draft_not_found",
                            "ok": False,
                            "effect": "refused",
                            "message": f"draft {revision_id} not found",
                        },
                        status_code=404,
                    )
                except DraftNotRunnable:
                    msg = f"draft {revision_id} is not runnable"
                    return JSONResponse(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "code": "draft_not_runnable",
                            "ok": False,
                            "effect": "refused",
                            "message": msg,
                        },
                        status_code=403,
                    )
            body = live_guardrails.evaluate(
                s.home,
                venue=venue,
                pack_data=pack_for_rails,
                journal=journal_flag,
                now=_now_seconds(),
            )
            return JSONResponse(body, status_code=200)
        if command == "live.preflight":
            # ``live.preflight`` is the single carve-out from the prefix
            # gate: a sandbox-mode dry-run against the injectable venue
            # transport that never sends an order. Every other ``live.*``
            # command still returns 403 below; the ``mode == "live"``
            # payload refusal remains in force for non-preflight commands.
            if live_gate.kill_state(s.home).engaged:
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "code": "kill_switch_engaged",
                        "ok": False,
                        "effect": "refused",
                        "message": "kill switch engaged",
                    },
                    status_code=200,
                )
            venue = inner.get("venue") or "kraken"
            account_id = inner.get("account_id") or venue
            revision_id = inner.get("revision_id")
            mode = inner.get("mode")
            if (
                not isinstance(venue, str)
                or not isinstance(account_id, str)
                or not isinstance(revision_id, str)
                or not isinstance(mode, str)
            ):
                return JSONResponse({"detail": "invalid body"}, status_code=400)
            stored_mode = _stored_deployment_mode(s.home)
            result = live_preflight_evaluate(
                account_id=account_id,
                revision_id=revision_id,
                mode=mode,
                transport=_live_transport(s, venue=venue),
                stored_mode=stored_mode,
            )
            return JSONResponse(result.to_dict(), status_code=200)
        if command == "live.promote":
            venue = inner.get("venue")
            pair = inner.get("pair")
            revision_id = inner.get("revision_id")
            if not isinstance(venue, str) or not isinstance(pair, str) or not isinstance(revision_id, str):
                return JSONResponse({"detail": "invalid body"}, status_code=400)
            promote = live_gate.check_promotion(
                s.home,
                venue=venue,
                pair=pair,
                revision_id=revision_id,
            )
            body_promote = {
                "schema_version": SCHEMA_VERSION,
                "code": promote.code,
                "ok": False,
                "message": promote.message,
                "effect": "refused",
                "venue": venue,
                "pair": pair,
            }
            return JSONResponse(body_promote, status_code=200)
        if command == "operations.kill":
            reason = inner.get("reason")
            if not isinstance(reason, str):
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "code": "bad_input",
                        "ok": False,
                        "effect": "refused",
                        "message": "reason must be a string",
                    },
                    status_code=200,
                )
            try:
                state = live_gate.engage_kill(s.home, reason=reason, now=_now_seconds())
            except ValueError as exc:
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "code": "bad_input",
                        "ok": False,
                        "effect": "refused",
                        "message": str(exc),
                    },
                    status_code=200,
                )
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "kill_switch_engaged",
                    "ok": True,
                    "effect": "changed",
                    "kill_switch": state.to_dict(),
                },
                status_code=200,
            )
        if command == "operations.release_kill":
            state = live_gate.release_kill(s.home)
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "kill_switch_released",
                    "ok": True,
                    "effect": "changed",
                    "kill_switch": state.to_dict(),
                },
                status_code=200,
            )
        if command == "operations.resolve_order":
            # Retire one unresolved order from recorded venue evidence, or on
            # the operator's assertion with the client order id typed back.
            # Never sends an order; the engine takes the venue's tick lock.
            from krellbot.run import OperatorResolveResult, resolve_order_by_operator

            client_order_id = inner.get("client_order_id")
            outcome = inner.get("outcome")
            if not isinstance(client_order_id, str) or (outcome is not None and not isinstance(outcome, str)):
                resolved = OperatorResolveResult(code="bad_input")
            else:
                resolved = resolve_order_by_operator(
                    home=s.home,
                    coid=client_order_id,
                    outcome=outcome,
                    executed_qty=inner.get("executed_qty"),
                    confirmation=inner.get("confirmation"),
                )
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": resolved.code,
                    "ok": resolved.ok,
                    "effect": resolved.effect,
                    "message": resolved.message,
                    "client_order_id": client_order_id if isinstance(client_order_id, str) else None,
                    "outcome": resolved.outcome,
                    "filled_qty": None if resolved.filled_qty is None else str(resolved.filled_qty),
                    "evidence": resolved.evidence,
                    "attributed": resolved.attributed,
                },
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        if command == "positions.close":
            # Sell exactly the strategy-owned quantity of one arm (paper or
            # live) on the operator's word, with the pair typed back. The
            # engine runs the tick's exit under the venue's tick lock: it
            # cancels the stop, sells, journals what executed and holds new
            # entries. A refusal writes nothing.
            from krellbot.run import ClosePositionResult, close_position

            venue = inner.get("venue")
            pair = inner.get("pair")
            confirmation = inner.get("confirmation")
            if (
                not isinstance(venue, str)
                or not isinstance(pair, str)
                or (confirmation is not None and not isinstance(confirmation, str))
            ):
                closed = ClosePositionResult(code="bad_input")
            else:
                closed = await asyncio.to_thread(
                    close_position,
                    venue=venue,
                    pair=pair,
                    confirmation=confirmation,
                    open_venue=s.close_venue_opener,
                    home=s.home,
                )
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "correlation_id": correlation_id,
                    "code": closed.code,
                    "ok": closed.ok,
                    "effect": closed.effect,
                    "message": closed.message,
                    "venue": venue if isinstance(venue, str) else None,
                    "pair": pair if isinstance(pair, str) else None,
                    "mode": closed.mode,
                    "client_order_id": closed.client_order_id,
                    "requested_qty": None if closed.requested_qty is None else str(closed.requested_qty),
                    "sold_qty": None if closed.sold_qty is None else str(closed.sold_qty),
                },
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        if command == "alerts.ack":
            alert_id = inner.get("alert_id")
            if not isinstance(alert_id, str):
                return JSONResponse({"detail": "invalid body"}, status_code=400)
            try:
                acked = alerts_app.acknowledge(s.home, alert_id, now=_now_seconds())
            except KeyError:
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "code": "alert_not_found",
                        "ok": False,
                    },
                    status_code=404,
                )
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "acknowledged",
                    "ok": True,
                    "alert": acked.to_dict(),
                },
                status_code=200,
            )
        if command == DAILY_LOSS_LIMIT_COMMAND:
            return _set_daily_loss_limit(s, inner)
        if command == "live.arm":
            # Web live arm behind the existing gate. While PAPER_ONLY_RELEASE
            # is active, refuse with an honest structured 403 before any
            # pack/key/transport work. When live is re-enabled, authorize via
            # grant + kill switch + env and skip stdin Type LIVE (grant
            # already required typed LIVE). Deploy/Operations confirm sends
            # typed LIVE again on the arm payload; a wrong confirmation is
            # refused before pack/key work. Kraken-only for M20. Operator
            # user-policy deny-live still applies after the release gate so
            # a future flip cannot bypass a local deny.
            if release_policy.paper_only_release_active():
                paper_only = live_gate.check_web_live_arm(
                    s.home,
                    venue="kraken",
                    pair="unused",
                )
                return _live_arm_gate_refusal(paper_only, home=s.home, venue="kraken")
            live_policy = user_policy_app.ensure_allowed(s.home, user_policy_app.ACTION_LIVE)
            if not live_policy.ok:
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "code": live_policy.code,
                        "ok": False,
                        "effect": "refused",
                        "message": live_policy.message,
                        "detail": live_policy.message,
                        "action": user_policy_app.ACTION_LIVE,
                    },
                    status_code=403,
                )

            venue = inner.get("venue")
            pack_path_raw = inner.get("pack_path")
            revision_id = inner.get("revision_id")
            confirmation = inner.get("confirmation")
            if not isinstance(venue, str) or not venue:
                return JSONResponse({"detail": "invalid body"}, status_code=400)
            venue_refusal = live_gate.check_web_live_arm_venue(venue)
            if venue_refusal is not None:
                return _live_arm_gate_refusal(venue_refusal, home=s.home, venue=venue)
            confirm_refusal = live_gate.validate_web_live_arm_confirmation(confirmation)
            if confirm_refusal is not None:
                return _live_arm_gate_refusal(confirm_refusal, home=s.home, venue=venue)
            if not pack_path_raw and not revision_id:
                return JSONResponse({"detail": "invalid body"}, status_code=400)

            trusted_pack: Path | None = None
            pack_data: object | None = None
            if revision_id:
                if not isinstance(revision_id, str):
                    return JSONResponse({"detail": "invalid body"}, status_code=400)
                try:
                    draft = _draft_service(s)
                    # Server-owned draft path + pack bytes; never from client path.
                    trusted_pack, pack_data = draft.pack_for_arm(revision_id)
                except RevisionNotFound:
                    return JSONResponse(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "code": "draft_not_found",
                            "ok": False,
                            "message": f"draft {revision_id} not found",
                            "effect": "refused",
                            "detail": f"draft {revision_id} not found",
                        },
                        status_code=404,
                    )
                except DraftNotRunnable:
                    msg = f"draft {revision_id} is not runnable"
                    return JSONResponse(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "code": "draft_not_runnable",
                            "ok": False,
                            "message": msg,
                            "effect": "refused",
                            "detail": msg,
                        },
                        status_code=403,
                    )
            else:
                if not isinstance(pack_path_raw, str):
                    return JSONResponse({"detail": "invalid body"}, status_code=400)
                basename = _client_pack_basename(pack_path_raw)
                if basename is None:
                    return JSONResponse({"detail": "invalid body"}, status_code=400)
                # Path is home + sanitized basename only (no user directories).
                trusted_pack = s.home / basename
                try:
                    pack_data = json.loads(trusted_pack.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    return JSONResponse(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "code": "pack_has_no_market",
                            "ok": False,
                            "effect": "refused",
                            "message": f"pack has no market for {venue}",
                            "detail": f"pack has no market for {venue}",
                        },
                        status_code=400,
                    )

            if trusted_pack is None:
                return JSONResponse({"detail": "invalid body"}, status_code=400)

            pair = _pair_from_pack_data(pack_data, venue)
            if pair is None:
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "code": "pack_has_no_market",
                        "ok": False,
                        "effect": "refused",
                        "message": f"pack has no market for {venue}",
                        "detail": f"pack has no market for {venue}",
                    },
                    status_code=400,
                )

            # Surface pack risk rails before arm_pack so refusals carry
            # closed reason codes in the journal (M20.3) without widening
            # the run/ arm path.
            pack_risk = live_guardrails.inspect_pack_risk(pack_data)
            pack_id = pack_risk.pack_id if pack_risk is not None else "-"
            if pack_risk is not None and not pack_risk.stop_ok:
                live_guardrails.journal_refusal(
                    s.home,
                    venue=venue,
                    pack_id=pack_id,
                    pair=pair,
                    code=live_guardrails.CODE_STOP_REQUIRED,
                    message="live arm requires a stop",
                    now=_now_seconds(),
                )
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "code": live_guardrails.CODE_STOP_REQUIRED,
                        "ok": False,
                        "effect": "refused",
                        "message": "live arm requires a stop",
                        "detail": "live arm requires a stop",
                        "venue": venue,
                        "pair": pair,
                    },
                    status_code=403,
                )
            if pack_risk is not None and not pack_risk.max_account_pct_ok:
                live_guardrails.journal_refusal(
                    s.home,
                    venue=venue,
                    pack_id=pack_id,
                    pair=pair,
                    code=live_guardrails.CODE_MAX_ACCOUNT_PCT,
                    message="live arm requires max_account_pct > 0",
                    now=_now_seconds(),
                )
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "code": live_guardrails.CODE_MAX_ACCOUNT_PCT,
                        "ok": False,
                        "effect": "refused",
                        "message": "live arm requires max_account_pct > 0",
                        "detail": "live arm requires max_account_pct > 0",
                        "venue": venue,
                        "pair": pair,
                    },
                    status_code=403,
                )

            gate = live_gate.check_web_live_arm(s.home, venue=venue, pair=pair)
            if not gate.ok:
                return _live_arm_gate_refusal(
                    gate,
                    home=s.home,
                    venue=venue,
                    pair=pair,
                    pack_id=pack_id,
                )
            # Successful gate ⇒ grant already collected typed LIVE; skip stdin
            # (see live_gate.web_arm_skips_stdin_confirmation).

            # Probe the stored key before arm_pack so withdraw-capable and
            # unknown-permission keys get typed 403s (M20.2 / SECURITY.md).
            # arm_pack still re-checks via key_check as defense in depth.
            key_refusal = _live_arm_key_refusal(
                venue=venue,
                pair=pair,
                correlation_id=correlation_id,
                probe=_live_arm_key_probe(s, venue),
            )
            if key_refusal is not None:
                return key_refusal

            from krellbot.run import arm_pack

            rc = arm_pack(
                trusted_pack,
                venue=venue,
                mode="live",
                home=s.home,
                skip_confirm=True,
                key_check=_live_arm_key_check(s),
            )
            if rc == 0:
                _emit_funnel(s, "arm", sku=trusted_pack.stem)
                if revision_id:
                    try:
                        _draft_service(s).mark_deployed(revision_id)
                    except (RevisionNotFound, DraftNotRunnable):
                        pass
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "correlation_id": correlation_id,
                        "code": "armed",
                        "ok": True,
                        "effect": "changed",
                        "message": f"armed on {venue} {pair}",
                        "venue": venue,
                        "pair": pair,
                    },
                    status_code=200,
                )
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "correlation_id": correlation_id,
                    "code": "live_arm_refused",
                    "ok": False,
                    "effect": "refused",
                    "message": "live arm refused",
                    "detail": "live arm refused",
                    "venue": venue,
                    "pair": pair,
                },
                status_code=403,
            )
        if command.startswith("live."):
            return JSONResponse({"detail": "live orders are disabled"}, status_code=403)
        if inner.get("mode") == "live":
            return JSONResponse({"detail": "live orders are disabled"}, status_code=403)
        if command not in PAPER_COMMANDS:
            return JSONResponse({"detail": f"unknown command {command}"}, status_code=403)

        service = _paper_service(s)

        if command == "paper.arm":
            # ``paper.arm`` may be addressed by ``pack_path`` (legacy) or
            # by ``revision_id`` for a draft (NS08a). Draft addressing
            # resolves the on-disk draft bytes without rewriting the
            # file. A non-runnable draft is refused before any config
            # byte changes.
            pack_path = inner.get("pack_path")
            revision_id = inner.get("revision_id")
            if not pack_path and not revision_id:
                return JSONResponse({"detail": "invalid body"}, status_code=400)
            if revision_id:
                try:
                    draft = _draft_service(s)
                    pack_path, _pack = draft.pack_for_arm(revision_id)
                except RevisionNotFound:
                    return JSONResponse(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "code": "draft_not_found",
                            "ok": False,
                            "message": f"draft {revision_id} not found",
                            "effect": "refused",
                            "revision_before": None,
                            "revision_after": None,
                        },
                        status_code=404,
                    )
                except DraftNotRunnable as exc:
                    return JSONResponse(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "code": "draft_not_runnable",
                            "ok": False,
                            "message": str(exc),
                            "effect": "refused",
                            "revision_before": None,
                            "revision_after": None,
                        },
                        status_code=403,
                    )
            else:
                # Trusted resolve under home packs / packs/community only —
                # never join a client-supplied directory (CodeQL). Same
                # basename confinement as live.arm; community installs live
                # under packs/community/.
                if not isinstance(pack_path, str):
                    return JSONResponse({"detail": "invalid body"}, status_code=400)
                basename = _client_pack_basename(pack_path)
                if basename is None:
                    return JSONResponse({"detail": "invalid body"}, status_code=400)
                candidates = (
                    s.home / "packs" / basename,
                    s.home / "packs" / "community" / basename,
                    s.home / "packs" / "catalog" / basename,
                )
                resolved = next((path for path in candidates if path.is_file()), None)
                if resolved is None:
                    return JSONResponse(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "code": "pack_not_found",
                            "ok": False,
                            "message": "pack not found",
                            "effect": "refused",
                            "revision_before": None,
                            "revision_after": None,
                        },
                        status_code=404,
                    )
                pack_path = resolved
            result = service.arm(
                Path(pack_path),
                venue=inner["venue"],
                mode=inner.get("mode", "paper"),
                paper_balance=Decimal(inner["paper_balance"]),
                correlation_id=correlation_id,
            )
            if result.ok:
                # Funnel arm event: pack id stem only — never strategy source.
                arm_sku = Path(pack_path).stem if pack_path is not None else ""
                _emit_funnel(s, "arm", sku=arm_sku)
            if revision_id and result.ok:
                try:
                    _draft_service(s).mark_deployed(revision_id)
                except (RevisionNotFound, DraftNotRunnable):
                    pass
        elif command == "paper.disarm":
            result = service.disarm(
                venue=inner["venue"],
                pair=inner["pair"],
                correlation_id=correlation_id,
            )
        elif command == "paper.pause_entries":
            result = service.pause_entries(
                venue=inner["venue"],
                pair=inner["pair"],
                correlation_id=correlation_id,
            )
        elif command == "paper.resume_entries":
            result = service.resume_entries(
                venue=inner["venue"],
                pair=inner["pair"],
                correlation_id=correlation_id,
            )
        elif command == "paper.raise_stop":
            if "new_stop" not in inner:
                return JSONResponse({"detail": "invalid stop", "code": "invalid_stop"}, status_code=400)
            try:
                new_stop_value = Decimal(str(inner["new_stop"]))
                if not new_stop_value.is_finite():
                    return JSONResponse({"detail": "invalid stop", "code": "invalid_stop"}, status_code=400)
            except (InvalidOperation, ValueError, TypeError):
                return JSONResponse({"detail": "invalid stop", "code": "invalid_stop"}, status_code=400)
            result = service.raise_stop(
                venue=inner["venue"],
                pair=inner["pair"],
                new_stop=new_stop_value,
                correlation_id=correlation_id,
            )
        elif command == "paper.export":
            result = service.export(
                venue=inner["venue"],
                pair=inner["pair"],
                correlation_id=correlation_id,
            )
        else:
            return JSONResponse({"detail": f"unknown command {command}"}, status_code=403)

        return JSONResponse(result.to_dict(), status_code=200)

    @app.post("/api/v1/research/jobs")
    async def research_jobs(request: Request) -> Response:
        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        kind = payload.get("kind", JOB_KIND_RESEARCH_BACKTEST)
        if not isinstance(kind, str):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        correlation_id = payload.get("correlation_id")
        if not isinstance(correlation_id, str):
            correlation_id = ""

        # F4 — refuse an ambiguous pack source: both a non-empty
        # ``pack_path`` and a non-empty ``revision_id``. The default
        # runner prefers ``pack_path`` over ``revision_id``, so a
        # payload that sets both would silently run the typed path
        # instead of the revision the UI showed. Surface the refusal
        # before ``jobs.submit`` so no job is created.
        pack_path_raw = payload.get("pack_path")
        revision_id_raw = payload.get("revision_id")
        pack_path_filled = isinstance(pack_path_raw, str) and pack_path_raw != ""
        revision_id_filled = isinstance(revision_id_raw, str) and revision_id_raw != ""
        if pack_path_filled and revision_id_filled:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "ambiguous_pack_source",
                    "message": ("request sets both pack_path and revision_id; pick one"),
                },
                status_code=400,
            )

        try:
            job = s.jobs.submit(kind=kind, correlation_id=correlation_id, request=payload)
        except QueueFull as qf:
            return JSONResponse(
                {"code": qf.code, "message": qf.message, "schema_version": SCHEMA_VERSION},
                status_code=429,
            )

        return JSONResponse(job.to_dict(), status_code=200)

    @app.get("/api/v1/jobs")
    async def list_jobs(request: Request) -> Response:
        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        rows = [job.to_dict() for job in s.jobs.list_jobs()]
        return JSONResponse({"schema_version": SCHEMA_VERSION, "jobs": rows}, status_code=200)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(request: Request, job_id: str) -> Response:
        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        snap = s.jobs.get(job_id)
        if snap is None:
            return JSONResponse(
                {"code": JOB_ERROR_NOT_FOUND, "message": "job not found"},
                status_code=404,
            )
        return JSONResponse(snap.to_dict(), status_code=200)

    @app.post("/api/v1/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: str) -> Response:
        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            pass

        snap = s.jobs.cancel(job_id)
        if snap is None:
            return JSONResponse(
                {"code": JOB_ERROR_NOT_FOUND, "message": "job not found"},
                status_code=404,
            )
        return JSONResponse(snap.to_dict(), status_code=200)

    @app.get("/api/v1/jobs/{job_id}/result")
    async def get_job_result(request: Request, job_id: str) -> Response:
        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        snap = s.jobs.get(job_id)
        if snap is None:
            return JSONResponse(
                {"code": JOB_ERROR_NOT_FOUND, "message": "job not found"},
                status_code=404,
            )
        result = s.jobs.result_for(job_id)
        if result is None:
            # The job is known and not succeeded: running, cancelling,
            # cancelled, queued, or failed. Per the contract the result
            # route is unavailable in those states.
            return JSONResponse(
                {"code": JOB_ERROR_RESULT_UNAVAILABLE},
                status_code=404,
            )
        return JSONResponse(result, status_code=200)

    @app.get("/api/v1/jobs/{job_id}/fills")
    async def get_job_fills(request: Request, job_id: str) -> Response:
        """Return a succeeded research job's simulated fills keyed by ``bar_ts``.

        Same session gate as the result route (``_gate_get``). The fills
        come from the job's side-car, never from the stored
        ``{legacy_receipt, trace}`` result, which stays unchanged. Each leg
        is ``{side, price, qty, fee, is_stop}`` with exact decimal strings.
        They are backtest fills, not venue fills. A run that recorded no
        fills is 404 ``fills_unavailable``; nothing is reconstructed here.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        snap = s.jobs.get(job_id)
        if snap is None:
            return JSONResponse(
                {"code": JOB_ERROR_NOT_FOUND, "message": "job not found"},
                status_code=404,
            )
        if snap.state != "succeeded":
            return JSONResponse({"code": JOB_ERROR_RESULT_UNAVAILABLE}, status_code=404)
        fills = s.jobs.fills_for(job_id)
        if fills is None:
            return JSONResponse(
                {"code": JOB_ERROR_FILLS_UNAVAILABLE, "message": "this run has no recorded fills"},
                status_code=404,
            )
        return JSONResponse(
            {"schema_version": SCHEMA_VERSION, "job_id": job_id, "fills": fills},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/jobs/{job_id}/costs")
    async def get_job_costs(request: Request, job_id: str) -> Response:
        """Serve a succeeded research job's cost assumptions side-car.

        Same session gate as the result route; the stored result and the
        receipt are untouched. A job without a side-car (not succeeded, or
        run before costs were recorded) is 404 ``costs_unavailable``.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        snap = s.jobs.get(job_id)
        if snap is None:
            return JSONResponse(
                {"code": JOB_ERROR_NOT_FOUND, "message": "job not found"},
                status_code=404,
            )
        costs = s.jobs.costs_for(job_id)
        if costs is None:
            return JSONResponse({"code": JOB_ERROR_COSTS_UNAVAILABLE}, status_code=404)
        return JSONResponse(costs, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/jobs/{job_id}/run")
    async def get_job_run(request: Request, job_id: str) -> Response:
        """Serve a finished research job's run record and its variants-tried count.

        Same session gate as the result route (``_gate_get``); read-only. The
        record names the run id and the inputs it hashes; the stored
        ``{legacy_receipt, trace}`` result is untouched. A job that has not
        ended, or that was stored before run records were kept, is 404
        ``run_unavailable``.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        snap = s.jobs.get(job_id)
        if snap is None:
            return JSONResponse(
                {"code": JOB_ERROR_NOT_FOUND, "message": "job not found"},
                status_code=404,
            )
        run = s.jobs.run_for(job_id)
        if run is None:
            return JSONResponse({"code": JOB_ERROR_RUN_UNAVAILABLE}, status_code=404)
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "job_id": job_id,
                "run": run["run"],
                "variants_tried": run["variants_tried"],
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/events")
    async def events(request: Request) -> Response:
        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        buffer = s.jobs.event_buffer()
        last_event_id_raw = request.headers.get("Last-Event-ID") or None
        last_event_id: int | None
        if last_event_id_raw is None:
            last_event_id = None
        else:
            try:
                last_event_id = int(last_event_id_raw)
            except (TypeError, ValueError):
                return JSONResponse({"detail": "bad last event id"}, status_code=400)

        gap = last_event_id is not None and buffer.is_gap(last_event_id)
        to_replay = [] if gap else buffer.replay(last_event_id)

        async def _emit():
            if gap:
                yield _format_resync_marker(str(last_event_id))
                return
            for event in to_replay:
                yield _format_sse(
                    event_id=event.event_id,
                    event_name=event.kind,
                    data_payload=event.to_dict(),
                )

        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        return StreamingResponse(_emit(), media_type="text/event-stream", headers=headers)

    @app.get("/api/v1/operations")
    async def operations(request: Request) -> Response:
        """Return the closed-shape operations view (M3-GUI).

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF
        required. The body aggregates ``live_gate.live_status`` (with the
        app's actual ``env`` so ``KRELLBOT_ENABLE_LIVE`` is honored), the
        per-deployment projection (mode verbatim, promotion refusal code),
        and the deduped, severity-sorted alert list. No ``cap``,
        ``stop``, ``starting_cash``, ``owned_qty``, ``pack_path``,
        ``pack_sha256``, balance, or key material appears in the body.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        now = _now_seconds()
        body = operations_view_app.operations_view(s.home, now=now)
        # Opt-in alert notifications (desktop/email). Separate from telemetry.
        # FakeNotifier is injected by tests; production builds the default.
        # Each occurrence notifies once; a notifier failure is recorded for
        # GET /api/v1/notifications/delivery and never fails this read.
        from krellbot import notifications as kb_notifications

        kb_notifications.maybe_notify(
            s.home,
            body.get("alerts") or [],
            notifier=s.alert_notifier,
            now=now,
        )
        return JSONResponse(body, status_code=200)

    @app.get("/api/v1/notifications")
    async def notifications_settings_get(request: Request) -> Response:
        """Return closed-shape notification settings (M9.4).

        Same session gate as ``GET /api/v1/operations`` (``_gate_get``).
        The body never carries an SMTP password or any secret — password
        is supplied only via ``KRELLBOT_SMTP_PASSWORD`` at send time.
        Telemetry remains a separate opt-in.
        """

        from krellbot import notifications as kb_notifications

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = kb_notifications.get_settings(s.home)
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/notifications/delivery")
    async def notifications_delivery_get(request: Request) -> Response:
        """Return each channel's real delivery record (M28.1).

        Same session gate as ``GET /api/v1/notifications`` (``_gate_get``).
        Per channel: enabled, last successful send, occurrences still
        waiting, and the last failure as a fixed code and message. Never
        carries exception text, an SMTP password or any other secret.
        """

        from krellbot import notifications as kb_notifications

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = kb_notifications.delivery_status(s.home)
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.put("/api/v1/notifications")
    async def notifications_settings_put(request: Request) -> Response:
        """Update opt-in desktop/email notification settings (M9.4).

        Same state-change gate as ``POST /api/v1/commands``. Rejects any
        attempt to persist an SMTP password in settings.
        """

        from krellbot import notifications as kb_notifications

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        try:
            body = kb_notifications.update_settings(s.home, payload)
        except ValueError:
            # Fixed copy only — do not echo exception text (CodeQL py/stack-trace-exposure).
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "bad_input",
                    "ok": False,
                    "message": "email password must come from KRELLBOT_SMTP_PASSWORD, not settings",
                },
                status_code=400,
            )
        except TypeError:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "bad_input",
                    "ok": False,
                    "message": "invalid notification settings",
                },
                status_code=400,
            )
        return JSONResponse(
            {**body, "ok": True, "code": "notifications_updated"},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/referral")
    async def referral_status_get(request: Request) -> Response:
        """Return disclosed referral / fee-share status and local receipts (M23.1).

        Same session gate as ``GET /api/v1/notifications`` (``_gate_get``).
        Copy matches product-direction earn rule (1). Never carries
        exchange credentials or license keys.
        """

        from krellbot import referral as kb_referral

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = kb_referral.status(s.home)
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/referral/attribute")
    async def referral_attribute_post(request: Request) -> Response:
        """Record local referral receipt and post to the server seam (M23.1).

        Same state-change gate as ``POST /api/v1/commands``. Accepts
        ``kind`` of ``exchange_signup`` or ``pack_purchase``. Tests inject
        ``referral_transport`` on app state so CI never egresses.
        """

        from krellbot import referral as kb_referral

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        kind = payload.get("kind", "")
        if not isinstance(kind, str):
            kind = ""
        venue = payload.get("venue", "")
        if not isinstance(venue, str):
            venue = ""
        pack_id = payload.get("pack_id", "")
        if not isinstance(pack_id, str):
            pack_id = ""

        # Explicit Settings/Packs attribution: use injected fake in tests,
        # otherwise the real HTTPS seam. Redeem side-effects stay local-only
        # unless a transport was injected (see activation redeem below).
        transport = s.referral_transport
        if transport is None:
            transport = kb_referral.UrllibTransport()
        outcome = kb_referral.attribute(
            s.home,
            kind=kind,
            venue=venue,
            pack_id=pack_id,
            transport=transport,
            now=_app_now(s),
        )
        status_code = 200 if outcome.ok else 400
        return JSONResponse(
            kb_referral.outcome_dict(outcome),
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/telemetry")
    async def telemetry_settings_get(request: Request) -> Response:
        """Return closed-shape telemetry consent (default off; M21.4).

        Same session gate as ``GET /api/v1/notifications``. Never returns
        keys, secrets, install_id value, or strategy source — only the
        opt-in flag and disclosed funnel event names.
        """

        from krellbot import telemetry as kb_telemetry

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = kb_telemetry.status(s.home)
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.put("/api/v1/telemetry")
    async def telemetry_settings_put(request: Request) -> Response:
        """Update opt-in telemetry consent from the checkout UI (M21.4).

        Same state-change gate as other POSTs/PUTs. Body is
        ``{"enabled": true|false}``. Enabling is an explicit consent write;
        nothing is sent by this route itself.
        """

        from krellbot import telemetry as kb_telemetry

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict) or "enabled" not in payload:
            return JSONResponse({"detail": "enabled is required"}, status_code=400)

        body = kb_telemetry.set_enabled(s.home, bool(payload.get("enabled")))
        return JSONResponse(
            {**body, "ok": True, "code": "telemetry_updated"},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/trust")
    async def trust_get(request: Request) -> Response:
        """Return the local trust snapshot + live-wizard honesty rails.

        Same session gate as ``GET /api/v1/operations`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF for
        the read. Presentation-only: never probes a venue, never reads
        exchange credentials, never asks for a license. While
        ``PAPER_ONLY_RELEASE`` is active, ``live_ready`` is always False
        even when a live grant file exists — paper-only release and live
        grant are distinct states.

        ``trust.bind`` stays the literal ``127.0.0.1``. ``bind_measured``
        is the local address this request arrived on, and
        ``remote_access`` says whether Tailscale Serve access is on.
        ``remote_scope`` is the stored tailnet scope (``read_only`` unless a
        loopback session stored ``full``); it sits beside ``remote_access``,
        whose two keys stay as they are.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        now = _now_seconds()
        snap = trust_snapshot(s.home)
        live = live_gate.live_status(s.home, now=now)
        auth = live_gate.authorization_state(s.home, now=now).to_dict()
        paper_only = live.get("release_policy") == "paper_only"
        body = {
            "schema_version": SCHEMA_VERSION,
            "trust": snap,
            "paper_only_release": bool(paper_only),
            "release_policy": live.get("release_policy"),
            "release_policy_code": live.get("release_policy_code"),
            "release_policy_message": (release_policy.paper_only_message() if paper_only else None),
            # Distinct from paper-only: a grant file may exist and still
            # never make this surface claim the workstation is live-ready.
            "live_grant_active": bool(auth.get("active")),
            "live_grant": auth,
            "live_ready": False,
            "live_arm_ui_allowed": False,
            "trade_only_required": True,
            "license_required_for_exchange": False,
            "keys_leave_machine": False,
            "bind_measured": _measured_bind(request),
            "remote_access": _remote_access_view(tailnet_access),
            "remote_scope": read_remote_scope(s.home),
        }
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.put("/api/v1/remote-access/scope")
    async def remote_scope_put(request: Request) -> Response:
        """Set what a Tailscale session may do (D14): ``read_only`` or ``full``.

        Same state-change gate as other PUTs (session cookie + CSRF +
        loopback Origin). Only a loopback session may change it: a server
        started with Tailscale access admits only tailnet sessions, so it
        refuses every write here with ``remote_scope_loopback_only``. Under
        ``read_only`` the Tailscale middleware refuses the write before it
        arrives. There is no CLI flag for this.
        """

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        if tailnet_access is not None:
            message = "Change remote access from a local session on this computer."
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "remote_scope_loopback_only",
                    "ok": False,
                    "effect": "refused",
                    "message": message,
                    "detail": message,
                },
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        scope = payload.get("scope") if isinstance(payload, dict) else None
        if scope not in REMOTE_SCOPES:
            return JSONResponse({"detail": "scope must be 'read_only' or 'full'"}, status_code=400)

        stored = write_remote_scope(s.home, scope)
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "code": "remote_scope_updated",
                "remote_scope": stored,
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/entitlements")
    async def entitlements_get(request: Request) -> Response:
        """Return what entitlements gate and what continues on lapse (M28.14).

        Same session gate as ``GET /api/v1/trust`` (``_gate_get``): session
        cookie + loopback Origin + loopback Host, no CSRF for the read.
        Read-only: reads ``license.read_cache``, ``license.entries_allowed``,
        ``catalog.requires_license_for`` and ``checkout.read_entitlements``,
        never refreshes or writes either record, and names no price. The
        license cache and the SKU entitlement records are reported side by
        side because they are separate and can disagree.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = _entitlements_body(s.home, now=_app_now(s))
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/demo-fixture")
    async def demo_fixture_get(request: Request) -> Response:
        """Return opt-in demo/fixture seed status (M18.5).

        Same session gate as ``GET /api/v1/operations`` (``_gate_get``).
        Default fresh homes report enabled=false (empty/honest). Fixture
        metrics never claim live performance or soak proof.
        """

        from krellbot.application import demo_fixture_seed as kb_demo_fixture

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = kb_demo_fixture.get_status(s.home)
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/demo-fixture/seed")
    async def demo_fixture_seed(request: Request) -> Response:
        """Opt-in enable the labeled demo/fixture seed (M18.5).

        Same state-change gate as ``POST /api/v1/commands``. Requires
        ``{"confirm": true}`` in the JSON body. Refuses to present
        fixture metrics as live or soak proof.
        """

        from krellbot.application import demo_fixture_seed as kb_demo_fixture

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        confirm = payload.get("confirm") is True
        body = kb_demo_fixture.seed(s.home, confirm=confirm)
        status = 200 if body.get("ok") else 400
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/demo-fixture/clear")
    async def demo_fixture_clear(request: Request) -> Response:
        """Clear the demo/fixture seed; restore empty/honest home (M18.5).

        Same state-change gate as ``POST /api/v1/commands``.
        """

        from krellbot.application import demo_fixture_seed as kb_demo_fixture

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        body = kb_demo_fixture.clear(s.home)
        status = 200 if body.get("ok") else 500
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/home/export")
    async def home_export(request: Request) -> Response:
        """Create a local home backup archive and return it as a download.

        Same state-change gate as ``POST /api/v1/commands`` (session +
        CSRF + loopback Origin/Host). Uses ``storage.home_backup.create``
        into a temporary path outside the data home so the archive never
        nests inside itself. Billing/store status is never consulted —
        export stays available when the store is down (product-direction).
        Secrets already excluded by ``home_backup`` stay off the wire.
        """

        import tempfile

        from krellbot.storage import home_backup

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        now = _app_now(s)
        filename = f"krellbot-home-{now}.tar.gz"
        try:
            with tempfile.TemporaryDirectory(prefix="krellbot-home-export-") as tmp:
                archive = Path(tmp) / filename
                home_backup.create(s.home, archive, now=now)
                payload = archive.read_bytes()
        except home_backup.BackupError as exc:
            code = getattr(exc, "code", "backup_refused")
            if not isinstance(code, str) or not code:
                code = "backup_refused"
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "code": code,
                    "message": "home export refused",
                },
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        except OSError:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "code": "export_failed",
                    "message": "home export failed",
                },
                status_code=500,
                headers={"Cache-Control": "no-store"},
            )

        return Response(
            content=payload,
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Krellbot-Home-Export": "1",
            },
        )

    @app.post("/api/v1/home/restore")
    async def home_restore(request: Request) -> Response:
        """Restore a home backup archive with honest restore fencing.

        Same state-change gate as ``POST /api/v1/home/export`` (session +
        CSRF + loopback Origin/Host). Body is the gzip archive bytes
        (``application/gzip`` or ``application/octet-stream``).

        Corrupt archives are refused and leave the live home untouched.
        A successful restore into an empty home engages the restore fence
        (entries paused / kill switch). While ``PAPER_ONLY_RELEASE`` is
        True the response never claims ``live_ready``. Non-empty homes
        are refused after archive validation so a good home is never
        destroyed by a restore attempt.
        """

        import tempfile

        from krellbot.storage import home_backup

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        payload = await request.body()
        if not payload:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "code": "bad_input",
                    "message": "home restore requires an archive body",
                    "live_ready": False,
                    "paper_only_release": bool(release_policy.PAPER_ONLY_RELEASE),
                    "fence_engaged": False,
                    "applied": False,
                },
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )

        paper_only = bool(release_policy.PAPER_ONLY_RELEASE)

        def _honesty(
            *,
            fence_engaged: bool,
            kill_reason: str | None,
            applied: bool,
            code: str,
            message: str,
            ok: bool,
            status_code: int,
            extra: dict | None = None,
        ) -> JSONResponse:
            body: dict = {
                "schema_version": SCHEMA_VERSION,
                "ok": ok,
                "code": code,
                "message": message,
                "fence_engaged": fence_engaged,
                "kill_reason": kill_reason,
                "entries_paused": bool(fence_engaged),
                # Restore surface never claims live-ready (paper-only or fenced).
                "live_ready": False,
                "paper_only_release": paper_only,
                "applied": applied,
            }
            if extra:
                body.update(extra)
            return JSONResponse(
                body,
                status_code=status_code,
                headers={"Cache-Control": "no-store"},
            )

        def _home_empty(home: Path) -> bool:
            if not home.exists():
                return True
            if not home.is_dir():
                return False
            try:
                next(home.iterdir())
            except StopIteration:
                return True
            return False

        with tempfile.TemporaryDirectory(prefix="krellbot-home-restore-") as tmp:
            archive = Path(tmp) / "upload.tar.gz"
            archive.write_bytes(payload)

            # Validate (and preview fence) into an empty staging home first so
            # a corrupt archive never touches the live workstation home.
            preview = Path(tmp) / "preview-home"
            try:
                home_backup.restore(archive, preview)
            except home_backup.BackupError as exc:
                code = getattr(exc, "code", "archive_corrupt")
                if not isinstance(code, str) or not code:
                    code = "archive_corrupt"
                return _honesty(
                    fence_engaged=False,
                    kill_reason=None,
                    applied=False,
                    code=code,
                    message="home restore refused; workstation home untouched",
                    ok=False,
                    status_code=400,
                )
            except OSError:
                return _honesty(
                    fence_engaged=False,
                    kill_reason=None,
                    applied=False,
                    code="restore_failed",
                    message="home restore failed; workstation home untouched",
                    ok=False,
                    status_code=500,
                )

            preview_kill = live_gate.kill_state(preview)
            preview_reason = preview_kill.reason if preview_kill.engaged else None

            if not _home_empty(s.home):
                return _honesty(
                    fence_engaged=True,
                    kill_reason=preview_reason,
                    applied=False,
                    code="home_not_empty",
                    message=(
                        "archive validated; restore not applied because the "
                        "workstation home is not empty. Stop the workstation, "
                        "point KRELLBOT_HOME at an empty directory, then restore. "
                        "A successful restore engages the kill switch and pauses "
                        "live entries; this paper-only release is never live-ready."
                    ),
                    ok=False,
                    status_code=409,
                    extra={"validated": True},
                )

            try:
                home_backup.restore(archive, s.home)
            except home_backup.BackupError as exc:
                code = getattr(exc, "code", "archive_corrupt")
                if not isinstance(code, str) or not code:
                    code = "archive_corrupt"
                return _honesty(
                    fence_engaged=False,
                    kill_reason=None,
                    applied=False,
                    code=code,
                    message="home restore refused; workstation home untouched",
                    ok=False,
                    status_code=400,
                )
            except OSError:
                return _honesty(
                    fence_engaged=False,
                    kill_reason=None,
                    applied=False,
                    code="restore_failed",
                    message="home restore failed; workstation home untouched",
                    ok=False,
                    status_code=500,
                )

        kill = live_gate.kill_state(s.home)
        return _honesty(
            fence_engaged=bool(kill.engaged),
            kill_reason=kill.reason if kill.engaged else None,
            applied=True,
            code="home_restored",
            message=(
                "home restored; restore fence engaged (entries paused / kill "
                "switch). This paper-only release is not live-ready."
                if paper_only
                else "home restored; restore fence engaged (entries paused / kill switch)."
            ),
            ok=True,
            status_code=200,
        )

    @app.get("/api/v1/operations/deployments")
    async def operations_deployment_history(request: Request) -> Response:
        """Return the closed-shape deployment history rows (read-only).

        Same session gate as ``GET /api/v1/operations`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF
        required for the read. Rows are projected from the append-only
        journal by ``operations_view_app.list_deployment_records`` and
        are capped at the 50 most recent. The body never carries
        ``cap``, ``stop``, ``starting_cash``, ``owned_qty``,
        ``pack_path``, ``pack_sha256``, balance, or key material.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        rows = operations_view_app.list_deployment_records(s.home)
        return JSONResponse(
            {"schema_version": SCHEMA_VERSION, "deployments": rows},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/operations/deployments/{deployment_id}")
    async def operations_deployment_record(request: Request, deployment_id: str) -> Response:
        """Return one closed-shape deployment history row, or 404.

        Same session gate and same closed row shape as the list route.
        An unknown ``deployment_id`` is a 404 with a typed refusal code;
        there is no editing, starting, or promotion on this path.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        row = operations_view_app.get_deployment_record(s.home, deployment_id)
        if row is None:
            return JSONResponse(
                {"code": "deployment_not_found", "message": "deployment record not found"},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(row, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/operations/unresolved-orders")
    async def operations_unresolved_orders(request: Request) -> Response:
        """Return every order awaiting reconcile (read-only, M1.9).

        Same session gate as ``GET /api/v1/operations`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF
        required for the read. Each row carries ``client_order_id``,
        ``age_seconds``, and ``last_lookup_result``. An empty list is a
        successful 200 with ``orders: []``. The body never carries
        ``cap``, ``stop``, ``starting_cash``, ``owned_qty``,
        ``pack_path``, ``pack_sha256``, balance, or key material.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        from krellbot.execution.obligations import ObligationsUnavailable

        try:
            rows = operations_view_app.list_unresolved_orders(s.home, now=_now_seconds())
        except ObligationsUnavailable as exc:
            # Unknown is not empty: say so instead of showing a clean list.
            return JSONResponse(
                {"schema_version": SCHEMA_VERSION, "error": "obligations_unavailable", "code": exc.code},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"schema_version": SCHEMA_VERSION, "orders": rows},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/operations/live-authorization")
    async def operations_live_authorization_get(request: Request) -> Response:
        """Return the closed-shape live authorization snapshot (M1.10).

        Same session gate as ``GET /api/v1/operations`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF
        required for the read. While ``PAPER_ONLY_RELEASE`` is active the
        body still reports the paper-only policy so the Operations panel
        can stay visible, disabled, and explain why.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        now = _now_seconds()
        live = live_gate.live_status(s.home, now=now)
        paper_only = live.get("release_policy") == "paper_only"
        body = {
            "schema_version": SCHEMA_VERSION,
            "authorization": live_gate.authorization_state(s.home, now=now).to_dict(),
            "release_policy": live.get("release_policy"),
            "release_policy_code": live.get("release_policy_code"),
            "release_policy_message": (release_policy.paper_only_message() if paper_only else None),
            "confirmation_required": live_gate.CONFIRMATION_TEXT,
        }
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/operations/live-authorization")
    async def operations_live_authorization_grant(request: Request) -> Response:
        """Create a live authorization for one exact venue/pair with expiry.

        Same state-change gate as ``POST /api/v1/commands``
        (``_gate_state_change``): session cookie + CSRF + loopback Origin.
        Requires the typed confirmation text ``LIVE``. While
        ``PAPER_ONLY_RELEASE`` is active the grant is refused and no file
        is written.
        """

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        venue = payload.get("venue")
        pair = payload.get("pair")
        expires_at = payload.get("expires_at")
        confirmation = payload.get("confirmation")
        if (
            not isinstance(venue, str)
            or not isinstance(pair, str)
            or not isinstance(expires_at, int)
            or not isinstance(confirmation, str)
        ):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        now = _now_seconds()
        result = live_gate.grant_authorization(
            s.home,
            venue=venue,
            pair=pair,
            expires_at=expires_at,
            confirmation=confirmation,
            now=now,
        )
        auth = live_gate.authorization_state(s.home, now=now).to_dict()
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "code": result.code,
                "ok": result.ok,
                "message": result.message,
                "effect": "changed" if result.ok else "refused",
                "authorization": auth,
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.delete("/api/v1/operations/live-authorization")
    async def operations_live_authorization_revoke(request: Request) -> Response:
        """Revoke the current live authorization record.

        Same state-change gate as ``POST /api/v1/commands``. While
        ``PAPER_ONLY_RELEASE`` is active revocation is refused so the
        panel stays disabled for the paper-only release.
        """

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        now = _now_seconds()
        result = live_gate.revoke_authorization(s.home, now=now)
        auth = live_gate.authorization_state(s.home, now=now).to_dict()
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "code": result.code,
                "ok": result.ok,
                "message": result.message,
                "effect": "changed" if result.ok else "refused",
                "authorization": auth,
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/operations/live-guardrails")
    async def operations_live_guardrails_get(request: Request) -> Response:
        """Return active live guardrails for Deploy preflight (M20.3).

        Same session gate as ``GET /api/v1/operations`` (``_gate_get``).
        Optional ``venue`` (default ``kraken``) and ``revision_id`` query
        params; revision loads pack stop / max_account_pct. Read-only —
        does not journal. Use ``live.guardrails`` to journal refusals.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        venue_q = request.query_params.get("venue") or "kraken"
        revision_id = request.query_params.get("revision_id")
        if not isinstance(venue_q, str) or not venue_q.strip():
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        pack_for_rails = None
        if revision_id:
            try:
                _trusted, pack_for_rails = _draft_service(s).pack_for_arm(revision_id)
            except RevisionNotFound:
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "code": "draft_not_found",
                        "ok": False,
                        "message": f"draft {revision_id} not found",
                    },
                    status_code=404,
                )
            except DraftNotRunnable:
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "code": "draft_not_runnable",
                        "ok": False,
                        "message": f"draft {revision_id} is not runnable",
                    },
                    status_code=403,
                )

        snap = live_guardrails.snapshot(
            s.home,
            venue=venue_q,
            pack_data=pack_for_rails,
            now=_now_seconds(),
        )
        return JSONResponse(snap.to_dict(), status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/scheduler")
    async def scheduler_status_get(request: Request) -> Response:
        """Return closed-shape scheduler status (M2.7).

        Same session gate as ``GET /api/v1/operations`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF.
        Body keys: ``installed``, ``enabled``, ``last_error``.
        """

        from krellbot.service import supervise as kb_supervise

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = kb_supervise.scheduler_status(
            s.home,
            write_root=_scheduler_write_root(s),
            supervisor=_scheduler_supervisor(s),
        )
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/diagnostics/report")
    async def diagnostic_report(request: Request) -> Response:
        """Return an allowlisted local paper diagnostic report."""
        import platform
        import re

        from krellbot import release_channel
        from krellbot.data.instruments import (
            METADATA_INVALID,
            METADATA_STALE,
            METADATA_SYMBOL_MISMATCH,
            METADATA_UNAVAILABLE,
            MINIMUM_NOT_MET,
        )
        from krellbot.service import supervise as kb_supervise

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        try:
            scheduler = await asyncio.to_thread(
                kb_supervise.scheduler_status,
                s.home,
                write_root=_scheduler_write_root(s),
                supervisor=_scheduler_supervisor(s),
            )
            scheduler_view = {
                "installed": bool(scheduler["installed"]),
                "enabled": bool(scheduler["enabled"]),
                "has_error": bool(scheduler.get("last_error")),
            }
        except Exception:  # noqa: BLE001 — diagnostics must survive an unavailable scheduler.
            scheduler_view = None

        allowed_codes = {
            METADATA_INVALID,
            METADATA_STALE,
            METADATA_SYMBOL_MISMATCH,
            METADATA_UNAVAILABLE,
            MINIMUM_NOT_MET,
        }
        from krellbot import secrets as kb_secrets

        events = []
        for record in _read_journal_records(s.home):
            venue = record.get("venue")
            if record.get("kind") != "tick" or not isinstance(venue, str) or venue not in kb_secrets.VENUES:
                continue
            detail = record.get("detail")
            if not isinstance(detail, dict) or detail.get("mode") != "paper":
                continue
            pair = detail.get("pair")
            ts = record.get("ts")
            if not isinstance(pair, str) or re.fullmatch(r"[A-Z0-9-]{3,24}", pair) is None:
                continue
            if type(ts) is not int or ts < 0:
                continue
            code = _tick_refusal_code(record)
            reason = detail.get("reason")
            if detail.get("ensure_stop_failed") is True:
                classification = "stop_repair_failed"
            elif detail.get("kill_switch") is True:
                classification = "kill_switch"
            elif code:
                classification = "refused"
            elif reason in ("fetch_error", "check_incomplete", "no_candles"):
                classification = reason
            elif detail.get("entries_paused") is True:
                classification = "paused"
            elif detail.get("entries_blocked") is True:
                classification = "entries_blocked"
            elif reason in ("flat", "entry", "exit", "warmup"):
                classification = reason
            else:
                classification = "other"
            events.append(
                {
                    "ts": ts,
                    "venue": venue,
                    "pair": pair,
                    "source": "journal",
                    "classification": classification,
                    "refusal_code": code if code in allowed_codes else ("other" if code else None),
                }
            )
        events.sort(key=lambda event: event["ts"])
        os_name = platform.system()
        version_info = release_channel.version_payload()
        source_commit = version_info.pop("source_commit", None)
        body = {
            "schema_version": SCHEMA_VERSION,
            "build": {**version_info, "commit": source_commit},
            "os": os_name if os_name in {"Linux", "Darwin", "Windows"} else "other",
            "scheduler": scheduler_view,
            "events": events[-100:],
            "reproduction_steps": "",
        }
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/diagnostics/operations")
    async def diagnostic_operations(request: Request) -> Response:
        """Return the allowlisted Operations section of a diagnostic report (M28.15).

        Same session gate as ``GET /api/v1/diagnostics/report``
        (``_gate_get``). Served on its own route so the report's six keys
        stay unchanged; the web app offers it as a separate, optional
        download. Read-only: it never acknowledges or notifies an alert.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        body = await asyncio.to_thread(_diagnostic_operations_section, s.home, now=_now_seconds())
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/scheduler/install")
    async def scheduler_install(request: Request) -> Response:
        """Install OS scheduler unit files via existing service renderers.

        Same state-change gate as ``POST /api/v1/commands``
        (``_gate_state_change``): session cookie + CSRF + loopback Origin.
        Already-installed units are reported as success without rewrite.
        """

        from krellbot.service import supervise as kb_supervise

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        body = kb_supervise.install_scheduler(
            s.home,
            write_root=_scheduler_write_root(s),
            supervisor=_scheduler_supervisor(s),
            executable=_scheduler_executable(s),
        )
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/scheduler/enable")
    async def scheduler_enable(request: Request) -> Response:
        """Enable the installed scheduler unit via the platform supervisor.

        Same state-change gate as ``POST /api/v1/commands``. Platform
        calls go through ``s.scheduler_supervisor`` (fake in tests).
        """

        from krellbot.service import supervise as kb_supervise

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        body = kb_supervise.enable_scheduler(
            s.home,
            write_root=_scheduler_write_root(s),
            supervisor=_scheduler_supervisor(s),
        )
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/paper/tradingview/preview")
    async def tradingview_paper_preview(request: Request) -> Response:
        """Validate an alert supplied by the local operator without executing it."""
        from krellbot.application.tradingview import MAX_ALERT_BYTES, preview_alert

        denied = _gate_state_change(request, _state(request.app))
        if denied is not None:
            return denied
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_ALERT_BYTES:
                return JSONResponse(
                    {"schema_version": SCHEMA_VERSION, "ok": False, "code": "alert_too_large"},
                    status_code=413,
                )
            body.extend(chunk)
        try:
            alert = preview_alert(bytes(body))
        except ValueError as exc:
            return JSONResponse(
                {"schema_version": SCHEMA_VERSION, "ok": False, "code": "invalid_alert", "message": str(exc)},
                status_code=400,
            )
        return JSONResponse(
            {"schema_version": SCHEMA_VERSION, "ok": True, "code": "alert_previewed", "alert": alert},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/paper/status")
    async def paper_status(request: Request) -> Response:
        """Return the closed-shape paper status projection (NS10b).

        The route uses the same session gate as ``GET /api/v1/jobs/{id}``
        (``_gate_get``): session cookie + loopback Origin + loopback Host,
        no CSRF required. The body never carries cash, quantity, stop,
        cap, or the pack path. ``mode`` is always ``"paper"`` for an
        armed record; the brief forbids a live-order control and
        forbids ``mode: live`` over the wire.

        Multi-deploy: optional ``venue``+``pair`` query params select one
        armed record. Without them the first record is projected, and
        ``armed_count`` always reports how many packs are armed so the
        UI is not silently limited to ``config.armed[0]``.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        config = kb_config.load_config(s.home)
        armed, armed_count = _select_armed_for_request(config, request)
        if armed is None:
            return JSONResponse(
                _with_armed_count(
                    {"schema_version": SCHEMA_VERSION, "armed": False},
                    armed_count,
                ),
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )

        body = _with_armed_count(
            {
                "schema_version": SCHEMA_VERSION,
                "armed": True,
                "venue": armed.venue,
                "pair": armed.pair,
                "entries_paused": kb_config.entries_held(armed),
                "mode": armed.mode,
                "pack_id": armed.pack_id,
            },
            armed_count,
        )
        return JSONResponse(body, status_code=200)

    @app.get("/api/v1/paper/health")
    async def paper_health(request: Request) -> Response:
        """Report current-arm paper evidence and scheduler state."""
        from krellbot.service import supervise as kb_supervise

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        armed, _ = _select_armed_for_request(kb_config.load_config(s.home), request)
        if armed is None or armed.mode != "paper":
            return JSONResponse(
                {"schema_version": SCHEMA_VERSION, "armed": False}, headers={"Cache-Control": "no-store"}
            )

        scheduler = await asyncio.to_thread(
            kb_supervise.scheduler_status,
            s.home,
            write_root=_scheduler_write_root(s),
            supervisor=_scheduler_supervisor(s),
        )
        records = alerts_app.current_arm_ticks(_read_journal_records(s.home), armed)
        latest = records[-1] if records else None
        detail = latest.get("detail", {}) if latest else {}
        reason = detail.get("reason")
        refusal = _tick_refusal_code(latest) if latest else None
        timeframe_seconds = alerts_app.pack_timeframe_seconds(armed.pack_path)

        def has_fresh_candle(rec: dict) -> bool:
            return alerts_app.candle_is_fresh(
                bar_ts_ms=rec.get("bar_ts"), read_at=rec["ts"], timeframe_seconds=timeframe_seconds
            )

        successful = [
            rec
            for rec in records
            if _tick_refusal_code(rec) is None
            and (rec.get("detail") or {}).get("reason") not in {"fetch_error", "check_incomplete", "no_candles"}
            and not any(
                (rec.get("detail") or {}).get(flag) for flag in ("kill_switch", "entries_blocked", "ensure_stop_failed")
            )
            and has_fresh_candle(rec)
        ]
        bar_ts = latest.get("bar_ts") if latest else None
        candle_stale = latest is not None and not has_fresh_candle(latest)
        try:
            has_fill = any(Decimal(str(detail.get(k, "0"))) > 0 for k in ("entry_qty", "exit_qty", "stop_qty"))
        except (InvalidOperation, ValueError):
            has_fill = False
        if kb_config.entries_held(armed):
            state = "paused"
        elif armed.armed_at_ts <= 0:
            state = "unavailable"
        elif detail.get("kill_switch") or detail.get("ensure_stop_failed") or detail.get("entries_blocked"):
            state = "blocked"
        elif reason == "fetch_error":
            state = "feed_error"
        elif reason == "check_incomplete":
            state = "check_incomplete"
        elif refusal:
            state = "stale_rules" if refusal == "metadata_stale" else "refused"
        elif reason == "no_candles":
            state = "no_candles"
        elif latest and (timeframe_seconds is None or not isinstance(bar_ts, (int, float))):
            state = "unavailable"
        elif candle_stale:
            state = "stale_candles"
        elif latest and _app_now(s) - latest["ts"] >= max(7200, (timeframe_seconds or 0) + 7200):
            state = "stale_check"
        elif latest:
            state = "filled" if has_fill else "checked"
        else:
            state = "waiting"
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "armed": True,
                "venue": armed.venue,
                "pair": armed.pair,
                "pack_id": armed.pack_id,
                "state": state,
                "failure_code": _feed_failure_code(detail) if state == "feed_error" else None,
                "last_check": int(latest["ts"]) if latest else None,
                "last_success": int(successful[-1]["ts"]) if successful else None,
                "scheduler_installed": scheduler["installed"],
                "scheduler_enabled": scheduler["enabled"],
                "scheduler_error": bool(scheduler["last_error"]),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/paper/details")
    async def paper_details(request: Request) -> Response:
        """Read configured paper deposit/percent and this pair's durable stop.

        The deposit is not free cash, and the cap is not a currency amount.
        Only a paper-mode armed record is projected (first by default, or
        the ``venue``+``pair`` query selection). Reading raw state never
        constructs a venue, seeds balances, evaluates stops, or infers a
        fill. Existing status/position contracts stay closed.
        """
        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        config = kb_config.load_config(s.home)
        armed, _armed_count = _select_armed_for_request(config, request)
        if armed is None or armed.mode != "paper":
            return JSONResponse(
                {"schema_version": SCHEMA_VERSION, "armed": False},
                headers={"Cache-Control": "no-store"},
            )

        try:
            # Unlike the legacy display reader, preserve raw JSON fractions
            # as Decimal: binary floats can round a price or underflow to zero.
            # Keep the default integer parser's safety limit local and intact.
            path = s.home / "run" / f"paper-{armed.venue}.json"
            state = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        except (OSError, ValueError, InvalidOperation, RecursionError):
            state = {}
        orders = state.get("open_orders") if isinstance(state, dict) else None
        resting_stop = None
        if isinstance(orders, list):
            # Match PaperVenue.resting_stop's first matching non-null stop,
            # but fail closed on malformed/nonfinite persisted values.
            for order in orders:
                if isinstance(order, dict) and order.get("pair") == armed.pair and order.get("stop_price") is not None:
                    resting_stop = _paper_details_decimal(order["stop_price"])
                    break

        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "armed": True,
                "venue": armed.venue,
                "pair": armed.pair,
                "pack_id": armed.pack_id,
                "starting_cash": _paper_details_decimal(armed.starting_cash),
                "risk_cap_pct": _paper_details_decimal(armed.cap),
                "resting_stop": resting_stop,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/paper/assumptions")
    async def paper_assumptions(request: Request) -> Response:
        """What a paper run on one venue assumes, as the paper engine uses it.

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF. ``venue``
        is required. The body gives that venue's taker fee and the paper
        slippage, with the research fee-model label. An optional ``pair``
        adds the paper-mode armed record's timeframe, stop rule and starting
        cash. A value the engine does not give is null. Read-only: never
        constructs a venue, writes, or reaches the network.
        """
        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        venue = (request.query_params.get("venue") or "").strip()
        if not venue:
            return JSONResponse(
                {"detail": "venue required"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        pair = (request.query_params.get("pair") or "").strip() or None
        body = project_paper_assumptions(s.home, venue=venue, pair=pair)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/paper/daily-loss-limit")
    async def paper_daily_loss_limit(request: Request) -> Response:
        """Return an armed record's daily loss limit and the gate as of its last tick.

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``).
        ``venue``+``pair`` select the record as for the status read. The body
        names the limit (a percent of ``starting_cash``) and its amount, the
        UTC ``day`` of the last tick that measured the arm, that day's
        opening, lowest and latest equity, and ``code``: ``daily_loss_limit``
        or ``loss_limit_unavailable`` (with ``reason``) while new entries are
        held, otherwise null. These are bookkeeping values from the run's
        recorded fills, not market performance. Read-only; changes go through
        ``risk.set_daily_loss_limit``.
        """

        from krellbot.run import daily_loss as kb_daily_loss

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        config = kb_config.load_config(s.home)
        armed, _armed_count = _select_armed_for_request(config, request)
        if armed is None:
            body: dict = {"schema_version": SCHEMA_VERSION, "armed": False}
        else:
            body = {
                "schema_version": SCHEMA_VERSION,
                "armed": True,
                **kb_daily_loss.project(s.home, armed, now=_app_now(s)),
            }
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/paper/position")
    async def paper_position(request: Request) -> Response:
        """Return the owned paper position read from the persisted state file.

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF. The
        body is the paper-only owned-position projection the workstation
        displays verbatim:

        * ``free_cash`` — the quote-asset balance string exactly as
          ``<home>/run/paper-<venue>.json`` persists it (Decimal-as-string,
          never re-formatted). When no state file exists yet (just-armed,
          before the first tick), the armed record's ``starting_cash`` is
          projected instead of a false ``\"0\"``.
        * ``holdings`` — at most one ``{"symbol", "qty"}`` row for this
          armed pair's base asset when its balance is above zero, qty
          verbatim from the state file. Other base assets in the same
          venue wallet are omitted so another market's position is never
          presented here. The quote asset is the pair's USD-style suffix,
          the same base/quote split ``krellbot.application.paper`` uses.
        * ``as_of_run`` — ``"<venue>:<pack>:<pair>"`` of the most recent
          journal tick run for the armed pair, or ``null`` when the
          journal has no tick record for it (the engine has never run).
        * ``armed_count`` — present only when more than one pack is armed;
          optional ``venue``+``pair`` query params select which record to
          project.

        The route never writes, arms, or flattens anything, and the
        closed-shape ``/api/v1/paper/status`` contract is untouched:
        cash, quantity, stop, cap and pack path stay off that body. When
        no pack is armed the body is ``{"schema_version": <v>,
        "armed": false}`` and no state file is read.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        config = kb_config.load_config(s.home)
        armed, armed_count = _select_armed_for_request(config, request)
        if armed is None:
            return JSONResponse(
                _with_armed_count(
                    {"schema_version": SCHEMA_VERSION, "armed": False},
                    armed_count,
                ),
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )

        body = project_owned_position(
            s.home,
            venue=armed.venue,
            pair=armed.pair,
            mode=armed.mode,
            as_of_run=_paper_position_run_id(s.home, armed.venue, armed.pair),
            starting_cash=armed.starting_cash,
        )
        return JSONResponse(
            _with_armed_count(body, armed_count),
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/paper/chart")
    async def paper_chart(request: Request) -> Response:
        """Return P&L + position (or mark) series for the armed paper market.

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF. The
        body is the closed-shape chart projection from journal ticks and
        paper-state fills:

        * ``pnl`` — fill-replay equity minus ``starting_cash``
        * ``position`` — journal ``owned_qty_after`` over time
        * ``marks`` — fill prices (mark series when position is thin)

        Series stay empty when evidence is missing; nothing is fabricated.
        When no pack is armed the body is
        ``{"schema_version": <v>, "armed": false}``. Read-only: never
        writes, arms, or flattens.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = project_paper_pnl_position_chart(s.home)
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/paper/history")
    async def paper_history(request: Request) -> Response:
        """Return the read-only "Last runs" projection (paper panel).

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF. The
        body is read-only evidence over ``<home>/journal/*.jsonl`` plus
        the paper state file ``<home>/run/paper-<venue>.json``: one run
        per ``(venue, pack, pair)`` the journal records, capped at 20
        runs with ``recent_fills`` capped at 50. The route never reads
        or returns owned cash, quantity, stop, cap, or the pack path —
        fill ``qty``/``price`` stay off the wire too — and there is no
        control surface here: nothing edits, arms, or starts a run.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = _paper_history_view(s.home)

        return JSONResponse(
            body,
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/paper/ledger")
    async def paper_ledger(request: Request) -> Response:
        """Return the forward chronological paper evidence ledger.

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF. The
        body projects journal ticks/refusals, paper-state fills, and
        paper-mode arm records oldest→newest for outsider reproduction.
        Labeled paper / local evidence — never live P&L. Fill qty/price
        and owned cash stay off the wire. Read-only: never arms or writes.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = project_forward_paper_ledger(s.home)
        return JSONResponse(
            body,
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/journal")
    async def journal_browse(request: Request) -> Response:
        """Browse journal events — CLI ``krellbot journal`` parity subset.

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF.

        Query params (mutually exclusive selection; ``since`` wins when both
        are present, matching the CLI):

        - ``tail`` — positive integer; default 5 when ``since`` is absent.
        - ``since`` — ``24h`` or ``14d`` only.
        - ``mode`` — optional ``paper`` or ``live``; filter-then-tail so
          "last N live" returns the last N live-classified rows.

        Body is ``{"schema_version": "1", "records": [...]}`` with export
        secret fields (``key``, ``secret``, ``license``, ``balance``)
        dropped at any depth. Each record also carries projected
        ``mode`` and ``summary`` siblings for the Operations panel.
        Empty journal → ``records: []``. Invalid params → 400 with a
        typed ``code``. Served with ``Cache-Control: no-store``.
        """

        from krellbot import journal as kb_journal

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        raw_tail = request.query_params.get("tail")
        raw_since = request.query_params.get("since")
        raw_mode = request.query_params.get("mode")

        tail: int | None = None
        since: str | None = None
        mode: str | None = None

        if raw_since is not None and raw_since != "":
            since = raw_since
        if raw_mode is not None and raw_mode != "":
            mode = raw_mode
        if raw_tail is not None and raw_tail != "":
            try:
                tail = int(raw_tail)
            except (TypeError, ValueError):
                return JSONResponse(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "detail": f"invalid --tail: {raw_tail}",
                        "code": "invalid_tail",
                    },
                    status_code=400,
                )

        try:
            records = kb_journal.browse(s.home, tail=tail, since=since, mode=mode)
        except kb_journal.BrowseError as err:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "detail": err.message,
                    "code": err.code,
                },
                status_code=400,
            )

        body = {
            "schema_version": SCHEMA_VERSION,
            "records": records,
        }
        return JSONResponse(
            body,
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/portfolio/history")
    async def portfolio_history(request: Request) -> Response:
        """Return cross-deployment portfolio history (equity + deployments).

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF. The
        body is read-only evidence over ``<home>/journal/*.jsonl`` and
        ``<home>/run/paper-<venue>.json``:

        * ``equity`` — absolute portfolio equity series from venue fill
          replay (empty when starting cash or fills are absent)
        * ``deployments`` — one card per journalled / armed
          ``(venue, pack, pair)``, with pair-scoped ``pnl`` only when
          ``starting_cash`` is known from the armed config

        Never fabricates sample bots or flat zero equity. Does not
        change opt-in telemetry. Read-only: never arms or writes.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = project_portfolio_history(s.home)
        return JSONResponse(
            body,
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/agents/activity")
    async def agents_activity(request: Request) -> Response:
        """Return recent agent tool calls (Krell and MCP clients), newest first.

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``).
        Optional exact-match filters ``actor``, ``tool``, ``outcome`` and an
        integer ``since`` timestamp; bad values are 400 with a typed code.
        Rows carry only the contract fields: recorded params are never
        returned (``pair`` is read from ``params.pair`` when present).
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        q = request.query_params
        outcome = q.get("outcome") or None
        if outcome is not None and outcome not in agent_audit.OUTCOMES:
            return JSONResponse(
                {"code": "agent_activity_outcome_invalid", "detail": "outcome filter not recognised"},
                status_code=400,
            )
        since_ts: int | None = None
        since_raw = q.get("since")
        if since_raw:
            try:
                since_ts = int(since_raw)
            except ValueError:
                return JSONResponse(
                    {"code": "agent_activity_since_invalid", "detail": "since must be an integer timestamp"},
                    status_code=400,
                )

        records = agent_audit.read_agent_activity(
            s.home,
            actor=q.get("actor") or None,
            tool=q.get("tool") or None,
            outcome=outcome,
            since_ts=since_ts,
            limit=200,
        )
        rows = []
        for record in records:
            detail = record["detail"]
            params = detail.get("params")
            pair = params.get("pair") if isinstance(params, dict) else None
            venue = record.get("venue")
            rows.append(
                {
                    "ts": record["ts"],
                    "actor": detail.get("actor"),
                    "tool": detail.get("tool"),
                    "outcome": detail.get("outcome"),
                    "code": detail.get("code"),
                    "venue": venue if venue and venue != "-" else None,
                    "pair": pair if isinstance(pair, str) and pair else None,
                    "confirm_ticket": detail.get("confirm_ticket"),
                    "tokens": detail.get("tokens"),
                }
            )
        return JSONResponse({"rows": rows}, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/paper/armed")
    async def paper_armed(request: Request) -> Response:
        """Return the closed-shape list of every armed pack record.

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF
        required. The service guarantees one armed record per
        (venue, pair); unlike the single-record status projection this
        view returns one row per armed record so the workstation can
        show which packs are armed where. Each row carries only the
        closed field set — venue, pair, mode, pack_id,
        entries_paused — never cash, quantity, stop, cap, or the pack
        path.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        config = kb_config.load_config(s.home)
        records = [
            {
                "venue": record.venue,
                "pair": record.pair,
                "mode": record.mode,
                "pack_id": record.pack_id,
                "entries_paused": kb_config.entries_held(record),
            }
            for record in config.armed
        ]
        body = {
            "schema_version": SCHEMA_VERSION,
            "armed": bool(records),
            "records": records,
        }

        return JSONResponse(
            body,
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/paper/fleet")
    async def paper_fleet(request: Request) -> Response:
        """Return the armed fleet management list with last tick evidence.

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF. One
        row per armed config record joins the closed armed identity with
        journal last-tick time/result and a relative ``detail_href`` deep
        link into the Workstation status surface. Optional ``venue`` /
        ``pair`` query params filter the list; ``groups`` always keys
        rows by ``(venue, pair)`` so multi-pair fleets never collapse to
        ``armed[0]``. ``armed_count`` is the unfiltered armed total.
        Cash, quantity, stop, cap, and pack path never appear on the wire.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = _fleet_armed_view(
            s.home,
            venue=request.query_params.get("venue"),
            pair=request.query_params.get("pair"),
        )

        return JSONResponse(
            body,
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/paper/fleet/freshness")
    async def paper_fleet_freshness(request: Request) -> Response:
        """Return candle age and staleness for every armed feed.

        Same session gate as ``GET /api/v1/paper/fleet`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF. One row
        per armed ``(venue, pack_id, pair)`` with ``last_bar_ts_ms``,
        ``candle_age_s`` and ``candle_stale``; ``null`` where the current
        arm has no candle evidence. Read-only: nothing is fetched or written.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        return JSONResponse(
            _fleet_freshness_view(s.home),
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/rehearsal/status")
    async def rehearsal_status(request: Request) -> Response:
        """Return the synthetic rehearsal projection.

        Same gate as ``GET /api/v1/paper/status``: session cookie +
        loopback Origin + loopback Host, no CSRF. The body is the closed
        plain-JSON rehearsal projection — status, cursor, total bars,
        balances, position, resting stop, recent fills — with the
        explicit synthetic label, and never an internal service object.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = _rehearsal_wire(_rehearsal_service(s).status_dict())
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/packs")
    async def packs(request: Request) -> Response:
        """Return the closed-shape installed pack rows (installed pack library).

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF
        required. The body is exactly the row list from
        ``krellbot.application.packs.list_packs`` — one object per pack
        with the closed keys ``bucket``, ``pack_id``, ``version``,
        ``permissions``, ``rollback_ref``. The route is read-only: it
        mounts no install, catalog, entitlement, or rollback surface,
        and adds nothing to ``/api/v1/capabilities``.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = app_packs.list_packs(s.home)
        return JSONResponse(body, status_code=200)

    @app.get("/api/v1/packs/starters")
    async def packs_starters(request: Request) -> Response:
        """Return the shipped read-only starter pack catalogue.

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF
        required. The body is the catalogue document from
        ``krellbot.packs.catalogue`` — ``schema_version``,
        ``catalogue_version``, ``examples`` rows, and additive ``modes``
        rows for built-in trading-mode packs. Each row carries exactly
        ``id``, ``label``, ``description``, ``pack``. The examples and
        modes are ordinary valid DSL packs served from package
        resources. The route is read-only: it installs nothing,
        validates no user draft, arms nothing, starts nothing,
        overwrites nothing, and edits no state. A user turns a starter
        or mode into an owned editable copy by POSTing ``pack`` through
        the existing drafts route.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        from krellbot.packs import catalogue as starter_catalogue

        try:
            body = starter_catalogue.catalogue_document()
        except starter_catalogue.StarterCatalogueError as exc:
            return JSONResponse(
                {"detail": f"starter catalogue unavailable: {exc}"},
                status_code=500,
            )
        return JSONResponse(body, status_code=200)

    @app.get("/api/v1/packs/modes")
    async def packs_modes(request: Request) -> Response:
        """Return shipped built-in trading-mode packs (DCA / grid / rebalance).

        Same session gate as ``GET /api/v1/packs/starters`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF. Body is
        ``{"schema_version": "1", "modes": [{"id","label","description","pack"}, ...]}``
        for every mode JSON present under ``krellbot.packs.modes``. Read-only;
        Studio loads these as real graphs (not demo nodes).
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        from krellbot.packs import mode_packs

        try:
            body = mode_packs.modes_document()
        except mode_packs.ModePackError:
            # Do not interpolate exception text into the wire body (CodeQL:
            # information exposure through an exception).
            return JSONResponse(
                {"detail": "mode catalogue unavailable"},
                status_code=500,
            )
        return JSONResponse(body, status_code=200)

    @app.get("/api/v1/catalogue")
    async def catalogue_list(request: Request) -> Response:
        """Fetch and verify the signed Strategy Pack catalogue (free + paid).

        Same session gate as ``GET /api/v1/packs`` (``_gate_get``): session
        cookie + loopback Origin + loopback Host, no CSRF required. The
        route asks ``pack_library.refresh_catalogue`` to pull the signed
        envelope through the injectable ``catalogue_transport`` (production
        uses the HTTPS JSON transport in ``license_activation``), verify
        the ed25519 signature, and rewrite the local catalogue cache.
        Tampered or malformed envelopes are refused with 502 and never
        rewrite the cache. The body is the closed catalogue document:
        ``schema_version``, ``catalogue_version``, optional ``updated``,
        and ``packs`` rows each carrying ``id``, ``tier`` (``free`` or
        ``paid``), and optional label/description/version/permissions.
        """

        from krellbot.application import pack_library as pack_library_app

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        try:
            document = pack_library_app.refresh_catalogue(
                s.home,
                transport=s.catalogue_transport,
            )
        except OSError:
            return JSONResponse(
                {"detail": "catalogue unreachable"},
                status_code=502,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return JSONResponse(
                {"detail": "catalogue signature rejected"},
                status_code=502,
            )

        body = {
            "schema_version": SCHEMA_VERSION,
            "catalogue_version": document["catalogue_version"],
            "packs": document["packs"],
        }
        if isinstance(document.get("updated"), str):
            body["updated"] = document["updated"]
        # Opt-in funnel: catalogue view (no SKU; never strategy source).
        _emit_funnel(s, "view")
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/capabilities")
    async def capabilities(request: Request) -> Response:
        # The bootstrap token reaches the shell only through the
        # ``<meta name="krellbot-bootstrap">`` tag the static layer
        # injects into the served index.html. It is never part of this
        # (or any) JSON body.
        body = {
            "schema_version": SCHEMA_VERSION,
            "paper_commands": list(PAPER_COMMANDS),
            "research": True,
            "live_orders": False,
        }
        return JSONResponse(body, status_code=200)

    @app.get("/api/v1/version")
    async def version(request: Request) -> Response:
        """Surface engine version and release-channel honesty.

        Same loopback Host gate as ``GET /api/v1/capabilities`` (no
        session). Channel is prerelease while artifacts are unsigned;
        GA is refused until signing state matches.
        """

        from krellbot import release_channel

        return JSONResponse(release_channel.version_payload(), status_code=200)

    # ---- keys onboarding (M3.1 Kraken + M11.1 Coinbase) ----------------

    KEYS_VENUES = frozenset({"kraken", "coinbase"})

    def _keys_keyring():
        import keyring as _keyring

        return _keyring.get_keyring()

    def _keys_venue_or_404(venue: str) -> Response | None:
        if venue not in KEYS_VENUES:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return None

    @app.post("/api/v1/keys/{venue}")
    async def keys_probe_and_store(venue: str, request: Request) -> Response:
        """Probe then store a trade-only key for ``venue`` (Kraken or Coinbase).

        Same state-change gate as ``POST /api/v1/commands``
        (``_gate_state_change``): session cookie + CSRF + loopback Origin.
        Persistence is owned by ``keys_onboarding.probe_and_store``; trade-
        only enforcement is unchanged. The response never carries the raw
        key, secret, or venue error text.
        """

        from krellbot import keys_onboarding

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        missing = _keys_venue_or_404(venue)
        if missing is not None:
            return missing

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        key = payload.get("key")
        secret = payload.get("secret")
        if not isinstance(key, str) or not isinstance(secret, str):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        result = keys_onboarding.probe_and_store(
            venue,
            key,
            secret,
            keyring_backend=_keys_keyring(),
            probe=s.keys_transport,
            allow_injected_fake_backend=s.keys_allow_fake_backend,
        )
        ok = result.status == keys_onboarding.Status.STORED
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "venue": venue,
                "status": result.status.value,
                "ok": ok,
                "message": result.message,
                "effect": "changed" if ok else "refused",
                "backend_label": result.backend_label,
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/keys/{venue}")
    async def keys_status(venue: str, request: Request) -> Response:
        """Return connection status and balances for ``venue`` (Kraken or Coinbase).

        Same session gate as ``GET /api/v1/paper/status`` (``_gate_get``):
        session cookie + loopback Origin + loopback Host, no CSRF. Balances
        are read through an injectable venue transport (tests use a fake).
        """

        from krellbot import keys_onboarding

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        missing = _keys_venue_or_404(venue)
        if missing is not None:
            return missing

        snap = keys_onboarding.connection_status(
            venue,
            keyring_backend=_keys_keyring(),
            transport=s.keys_transport,
            allow_injected_fake_backend=s.keys_allow_fake_backend,
            include_balances=True,
        )
        balances = None
        if snap.balances is not None:
            balances = [{"asset": row.asset, "free": row.free} for row in snap.balances]
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "venue": snap.venue,
                "present": snap.present,
                "corrupt": snap.corrupt,
                "message": snap.message,
                "backend_label": snap.backend_label,
                "balances": balances,
                "balances_available": snap.balances_available,
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.delete("/api/v1/keys/{venue}")
    async def keys_remove(venue: str, request: Request) -> Response:
        """Remove the stored key pair for ``venue`` (Kraken or Coinbase).

        Same state-change gate as ``POST /api/v1/commands``
        (``_gate_state_change``): session cookie + CSRF + loopback Origin.
        """

        from krellbot import keys_onboarding

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        missing = _keys_venue_or_404(venue)
        if missing is not None:
            return missing

        result = keys_onboarding.remove_stored(
            venue,
            keyring_backend=_keys_keyring(),
            allow_injected_fake_backend=s.keys_allow_fake_backend,
        )
        ok = result.status == keys_onboarding.RemovalStatus.REMOVED
        absent = result.status == keys_onboarding.RemovalStatus.ABSENT
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "venue": venue,
                "status": result.status.value,
                "ok": ok or absent,
                "message": result.message,
                "effect": "changed" if ok else ("unchanged" if absent else "refused"),
                "backend_label": result.backend_label,
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/v1/keys/{venue}/export")
    async def keys_export(venue: str, request: Request) -> Response:
        """Refuse key export; honors operator user-policy before the hard deny.

        Exchange keys never leave the OS keyring over the loopback API.
        When user-policy denies ``key_export``, the typed policy code wins;
        otherwise the hard ``key_export_unsupported`` refuse still fires.
        """

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        missing = _keys_venue_or_404(venue)
        if missing is not None:
            return missing

        decision = user_policy_app.ensure_allowed(s.home, user_policy_app.ACTION_KEY_EXPORT)
        if not decision.ok:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "effect": "refused",
                    "code": decision.code,
                    "message": decision.message,
                    "action": user_policy_app.ACTION_KEY_EXPORT,
                    "venue": venue,
                },
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "effect": "refused",
                "code": "key_export_unsupported",
                "message": "workstation does not export exchange keys",
                "action": user_policy_app.ACTION_KEY_EXPORT,
                "venue": venue,
            },
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )

    # ---- AI connections (M38.12) ---------------------------------------
    #
    # The user's own model for Krell. Credentials go only to the OS keychain
    # (``krellbot-ai:<id>``) and are never returned, logged or echoed; GET
    # never reads the keychain. Ids in paths must be Krellbot-generated ids.
    # Store calls that touch the keychain (or wait on a store write) run in a
    # worker thread: a keychain unlock prompt must not freeze the loop.

    _AI_REFUSAL_STATUS = {
        "ai_connection_not_found": 404,
        "ai_connections_unreadable": 409,
        "ai_keychain_unavailable": 503,
        "ai_signin_not_found": 404,
        "ai_signin_listener_failed": 503,
        "ai_signin_local_only": 403,
    }

    def _ai_json(body: dict, status_code: int = 200) -> Response:
        return JSONResponse(
            {"schema_version": SCHEMA_VERSION, **body},
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    def _ai_refused(exc) -> Response:
        return _ai_json(
            {"ok": False, "effect": "refused", "code": exc.code, "detail": exc.message},
            _AI_REFUSAL_STATUS.get(exc.code, 400),
        )

    def _ai_invalid_body() -> Response:
        from krellbot.ai.connections import ConnectionRefused

        return _ai_refused(ConnectionRefused("ai_body_invalid"))

    def _ai_store(s: _AppState):
        from krellbot.ai.connections import ConnectionStore

        # The tests-only FakeKeyring opt-in is the one the exchange-key routes use.
        return ConnectionStore(s.home, allow_injected_fake_backend=s.keys_allow_fake_backend)

    def _ai_not_found() -> Response:
        return _ai_json(
            {"ok": False, "effect": "refused", "code": "ai_connection_not_found", "detail": "not found"},
            404,
        )

    def _ai_connection_view(conn, active_id: str | None) -> dict:
        from krellbot.ai.connections import provider_host
        from krellbot.ai.disclosure import egress_disclosure

        return {
            "id": conn.id,
            "preset": conn.preset,
            "label": conn.label,
            "format": conn.format,
            "base_url": conn.base_url,
            "host": provider_host(conn.base_url),
            "model": conn.model,
            "auth": conn.auth,
            "capability": conn.capability,
            "created_at": conn.created_at,
            "active": conn.id == active_id,
            "disclosure": egress_disclosure(conn),
        }

    async def _ai_probe_and_record(s: _AppState, store, conn) -> Response:
        """Probe ``conn`` off the event loop, record its capability, and answer."""

        from krellbot.ai.connections import ConnectionRefused, build_adapter, check_endpoint
        from krellbot.ai.http import UrllibTransport
        from krellbot.ai.probe import probe
        from krellbot.ai.providers import fetch_model_ids

        transport = s.ai_transport or UrllibTransport()
        try:
            check_endpoint(conn)  # before the keychain is touched
            credential = await asyncio.to_thread(store.credential_for, conn)
            adapter = build_adapter(conn, credential, transport)
        except ConnectionRefused as exc:
            return _ai_refused(exc)
        result = await asyncio.to_thread(
            probe,
            conn,
            adapter,
            list_models=lambda: fetch_model_ids(conn.format, conn.base_url, credential, transport),
        )
        try:
            updated = await asyncio.to_thread(store.set_capability, conn.id, result.capability)
            active = store.active()
        except ConnectionRefused as exc:
            return _ai_refused(exc)
        return _ai_json(
            {
                "ok": True,
                "effect": "changed",
                "connection": _ai_connection_view(updated, active.id if active else None),
                "probe": {
                    "capability": result.capability,
                    "models": result.models,
                    "message": result.message,
                    "error": result.error,
                },
            }
        )

    @app.get("/api/v1/ai/providers")
    async def ai_providers(request: Request) -> Response:
        """List the AI provider presets a connection can start from."""

        from krellbot.ai.providers import PRESETS

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        providers = [
            {
                "id": p.id,
                "label": p.label,
                "format": p.format,
                "base_url": p.base_url or None,
                "custom": p.custom,
                "signin": p.signin,
                "lists_models": p.lists_models,
                "credential": "optional" if p.custom else "required",
            }
            for p in PRESETS.values()
        ]
        return _ai_json({"ok": True, "effect": "unchanged", "providers": providers})

    @app.get("/api/v1/ai/connections")
    async def ai_connections(request: Request) -> Response:
        """List AI connections and the active one. Never returns or reads a credential."""

        from krellbot.ai.connections import ConnectionRefused

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        store = _ai_store(s)
        try:
            conns = store.list()
            active = store.active()
        except ConnectionRefused as exc:
            return _ai_refused(exc)
        active_id = active.id if active else None
        return _ai_json(
            {
                "ok": True,
                "effect": "unchanged",
                "active": active_id,
                "connections": [_ai_connection_view(c, active_id) for c in conns],
            }
        )

    @app.post("/api/v1/ai/connections")
    async def ai_connections_add(request: Request) -> Response:
        """Add a connection (key to the keychain, metadata to disk), then probe it.

        Body: ``preset``, ``model``, and optionally ``credential``,
        ``base_url`` (custom presets only) and ``label``. A custom endpoint
        with no key is stored as ``auth: "none"``.
        """

        from krellbot.ai.connections import ConnectionRefused

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return _ai_invalid_body()
        if not isinstance(payload, dict):
            return _ai_invalid_body()
        preset = payload.get("preset")
        model = payload.get("model")
        optional = {name: payload.get(name) for name in ("credential", "base_url", "label")}
        if (
            not isinstance(preset, str)
            or not isinstance(model, str)
            or any(v is not None and not isinstance(v, str) for v in optional.values())
        ):
            return _ai_invalid_body()

        store = _ai_store(s)
        try:
            conn = await asyncio.to_thread(store.add, preset, model=model, **optional)
        except ConnectionRefused as exc:
            return _ai_refused(exc)
        return await _ai_probe_and_record(s, store, conn)

    @app.delete("/api/v1/ai/connections/{conn_id}")
    async def ai_connections_remove(conn_id: str, request: Request) -> Response:
        """Remove a connection and delete its keychain entry."""

        from krellbot.ai.connections import ConnectionRefused, is_connection_id

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        if not is_connection_id(conn_id):
            return _ai_not_found()
        try:
            await asyncio.to_thread(_ai_store(s).remove, conn_id)
        except ConnectionRefused as exc:
            return _ai_refused(exc)
        return _ai_json({"ok": True, "effect": "changed", "removed": conn_id})

    @app.post("/api/v1/ai/connections/{conn_id}/activate")
    async def ai_connections_activate(conn_id: str, request: Request) -> Response:
        """Make this connection the one Krell uses."""

        from krellbot.ai.connections import ConnectionRefused, is_connection_id

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        if not is_connection_id(conn_id):
            return _ai_not_found()
        try:
            conn = await asyncio.to_thread(_ai_store(s).activate, conn_id)
        except ConnectionRefused as exc:
            return _ai_refused(exc)
        return _ai_json(
            {"ok": True, "effect": "changed", "active": conn.id, "connection": _ai_connection_view(conn, conn.id)}
        )

    @app.post("/api/v1/ai/connections/{conn_id}/probe")
    async def ai_connections_probe(conn_id: str, request: Request) -> Response:
        """Re-run the capability probe and record the result."""

        from krellbot.ai.connections import ConnectionRefused, is_connection_id

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        if not is_connection_id(conn_id):
            return _ai_not_found()
        store = _ai_store(s)
        try:
            conn = store.get(conn_id)
        except ConnectionRefused as exc:
            return _ai_refused(exc)
        return await _ai_probe_and_record(s, store, conn)

    # ---- Sign in with OpenRouter (M38.20) ------------------------------
    #
    # OAuth PKCE through a one-shot listener on 127.0.0.1 (a separate socket,
    # not a route here). The browser polls the status route; the code, the
    # verifier and the key never pass through these routes. Starting is
    # local-only: OpenRouter redirects to 127.0.0.1, which from a remote
    # (tailnet) browser is that device, not this one.

    def _ai_signins(s: _AppState):
        from krellbot.ai.oauth import SigninRegistry

        if s.ai_signins is None:
            s.ai_signins = SigninRegistry()
        return s.ai_signins

    def _ai_signin_view(flow_id: str, status: dict) -> dict:
        body: dict = {"flow_id": flow_id, "state": status["state"]}
        if "connection_id" in status:
            body["connection_id"] = status["connection_id"]
        if "code" in status:
            body["code"] = status["code"]
            body["detail"] = status["message"]
        return body

    @app.post("/api/v1/ai/signin/openrouter")
    async def ai_signin_openrouter(request: Request) -> Response:
        """Start a sign-in. Body: ``model``. Returns ``flow_id`` and the ``authorize_url`` to open."""

        from krellbot.ai.connections import ConnectionRefused
        from krellbot.ai.http import UrllibTransport
        from krellbot.ai.oauth import SigninRefused, start_openrouter_signin

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        # A server started with Tailscale access admits only tailnet sessions (see remote_scope_put);
        # the peer check is defence in depth for a loopback-only server.
        peer = request.client.host if request.client is not None else None
        if tailnet_access is not None or peer not in ("127.0.0.1", "::1"):
            return _ai_refused(SigninRefused("ai_signin_local_only"))
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return _ai_invalid_body()
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            return _ai_invalid_body()
        try:
            flow = await asyncio.to_thread(
                start_openrouter_signin,
                s.home,
                model=payload["model"],
                clock=lambda: _app_now(s),
                transport=s.ai_transport or UrllibTransport(),
                listener_factory=s.ai_signin_listener_factory,
                registry=_ai_signins(s),
                allow_injected_fake_backend=s.keys_allow_fake_backend,
            )
        except (ConnectionRefused, SigninRefused) as exc:
            return _ai_refused(exc)
        return _ai_json({"ok": True, "effect": "changed", "flow_id": flow.id, "authorize_url": flow.authorize_url})

    @app.get("/api/v1/ai/signin/{flow_id}")
    async def ai_signin_status(flow_id: str, request: Request) -> Response:
        """A sign-in's state: ``waiting``, ``connected`` (with ``connection_id``), ``failed`` or ``expired``."""

        from krellbot.ai.oauth import SigninRefused, signin_status

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        try:
            status = signin_status(flow_id, registry=_ai_signins(s))
        except SigninRefused as exc:
            return _ai_refused(exc)
        return _ai_json({"ok": True, "effect": "unchanged", **_ai_signin_view(flow_id, status)})

    @app.delete("/api/v1/ai/signin/{flow_id}")
    async def ai_signin_cancel(flow_id: str, request: Request) -> Response:
        """Cancel a sign-in still waiting for OpenRouter: its listener closes and it can never add a connection.

        A sign-in whose callback already arrived, or that finished, is left
        alone; the reply says ``cancelled: false`` with its current state.
        """

        from krellbot.ai.oauth import SigninRefused, cancel_signin

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        try:
            cancelled, status = cancel_signin(flow_id, registry=_ai_signins(s))
        except SigninRefused as exc:
            return _ai_refused(exc)
        return _ai_json(
            {
                "ok": True,
                "effect": "changed" if cancelled else "unchanged",
                "cancelled": cancelled,
                **_ai_signin_view(flow_id, status),
            }
        )

    # ---- operator user policy (M22.2) ---------------------------------

    @app.get("/api/v1/user-policy")
    async def get_user_policy(request: Request) -> Response:
        """Return the operator user-policy snapshot (defaults if missing)."""

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        policy = user_policy_app.load_policy(s.home)
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "effect": "unchanged",
                "policy": policy.to_dict(),
                "path": user_policy_app.POLICY_FILENAME,
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.put("/api/v1/user-policy")
    async def put_user_policy(request: Request) -> Response:
        """Replace the operator user-policy file; audits the change."""

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        body = payload.get("policy", payload)
        now = _app_now(s)
        result = user_policy_app.save_policy(s.home, body, clock=lambda: now)
        status = 200 if result.ok else 400
        out: dict = {
            "schema_version": SCHEMA_VERSION,
            "ok": result.ok,
            "code": result.code,
            "message": result.message,
            "effect": "changed"
            if result.code == user_policy_app.CODE_POLICY_UPDATED
            else ("unchanged" if result.ok else "refused"),
        }
        if result.policy is not None:
            out["policy"] = result.policy.to_dict()
        return JSONResponse(out, status_code=status, headers={"Cache-Control": "no-store"})

    # ---- strategy drafts (NS08a) --------------------------------------

    def _resolve_pack_field(payload: dict) -> tuple[dict | None, Response | None]:
        """Return ``(pack, None)`` or ``(None, bad_request)``."""

        if not isinstance(payload, dict):
            return None, JSONResponse({"detail": "invalid body"}, status_code=400)
        raw = payload.get("pack")
        if not isinstance(raw, dict):
            return None, JSONResponse({"detail": "invalid body"}, status_code=400)
        return raw, None

    @app.post("/api/v1/strategies/drafts")
    async def create_draft(request: Request) -> Response:
        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        pack, err = _resolve_pack_field(payload)
        if err is not None:
            return err

        try:
            summary = _draft_service(s).create(pack)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(summary, status_code=200)

    @app.put("/api/v1/strategies/drafts/{revision_id}")
    async def edit_draft(request: Request, revision_id: str) -> Response:
        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        pack, err = _resolve_pack_field(payload)
        if err is not None:
            return err

        try:
            summary = _draft_service(s).edit(revision_id, pack)
        except RevisionNotFound:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "draft_not_found",
                    "message": f"draft {revision_id} not found",
                },
                status_code=404,
            )
        except StrategyIdMismatch as exc:
            # F3 — pack strategy id differs from the parent's strategy
            # id. Catching this before the generic ``ValueError``
            # branch keeps the 400 mapping for the other errors.
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "strategy_id_mismatch",
                    "message": str(exc),
                },
                status_code=409,
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(summary, status_code=200)

    @app.post("/api/v1/strategies/drafts/{revision_id}/validate")
    async def validate_draft(request: Request, revision_id: str) -> Response:
        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        # Body is optional. Eat it so a trailing JSON parse error does
        # not 400 a valid request.
        try:
            await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            pass

        try:
            summary = _draft_service(s).validate(revision_id)
        except RevisionNotFound:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "draft_not_found",
                    "message": f"draft {revision_id} not found",
                },
                status_code=404,
            )
        return JSONResponse(summary, status_code=200)

    @app.get("/api/v1/strategies/drafts/{revision_id}")
    async def get_draft(request: Request, revision_id: str) -> Response:
        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        summary = _draft_service(s).get(revision_id)
        if summary is None:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "draft_not_found",
                    "message": f"draft {revision_id} not found",
                },
                status_code=404,
            )
        return JSONResponse(summary, status_code=200)

    @app.post("/api/v1/strategies/drafts/{revision_id}/editor")
    async def save_draft_editor(request: Request, revision_id: str) -> Response:
        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict) or not isinstance(payload.get("editor"), dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        try:
            summary = _draft_service(s).save_editor(revision_id, payload["editor"])
        except RevisionNotFound:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "draft_not_found",
                    "message": f"draft {revision_id} not found",
                },
                status_code=404,
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse(summary, status_code=200)

    @app.get("/api/v1/strategies/drafts")
    async def list_drafts(request: Request) -> Response:
        """List every owned revision the user has saved locally.

        Read-only walk of ``<home>/drafts`` behind the same
        session/loopback gate as ``GET
        /api/v1/strategies/drafts/{revision_id}``. Rows are one summary
        per revision (newest first); a revision whose stored bytes or
        meta fail to parse is skipped rather than failing the request.
        Imported packs that never became an owned revision live under
        ``<home>/packs`` and never appear here. Nothing is armed,
        rewritten, or state-changed by this route.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied
        rows = _draft_service(s).list_owned_summaries()
        body = {"schema_version": SCHEMA_VERSION, "drafts": rows}
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    # ---- research result download (lane E) ------------------------------

    @app.get("/api/v1/jobs/{job_id}/result/download")
    async def download_job_result(request: Request, job_id: str) -> Response:
        """Return the canonical receipt bytes for ``job_id`` as a download.

        The route uses ``_gate_get`` so a session is required; no CSRF
        since it is a read. The body is the byte-identical canonical
        UTF-8 JSON of ``legacy_receipt`` so a workstation ``Save As``
        yields the exact bytes the runner shipped. A missing or non-
        succeeded job is 404 with the same closed shape the inline
        result route uses.
        """

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        snap = s.jobs.get(job_id)
        if snap is None:
            return JSONResponse(
                {"code": JOB_ERROR_NOT_FOUND, "message": "job not found"},
                status_code=404,
            )
        if snap.state != "succeeded":
            return JSONResponse(
                {"code": JOB_ERROR_RESULT_UNAVAILABLE},
                status_code=404,
            )
        result = s.jobs.result_for(job_id)
        if result is None or "legacy_receipt" not in result:
            return JSONResponse(
                {"code": JOB_ERROR_RESULT_UNAVAILABLE},
                status_code=404,
            )
        receipt = result["legacy_receipt"]
        if not isinstance(receipt, dict):
            return JSONResponse(
                {"code": JOB_ERROR_RESULT_UNAVAILABLE},
                status_code=404,
            )
        # ``canonical_receipt_bytes`` is the single source of truth for
        # the wire shape; using it keeps the inline route and the
        # download byte-identical.
        from krellbot.api.jobs import canonical_receipt_bytes

        body = canonical_receipt_bytes(receipt)
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="research-result.json"',
                "Cache-Control": "no-store",
            },
        )

    # ---- community packs list / index / install (M17.2) -----------------

    @app.get("/api/v1/community/packs")
    async def community_packs_list(request: Request) -> Response:
        """List installed community packs under ``<home>/packs/community/``.

        Same session gate as ``GET /api/v1/packs`` (``_gate_get``): session
        cookie + loopback Origin + loopback Host, no CSRF required. Body is
        closed rows from ``community.installed_rows`` — ``id`` plus optional
        ``version`` / ``label`` / ``author`` / ``origin``. Empty home returns
        ``[]``. No network call.
        """

        from krellbot import community as kb_community

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        body = {
            "schema_version": SCHEMA_VERSION,
            "packs": kb_community.installed_rows(s.home),
        }
        return JSONResponse(body, status_code=200, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/community/packs/{pack_id}")
    async def community_pack_open(request: Request, pack_id: str) -> Response:
        """Open an installed community pack for Strategies validate-before-arm.

        Same session gate as ``GET /api/v1/community/packs``. Reads
        ``<home>/packs/community/<id>.json`` through ``community.read_installed``
        (basename-confined). Returns the pack object plus ``source: community``
        so the UI never pretends this is a signed catalogue SKU. Missing ids
        are 404; bad ids are 400.
        """
        import os.path

        from krellbot import community as kb_community

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        # CodeQL-recognized sanitizer at the API boundary (same as install):
        # basename strips directory components before any FS open.
        raw_pack_id = pack_id
        safe_id = os.path.basename(raw_pack_id)
        if not safe_id or safe_id != raw_pack_id or os.path.basename(safe_id) != safe_id or safe_id in {".", ".."}:
            return JSONResponse({"detail": "pack_id is required"}, status_code=400)

        try:
            pack = kb_community.read_installed(s.home, safe_id)
        except kb_community.InstallPayloadError:
            return JSONResponse({"detail": "pack_id is required"}, status_code=400)
        except kb_community.IndexError_:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "code": "pack_not_found",
                    "message": "community pack not installed",
                },
                status_code=404,
            )

        # Closed relative path from sanitized id only — never echo a
        # request-derived filesystem string.
        rel_path = f"packs/community/{os.path.basename(safe_id)}.json"
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "source": "community",
                "pack": pack,
                "path": rel_path,
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/community/index")
    async def community_index(request: Request) -> Response:
        """Browse the allow-listed upstream community index via injectable transport.

        Same session gate as ``GET /api/v1/packs`` (``_gate_get``). Fetches
        ``community.INDEX_URL`` through ``community_transport`` (production
        uses ``UrllibBytesTransport``). Returns closed rows with ``id``,
        ``url``, and optional ``author`` / ``label``. Malformed indexes
        return 502; transport failures return 502. Browse does not install.
        """

        from krellbot import community as kb_community

        s = _state(request.app)
        denied = _gate_get(request, s)
        if denied is not None:
            return denied

        transport = kb_community.resolve_transport(s.community_transport)
        try:
            packs = kb_community.fetch_index(transport=transport)
        except kb_community.IndexError_:
            return JSONResponse(
                {"detail": "community index rejected"},
                status_code=502,
            )
        except (OSError, urllib.error.URLError):
            return JSONResponse(
                {"detail": "community index unreachable"},
                status_code=502,
            )

        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "index_url": kb_community.INDEX_URL,
                "packs": packs,
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/v1/community/install")
    async def community_install(request: Request) -> Response:
        """Install a community pack by id from the allow-listed index.

        Same state-change gate as other POSTs (``_gate_state_change``):
        session cookie + CSRF + loopback Origin. Body is
        ``{"pack_id": "<id>"}``. Uses injectable ``community_transport``.
        Foreign URLs, missing ids, oversized/invalid payloads, and
        transport failures map to closed refusal codes — never echoes
        exception text that may contain paths.
        """

        from krellbot import community as kb_community

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        import os.path

        raw_pack_id = payload.get("pack_id")
        if not isinstance(raw_pack_id, str) or not raw_pack_id:
            nested = payload.get("payload")
            if isinstance(nested, dict):
                raw_pack_id = nested.get("pack_id")
        if not isinstance(raw_pack_id, str) or not raw_pack_id:
            return JSONResponse({"detail": "pack_id is required"}, status_code=400)

        # CodeQL-recognized sanitizer at the API boundary: basename strips
        # directory components before any install / path join.
        pack_id = os.path.basename(raw_pack_id)
        if not pack_id or pack_id != raw_pack_id or os.path.basename(pack_id) != pack_id or pack_id in {".", ".."}:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "code": "payload_refused",
                    "message": "community pack payload refused",
                },
                status_code=400,
            )

        transport = kb_community.resolve_transport(s.community_transport)
        try:
            path = kb_community.install(pack_id, transport=transport, home=s.home)
        except kb_community.ForeignUrlError:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "code": "foreign_url",
                    "message": "community pack url is not allow-listed",
                },
                status_code=403,
            )
        except kb_community.IndexError_ as exc:
            missing = "not found" in str(exc)
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "code": "pack_not_found" if missing else "index_rejected",
                    "message": ("community pack not found in index" if missing else "community index rejected"),
                },
                status_code=404 if missing else 502,
            )
        except kb_community.InstallPayloadError:
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "code": "payload_refused",
                    "message": "community pack payload refused",
                },
                status_code=400,
            )
        except (OSError, urllib.error.URLError):
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "code": "unreachable",
                    "message": "community pack unreachable",
                },
                status_code=502,
            )

        rows = kb_community.installed_rows(s.home)
        row = next((r for r in rows if r.get("id") == pack_id), {"id": pack_id})
        # Closed relative path from sanitized id only — never echo a
        # request-derived filesystem string.
        rel_path = f"packs/community/{os.path.basename(pack_id)}.json"
        if path.name != os.path.basename(f"{pack_id}.json"):
            rel_path = path.name
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "pack": row,
                "path": rel_path,
            },
            status_code=200,
        )

    # ---- pack catalogue one-click install (M5.2) ------------------------

    @app.post("/api/v1/packs/install")
    async def packs_install(request: Request) -> Response:
        """One-click install a free signed-catalogue pack into the library.

        Same state-change gate as other POSTs (``_gate_state_change``):
        session cookie + CSRF + loopback Origin. Body is
        ``{"pack_id": "<id>"}`` (optional ``schema_version`` /
        ``command`` wrappers are ignored). Free catalogue entries write
        a ``download_pending`` marker so the library can show
        bucket+version; paid entries return 403 with
        ``activation_required``. Unknown ids return 404. No network
        call — the verified catalogue cache is the only source.
        Install does not claim rollback: ``POST /api/v1/packs/rollback``
        is intentionally unmounted until a prior-revision store exists.
        """

        from krellbot.application import pack_library as pack_library_app

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        pack_id = payload.get("pack_id")
        if not isinstance(pack_id, str) or not pack_id:
            nested = payload.get("payload")
            if isinstance(nested, dict):
                pack_id = nested.get("pack_id")
        if not isinstance(pack_id, str) or not pack_id:
            return JSONResponse({"detail": "pack_id is required"}, status_code=400)

        try:
            row = pack_library_app.install_from_catalogue(s.home, pack_id)
        except pack_library_app.InstallRefused as exc:
            # Closed safe messages only — never echo exception text
            # (paths / pack ids from the refusal) to the wire.
            status = 404 if exc.code == "pack_not_found" else 403
            safe = {
                "pack_not_found": "catalogue entry not found",
                "activation_required": "activation required for paid packs",
                "invalid_request": "pack_id is required",
            }.get(exc.code, "install refused")
            return JSONResponse(
                {
                    "schema_version": SCHEMA_VERSION,
                    "ok": False,
                    "code": exc.code,
                    "message": safe,
                },
                status_code=status,
            )

        _emit_funnel(s, "install", sku=pack_id)
        return JSONResponse(
            {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "pack": row,
            },
            status_code=200,
        )

    # ---- live checkout seam (M21.1; fake processor in CI) ---------------

    @app.post("/api/v1/checkout/start")
    async def checkout_start(request: Request) -> Response:
        """Start paid checkout for a catalogue SKU via injectable transport.

        Same state-change gate as other POSTs (session + CSRF + loopback
        Origin). Body is ``{"sku": "<catalogue id>"}``. Tests inject
        ``checkout_transport`` (typically ``FakePaymentTransport``); the
        default production transport never embeds processor keys. On
        ``paid``, entitlements refresh; on failure the license cache and
        trading/export paths are left untouched.
        """

        from krellbot.application import checkout as checkout_app

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        sku = payload.get("sku")
        if not isinstance(sku, str) or not sku:
            nested = payload.get("payload")
            if isinstance(nested, dict):
                sku = nested.get("sku")
        if not isinstance(sku, str) or not sku:
            return JSONResponse({"detail": "sku is required"}, status_code=400)

        _emit_funnel(s, "checkout_start", sku=sku)
        outcome = checkout_app.start_checkout(
            s.home,
            sku,
            transport=s.checkout_transport,
            now=int(_app_now(s)),
        )
        funnel_sku = outcome.sku or sku
        if outcome.status == "paid":
            _emit_funnel(s, "checkout_success", sku=funnel_sku)
        else:
            _emit_funnel(s, "checkout_fail", sku=funnel_sku)
        body = {
            "schema_version": SCHEMA_VERSION,
            "status": outcome.status,
            "message": outcome.message,
            "sku": outcome.sku,
            "entitlements": list(outcome.entitlements),
            "session_id": outcome.session_id,
        }
        return JSONResponse(body, status_code=200)

    @app.post("/api/v1/checkout/refund")
    async def checkout_refund(request: Request) -> Response:
        """Request a pack SKU refund via injectable transport; revoke entitlement.

        Same state-change gate as other POSTs (session + CSRF + loopback
        Origin). Body is ``{"sku": "<catalogue id>"}``. On ``refunded``,
        the local SKU entitlement is disabled and a journal audit row is
        written. On transport failure the license cache and trading/export
        paths stay untouched. Never embeds processor keys.
        """

        from krellbot.application import checkout as checkout_app

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse({"detail": "invalid body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "invalid body"}, status_code=400)

        sku = payload.get("sku")
        if not isinstance(sku, str) or not sku:
            nested = payload.get("payload")
            if isinstance(nested, dict):
                sku = nested.get("sku")
        if not isinstance(sku, str) or not sku:
            return JSONResponse({"detail": "sku is required"}, status_code=400)

        outcome = checkout_app.request_refund(
            s.home,
            sku,
            transport=s.checkout_transport,
            now=int(_app_now(s)),
        )
        body = {
            "schema_version": SCHEMA_VERSION,
            "status": outcome.status,
            "message": outcome.message,
            "sku": outcome.sku,
            "entitlements": list(outcome.entitlements),
            "refund_id": outcome.refund_id,
        }
        return JSONResponse(body, status_code=200)

    # ---- activation redeem (lane E) -------------------------------------

    @app.post("/api/v1/activation/redeem")
    async def redeem_activation(request: Request) -> Response:
        """Delegate to ``krellbot.application.license_activation.redeem``; never echoes a key.

        The route is a state change (it can rewrite the license cache),
        so it runs the same session-cookie + CSRF + loopback-origin
        gate as every other POST before any body byte is read. The
        redeem pass then runs against ``s.home`` and returns the closed
        ``ActivateOutcome`` as JSON. Any ``OSError`` / decode failure
        surfaces as the matching safe message; the key never appears
        in the URL, body, header, or response.
        """

        s = _state(request.app)
        denied = _gate_state_change(request, s)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        key = payload.get("key", "")
        if not isinstance(key, str):
            key = ""

        from krellbot.application.license_activation import redeem

        outcome = redeem(key, home=s.home, now=int(_now_seconds()))
        body = {
            "schema_version": SCHEMA_VERSION,
            "status": outcome.status,
            "message": outcome.message,
            "catalog_downloaded": outcome.catalog_downloaded,
        }
        # Disclosed pack-purchase referral attribution (M23.1). Local
        # receipt + injectable server seam; never includes the key.
        if outcome.status in {"paid", "grace"}:
            from krellbot import referral as kb_referral

            kb_referral.attribute(
                s.home,
                kind="pack_purchase",
                transport=s.referral_transport,
                now=_app_now(s),
            )
        return JSONResponse(body, status_code=200)

    return app
