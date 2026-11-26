"""Opt-in desktop and email notifications for Operations alerts.

Notifications are off by default. The operator enables desktop and/or
email from the Settings UI; acknowledgement of alerts remains in-app.
Telemetry is a separate opt-in (see ``krellbot.telemetry``) and is never
toggled by this module.

Desktop is the preferred channel: it needs no external credentials.
Email is optional and never stores an SMTP password in
``notifications.json`` — when email is enabled the password is read
only from the ``KRELLBOT_SMTP_PASSWORD`` environment variable at send
time. No secrets ship in the repository.

Tests inject a ``FakeNotifier`` so no desktop bus or SMTP host is
touched.

Each alert notifies once per occurrence: deliveries dedupe on
``(alert_id, first_ts)`` in ``<home>/run/notifications-sent.json``. An
occurrence ends when its alert clears or is acknowledged, so a zero-time
alert (``needs_reconcile``, ``store_fault``) notifies once per appearance.
A channel is recorded as sent only when its notifier returns; a notifier
reports failure by raising, the failure is kept per channel for
``GET /api/v1/notifications/delivery``, and a failing channel is retried at
most once every ``RETRY_SECONDS``.

Two processes deliver: the workstation on each Operations read and the
scheduled tick after each tick, so alerts arrive without a tab open. They
share the occurrence store, and each pass holds
``<home>/run/notifications-sent.lock`` from reading the store to writing it,
so an occurrence is sent once whichever process sees it first.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Literal, Protocol

from krellbot import paths as kb_paths

SCHEMA_VERSION = "1"
CONFIG_FILE = "notifications.json"
SENT_FILE = "notifications-sent.json"
SENT_LOCK_FILE = "notifications-sent.lock"
SMTP_PASSWORD_ENV = "KRELLBOT_SMTP_PASSWORD"

# The occurrence store's shape. A file without it is the earlier flat
# ``{alert_id: last_ts}`` map, which is read once and upgraded.
STORE_SCHEMA_VERSION = "2"
DELIVERY_SCHEMA_VERSION = "1"

# A channel whose last send failed is not tried again for this long.
RETRY_SECONDS = 60

Channel = Literal["desktop", "email"]
CHANNELS: tuple[Channel, ...] = ("desktop", "email")

_SENT = "sent"
_PENDING = "pending"

# Fixed copy per failure code. Exception text never reaches the API: it can
# carry paths, server banners or addresses.
FAILURE_MESSAGES: dict[str, str] = {
    "desktop_unsupported": "Desktop notifications are not supported on this platform.",
    "desktop_command_missing": "The desktop notification command (notify-send or osascript) was not found.",
    "desktop_command_failed": "The desktop notification command exited with an error.",
    "desktop_timeout": "The desktop notification command timed out.",
    "email_not_configured": "Email needs an SMTP host and a To address.",
    "smtp_unreachable": "Could not connect to the SMTP server.",
    "smtp_auth_failed": f"The SMTP server refused the login. Check {SMTP_PASSWORD_ENV}.",
    "smtp_rejected": "The SMTP server refused the sender, a recipient or the message.",
    "smtp_error": "The SMTP server returned an error.",
    "notifier_error": "The notifier failed unexpectedly.",
}
_UNKNOWN_FAILURE_MESSAGE = "The notification was not delivered."
_FAILURE_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")


class NotificationFailed(Exception):
    """A notifier could not deliver. ``code`` is a fixed, secret-free label."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Notifier(Protocol):
    """Deliver one alert notification on a single channel.

    Returning means delivered. A notifier that could not deliver raises,
    preferably :class:`NotificationFailed` with a code from
    ``FAILURE_MESSAGES``.
    """

    def notify(
        self,
        *,
        channel: Channel,
        title: str,
        body: str,
        alert_id: str,
    ) -> None: ...


@dataclass
class FakeNotifier:
    """Test double that records every notify call and never egresses.

    ``attempts`` records every call; ``sent`` only the calls that succeeded.
    A channel listed in ``failures`` raises :class:`NotificationFailed` with
    the mapped code.
    """

    sent: list[dict[str, str]] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    attempts: list[dict[str, str]] = field(default_factory=list)

    def notify(
        self,
        *,
        channel: Channel,
        title: str,
        body: str,
        alert_id: str,
    ) -> None:
        row = {
            "channel": channel,
            "title": title,
            "body": body,
            "alert_id": alert_id,
        }
        self.attempts.append(dict(row))
        code = self.failures.get(channel)
        if code:
            raise NotificationFailed(code)
        self.sent.append(row)


def _config_path(home: Path) -> Path:
    return Path(home) / CONFIG_FILE


def _sent_path(home: Path) -> Path:
    return Path(home) / "run" / SENT_FILE


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kb_paths.atomic_write(path, (json.dumps(data, sort_keys=True) + "\n").encode("utf-8"))


def _normalize_email(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raw = {}
    port_raw = raw.get("smtp_port", 587)
    try:
        port = int(port_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        port = 587
    if port <= 0 or port > 65535:
        port = 587
    return {
        "to": str(raw.get("to") or ""),
        "from_addr": str(raw.get("from_addr") or ""),
        "smtp_host": str(raw.get("smtp_host") or ""),
        "smtp_port": port,
    }


def _load(home: Path) -> dict:
    data = _load_json(_config_path(home))
    return {
        "desktop_enabled": bool(data.get("desktop_enabled")),
        "email_enabled": bool(data.get("email_enabled")),
        "email": _normalize_email(data.get("email")),
    }


def _save(home: Path, data: dict) -> None:
    payload = {
        "desktop_enabled": bool(data.get("desktop_enabled")),
        "email_enabled": bool(data.get("email_enabled")),
        "email": _normalize_email(data.get("email")),
    }
    _save_json(_config_path(home), payload)


def email_is_configured(email: dict[str, object]) -> bool:
    """True when SMTP host and destination address are both set."""
    return bool(str(email.get("to") or "").strip() and str(email.get("smtp_host") or "").strip())


def get_settings(home: Path) -> dict:
    """Closed-shape settings for the Settings UI / API (no secrets)."""
    data = _load(home)
    email = data["email"]
    assert isinstance(email, dict)
    return {
        "schema_version": SCHEMA_VERSION,
        "desktop_enabled": bool(data["desktop_enabled"]),
        "email_enabled": bool(data["email_enabled"]),
        "email_configured": email_is_configured(email),
        "email": {
            "to": str(email["to"]),
            "from_addr": str(email["from_addr"]),
            "smtp_host": str(email["smtp_host"]),
            "smtp_port": int(email["smtp_port"]),  # type: ignore[arg-type]
        },
        "smtp_password_env": SMTP_PASSWORD_ENV,
        "telemetry_separate": True,
    }


def update_settings(home: Path, patch: dict) -> dict:
    """Apply a settings patch. Unknown keys are ignored. Never stores a password."""

    if not isinstance(patch, dict):
        raise TypeError("settings patch must be an object")
    data = _load(home)
    if "desktop_enabled" in patch:
        data["desktop_enabled"] = bool(patch["desktop_enabled"])
    if "email_enabled" in patch:
        data["email_enabled"] = bool(patch["email_enabled"])
    if "email" in patch:
        email_patch = patch["email"]
        if email_patch is None:
            data["email"] = _normalize_email({})
        elif isinstance(email_patch, dict):
            # Reject password-shaped fields so secrets never land on disk.
            forbidden = {"password", "smtp_password", "secret", "api_key"}
            if forbidden.intersection(email_patch):
                raise ValueError(f"email password must come from ${SMTP_PASSWORD_ENV}, not settings")
            merged = dict(data["email"])  # type: ignore[arg-type]
            for key in ("to", "from_addr", "smtp_host", "smtp_port"):
                if key in email_patch:
                    merged[key] = email_patch[key]
            data["email"] = _normalize_email(merged)
        else:
            raise TypeError("email must be an object")
    _save(home, data)
    return get_settings(home)


def desktop_enabled(home: Path) -> bool:
    return bool(_load(home)["desktop_enabled"])


def email_enabled(home: Path) -> bool:
    return bool(_load(home)["email_enabled"])


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _empty_channel_state() -> dict:
    return {"last_sent_ts": None, "last_failure": None}


def _normalize_failure(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    ts = _as_int(raw.get("ts"))
    code = raw.get("code")
    if ts is None or not isinstance(code, str) or not _FAILURE_CODE_RE.fullmatch(code):
        return None
    kind = raw.get("kind")
    return {"ts": ts, "code": code, "kind": kind if isinstance(kind, str) else None}


def _normalize_occurrence(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    first_ts = _as_int(raw.get("first_ts"))
    if first_ts is None:
        return None
    channels_raw = raw.get("channels")
    channels: dict[str, str] = {}
    if isinstance(channels_raw, dict):
        for name in CHANNELS:
            state = channels_raw.get(name)
            if state in (_SENT, _PENDING):
                channels[name] = state
    return {"first_ts": first_ts, "channels": channels}


def _load_store(home: Path) -> dict:
    """Read the occurrence store; a missing or corrupt file reads as empty."""

    raw = _load_json(_sent_path(home))
    occurrences: dict[str, dict] = {}
    channels = {name: _empty_channel_state() for name in CHANNELS}
    store = {
        "schema_version": STORE_SCHEMA_VERSION,
        "occurrences": occurrences,
        "channels": channels,
    }
    if raw.get("schema_version") != STORE_SCHEMA_VERSION:
        # The earlier flat map kept the ``last_ts`` each alert was notified at.
        for key, value in raw.items():
            last_ts = _as_int(value)
            if isinstance(key, str) and last_ts is not None:
                occurrences[key] = {"legacy_last_ts": last_ts}
        return store
    occ_raw = raw.get("occurrences")
    if isinstance(occ_raw, dict):
        for key, value in occ_raw.items():
            record = _normalize_occurrence(value)
            if isinstance(key, str) and record is not None:
                occurrences[key] = record
    ch_raw = raw.get("channels")
    if isinstance(ch_raw, dict):
        for name in CHANNELS:
            state = ch_raw.get(name)
            if isinstance(state, dict):
                channels[name] = {
                    "last_sent_ts": _as_int(state.get("last_sent_ts")),
                    "last_failure": _normalize_failure(state.get("last_failure")),
                }
    return store


def _save_store(home: Path, store: dict) -> None:
    _save_json(_sent_path(home), store)


@contextmanager
def _store_lock(home: Path) -> Iterator[bool]:
    """Hold the occurrence store's lock for one read-send-record pass.

    Yields True when held. The lock is never waited for: when another
    process holds it, that process is delivering, so this pass yields False
    and must neither send nor write; what is still pending goes out on a
    later pass. A lock that cannot be taken for any other reason also
    yields False, because sending without it could send an occurrence
    twice. The file stays after release so every contender locks the same
    inode; the kernel releases the lock if its holder exits.
    """

    path = Path(home) / "run" / SENT_LOCK_FILE
    fd: int | None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        fd = None
    if fd is None:
        yield False
        return
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)


def _current_occurrence(record: dict | None, first_ts: int) -> dict | None:
    """Return the stored record when it is this occurrence, else None."""

    if record is None:
        return None
    legacy = record.get("legacy_last_ts")
    if legacy is not None:
        # Notified under the earlier store at or after this occurrence began.
        return {"first_ts": first_ts, "channels": {}} if legacy >= first_ts else None
    return record if record.get("first_ts") == first_ts else None


def _backing_off(state: dict, now: int) -> bool:
    """True while a channel's last failure is newer than its last send and
    less than ``RETRY_SECONDS`` old."""

    failure = state.get("last_failure")
    if failure is None:
        return False
    last_sent = state.get("last_sent_ts")
    if last_sent is not None and last_sent >= failure["ts"]:
        return False
    return now - failure["ts"] < RETRY_SECONDS


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, NotificationFailed) and _FAILURE_CODE_RE.fullmatch(str(exc.code)):
        return str(exc.code)
    return "notifier_error"


def _failure_message(code: str) -> str:
    return FAILURE_MESSAGES.get(code, _UNKNOWN_FAILURE_MESSAGE)


def _alert_title(alert: object) -> str:
    kind = getattr(alert, "kind", None)
    if isinstance(alert, dict):
        kind = alert.get("kind")
    severity = getattr(alert, "severity", None)
    if isinstance(alert, dict):
        severity = alert.get("severity")
    kind_s = str(kind or "alert")
    sev_s = str(severity or "warning")
    return f"Krellbot {sev_s}: {kind_s}"


def _alert_body(alert: object) -> str:
    if isinstance(alert, dict):
        code = alert.get("code")
        venue = alert.get("venue")
        pair = alert.get("pair")
        count = alert.get("count", 1)
        acknowledged = alert.get("acknowledged")
    else:
        code = getattr(alert, "code", None)
        venue = getattr(alert, "venue", None)
        pair = getattr(alert, "pair", None)
        count = getattr(alert, "count", 1)
        acknowledged = getattr(alert, "acknowledged", False)
    parts = [f"count={count}"]
    if code:
        parts.append(f"code={code}")
    if venue:
        parts.append(f"venue={venue}")
    if pair:
        parts.append(f"pair={pair}")
    parts.append("ack=in-app" if not acknowledged else "ack=done")
    return " ".join(parts)


def _alert_id(alert: object) -> str | None:
    if isinstance(alert, dict):
        value = alert.get("id")
    else:
        value = getattr(alert, "id", None)
    return value if isinstance(value, str) and value else None


def _alert_first_ts(alert: object) -> int:
    if isinstance(alert, dict):
        value = alert.get("first_ts", 0)
    else:
        value = getattr(alert, "first_ts", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _alert_kind(alert: object) -> str | None:
    value = alert.get("kind") if isinstance(alert, dict) else getattr(alert, "kind", None)
    return value if isinstance(value, str) and value else None


def _alert_acknowledged(alert: object) -> bool:
    if isinstance(alert, dict):
        return bool(alert.get("acknowledged"))
    return bool(getattr(alert, "acknowledged", False))


class DesktopLocalNotifier:
    """Local desktop notification (notify-send / osascript).

    Raises :class:`NotificationFailed` when nothing was shown: an
    unsupported platform, a missing command, a non-zero exit or a timeout.
    """

    def notify(
        self,
        *,
        channel: Channel,
        title: str,
        body: str,
        alert_id: str,
    ) -> None:
        if channel != "desktop":
            raise NotificationFailed("notifier_error")
        if sys.platform == "darwin":
            script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
            argv = ["osascript", "-e", script]
        elif sys.platform.startswith("linux"):
            argv = ["notify-send", "--app-name=Krellbot", title, body]
        else:
            raise NotificationFailed("desktop_unsupported")
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                timeout=5,
            )
        except FileNotFoundError:
            raise NotificationFailed("desktop_command_missing") from None
        except subprocess.TimeoutExpired:
            raise NotificationFailed("desktop_timeout") from None
        except (OSError, subprocess.SubprocessError):
            raise NotificationFailed("desktop_command_failed") from None
        if completed.returncode != 0:
            raise NotificationFailed("desktop_command_failed")


class EmailSmtpNotifier:
    """SMTP email. Password from ``KRELLBOT_SMTP_PASSWORD`` only.

    Raises :class:`NotificationFailed` when the message was not accepted.
    """

    def __init__(self, email: dict[str, object], *, password: str | None = None) -> None:
        self._email = _normalize_email(email)
        self._password = password if password is not None else os.environ.get(SMTP_PASSWORD_ENV, "")

    def notify(
        self,
        *,
        channel: Channel,
        title: str,
        body: str,
        alert_id: str,
    ) -> None:
        if channel != "email":
            raise NotificationFailed("notifier_error")
        if not email_is_configured(self._email):
            raise NotificationFailed("email_not_configured")
        to_addr = str(self._email["to"]).strip()
        from_addr = str(self._email["from_addr"]).strip() or to_addr
        host = str(self._email["smtp_host"]).strip()
        port = int(self._email["smtp_port"])  # type: ignore[arg-type]
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.set_content(f"{body}\n\nalert_id={alert_id}\n")
        try:
            with smtplib.SMTP(host, port, timeout=10) as smtp:
                smtp.ehlo()
                try:
                    smtp.starttls()
                    smtp.ehlo()
                except smtplib.SMTPException:
                    pass
                if self._password:
                    smtp.login(from_addr, self._password)
                smtp.send_message(msg)
        # SMTPException subclasses OSError, so the SMTP cases come first.
        except smtplib.SMTPAuthenticationError:
            raise NotificationFailed("smtp_auth_failed") from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise NotificationFailed("smtp_rejected") from None
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected):
            raise NotificationFailed("smtp_unreachable") from None
        except smtplib.SMTPException:
            raise NotificationFailed("smtp_error") from None
        except OSError:
            raise NotificationFailed("smtp_unreachable") from None


@dataclass
class CompositeNotifier:
    """Routes channel-tagged notify calls to desktop and email backends."""

    desktop: Notifier
    email: Notifier

    def notify(
        self,
        *,
        channel: Channel,
        title: str,
        body: str,
        alert_id: str,
    ) -> None:
        if channel == "desktop":
            self.desktop.notify(channel=channel, title=title, body=body, alert_id=alert_id)
        elif channel == "email":
            self.email.notify(channel=channel, title=title, body=body, alert_id=alert_id)


def default_notifier(home: Path) -> Notifier:
    """Build the production composite notifier from current settings."""
    data = _load(home)
    email = data["email"]
    assert isinstance(email, dict)
    return CompositeNotifier(
        desktop=DesktopLocalNotifier(),
        email=EmailSmtpNotifier(email),
    )


def _ready_channels(data: dict) -> dict[str, bool]:
    email_cfg = data["email"]
    assert isinstance(email_cfg, dict)
    return {
        "desktop": bool(data["desktop_enabled"]),
        "email": bool(data["email_enabled"]) and email_is_configured(email_cfg),
    }


def delivery_enabled(home: Path) -> bool:
    """True when the operator has opted in to at least one ready channel.

    Reads only the settings file, so a caller can skip collecting alerts
    when nothing would be sent.
    """

    return any(_ready_channels(_load(home)).values())


def maybe_notify(
    home: Path,
    alerts: list,
    *,
    notifier: Notifier | None = None,
    now: int | None = None,
) -> int:
    """Notify each unacknowledged alert occurrence once on enabled channels.

    ``alerts`` is the full current alert list (``alerts.collect``). An
    occurrence is ``(alert_id, first_ts)``; it is notified once per
    channel that was enabled when it was first seen. It ends when the alert
    leaves the list or is acknowledged, so a later appearance or an event
    that un-acknowledges it notifies again, and an ack holds.

    A channel is recorded as sent only when the notifier returns. A failed
    channel keeps its occurrences pending, records its last failure, and is
    retried at most once every ``RETRY_SECONDS``. A notifier exception never
    propagates. When both channels are disabled nothing is read, written or
    called. Returns the number of successful deliveries.

    The workstation and the scheduled tick both call this. The pass holds the
    store's lock from reading it to writing it; when the other process holds
    it, this pass sends and writes nothing and returns 0.
    """

    data = _load(home)
    ready = _ready_channels(data)
    enabled = [name for name in CHANNELS if ready[name]]
    if not enabled:
        return 0

    with _store_lock(home) as held:
        if not held:
            return 0
        return _notify_locked(home, alerts, enabled=enabled, notifier=notifier, now=now)


def _notify_locked(
    home: Path,
    alerts: list,
    *,
    enabled: list[Channel],
    notifier: Notifier | None,
    now: int | None,
) -> int:
    """One ``maybe_notify`` pass. The caller holds the store's lock."""

    now_s = int(time.time()) if now is None else int(now)
    active = notifier if notifier is not None else default_notifier(home)
    store = _load_store(home)
    occurrences: dict[str, dict] = store["occurrences"]
    channels: dict[str, dict] = store["channels"]
    waiting = {name for name in enabled if _backing_off(channels[name], now_s)}
    open_ids: set[str] = set()
    deliveries = 0
    dirty = False

    for alert in alerts:
        alert_id = _alert_id(alert)
        if alert_id is None or _alert_acknowledged(alert):
            continue
        open_ids.add(alert_id)
        stored = occurrences.get(alert_id)
        record = _current_occurrence(stored, _alert_first_ts(alert))
        if record is None:
            record = {
                "first_ts": _alert_first_ts(alert),
                "channels": {name: _PENDING for name in enabled},
            }
        if record is not stored:
            occurrences[alert_id] = record
            dirty = True
        for channel in enabled:
            if record["channels"].get(channel) != _PENDING or channel in waiting:
                continue
            try:
                active.notify(
                    channel=channel,
                    title=_alert_title(alert),
                    body=_alert_body(alert),
                    alert_id=alert_id,
                )
            except Exception as exc:  # noqa: BLE001 — a notifier never fails the caller
                channels[channel]["last_failure"] = {
                    "ts": now_s,
                    "code": _failure_code(exc),
                    "kind": _alert_kind(alert),
                }
                waiting.add(channel)
            else:
                record["channels"][channel] = _SENT
                channels[channel]["last_sent_ts"] = now_s
                deliveries += 1
            dirty = True

    for alert_id in [key for key in occurrences if key not in open_ids]:
        del occurrences[alert_id]
        dirty = True

    if dirty:
        _save_store(home, store)
    return deliveries


def delivery_status(home: Path) -> dict:
    """Closed-shape per-channel delivery record for the API (no secrets).

    Each channel reports whether it is enabled and ready, when it last
    delivered, how many open occurrences still wait for it, and its last
    failure with a fixed message. Never creates a file.
    """

    ready = _ready_channels(_load(home))
    store = _load_store(home)
    out: dict[str, dict] = {}
    for name in CHANNELS:
        state = store["channels"][name]
        failure = state["last_failure"]
        pending = sum(1 for record in store["occurrences"].values() if record.get("channels", {}).get(name) == _PENDING)
        out[name] = {
            "enabled": ready[name],
            "last_sent_ts": state["last_sent_ts"],
            "pending": pending,
            "last_failure": None
            if failure is None
            else {
                "ts": failure["ts"],
                "code": failure["code"],
                "message": _failure_message(failure["code"]),
                "kind": failure["kind"],
            },
        }
    return {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "retry_seconds": RETRY_SECONDS,
        "channels": out,
    }
