"""Krell's local conversation store: one thread, the profile and token usage.

Everything lives in ``<home>/krell.sqlite`` (WAL, mode 0600). Nothing here
touches the network or the keychain. ``clear()`` removes the messages and the
profile and starts a new conversation; the ``usage`` rows survive it, so the
monthly token count cannot be reset by clearing. Only the conversation count
starts again.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from krellbot.storage.database import (
    StoreCorrupt,
    StoreError,
    _classify_operational_error,
)

Role = Literal["user", "assistant", "tool", "notice"]
Mode = Literal["standard", "advanced"]

_ROLES = frozenset({"user", "assistant", "tool", "notice"})
_MODES = frozenset({"standard", "advanced"})
_SQLITE_HEADER = b"SQLite format 3\x00"
_SIDE_SUFFIXES = ("", "-wal", "-shm")


@dataclass(frozen=True)
class StoredMessage:
    id: int
    role: str
    payload: dict[str, Any]
    ts: int


@dataclass(frozen=True)
class Profile:
    mode: str | None
    answers: dict[str, Any]


def _month_bounds(now: int) -> tuple[int, int]:
    """Start (inclusive) and end (exclusive) of the UTC calendar month of ``now``."""
    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    start = datetime(moment.year, moment.month, 1, tzinfo=timezone.utc)
    if moment.month == 12:
        end = datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(moment.year, moment.month + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


class ConversationStore:
    def __init__(self, home: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / "krell.sqlite"
        self._clock = clock
        self._lock = threading.Lock()
        with self._connection():
            pass

    def _now(self) -> int:
        return int(self._clock())

    def _secure(self) -> None:
        for suffix in _SIDE_SUFFIXES:
            try:
                os.chmod(f"{self.path}{suffix}", 0o600)
            except FileNotFoundError:
                continue
            except OSError:
                if os.name == "nt":
                    continue
                raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                with open(self.path, "rb") as handle:
                    header = handle.read(16)
                if header not in {b"", _SQLITE_HEADER}:
                    raise StoreCorrupt("database header is not sqlite")
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
                conn = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
            except (sqlite3.DatabaseError, OSError) as exc:
                raise StoreCorrupt(f"store open failed: {exc}") from exc
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                self._migrate(conn)
                self._secure()
                yield conn
                self._secure()
            except sqlite3.OperationalError as exc:
                raise _classify_operational_error(exc) from exc
            except sqlite3.DatabaseError as exc:
                raise StoreCorrupt(f"store unreadable: {exc}") from exc
            finally:
                conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, "
            "payload TEXT NOT NULL, ts INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS profile ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), mode TEXT, answers TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS usage ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, input_tokens INTEGER NOT NULL, "
            "output_tokens INTEGER NOT NULL, ts INTEGER NOT NULL, "
            "cleared INTEGER NOT NULL DEFAULT 0)"
        )

    def append(self, role: Role, payload: dict[str, Any]) -> int:
        if role not in _ROLES:
            raise ValueError(f"unknown role: {role!r}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")  # noqa: TRY004
        text = json.dumps(payload, sort_keys=True)
        ts = self._now()
        with self._connection() as conn:
            cur = conn.execute("INSERT INTO messages (role, payload, ts) VALUES (?, ?, ?)", (role, text, ts))
            return int(cur.lastrowid)

    def history(self) -> list[StoredMessage]:
        with self._connection() as conn:
            rows = conn.execute("SELECT id, role, payload, ts FROM messages ORDER BY id").fetchall()
        return [StoredMessage(int(r["id"]), str(r["role"]), json.loads(r["payload"]), int(r["ts"])) for r in rows]

    def set_profile(self, *, mode: Mode | None = None, answers: dict[str, Any] | None = None) -> None:
        if mode is not None and mode not in _MODES:
            raise ValueError(f"unknown mode: {mode!r}")
        if answers is not None and not isinstance(answers, dict):
            raise ValueError("answers must be a JSON object")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT mode, answers FROM profile WHERE id = 1").fetchone()
                new_mode = mode if mode is not None else (row["mode"] if row else None)
                new_answers = (
                    json.dumps(answers, sort_keys=True) if answers is not None else (row["answers"] if row else "{}")
                )
                conn.execute(
                    "INSERT OR REPLACE INTO profile (id, mode, answers) VALUES (1, ?, ?)",
                    (new_mode, new_answers),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def profile(self) -> Profile:
        with self._connection() as conn:
            row = conn.execute("SELECT mode, answers FROM profile WHERE id = 1").fetchone()
        if row is None:
            return Profile(mode=None, answers={})
        return Profile(mode=row["mode"], answers=json.loads(row["answers"]))

    def record_usage(self, input_tokens: int, output_tokens: int, *, ts: int) -> None:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts cannot be negative")
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO usage (input_tokens, output_tokens, ts) VALUES (?, ?, ?)",
                (int(input_tokens), int(output_tokens), int(ts)),
            )

    def conversation_tokens(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM usage WHERE cleared = 0"
            ).fetchone()
        return int(row[0])

    def month_tokens(self, *, now: int) -> int:
        start, end = _month_bounds(now)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM usage WHERE ts >= ? AND ts < ?",
                (start, end),
            ).fetchone()
        return int(row[0])

    def clear(self) -> None:
        """Remove messages and profile in one transaction; usage rows stay."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM messages")
                conn.execute("DELETE FROM profile")
                conn.execute("UPDATE usage SET cleared = 1")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise


__all__ = ["ConversationStore", "Profile", "StoreError", "StoredMessage"]
