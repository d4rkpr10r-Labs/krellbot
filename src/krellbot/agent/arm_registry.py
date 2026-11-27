"""Which paper arms an agent made, counted against the live config.

The agent limit ``max_agent_arms`` (:mod:`krellbot.agent.limits`) counts
agent arms that are still running. Krell's loop and the MCP server run in
different processes, so the count is not a per-process counter: every
successful agent ``paper_arm`` appends an entry to ``<home>/agent-arms.json``
(mode 0600, atomic write), and :func:`running_agent_arms` keeps only the
entries that still match the on-disk config. An entry counts while
``load_config(home)`` has a paper arm on that venue and pair with the same
``armed_at_ts``. A disarm, or a human re-arm of the same pair (a new
``armed_at_ts``), drops it out. Human arms are never recorded.

A registry file that exists but cannot be read raises
:class:`AgentArmRegistryUnreadable`, so the limit fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path

from krellbot.agent.actors import Actor
from krellbot.config import Config, find_armed, load_config
from krellbot.paths import atomic_write

REGISTRY_FILENAME = "agent-arms.json"
REGISTRY_SCHEMA_VERSION = "1"


class AgentArmRegistryUnreadable(ValueError):
    """``agent-arms.json`` exists but is not a well-formed registry."""


def registry_path(home: Path) -> Path:
    """Return the path to the agent arm registry."""
    return Path(home) / REGISTRY_FILENAME


def _entry(row: object) -> dict | None:
    if not isinstance(row, dict):
        return None
    venue, pair, actor = row.get("venue"), row.get("pair"), row.get("actor")
    armed_at_ts, revision_id = row.get("armed_at_ts"), row.get("revision_id")
    if not all(isinstance(value, str) and value for value in (venue, pair, actor)):
        return None
    if type(armed_at_ts) is not int:
        return None
    if revision_id is not None and not isinstance(revision_id, str):
        return None
    return {
        "venue": venue,
        "pair": pair,
        "actor": actor,
        "armed_at_ts": armed_at_ts,
        "revision_id": revision_id,
    }


def _read(home: Path) -> list[dict]:
    """All recorded entries; ``[]`` when the file is missing."""
    path = registry_path(home)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AgentArmRegistryUnreadable("agent arm registry cannot be read") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentArmRegistryUnreadable("agent arm registry is not JSON") from exc
    if not isinstance(data, dict) or data.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise AgentArmRegistryUnreadable("agent arm registry has an unknown shape")
    rows = data.get("arms")
    if not isinstance(rows, list):
        raise AgentArmRegistryUnreadable("agent arm registry has an unknown shape")
    entries = [_entry(row) for row in rows]
    if any(entry is None for entry in entries):
        raise AgentArmRegistryUnreadable("agent arm registry has a malformed entry")
    return [entry for entry in entries if entry is not None]


def _is_running(entry: dict, config: Config) -> bool:
    armed = find_armed(config, entry["venue"], entry["pair"])
    return armed is not None and armed.mode == "paper" and armed.armed_at_ts == entry["armed_at_ts"]


def running_agent_arms(home: Path) -> list[dict]:
    """Agent arms still running: recorded entries the live config still holds.

    Raises :class:`AgentArmRegistryUnreadable` when the registry is unreadable.
    """
    entries = _read(home)
    if not entries:
        return []
    config = load_config(home)
    return [entry for entry in entries if _is_running(entry, config)]


def record_agent_arm(
    home: Path,
    *,
    venue: str,
    pair: str,
    actor: Actor,
    armed_at_ts: int,
    revision_id: str | None,
) -> None:
    """Record a successful agent paper arm.

    Entries that no longer match the config, and any older entry for the same
    venue and pair, are dropped, so the file stays as small as the set of
    running arms. Raises :class:`AgentArmRegistryUnreadable` (an unreadable
    registry is never overwritten) or ``OSError`` when the write fails.
    """
    config = load_config(home)
    kept = [
        entry
        for entry in _read(home)
        if _is_running(entry, config) and not (entry["venue"] == venue and entry["pair"] == pair)
    ]
    kept.append(
        {
            "venue": str(venue),
            "pair": str(pair),
            "actor": actor.label,
            "armed_at_ts": int(armed_at_ts),
            "revision_id": revision_id or None,
        }
    )
    payload = {"schema_version": REGISTRY_SCHEMA_VERSION, "arms": kept}
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write(registry_path(home), encoded, mode=0o600)


__all__ = [
    "REGISTRY_FILENAME",
    "REGISTRY_SCHEMA_VERSION",
    "AgentArmRegistryUnreadable",
    "record_agent_arm",
    "registry_path",
    "running_agent_arms",
]
