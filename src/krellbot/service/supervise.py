"""Supervision helpers for the scheduled tick.

The OS scheduler fires ``krellbot tick`` (or ``krellbot.service.runner``,
which delegates) on an hourly cadence. After a sleep/wake cycle the gap
between scheduled firings can be much larger than an hour. ``record_wake_gap``
detects that gap and journals a ``kind=supervision / event=wake_gap`` record
so the operator and the doctor can see a long absence happened.

The wake-gap check fires *before* the tick evaluates the latest bar: it is a
supervision signal, not a backfill request. The engine itself does no replay
of missed bars; ``run.tick`` already coalesces by feeding every candle up to
the latest into ``evaluate.run`` so the indicator state is correct on the
post-wake bar, and outbox coids prevent a duplicate send for a bar that was
already sent.

Platform enable (``systemctl`` / ``launchctl`` / ``schtasks``) lives behind
the ``PlatformSupervisor`` seam so the loopback API and tests never call the
real OS scheduler unless a production supervisor is selected.

A tick-driven notifier cannot report a tick that never runs, so the
scheduler also fires ``krellbot service heartbeat`` every 15 minutes from its
own units (``check_heartbeat``). It reads only scheduler and journal state and
sends ``scheduler_stale`` and ``tick_failure`` through the shared occurrence
store. The web scheduler install writes both schedules and enable enables
both; ``installed`` and ``enabled`` still describe the tick.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from krellbot import doctor
from krellbot import journal as kb_journal
from krellbot import paths as kb_paths

from . import render

SCHEMA_VERSION = "1"
LAST_ERROR_FILENAME = "last_error"
LAUNCHD_LABEL = "dev.krellbot.tick"
LAUNCHD_FILENAME = f"{LAUNCHD_LABEL}.plist"
SYSTEMD_TIMER_NAME = "krellbot-tick.timer"
SYSTEMD_SERVICE_NAME = "krellbot-tick.service"
WINDOWS_TASK_NAME = r"\krellbot\tick"
WINDOWS_TASK_FILENAME = "krellbot-tick.xml"

HEARTBEAT_LAUNCHD_LABEL = "dev.krellbot.heartbeat"
HEARTBEAT_LAUNCHD_FILENAME = f"{HEARTBEAT_LAUNCHD_LABEL}.plist"
HEARTBEAT_SYSTEMD_TIMER_NAME = "krellbot-heartbeat.timer"
HEARTBEAT_SYSTEMD_SERVICE_NAME = "krellbot-heartbeat.service"
HEARTBEAT_WINDOWS_TASK_NAME = r"\krellbot\heartbeat"
HEARTBEAT_WINDOWS_TASK_FILENAME = "krellbot-heartbeat.xml"
LAST_HEARTBEAT_FILENAME = "last_heartbeat"
HEARTBEAT_INSTALL_FAILED = "heartbeat_install_failed"

# The heartbeat fires every ``render.HEARTBEAT_SECONDS``. A previous check at
# most this long ago means the computer stayed awake since. A healthy hourly
# tick goes stale only after the computer was off or asleep for over an hour,
# which leaves a longer gap between checks than this.
HEARTBEAT_AWAKE_SECONDS = 3 * render.HEARTBEAT_SECONDS


def last_tick_ts(home: Path, venue: str) -> int | None:
    """Return the maximum ``ts`` of ``kind=tick`` journal records for ``venue``.

    Skips unparseable lines. Returns None when no tick record exists for
    the venue. ``home`` is the krellbot data home (does not create it).
    """
    journal_dir = Path(home) / "journal"
    if not journal_dir.is_dir():
        return None
    latest_ts = 0
    found = False
    for path in sorted(journal_dir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") != "tick":
                continue
            if rec.get("venue") != venue:
                continue
            try:
                ts = int(rec.get("ts", 0) or 0)
            except (TypeError, ValueError):
                continue
            if ts <= 0:
                continue
            found = True
            latest_ts = max(latest_ts, ts)
    if not found:
        return None
    return latest_ts


def record_wake_gap(
    home: Path,
    venue: str,
    *,
    now: int,
    threshold: int = doctor.TICK_STALE_SECONDS,
) -> int | None:
    """Journal a wake-gap record when the gap since the last tick is large.

    When ``last_tick_ts`` is not None and ``now - last >= threshold``, a
    journal record of the form::

        {"ts": now, "kind": "supervision", "venue": venue,
         "pack": "-", "bar_ts": 0,
         "detail": {"event": "wake_gap", "gap_s": now - last,
                    "last_tick_ts": last}}

    is appended and the gap in seconds is returned. Otherwise (no tick
    record, or the gap is below the threshold) nothing is journaled and
    None is returned. Failures to journal (e.g. read-only home) are
    swallowed so the tick itself is never blocked by supervision IO.
    """
    last = last_tick_ts(home, venue)
    if last is None:
        return None
    gap = int(now) - int(last)
    if gap < int(threshold):
        return None
    try:
        kb_journal.append(
            {
                "ts": int(now),
                "kind": "supervision",
                "venue": venue,
                "pack": "-",
                "bar_ts": 0,
                "detail": {
                    "event": "wake_gap",
                    "gap_s": gap,
                    "last_tick_ts": int(last),
                },
            }
        )
    except (OSError, ValueError):
        pass
    return gap


def latest_wake_gap(home: Path, venue: str | None = None) -> int | None:
    """Return the ``gap_s`` of the newest ``wake_gap`` record, or None.

    When ``venue`` is None, looks across all venues. ``home`` is the
    data home and is not created.
    """
    journal_dir = Path(home) / "journal"
    if not journal_dir.is_dir():
        return None
    newest_ts = -1
    newest_gap: int | None = None
    for path in sorted(journal_dir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") != "supervision":
                continue
            detail = rec.get("detail") or {}
            if detail.get("event") != "wake_gap":
                continue
            if venue is not None and rec.get("venue") != venue:
                continue
            try:
                ts = int(rec.get("ts", 0) or 0)
            except (TypeError, ValueError):
                continue
            if ts < newest_ts:
                continue
            newest_ts = ts
            try:
                newest_gap = int(detail.get("gap_s", 0) or 0)
            except (TypeError, ValueError):
                newest_gap = None
    return newest_gap


def resolve_owner(home: Path) -> dict | None:
    """Parse the default owner file, returning the supervisor record or None.

    Returns None when the owner file does not exist. Keys are sorted
    alphabetically. ``home`` is the data home; it is not created here.
    """
    owner = Path(home) / "service" / "owners" / "default.owner"
    if not owner.exists():
        return None
    try:
        text = owner.read_text(encoding="utf-8")
    except OSError:
        return None
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        out[key] = value
    return out or None


# ---------------------------------------------------------------------------
# The heartbeat check (``krellbot service heartbeat``)
# ---------------------------------------------------------------------------


def last_heartbeat_path(home: Path) -> Path:
    """Where the heartbeat records when it last checked."""
    return Path(home) / "service" / LAST_HEARTBEAT_FILENAME


def _record_heartbeat(home: Path, *, now: int) -> bool:
    """Record this check; return True when the previous one ran recently.

    A previous check at most ``HEARTBEAT_AWAKE_SECONDS`` ago means the
    computer was awake since then, so a tick missed meanwhile was the
    scheduler's to run. A first check, a long gap (sleep or power-off) or a
    clock that moved back returns False. A failed write is ignored.
    """
    path = last_heartbeat_path(home)
    try:
        previous: int | None = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        previous = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        kb_paths.atomic_write(path, f"{int(now)}\n".encode())
    except OSError:
        pass
    return previous is not None and 0 <= int(now) - previous <= HEARTBEAT_AWAKE_SECONDS


def check_heartbeat(home: Path, *, now: int, notifier=None) -> int:
    """Send the scheduler alerts a tick that never ran cannot send itself.

    The scheduler fires this every 15 minutes, independently of the tick. It
    reads only scheduler and journal state (``alerts.collect_scheduler``:
    the scheduler's ``last_error``, the armed venues and the journal) and
    sends ``tick_failure`` and ``scheduler_stale`` through
    ``notifications.maybe_notify``: the same opt-in settings, notifiers,
    occurrence store and lock as the tick and the workstation, so each
    occurrence is sent once whichever process sees it first. It never
    fetches candles, builds a venue, reads keys or ticks.

    ``scheduler_stale`` is sent only when the previous check ran in the last
    ``HEARTBEAT_AWAKE_SECONDS``. A computer that was asleep or off ran
    neither the tick nor this check, and on wake the scheduler runs the
    missed tick; reporting the gap first would warn about a tick that is
    about to run. If it still does not run, the next check reports it.

    With every channel off it reads nothing more and writes nothing. A
    notifier failure is recorded by ``maybe_notify`` and never raised.
    Returns the number of deliveries.
    """
    from krellbot import notifications as kb_notifications
    from krellbot.application import alerts as kb_alerts

    home = Path(home)
    if not kb_notifications.delivery_enabled(home):
        return 0
    awake = _record_heartbeat(home, now=now)
    found = kb_alerts.collect_scheduler(home, now=now)
    if not awake:
        found = [alert for alert in found if alert.kind != kb_alerts.KIND_SCHEDULER_STALE]
    return kb_notifications.maybe_notify(
        home,
        found,
        notifier=notifier,
        now=now,
        kinds=kb_alerts.SCHEDULER_KINDS,
    )


def heartbeat_unit_paths(write_root: Path) -> list[Path]:
    """The heartbeat unit files for ``sys.platform`` under ``write_root``."""
    root = Path(write_root)
    if sys.platform == "darwin":
        return [root / "Library" / "LaunchAgents" / HEARTBEAT_LAUNCHD_FILENAME]
    if sys.platform.startswith("linux"):
        d = root / ".config" / "systemd" / "user"
        return [d / HEARTBEAT_SYSTEMD_TIMER_NAME, d / HEARTBEAT_SYSTEMD_SERVICE_NAME]
    if sys.platform == "win32":
        return [root / "Tasks" / HEARTBEAT_WINDOWS_TASK_FILENAME]
    return []


def _render_heartbeat_units(write_root: Path, home: Path, executable: str) -> dict[Path, bytes]:
    root = Path(write_root)
    if sys.platform == "darwin":
        body = render.render_heartbeat_launchd(executable, home)
        return {root / "Library" / "LaunchAgents" / HEARTBEAT_LAUNCHD_FILENAME: body}
    if sys.platform.startswith("linux"):
        d = root / ".config" / "systemd" / "user"
        return {d / name: body for name, body in render.render_heartbeat_systemd(executable, home).items()}
    if sys.platform == "win32":
        body = render.render_heartbeat_windows(executable, home)
        return {root / "Tasks" / HEARTBEAT_WINDOWS_TASK_FILENAME: body}
    return {}


def _heartbeat_executable(home: Path, executable: str | None) -> str:
    """The executable the tick unit runs, from the owner file when it is this home's."""
    owner = resolve_owner(home) or {}
    recorded = owner.get("executable")
    if recorded and owner.get("krellbot_home") == str(home):
        return recorded
    return executable or sys.executable


def install_heartbeat_units(home: Path, *, write_root: Path, executable: str | None = None) -> None:
    """Write the heartbeat units ``sys.platform`` needs, leaving any present file as is.

    The units run the executable the tick unit runs. Raises ``OSError`` when
    a unit cannot be written and ``ValueError`` when the renderer refuses
    the path; an unsupported platform writes nothing.
    """
    units = _render_heartbeat_units(write_root, Path(home), _heartbeat_executable(Path(home), executable))
    for path, body in units.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def uninstall_heartbeat_units(write_root: Path) -> None:
    """Delete the heartbeat unit files; files outside that set are never touched."""
    for path in heartbeat_unit_paths(write_root):
        if path.exists():
            path.unlink()


# ---------------------------------------------------------------------------
# Scheduler install / enable / status (API seam)
# ---------------------------------------------------------------------------


def last_error_path(home: Path) -> Path:
    """Where the durable scheduler ``last_error`` text lives under the data home."""
    return Path(home) / "service" / LAST_ERROR_FILENAME


def get_last_error(home: Path) -> str | None:
    """Return the last scheduler error string, or None when clear/missing."""
    path = last_error_path(home)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def last_error_ts(home: Path) -> int | None:
    """Return when ``last_error`` was last written (unix seconds), or None.

    Every ``set_last_error`` with a message rewrites the file, so its
    modification time dates the failure. Alerts use it as the occurrence
    time instead of the time the alert happens to be read.
    """
    try:
        return int(last_error_path(home).stat().st_mtime)
    except (OSError, ValueError, OverflowError):
        return None


def set_last_error(home: Path, message: str | None) -> None:
    """Persist or clear the scheduler ``last_error`` under ``home/service``.

    ``message=None`` or empty removes the file. Failures to write are
    swallowed so a read-only home cannot block status reads; callers that
    need a durable error should still surface the in-memory message.
    """
    path = last_error_path(home)
    if not message:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(message).strip() + "\n", encoding="utf-8")
    except OSError:
        pass


def units_installed(write_root: Path) -> bool:
    """True when the platform unit files exist under ``write_root``."""
    root = Path(write_root)
    if sys.platform == "darwin":
        return (root / "Library" / "LaunchAgents" / LAUNCHD_FILENAME).is_file()
    if sys.platform.startswith("linux"):
        d = root / ".config" / "systemd" / "user"
        return (d / SYSTEMD_TIMER_NAME).is_file() and (d / SYSTEMD_SERVICE_NAME).is_file()
    if sys.platform == "win32":
        return (root / "Tasks" / WINDOWS_TASK_FILENAME).is_file()
    return False


class PlatformSupervisor(Protocol):
    """OS enable/query surface used by the scheduler API.

    Production code uses ``platform_supervisor()``. Tests inject a
    ``FakeSupervisor`` so no ``systemctl`` / ``launchctl`` / ``schtasks``
    process is ever spawned.
    """

    def enable(self, *, write_root: Path) -> None:
        """Enable (and start) the installed scheduler unit under ``write_root``."""

    def is_enabled(self, *, write_root: Path) -> bool:
        """Return whether the platform reports the unit as enabled."""


@dataclass
class FakeSupervisor:
    """In-memory supervisor for tests. Never talks to the OS."""

    enabled: bool = False
    fail_message: str | None = None
    enable_calls: list[Path] = field(default_factory=list)

    def enable(self, *, write_root: Path) -> None:
        self.enable_calls.append(Path(write_root))
        if self.fail_message:
            raise RuntimeError(self.fail_message)
        self.enabled = True

    def is_enabled(self, *, write_root: Path) -> bool:
        return bool(self.enabled)


@dataclass(frozen=True)
class UnavailableSupervisor:
    """Used on platforms without a known scheduler enable path."""

    reason: str = "unsupported platform"

    def enable(self, *, write_root: Path) -> None:
        raise RuntimeError(self.reason)

    def is_enabled(self, *, write_root: Path) -> bool:
        return False


def _run_checked(argv: list[str], *, label: str) -> None:
    """Run ``argv``; raise RuntimeError with stderr/stdout on non-zero exit."""
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{label}: {exc}") from exc
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "").strip()
    if detail:
        raise RuntimeError(f"{label}: {detail}")
    raise RuntimeError(f"{label}: exit {completed.returncode}")


@dataclass(frozen=True)
class SystemdUserSupervisor:
    """Linux: ``systemctl --user`` enable/start of ``krellbot-tick.timer``,
    and of ``krellbot-heartbeat.timer`` when it is installed."""

    def enable(self, *, write_root: Path) -> None:
        # ``write_root`` is expected to be the user home so the units land
        # where ``systemctl --user`` already looks (``~/.config/systemd/user``).
        _run_checked(
            ["systemctl", "--user", "daemon-reload"],
            label="systemctl daemon-reload",
        )
        _run_checked(
            ["systemctl", "--user", "enable", "--now", SYSTEMD_TIMER_NAME],
            label="systemctl enable",
        )
        heartbeat = Path(write_root) / ".config" / "systemd" / "user" / HEARTBEAT_SYSTEMD_TIMER_NAME
        if heartbeat.is_file():
            _run_checked(
                ["systemctl", "--user", "enable", "--now", HEARTBEAT_SYSTEMD_TIMER_NAME],
                label="systemctl enable heartbeat",
            )

    def is_enabled(self, *, write_root: Path) -> bool:
        _ = write_root
        try:
            completed = subprocess.run(
                ["systemctl", "--user", "is-enabled", SYSTEMD_TIMER_NAME],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        state = (completed.stdout or "").strip()
        return completed.returncode == 0 and state in {"enabled", "static", "indirect"}


@dataclass(frozen=True)
class LaunchdSupervisor:
    """macOS: ``launchctl bootstrap`` / ``enable`` for ``dev.krellbot.tick``,
    and for ``dev.krellbot.heartbeat`` when it is installed."""

    def enable(self, *, write_root: Path) -> None:
        agents = Path(write_root) / "Library" / "LaunchAgents"
        plist = agents / LAUNCHD_FILENAME
        if not plist.is_file():
            raise RuntimeError(f"unit not installed: {plist}")
        domain = _launchd_domain()
        _launchd_load(domain, plist, LAUNCHD_LABEL)
        heartbeat = agents / HEARTBEAT_LAUNCHD_FILENAME
        if heartbeat.is_file():
            _launchd_load(domain, heartbeat, HEARTBEAT_LAUNCHD_LABEL)

    def is_enabled(self, *, write_root: Path) -> bool:
        _ = write_root
        domain = _launchd_domain()
        try:
            completed = subprocess.run(
                ["launchctl", "print", f"{domain}/{LAUNCHD_LABEL}"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0


def _launchd_load(domain: str, plist: Path, label: str) -> None:
    """Bootstrap and enable one launchd job."""
    # bootstrap may fail if already loaded; enable + kickstart cover that.
    try:
        _run_checked(
            ["launchctl", "bootstrap", domain, str(plist)],
            label="launchctl bootstrap",
        )
    except RuntimeError:
        pass
    _run_checked(
        ["launchctl", "enable", f"{domain}/{label}"],
        label="launchctl enable",
    )


def _launchd_domain() -> str:
    """Return ``gui/<uid>`` for the current user (best-effort)."""
    try:
        import os

        return f"gui/{os.getuid()}"
    except AttributeError:
        return "gui/501"


@dataclass(frozen=True)
class WindowsTaskSupervisor:
    """Windows: register/enable the ``\\krellbot\\tick`` scheduled task, and
    ``\\krellbot\\heartbeat`` when it is installed."""

    def enable(self, *, write_root: Path) -> None:
        tasks = Path(write_root) / "Tasks"
        xml_path = tasks / WINDOWS_TASK_FILENAME
        if not xml_path.is_file():
            raise RuntimeError(f"unit not installed: {xml_path}")
        _schtasks_register(WINDOWS_TASK_NAME, xml_path)
        heartbeat = tasks / HEARTBEAT_WINDOWS_TASK_FILENAME
        if heartbeat.is_file():
            _schtasks_register(HEARTBEAT_WINDOWS_TASK_NAME, heartbeat)

    def is_enabled(self, *, write_root: Path) -> bool:
        _ = write_root
        try:
            completed = subprocess.run(
                ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0


def _schtasks_register(task_name: str, xml_path: Path) -> None:
    """Create (or replace) and enable one scheduled task from its XML."""
    _run_checked(
        [
            "schtasks",
            "/Create",
            "/TN",
            task_name,
            "/XML",
            str(xml_path),
            "/F",
        ],
        label="schtasks create",
    )
    _run_checked(
        ["schtasks", "/Change", "/TN", task_name, "/ENABLE"],
        label="schtasks enable",
    )


def platform_supervisor() -> PlatformSupervisor:
    """Return the production supervisor for ``sys.platform``."""
    if sys.platform == "darwin":
        return LaunchdSupervisor()
    if sys.platform.startswith("linux"):
        return SystemdUserSupervisor()
    if sys.platform == "win32":
        return WindowsTaskSupervisor()
    return UnavailableSupervisor(reason=f"unsupported platform: {sys.platform}")


def scheduler_status(
    home: Path,
    *,
    write_root: Path,
    supervisor: PlatformSupervisor,
) -> dict[str, object]:
    """Closed-shape scheduler status for the loopback API."""
    installed = units_installed(write_root)
    enabled = False
    if installed:
        try:
            enabled = bool(supervisor.is_enabled(write_root=Path(write_root)))
        except Exception:  # noqa: BLE001 — status must never raise
            enabled = False
    return {
        "schema_version": SCHEMA_VERSION,
        "installed": installed,
        "enabled": enabled,
        "last_error": get_last_error(home),
    }


def install_scheduler(
    home: Path,
    *,
    write_root: Path,
    supervisor: PlatformSupervisor,
    executable: str | None = None,
) -> dict[str, object]:
    """Install the tick and heartbeat units and return scheduler status.

    The tick unit comes from ``service.install``; the heartbeat units from
    ``install_heartbeat_units``, once the tick unit is in place. A home
    whose tick unit is already installed is treated as success without
    rewriting it, and gains any missing heartbeat unit. Failures set
    ``last_error`` and leave units alone. Uses a silent install (stdout
    discarded) so the API log stays clean.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from krellbot import service as kb_service

    root = Path(write_root)
    if units_installed(root):
        set_last_error(home, _install_heartbeat(home, write_root=root, executable=executable))
        return scheduler_status(home, write_root=root, supervisor=supervisor)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        rc = kb_service.install(
            home=Path(home),
            executable=executable,
            write_root=root,
            dry_run=False,
        )

    if rc == 0:
        set_last_error(home, _install_heartbeat(home, write_root=root, executable=executable))
    elif rc == 2:
        set_last_error(home, "owner_conflict")
    else:
        detail = stderr_buf.getvalue().strip() or f"install failed (rc={rc})"
        set_last_error(home, detail)

    return scheduler_status(home, write_root=root, supervisor=supervisor)


def _install_heartbeat(home: Path, *, write_root: Path, executable: str | None) -> str | None:
    """Install the heartbeat units; return a ``last_error`` code, or None."""
    try:
        install_heartbeat_units(home, write_root=write_root, executable=executable)
    except (OSError, ValueError):
        return HEARTBEAT_INSTALL_FAILED
    return None


def enable_scheduler(
    home: Path,
    *,
    write_root: Path,
    supervisor: PlatformSupervisor,
) -> dict[str, object]:
    """Enable the installed units via ``supervisor`` and return status.

    The platform supervisors enable the tick and, when installed, the
    heartbeat in one ``enable`` call.
    """
    root = Path(write_root)
    if not units_installed(root):
        set_last_error(home, "not_installed")
        return scheduler_status(home, write_root=root, supervisor=supervisor)

    try:
        supervisor.enable(write_root=root)
    except Exception as exc:  # noqa: BLE001 — surface as last_error
        set_last_error(home, str(exc) or "enable failed")
        return scheduler_status(home, write_root=root, supervisor=supervisor)

    set_last_error(home, None)
    return scheduler_status(home, write_root=root, supervisor=supervisor)
