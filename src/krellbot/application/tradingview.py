"""Normalize a bounded TradingView alert for local paper review."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

MAX_ALERT_BYTES = 4096
_FIELDS = frozenset({"schema_version", "rule_id", "ticker", "signal", "occurred_at"})
_RULE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_TICKER = re.compile(r"[A-Z0-9_]{1,24}:[A-Z0-9_.-]{1,32}")
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("alert JSON contains duplicate fields")
    return fields


def preview_alert(body: bytes) -> dict[str, str]:
    try:
        alert = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("alert must be a UTF-8 JSON object") from exc

    if not isinstance(alert, dict) or alert.keys() != _FIELDS:
        raise ValueError("alert must contain schema_version, rule_id, ticker, signal, and occurred_at")
    if not all(isinstance(value, str) for value in alert.values()):
        raise ValueError("all alert fields must be strings")
    if alert["schema_version"] != "1":
        raise ValueError("unsupported alert schema version")
    if not _RULE_ID.fullmatch(alert["rule_id"]):
        raise ValueError("invalid alert rule_id")
    if not _TICKER.fullmatch(alert["ticker"]):
        raise ValueError("ticker must use EXCHANGE:SYMBOL spelling")
    if alert["signal"] not in {"entry", "exit"}:
        raise ValueError("signal must be entry or exit")
    timestamp = alert["occurred_at"]
    if not _UTC_TIMESTAMP.fullmatch(timestamp):
        raise ValueError("occurred_at must be a UTC timestamp with a Z suffix")
    try:
        datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("occurred_at must be a valid calendar timestamp") from exc

    canonical = json.dumps(alert, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        **alert,
        "source": "tradingview",
        "mode": "paper",
        "fingerprint": hashlib.sha256(canonical).hexdigest(),
    }
