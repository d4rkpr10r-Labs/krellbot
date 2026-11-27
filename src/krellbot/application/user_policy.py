"""Operator-owned user policy file under ``$KRELLBOT_HOME``.

The policy is a closed JSON schema the operator controls. MCP and the
workstation consult it before paper-arm, live, or key-export actions.
Hard product boundaries still apply: MCP never live-arms or reads keys,
and ``PAPER_ONLY_RELEASE`` still blocks live regardless of this file.

Missing file → safe defaults (allow paper-arm, allow live at the policy
layer, deny key export). Live remains blocked by ``PAPER_ONLY_RELEASE``
and the MCP hard refuse until the operator writes an explicit policy.
Malformed file → fail closed (deny all three). Writes go through
:func:`save_policy` and append a journal audit record. Enforce seams
call :func:`ensure_allowed`, which journals a refuse row with a reason
code when denied; :func:`check` stays silent for probes.

Schema "2" adds an ``agents`` object: the hard limits on what agents (Krell
and external MCP clients) may paper-arm or change (:class:`AgentLimits`).
Agents can only be more restricted than the human. A v1 file, or no file,
gets :data:`DEFAULT_AGENT_LIMITS`. A v2 file must carry exactly the
:data:`AGENT_LIMIT_KEYS` with their exact JSON types (Decimals as plain
decimal strings, ``None`` as null); anything else is malformed, which
denies all three actions and locks agents out (:data:`LOCKED_AGENT_LIMITS`).
:func:`save_policy` takes v1 or v2 payloads and always writes v2. The agent
tool core enforces the limits (:mod:`krellbot.agent.limits`).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from krellbot.paths import atomic_write

SCHEMA_VERSION = "2"
POLICY_FILENAME = "user-policy.json"

_V1_VERSIONS: tuple[object, ...] = (1, "1")
_V2_VERSIONS: tuple[object, ...] = (2, "2")
_V2_KEYS: frozenset[str] = frozenset({"schema_version", "allow_paper_arm", "allow_live", "allow_key_export", "agents"})

ACTION_PAPER_ARM = "paper_arm"
ACTION_LIVE = "live"
ACTION_KEY_EXPORT = "key_export"

ACTIONS: tuple[str, ...] = (ACTION_PAPER_ARM, ACTION_LIVE, ACTION_KEY_EXPORT)

CODE_OK = "ok"
CODE_POLICY_DENIED = "user_policy_denied"
CODE_POLICY_INVALID = "user_policy_invalid"
CODE_POLICY_UPDATED = "user_policy_updated"
CODE_BAD_INPUT = "user_policy_bad_input"

MESSAGE_DENY_PAPER_ARM = "user policy denies paper-arm"
MESSAGE_DENY_LIVE = "user policy denies live"
MESSAGE_DENY_KEY_EXPORT = "user policy denies key export"
MESSAGE_INVALID = "user policy file is invalid; fail closed"
MESSAGE_UPDATED = "user policy updated"

_IS_WINDOWS = sys.platform == "win32"

_DENY_MESSAGES: dict[str, str] = {
    ACTION_PAPER_ARM: MESSAGE_DENY_PAPER_ARM,
    ACTION_LIVE: MESSAGE_DENY_LIVE,
    ACTION_KEY_EXPORT: MESSAGE_DENY_KEY_EXPORT,
}


@dataclass(frozen=True)
class AgentLimits:
    """Hard limits the operator sets on every agent (schema "2" ``agents``).

    ``allowed_venues`` / ``allowed_pairs``: ``None`` allows every supported
    one, a tuple allows only those (an empty tuple allows none).
    ``max_paper_balance`` and ``max_risk_cap_pct`` bound an agent paper arm's
    starting cash and its pack's ``risk.max_account_pct``.
    ``require_daily_loss_limit`` makes agent arms carry a daily loss limit
    (M28.31). ``max_agent_arms`` bounds the agent arms running at once. The
    ``*_allow_pause_resume`` / ``*_allow_disarm`` flags gate the paper change
    tools per actor kind. The token budgets bound Krell's model usage.
    """

    allowed_venues: tuple[str, ...] | None
    allowed_pairs: tuple[str, ...] | None
    max_paper_balance: Decimal
    max_risk_cap_pct: Decimal
    require_daily_loss_limit: bool
    max_agent_arms: int
    krell_allow_pause_resume: bool
    krell_allow_disarm: bool
    mcp_allow_pause_resume: bool
    mcp_allow_disarm: bool
    token_budget_per_conversation: int
    token_budget_per_month: int

    def to_dict(self) -> dict[str, object]:
        """The on-disk form: Decimals as strings, ``None`` as null."""
        return {
            "allowed_venues": None if self.allowed_venues is None else list(self.allowed_venues),
            "allowed_pairs": None if self.allowed_pairs is None else list(self.allowed_pairs),
            "max_paper_balance": str(self.max_paper_balance),
            "max_risk_cap_pct": str(self.max_risk_cap_pct),
            "require_daily_loss_limit": self.require_daily_loss_limit,
            "max_agent_arms": self.max_agent_arms,
            "krell_allow_pause_resume": self.krell_allow_pause_resume,
            "krell_allow_disarm": self.krell_allow_disarm,
            "mcp_allow_pause_resume": self.mcp_allow_pause_resume,
            "mcp_allow_disarm": self.mcp_allow_disarm,
            "token_budget_per_conversation": self.token_budget_per_conversation,
            "token_budget_per_month": self.token_budget_per_month,
        }


AGENT_LIMIT_KEYS: tuple[str, ...] = tuple(field.name for field in dataclasses.fields(AgentLimits))

# The spec's defaults (M38 Global Constraints): every supported venue and
# pair, a 10000 paper balance, a 2% risk cap, a daily loss limit required,
# three agent arms; Krell may pause, resume and disarm (with confirm),
# external MCP agents may not.
DEFAULT_AGENT_LIMITS = AgentLimits(
    allowed_venues=None,
    allowed_pairs=None,
    max_paper_balance=Decimal(10000),
    max_risk_cap_pct=Decimal(2),
    require_daily_loss_limit=True,
    max_agent_arms=3,
    krell_allow_pause_resume=True,
    krell_allow_disarm=True,
    mcp_allow_pause_resume=False,
    mcp_allow_disarm=False,
    token_budget_per_conversation=250_000,
    token_budget_per_month=2_500_000,
)

# Agents locked out: what a malformed policy file gives.
LOCKED_AGENT_LIMITS = AgentLimits(
    allowed_venues=(),
    allowed_pairs=(),
    max_paper_balance=Decimal(0),
    max_risk_cap_pct=Decimal(0),
    require_daily_loss_limit=True,
    max_agent_arms=0,
    krell_allow_pause_resume=False,
    krell_allow_disarm=False,
    mcp_allow_pause_resume=False,
    mcp_allow_disarm=False,
    token_budget_per_conversation=0,
    token_budget_per_month=0,
)


@dataclass(frozen=True)
class UserPolicy:
    """Closed operator policy snapshot."""

    schema_version: str
    allow_paper_arm: bool
    allow_live: bool
    allow_key_export: bool
    agents: AgentLimits = DEFAULT_AGENT_LIMITS

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "allow_paper_arm": self.allow_paper_arm,
            "allow_live": self.allow_live,
            "allow_key_export": self.allow_key_export,
            "agents": self.agents.to_dict(),
        }

    def allows(self, action: str) -> bool:
        if action == ACTION_PAPER_ARM:
            return self.allow_paper_arm
        if action == ACTION_LIVE:
            return self.allow_live
        if action == ACTION_KEY_EXPORT:
            return self.allow_key_export
        return False


# Safe defaults when the operator has not written a file yet.
# ``allow_live`` defaults True at this layer so release-policy / MCP hard
# gates remain the sole live blockers until the operator writes a file;
# a written ``allow_live: false`` is still enforced.
DEFAULT_POLICY = UserPolicy(
    schema_version=SCHEMA_VERSION,
    allow_paper_arm=True,
    allow_live=True,
    allow_key_export=False,
)

# Fail-closed snapshot when the on-disk file is present but unusable.
_INVALID_POLICY = UserPolicy(
    schema_version=SCHEMA_VERSION,
    allow_paper_arm=False,
    allow_live=False,
    allow_key_export=False,
    agents=LOCKED_AGENT_LIMITS,
)


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of a single policy check or save."""

    code: str
    ok: bool
    message: str
    action: str | None = None
    policy: UserPolicy | None = None

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "code": self.code,
            "ok": self.ok,
            "message": self.message,
            "effect": "refused" if not self.ok else "unchanged",
        }
        if self.action is not None:
            body["action"] = self.action
        if self.policy is not None:
            body["policy"] = self.policy.to_dict()
        return body


def policy_path(home: Path) -> Path:
    """Return the path to the operator-owned user policy JSON."""
    return Path(home) / POLICY_FILENAME


def _parse_bool(raw: object) -> bool | None:
    if type(raw) is bool:
        return raw
    return None


class _Malformed(ValueError):
    """A v2 ``agents`` value that does not have its exact type."""


# A plain decimal: digits and an optional fraction; no sign, exponent or spaces.
_PLAIN_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")


def _names(raw: object) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise _Malformed
    return tuple(raw)


def _decimal(raw: object, *, at_most: Decimal | None = None) -> Decimal:
    if not isinstance(raw, str) or not _PLAIN_DECIMAL.fullmatch(raw):
        raise _Malformed
    value = Decimal(raw)
    if at_most is not None and value > at_most:
        raise _Malformed
    return value


def _flag(raw: object) -> bool:
    if type(raw) is not bool:
        raise _Malformed
    return raw


def _count(raw: object) -> int:
    if type(raw) is not int or raw < 0:
        raise _Malformed
    return raw


def _parse_agents(raw: object) -> AgentLimits | None:
    """Parse a v2 ``agents`` object: exactly :data:`AGENT_LIMIT_KEYS`, exact types."""
    if not isinstance(raw, dict) or set(raw) != set(AGENT_LIMIT_KEYS):
        return None
    try:
        return AgentLimits(
            allowed_venues=_names(raw["allowed_venues"]),
            allowed_pairs=_names(raw["allowed_pairs"]),
            max_paper_balance=_decimal(raw["max_paper_balance"]),
            max_risk_cap_pct=_decimal(raw["max_risk_cap_pct"], at_most=Decimal(100)),
            require_daily_loss_limit=_flag(raw["require_daily_loss_limit"]),
            max_agent_arms=_count(raw["max_agent_arms"]),
            krell_allow_pause_resume=_flag(raw["krell_allow_pause_resume"]),
            krell_allow_disarm=_flag(raw["krell_allow_disarm"]),
            mcp_allow_pause_resume=_flag(raw["mcp_allow_pause_resume"]),
            mcp_allow_disarm=_flag(raw["mcp_allow_disarm"]),
            token_budget_per_conversation=_count(raw["token_budget_per_conversation"]),
            token_budget_per_month=_count(raw["token_budget_per_month"]),
        )
    except _Malformed:
        return None


def _parse_payload(data: object, *, v1_agents: AgentLimits = DEFAULT_AGENT_LIMITS) -> UserPolicy | None:
    """Parse a v1 or v2 payload; ``None`` when malformed.

    A v1 payload has no ``agents`` object and gets ``v1_agents``. A v2 payload
    has exactly the v2 top-level keys and a well-formed ``agents`` object.
    """
    if not isinstance(data, dict):
        return None
    version = data.get("schema_version")
    agents: AgentLimits | None
    if version in _V1_VERSIONS:
        if "agents" in data:
            return None
        agents = v1_agents
    elif version in _V2_VERSIONS:
        if set(data) != _V2_KEYS:
            return None
        agents = _parse_agents(data["agents"])
    else:
        return None
    paper = _parse_bool(data.get("allow_paper_arm"))
    live = _parse_bool(data.get("allow_live"))
    keys = _parse_bool(data.get("allow_key_export"))
    if paper is None or live is None or keys is None or agents is None:
        return None
    return UserPolicy(
        schema_version=SCHEMA_VERSION,
        allow_paper_arm=paper,
        allow_live=live,
        allow_key_export=keys,
        agents=agents,
    )


@dataclass(frozen=True)
class _LoadResult:
    policy: UserPolicy
    invalid: bool


def _load(home: Path) -> _LoadResult:
    path = policy_path(home)
    if not path.exists():
        return _LoadResult(DEFAULT_POLICY, False)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _LoadResult(_INVALID_POLICY, True)
    parsed = _parse_payload(raw)
    if parsed is None:
        return _LoadResult(_INVALID_POLICY, True)
    return _LoadResult(parsed, False)


def load_policy(home: Path) -> UserPolicy:
    """Load the operator policy, or return defaults / fail-closed.

    Missing file → :data:`DEFAULT_POLICY`. Unreadable or malformed file →
    deny all three actions (fail closed).
    """
    return _load(home).policy


def check(home: Path, action: str) -> PolicyDecision:
    """Return whether ``action`` is allowed under the operator policy."""
    if action not in ACTIONS:
        return PolicyDecision(
            CODE_BAD_INPUT,
            False,
            f"unknown policy action: {action}",
            action=action,
        )
    loaded = _load(home)
    if loaded.invalid:
        return PolicyDecision(
            CODE_POLICY_INVALID,
            False,
            MESSAGE_INVALID,
            action=action,
            policy=loaded.policy,
        )
    if loaded.policy.allows(action):
        return PolicyDecision(
            CODE_OK,
            True,
            "allowed",
            action=action,
            policy=loaded.policy,
        )
    return PolicyDecision(
        CODE_POLICY_DENIED,
        False,
        _DENY_MESSAGES[action],
        action=action,
        policy=loaded.policy,
    )


def ensure_allowed(
    home: Path,
    action: str,
    *,
    clock: Any | None = None,
) -> PolicyDecision:
    """Enforce ``action`` and journal a refuse row when denied.

    Call this at seams that actually attempt the action (paper-arm, live
    arm, key export). Probes and agent ``policy_check`` should use
    :func:`check` so they do not flood the journal.
    """
    decision = check(home, action)
    if not decision.ok:
        now = int((clock or time.time)())
        _append_policy_refuse(
            home,
            ts=now,
            action=action,
            code=decision.code,
            message=decision.message,
        )
    return decision


def save_policy(
    home: Path,
    policy: UserPolicy | dict[str, Any],
    *,
    clock: Any | None = None,
) -> PolicyDecision:
    """Persist the operator policy (always as schema "2") and audit the change.

    ``policy`` is a :class:`UserPolicy` or a v1 or v2 payload dict. A v1
    payload keeps the agent limits already on disk (the defaults when the
    file is missing or malformed), because a v1 writer knows nothing about
    them. No-op writes (byte-identical payload) still return ok but do not
    append a second audit row.
    """
    home = Path(home)
    current = _load(home)
    if isinstance(policy, UserPolicy):
        parsed = dataclasses.replace(policy, schema_version=SCHEMA_VERSION)
    else:
        v1_agents = DEFAULT_AGENT_LIMITS if current.invalid else current.policy.agents
        parsed = _parse_payload(policy, v1_agents=v1_agents)
        if parsed is None:
            return PolicyDecision(CODE_BAD_INPUT, False, "invalid user policy payload")

    # Policy cannot loosen hard product invariants via this API — callers
    # may set allow_live / allow_key_export True for a future release, but
    # MCP / PAPER_ONLY_RELEASE still refuse those surfaces today.
    before = current.policy
    # When the file was missing, before is DEFAULT; still audit the write.
    path = policy_path(home)
    home.mkdir(parents=True, exist_ok=True)
    payload = parsed.to_dict()
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    previous_bytes: bytes | None
    if path.exists():
        try:
            previous_bytes = path.read_bytes()
        except OSError:
            previous_bytes = None
    else:
        previous_bytes = None

    atomic_write(path, encoded, mode=0o600)

    changed = previous_bytes != encoded
    if changed:
        now = int((clock or time.time)())
        _append_policy_audit(
            home,
            ts=now,
            before=before.to_dict(),
            after=parsed.to_dict(),
        )

    return PolicyDecision(
        CODE_POLICY_UPDATED if changed else CODE_OK,
        True,
        MESSAGE_UPDATED if changed else "user policy unchanged",
        policy=parsed,
    )


def _append_user_policy_record(home: Path, *, ts: int, detail: dict[str, object]) -> None:
    """Append a ``kind=user_policy`` journal row under ``home/journal``.

    Uses an explicit home (not process ``KRELLBOT_HOME``) so tests and
    multi-home callers audit the correct tree. Failures are swallowed so
    a read-only journal cannot block a successful policy write or refuse.
    """
    record = {
        "ts": int(ts),
        "kind": "user_policy",
        "venue": "-",
        "pack": "-",
        "bar_ts": 0,
        "detail": detail,
    }
    journal_dir = Path(home) / "journal"
    try:
        journal_dir.mkdir(parents=True, exist_ok=True)
        if not _IS_WINDOWS:
            os.chmod(journal_dir, 0o700)
        import datetime

        utc_ts = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)
        file_path = journal_dir / f"{utc_ts:%Y-%m}.jsonl"
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        fd = os.open(str(file_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8", newline="\n", closefd=False) as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        if not _IS_WINDOWS:
            os.chmod(file_path, 0o600)
    except OSError:
        return


def _append_policy_audit(
    home: Path,
    *,
    ts: int,
    before: dict[str, object],
    after: dict[str, object],
) -> None:
    """Append a policy-change audit row under ``home/journal``."""
    _append_user_policy_record(
        home,
        ts=ts,
        detail={
            "event": "changed",
            "before": before,
            "after": after,
        },
    )


def _append_policy_refuse(
    home: Path,
    *,
    ts: int,
    action: str,
    code: str,
    message: str,
    actor: str | None = None,
) -> None:
    """Append a policy-refuse receipt under ``home/journal``.

    ``actor`` (an agent's actor label) is recorded when an agent limit refused.
    """
    detail: dict[str, object] = {
        "event": "refused",
        "reason": code,
        "code": code,
        "action": action,
        "message": message,
    }
    if actor is not None:
        detail["actor"] = actor
    _append_user_policy_record(home, ts=ts, detail=detail)


__all__ = [
    "ACTIONS",
    "ACTION_KEY_EXPORT",
    "ACTION_LIVE",
    "ACTION_PAPER_ARM",
    "AGENT_LIMIT_KEYS",
    "CODE_BAD_INPUT",
    "CODE_OK",
    "CODE_POLICY_DENIED",
    "CODE_POLICY_INVALID",
    "CODE_POLICY_UPDATED",
    "DEFAULT_AGENT_LIMITS",
    "DEFAULT_POLICY",
    "LOCKED_AGENT_LIMITS",
    "MESSAGE_DENY_KEY_EXPORT",
    "MESSAGE_DENY_LIVE",
    "MESSAGE_DENY_PAPER_ARM",
    "MESSAGE_INVALID",
    "MESSAGE_UPDATED",
    "POLICY_FILENAME",
    "SCHEMA_VERSION",
    "AgentLimits",
    "PolicyDecision",
    "UserPolicy",
    "check",
    "ensure_allowed",
    "load_policy",
    "policy_path",
    "save_policy",
]
