"""Critical alerts without a timestamp notify once, and "once" means delivered.

``needs_reconcile`` and ``store_fault`` carry ``first_ts = last_ts = 0``.
Deduping on a timestamp that never grows used to mean they never notified.

Pins:

* A zero-time alert notifies once per appearance, however often Operations
  polls; it notifies again when it clears and later reappears.
* An acknowledged zero-time alert stays quiet: the ack holds.
* A send that fails is not recorded as sent. The channel retries at most
  once a minute, and a channel that worked is not sent again.
* The production desktop and SMTP notifiers report failure instead of
  swallowing it, with a fixed failure code (no exception text).
* ``GET /api/v1/notifications/delivery`` serves each channel's last failure
  behind the same session, loopback Origin and Host gate as the other reads.
* A notifier that raises never fails the Operations read.

Every test uses fakes and an isolated home: no desktop bus or SMTP host is
touched and no real notification is sent.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from krellbot import notifications as kb_notifications
from krellbot.application import alerts

TEST_PORT = 8080
BOOT_TOKEN = "boot-critical-alerts"
POLL_SECONDS = 5
T0 = 1_700_000_000


# ---- fixtures on disk -------------------------------------------------------


def _seed_needs_reconcile(home: Path, *, coid: str) -> None:
    from krellbot.storage.database import OperationalStore
    from krellbot.storage.outbox import audit_needs_reconcile

    audit_needs_reconcile(OperationalStore(home / "ops.sqlite"), coid=coid)


def _corrupt_store(home: Path) -> None:
    (home / "ops.sqlite").write_bytes(b"definitely not a sqlite database\n")


def _poll(home: Path, notifier, *, now: int | None = None) -> list[alerts.Alert]:
    collected = alerts.collect(home, now=T0 if now is None else now)
    if now is None:
        kb_notifications.maybe_notify(home, collected, notifier=notifier)
    else:
        kb_notifications.maybe_notify(home, collected, notifier=notifier, now=now)
    return collected


def _sent_for(fake: kb_notifications.FakeNotifier, kind: str) -> list[dict[str, str]]:
    return [row for row in fake.sent if row["title"].endswith(f": {kind}")]


class _RaisingNotifier:
    """A notifier whose backend blows up with an unexpected error."""

    def __init__(self) -> None:
        self.calls = 0

    def notify(self, *, channel, title, body, alert_id) -> None:
        self.calls += 1
        raise RuntimeError("backend exploded with /secret/path in the message")


# ---- ASGI helpers (same idiom as test_opt_in_notifications) -----------------


def _asgi_call(app, *, method: str, path: str, body: bytes = b"", headers=None):
    headers = list(headers or [])
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    captured: dict[str, object] = {"status": 500, "headers": [], "body": bytearray()}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = int(message["status"])
            captured["headers"] = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            captured["body"] = captured["body"] + message.get("body", b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "server": ("127.0.0.1", 0),
        "client": ("127.0.0.1", 12345),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    flat = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in captured["headers"]]
    cookies = [v for k, v in flat if k == "set-cookie"]
    return int(captured["status"]), dict(flat), bytes(captured["body"]), cookies


def _app(home: Path, notifier):
    from krellbot.api.app import create_app

    app = create_app(home=home, port=TEST_PORT, bootstrap_token=BOOT_TOKEN)
    app.state.krellbot.alert_notifier = notifier
    return app


def _session(app) -> list[tuple[str, str]]:
    body = json.dumps({"token": BOOT_TOKEN}).encode("utf-8")
    status, _h, raw, cookies = _asgi_call(
        app,
        method="POST",
        path="/api/v1/session/bootstrap",
        body=body,
        headers=[
            ("Host", f"127.0.0.1:{TEST_PORT}"),
            ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ],
    )
    assert status == 200, raw
    cookie = next(c for c in cookies if c.startswith("krellbot_session="))
    return [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
        ("Cookie", cookie.split(";", 1)[0]),
    ]


def _get(app, path: str, headers):
    return _asgi_call(app, method="GET", path=path, headers=headers)


# ---- zero-time alerts notify once per appearance ------------------------------


def test_needs_reconcile_notifies_once_across_polls(home: Path) -> None:
    _seed_needs_reconcile(home, coid="stuck-coid-1")
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()

    for _ in range(4):
        _poll(home, fake)

    sent = _sent_for(fake, "needs_reconcile")
    assert len(sent) == 1, fake.sent
    assert "code=stuck-coid-1" in sent[0]["body"]


def test_store_fault_notifies_once_across_polls(home: Path) -> None:
    _corrupt_store(home)
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()

    for _ in range(4):
        _poll(home, fake)

    assert len(_sent_for(fake, "store_fault")) == 1, fake.sent


def test_zero_time_alert_notifies_again_when_it_reappears(home: Path) -> None:
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()

    _corrupt_store(home)
    _poll(home, fake)
    _poll(home, fake)
    assert len(_sent_for(fake, "store_fault")) == 1

    (home / "ops.sqlite").unlink()
    assert [a for a in _poll(home, fake) if a.kind == "store_fault"] == []

    _corrupt_store(home)
    _poll(home, fake)
    _poll(home, fake)
    assert len(_sent_for(fake, "store_fault")) == 2, fake.sent


def test_acknowledged_zero_time_alert_stays_quiet(home: Path) -> None:
    _seed_needs_reconcile(home, coid="stuck-coid-2")
    alert = next(a for a in alerts.collect(home, now=T0) if a.kind == "needs_reconcile")
    alerts.acknowledge(home, alert.id, now=T0)

    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()
    for _ in range(3):
        collected = _poll(home, fake)
        assert next(a for a in collected if a.id == alert.id).acknowledged is True
    assert fake.sent == []


# ---- "once" means one real delivery -----------------------------------------


def test_failed_send_is_not_sent_and_retries_at_most_once_a_minute(home: Path) -> None:
    _seed_needs_reconcile(home, coid="stuck-coid-3")
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier(failures={"desktop": "desktop_command_missing"})

    for offset in (0, POLL_SECONDS, 30, 59):
        _poll(home, fake, now=T0 + offset)
    assert fake.sent == []
    assert len(fake.attempts) == 1, fake.attempts

    _poll(home, fake, now=T0 + 60)
    assert len(fake.attempts) == 2, fake.attempts
    assert fake.sent == []

    # The desktop recovers: the next retry delivers, and that is the only send.
    fake.failures.clear()
    for offset in (65, 119, 120, 125, 300):
        _poll(home, fake, now=T0 + offset)
    assert len(fake.attempts) == 3, fake.attempts
    assert len(_sent_for(fake, "needs_reconcile")) == 1, fake.sent


def test_a_failing_channel_does_not_resend_the_channel_that_worked(home: Path) -> None:
    _seed_needs_reconcile(home, coid="stuck-coid-4")
    kb_notifications.update_settings(
        home,
        {
            "desktop_enabled": True,
            "email_enabled": True,
            "email": {"to": "ops@example.com", "smtp_host": "smtp.example.com"},
        },
    )
    fake = kb_notifications.FakeNotifier(failures={"email": "smtp_unreachable"})

    _poll(home, fake, now=T0)
    _poll(home, fake, now=T0 + 60)
    assert [row["channel"] for row in fake.sent] == ["desktop"]

    fake.failures.clear()
    _poll(home, fake, now=T0 + 120)
    _poll(home, fake, now=T0 + 180)
    assert [row["channel"] for row in fake.sent] == ["desktop", "email"]


def test_production_smtp_failure_is_reported_not_swallowed(home: Path, monkeypatch) -> None:
    class _RefusingSMTP:
        def __init__(self, *args, **kwargs) -> None:
            raise ConnectionRefusedError("refused")

    monkeypatch.setattr(kb_notifications.smtplib, "SMTP", _RefusingSMTP)
    monkeypatch.setenv(kb_notifications.SMTP_PASSWORD_ENV, "hunter2")
    email = {"to": "ops@example.com", "smtp_host": "smtp.invalid", "smtp_port": 587}

    with pytest.raises(kb_notifications.NotificationFailed) as raised:
        kb_notifications.EmailSmtpNotifier(email).notify(channel="email", title="t", body="b", alert_id="a1")
    assert raised.value.code == "smtp_unreachable"

    _seed_needs_reconcile(home, coid="stuck-coid-5")
    kb_notifications.update_settings(home, {"email_enabled": True, "email": email})
    collected = alerts.collect(home, now=T0)
    assert kb_notifications.maybe_notify(home, collected, now=T0) == 0

    status = kb_notifications.delivery_status(home)
    failure = status["channels"]["email"]["last_failure"]
    assert failure["code"] == "smtp_unreachable", status
    assert failure["ts"] == T0
    assert status["channels"]["email"]["last_sent_ts"] is None
    assert "hunter2" not in json.dumps(status)


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (FileNotFoundError("notify-send"), "desktop_command_missing"),
        (subprocess.CompletedProcess(args=["notify-send"], returncode=1), "desktop_command_failed"),
        (subprocess.TimeoutExpired(cmd="notify-send", timeout=5), "desktop_timeout"),
    ],
)
def test_production_desktop_failure_is_reported_not_swallowed(monkeypatch, outcome, code) -> None:
    def _fake_run(*args, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(kb_notifications.subprocess, "run", _fake_run)
    monkeypatch.setattr(kb_notifications.sys, "platform", "linux")

    with pytest.raises(kb_notifications.NotificationFailed) as raised:
        kb_notifications.DesktopLocalNotifier().notify(channel="desktop", title="t", body="b", alert_id="a1")
    assert raised.value.code == code


# ---- delivery route ----------------------------------------------------------


def test_delivery_route_serves_each_channel_last_failure(home: Path, monkeypatch) -> None:
    _seed_needs_reconcile(home, coid="stuck-coid-6")
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier(failures={"desktop": "desktop_command_missing"})
    app = _app(home, fake)
    auth = _session(app)
    monkeypatch.setattr("krellbot.api.app._now_seconds", lambda: T0)

    status, _h, body, _c = _get(app, "/api/v1/notifications/delivery", auth)
    assert status == 200, body
    before = json.loads(body)
    assert before["channels"]["desktop"]["last_failure"] is None
    assert before["channels"]["email"]["last_failure"] is None

    status, _h, body, _c = _get(app, "/api/v1/operations", auth)
    assert status == 200, body

    status, headers, body, _c = _get(app, "/api/v1/notifications/delivery", auth)
    assert status == 200, body
    assert headers.get("cache-control") == "no-store"
    payload = json.loads(body)
    assert set(payload["channels"]) == {"desktop", "email"}
    desktop = payload["channels"]["desktop"]
    assert desktop["enabled"] is True
    assert desktop["last_sent_ts"] is None
    assert desktop["pending"] == 1
    assert desktop["last_failure"]["ts"] == T0
    assert desktop["last_failure"]["code"] == "desktop_command_missing"
    assert desktop["last_failure"]["kind"] == "needs_reconcile"
    assert isinstance(desktop["last_failure"]["message"], str) and desktop["last_failure"]["message"]
    email = payload["channels"]["email"]
    assert email["enabled"] is False
    assert email["last_failure"] is None
    assert payload["retry_seconds"] == 60


def test_delivery_route_requires_session_and_loopback_origin(home: Path) -> None:
    app = _app(home, kb_notifications.FakeNotifier())
    auth = _session(app)
    host = [("Host", f"127.0.0.1:{TEST_PORT}")]

    status, _h, _b, _c = _get(app, "/api/v1/notifications/delivery", host)
    assert status == 403

    invented = host + [("Cookie", "krellbot_session=invented")]
    status, _h, _b, _c = _get(app, "/api/v1/notifications/delivery", invented)
    assert status == 403

    foreign = [h for h in auth if h[0] != "Origin"] + [("Origin", "https://evil.example")]
    status, _h, _b, _c = _get(app, "/api/v1/notifications/delivery", foreign)
    assert status == 403

    foreign_host = [h for h in auth if h[0] != "Host"] + [("Host", "evil.example")]
    status, _h, _b, _c = _get(app, "/api/v1/notifications/delivery", foreign_host)
    assert status == 403

    status, _h, _b, _c = _get(app, "/api/v1/notifications/delivery", auth)
    assert status == 200


def test_raising_notifier_never_fails_the_operations_read(home: Path, monkeypatch) -> None:
    _seed_needs_reconcile(home, coid="stuck-coid-7")
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    notifier = _RaisingNotifier()
    app = _app(home, notifier)
    auth = _session(app)
    monkeypatch.setattr("krellbot.api.app._now_seconds", lambda: T0)

    status, _h, body, _c = _get(app, "/api/v1/operations", auth)
    assert status == 200, body
    assert any(a["kind"] == "needs_reconcile" for a in json.loads(body)["alerts"])
    assert notifier.calls == 1

    status, _h, body, _c = _get(app, "/api/v1/notifications/delivery", auth)
    assert status == 200, body
    assert b"/secret/path" not in body
    failure = json.loads(body)["channels"]["desktop"]["last_failure"]
    assert failure["code"] == "notifier_error"
