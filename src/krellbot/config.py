"""Armed-pack config under `$KRELLBOT_HOME/config.json`.

The config holds the list of armed packs (one per venue+pair) plus a flag
indicating whether the first live arm has happened (so subsequent live arms
skip the typed confirmation). Persistence uses `paths.atomic_write`.

An `ArmedPack` record has:
    pack_path: absolute or home-relative path to the pack JSON
    pack_sha256: sha256 of the pack JSON bytes
    pack_id, pack_version
    venue, pair
    cap: percent (1..100)
    stop: Decimal stop price (resolved from the pack risk on arm)
    mode: "paper" | "live"
    starting_cash: Decimal | None (paper deposit; live reserved cash when set)
    requires_license: bool
    armed_at_ts: int (unix seconds when armed)
    owned_qty: Decimal (per-pack filled - exited, from journal + paper state)
    pending_version: str | None (version to adopt when flat)
    entries_paused: bool (tick stops placing new entries while true;
        reconcile / existing-position protection still runs; persisted)
    daily_loss_limit_pct: Decimal | None (optional daily loss limit, a
        percent of starting_cash; off when None, and then not written, so an
        arm without it keeps its config bytes. A stored value that is not a
        number loads as NaN, which the tick treats as unusable.)
"""

from __future__ import annotations

import errno
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from krellbot import paths

CONFIG_FILENAME = "config.json"


def config_path(home: Path) -> Path:
    """Return the path to the armed-pack config JSON."""
    return Path(home) / CONFIG_FILENAME


@contextmanager
def _paper_command_transaction(home: Path, *, timeout: float = 5.0) -> Iterator[bool]:
    """Serialize stop/pause/resume only; other config writers are not covered.

    Keep the lock file's identity stable across handoffs. Kernel locks release
    on process exit, and the deadline bounds waiting on another command.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / ".paper-command.lock", os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            # Windows permits locking beyond EOF; no unlocked initialization
            # write may race a competing process that already holds the byte.

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


@dataclass
class ArmedPack:
    """One armed pack record."""

    pack_path: str
    pack_sha256: str
    pack_id: str
    pack_version: str
    venue: str
    pair: str
    cap: Decimal
    stop: Decimal
    mode: str
    starting_cash: Decimal | None
    requires_license: bool
    armed_at_ts: int
    owned_qty: Decimal = Decimal(0)
    pending_version: str | None = None
    entries_paused: bool = False
    daily_loss_limit_pct: Decimal | None = None


@dataclass
class Config:
    """Root config: the list of armed packs + first-live-arm flag."""

    armed: list[ArmedPack] = field(default_factory=list)
    live_first_armed: bool = False


def load_config(home: Path) -> Config:
    """Load the armed-pack config. Returns an empty Config if the file is missing."""
    path = config_path(home)
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return Config()
    if not isinstance(raw, dict):
        return Config()
    armed_raw = raw.get("armed", [])
    armed: list[ArmedPack] = []
    if isinstance(armed_raw, list):
        for entry in armed_raw:
            if not isinstance(entry, dict):
                continue
            try:
                armed.append(
                    ArmedPack(
                        pack_path=str(entry.get("pack_path", "")),
                        pack_sha256=str(entry.get("pack_sha256", "")),
                        pack_id=str(entry.get("pack_id", "")),
                        pack_version=str(entry.get("pack_version", "")),
                        venue=str(entry.get("venue", "")),
                        pair=str(entry.get("pair", "")),
                        cap=Decimal(str(entry.get("cap", "100"))),
                        stop=Decimal(str(entry.get("stop", "0"))),
                        mode=str(entry.get("mode", "paper")),
                        starting_cash=(
                            Decimal(str(entry["starting_cash"])) if entry.get("starting_cash") is not None else None
                        ),
                        requires_license=bool(entry.get("requires_license", False)),
                        armed_at_ts=int(entry.get("armed_at_ts", 0)),
                        owned_qty=Decimal(str(entry.get("owned_qty", "0"))),
                        pending_version=(
                            str(entry["pending_version"]) if entry.get("pending_version") is not None else None
                        ),
                        entries_paused=bool(entry.get("entries_paused", False)),
                        daily_loss_limit_pct=_stored_loss_limit(entry),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
    return Config(
        armed=armed,
        live_first_armed=bool(raw.get("live_first_armed", False)),
    )


def save_config(home: Path, config: Config) -> None:
    """Persist the config atomically."""
    path = config_path(home)
    payload = {
        "armed": [_serialize_armed(a) for a in config.armed],
        "live_first_armed": config.live_first_armed,
    }
    paths.atomic_write(path, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))


def _serialize_armed(a: ArmedPack) -> dict:
    out = asdict(a)
    # Decimal -> str so JSON does not lose precision.
    out["cap"] = str(a.cap)
    out["stop"] = str(a.stop)
    out["owned_qty"] = str(a.owned_qty)
    if a.starting_cash is not None:
        out["starting_cash"] = str(a.starting_cash)
    else:
        out["starting_cash"] = None
    # Off is absent, so an arm that never had a limit keeps its exact bytes.
    if a.daily_loss_limit_pct is None:
        del out["daily_loss_limit_pct"]
    else:
        out["daily_loss_limit_pct"] = str(a.daily_loss_limit_pct)
    return out


def _stored_loss_limit(entry: dict) -> Decimal | None:
    """The stored daily loss limit; NaN when present but not a number, so it fails closed."""
    raw = entry.get("daily_loss_limit_pct")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return Decimal("NaN")
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return Decimal("NaN")


def set_daily_loss_limit(home: Path, *, venue: str, pair: str, pct: Decimal | None) -> str:
    """Set (or clear, with ``None``) the daily loss limit of the arm on ``venue`` ``pair``.

    Serialized with the stop/pause/resume commands. Returns ``changed``,
    ``unchanged``, ``not_armed``, ``starting_cash_unknown`` (a limit needs a
    starting cash to be a percent of) or ``busy``. Validating ``pct`` is the
    caller's job. Nothing is written unless the result is ``changed``.
    """
    with _paper_command_transaction(home) as acquired:
        if not acquired:
            return "busy"
        config = load_config(home)
        armed = find_armed(config, venue, pair)
        if armed is None:
            return "not_armed"
        cash = armed.starting_cash
        if pct is not None and (cash is None or not cash.is_finite() or cash <= 0):
            return "starting_cash_unknown"
        current = armed.daily_loss_limit_pct
        if current is pct or (current is not None and pct is not None and current == pct):
            return "unchanged"
        armed.daily_loss_limit_pct = pct
        save_config(home, config)
        return "changed"


def find_armed(config: Config, venue: str, pair: str) -> ArmedPack | None:
    """Return the armed pack on (venue, pair), or None."""
    for a in config.armed:
        if a.venue == venue and a.pair == pair:
            return a
    return None


def find_armed_by_id(config: Config, pack_id: str) -> ArmedPack | None:
    """Return the first armed pack with the given pack_id, or None."""
    for a in config.armed:
        if a.pack_id == pack_id:
            return a
    return None


def adopt_pending_version(armed: ArmedPack) -> None:
    """Move pending_version into pack_version and clear pending_version."""
    if armed.pending_version and armed.owned_qty == Decimal(0):
        armed.pack_version = armed.pending_version
        armed.pending_version = None
