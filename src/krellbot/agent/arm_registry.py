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

Writers serialize on an exclusive lock on ``<home>/.agent-arms.lock``
(:func:`registry_lock`), so two processes cannot lose each other's entry,
and :func:`claim_agent_arm` re-counts and records under that one lock, so
two agent arms that both passed an earlier count cannot both be kept.

A registry file that exists but cannot be read raises
:class:`AgentArmRegistryUnreadable`, so the limit fails closed.

Agent arms arm a pack snapshot, never the agent-supplied file:
:func:`snapshot_pack` writes the exact bytes the agent limits were checked
against to ``<home>/agent-packs/<sha256>.json`` (directory 0700, file 0600,
atomic, content-addressed). A file swapped after the check cannot change
what is armed, and the snapshot path identifies the arm's record afterwards.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from krellbot.agent.actors import Actor
from krellbot.config import Config, find_armed, load_config
from krellbot.paths import atomic_write

REGISTRY_FILENAME = "agent-arms.json"
LOCK_FILENAME = ".agent-arms.lock"
REGISTRY_SCHEMA_VERSION = "1"
SNAPSHOT_DIRNAME = "agent-packs"


def snapshot_dir(home: Path) -> Path:
    """Return the directory of agent pack snapshots."""
    return Path(home) / SNAPSHOT_DIRNAME


def snapshot_pack(home: Path, raw: bytes) -> Path:
    """Store ``raw`` as ``<home>/agent-packs/<sha256 of raw>.json`` and return its path.

    The directory is 0700 and the file 0600, written atomically through a
    temp file unique to this call, so concurrent calls with the same bytes
    never share one. A snapshot that already holds exactly these bytes is
    never rewritten; one whose bytes no longer match its name is replaced.
    """
    digest = hashlib.sha256(raw).hexdigest()
    directory = snapshot_dir(home)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    path = directory / f"{digest}.json"
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            return path
    except FileNotFoundError:
        pass
    _write_unique_then_replace(path, raw)
    return path


def _write_unique_then_replace(path: Path, raw: bytes) -> None:
    """Write ``raw`` to a per-call temp file (0600) beside ``path``, then replace ``path``."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


class AgentArmRegistryUnreadable(ValueError):
    """``agent-arms.json`` exists but is not a well-formed registry."""


class AgentArmRegistryBusy(TimeoutError):
    """Another writer held the registry lock past the deadline."""


def registry_path(home: Path) -> Path:
    """Return the path to the agent arm registry."""
    return Path(home) / REGISTRY_FILENAME


@contextmanager
def registry_lock(home: Path, *, timeout: float = 5.0) -> Iterator[bool]:
    """Hold the exclusive registry lock; yields False if it was not acquired in time.

    The same flock-with-deadline pattern as ``config._paper_command_transaction``,
    on its own lock file. The lock file's identity stays stable across
    handoffs, and kernel locks release on process exit.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            def acquire() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

            def release() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire() -> None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release() -> None:
                fcntl.flock(fd, fcntl.LOCK_UN)

        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield False
                    return
                time.sleep(min(0.05, remaining))
        yield True
    finally:
        try:
            if acquired:
                release()
        finally:
            os.close(fd)


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


def _running(home: Path) -> list[dict]:
    entries = _read(home)
    if not entries:
        return []
    config = load_config(home)
    return [entry for entry in entries if _is_running(entry, config)]


def running_agent_arms(home: Path) -> list[dict]:
    """Agent arms still running: recorded entries the live config still holds.

    Raises :class:`AgentArmRegistryUnreadable` when the registry is unreadable.
    """
    return _running(home)


def _record_locked(
    home: Path,
    *,
    venue: str,
    pair: str,
    actor: Actor,
    armed_at_ts: int,
    revision_id: str | None,
) -> None:
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


def record_agent_arm(
    home: Path,
    *,
    venue: str,
    pair: str,
    actor: Actor,
    armed_at_ts: int,
    revision_id: str | None,
) -> None:
    """Record a successful agent paper arm, under the registry lock.

    Entries that no longer match the config, and any older entry for the same
    venue and pair, are dropped, so the file stays as small as the set of
    running arms. Raises :class:`AgentArmRegistryUnreadable` (an unreadable
    registry is never overwritten), :class:`AgentArmRegistryBusy`, or
    ``OSError`` when the write fails.
    """
    with registry_lock(home) as acquired:
        if not acquired:
            raise AgentArmRegistryBusy("agent arm registry is busy")
        _record_locked(home, venue=venue, pair=pair, actor=actor, armed_at_ts=armed_at_ts, revision_id=revision_id)


def claim_agent_arm(
    home: Path,
    *,
    venue: str,
    pair: str,
    actor: Actor,
    armed_at_ts: int,
    revision_id: str | None,
    max_agent_arms: int,
) -> bool:
    """Count the other running agent arms and record this one, under one lock.

    Returns False, recording nothing, when the other running agent arms
    already reach ``max_agent_arms``. Raises like :func:`record_agent_arm`.
    """
    with registry_lock(home) as acquired:
        if not acquired:
            raise AgentArmRegistryBusy("agent arm registry is busy")
        others = [e for e in _running(home) if not (e["venue"] == venue and e["pair"] == pair)]
        if len(others) >= max_agent_arms:
            return False
        _record_locked(home, venue=venue, pair=pair, actor=actor, armed_at_ts=armed_at_ts, revision_id=revision_id)
        return True


__all__ = [
    "LOCK_FILENAME",
    "REGISTRY_FILENAME",
    "REGISTRY_SCHEMA_VERSION",
    "SNAPSHOT_DIRNAME",
    "AgentArmRegistryBusy",
    "AgentArmRegistryUnreadable",
    "claim_agent_arm",
    "record_agent_arm",
    "registry_lock",
    "registry_path",
    "running_agent_arms",
    "snapshot_dir",
    "snapshot_pack",
]
