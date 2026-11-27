"""Agent audit trail: one ``kind=agent_action`` journal record per tool call.

Records land in ``<home>/journal/YYYY-MM.jsonl`` with the same append and
mode rules as the user-policy receipts (``user_policy._append_policy_refuse``):
an explicit home (not the process ``KRELLBOT_HOME``), journal directory 0o700,
file 0o600, one fsynced line per record, integer-second ``ts``, and a failed
write never blocks or flips the tool call it records.

Params are copied for the record, never stored raw: values of secret-shaped
names are replaced with ``***`` at any depth, strings longer than 200
characters are truncated with a ``…(+N)`` suffix, and nested objects are
stored as one truncated compact-JSON string. The whole record then goes
through :func:`krellbot.sanitize.redact`.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Literal

from krellbot import journal, sanitize
from krellbot.agent.actors import Actor

AGENT_ACTION_KIND = "agent_action"

Outcome = Literal["ok", "refused", "needs_confirm", "error"]
OUTCOMES: tuple[str, ...] = ("ok", "refused", "needs_confirm", "error")

# Lowercased substrings that make a param name secret-shaped. The core refuses
# such params before dispatch; the audit omits their values wherever they sit.
SECRET_PARAM_MARKERS: tuple[str, ...] = (
    "key",
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
)
OMITTED_VALUE = "***"
MAX_VALUE_CHARS = 200

_IS_WINDOWS = sys.platform == "win32"


def is_secret_param_name(name: object) -> bool:
    """True when a param name contains a secret marker (case-insensitive)."""
    lowered = str(name).lower()
    return any(marker in lowered for marker in SECRET_PARAM_MARKERS)


def truncate_text(text: str, limit: int = MAX_VALUE_CHARS) -> str:
    """Keep the first ``limit`` characters and note how many were dropped."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit})"


def _omit_secret_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): OMITTED_VALUE if is_secret_param_name(k) else _omit_secret_values(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_omit_secret_values(v) for v in value]
    return value


def _audit_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) < 10**18:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return truncate_text(value)
    if isinstance(value, dict | list | tuple):
        try:
            rendered = json.dumps(
                _omit_secret_values(value),
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
        except Exception:  # noqa: BLE001 — deep nesting or a failing nested __str__ must not break the audit.
            rendered = f"<{type(value).__name__}>"
        return truncate_text(rendered)
    # Decimal, Path, huge ints and anything else: the text form, truncated.
    try:
        return truncate_text(str(value))
    except Exception:  # noqa: BLE001 — a failing __str__ (or int digit limit) must not break the audit.
        return f"<{type(value).__name__}>"


def audit_params(params: object) -> dict[str, Any]:
    """Return the record-safe copy of a tool call's params."""
    if not isinstance(params, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in params.items():
        name = truncate_text(str(key))
        out[name] = OMITTED_VALUE if is_secret_param_name(key) else _audit_value(value)
    return out


def append_agent_record(
    home: Path,
    *,
    ts: int,
    actor: Actor,
    tool: str,
    params: dict,
    outcome: Outcome,
    code: str,
    venue: str = "-",
    pack: str = "-",
    confirm_ticket: str | None = None,
    tokens: dict | None = None,
) -> None:
    """Append one ``kind=agent_action`` record under ``home/journal``.

    Write failures are swallowed, like the user-policy receipts, so a
    read-only journal cannot turn a refusal into a crash or a success.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown agent action outcome: {outcome!r}")
    record = {
        "ts": int(ts),
        "kind": AGENT_ACTION_KIND,
        "venue": str(venue or "-"),
        "pack": str(pack or "-"),
        "bar_ts": 0,
        "detail": {
            "actor": actor.label,
            "tool": truncate_text(str(tool)),
            "params": audit_params(params),
            "outcome": outcome,
            "code": str(code),
            "confirm_ticket": confirm_ticket,
            "tokens": tokens,
        },
    }
    _append_line(Path(home), int(ts), sanitize.redact(record))


def _append_line(home: Path, ts: int, record: dict) -> None:
    journal_dir = home / "journal"
    try:
        journal_dir.mkdir(parents=True, exist_ok=True)
        if not _IS_WINDOWS:
            os.chmod(journal_dir, 0o700)
        utc_ts = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        file_path = journal_dir / f"{utc_ts:%Y-%m}.jsonl"
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str) + "\n"
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
    except (OSError, ValueError, OverflowError):
        return


def read_agent_activity(
    home: Path,
    *,
    actor: str | None = None,
    tool: str | None = None,
    outcome: str | None = None,
    since_ts: int | None = None,
    limit: int = 200,
) -> list[dict]:
    """Return ``agent_action`` records, newest first, with exact-match filters.

    ``actor`` matches an actor label (``"krell"``, ``"mcp:cursor"``).
    ``since_ts`` keeps records with ``ts >= since_ts``. Malformed rows are
    skipped, as :func:`krellbot.journal.read_records_lenient` does.
    """
    if limit < 1:
        return []
    rows: list[dict] = []
    for record in journal.read_records_lenient(home):
        if record.get("kind") != AGENT_ACTION_KIND:
            continue
        detail = record.get("detail")
        ts = record.get("ts")
        if not isinstance(detail, dict) or not isinstance(ts, int) or isinstance(ts, bool):
            continue
        if actor is not None and detail.get("actor") != actor:
            continue
        if tool is not None and detail.get("tool") != tool:
            continue
        if outcome is not None and detail.get("outcome") != outcome:
            continue
        if since_ts is not None and ts < since_ts:
            continue
        rows.append(record)
    # Newest first; equal timestamps keep the later append first (stable sort).
    rows.reverse()
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:limit]


__all__ = [
    "AGENT_ACTION_KIND",
    "MAX_VALUE_CHARS",
    "OMITTED_VALUE",
    "OUTCOMES",
    "SECRET_PARAM_MARKERS",
    "Outcome",
    "append_agent_record",
    "audit_params",
    "is_secret_param_name",
    "read_agent_activity",
    "truncate_text",
]
