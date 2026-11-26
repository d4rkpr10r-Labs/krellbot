"""The scheduler heartbeat reports missed ticks without running a tick.

M28.19 delivers alerts after each scheduled tick, so a tick that never runs
reports nothing. ``krellbot service heartbeat`` is a second scheduler entry
with its own units, fired every 15 minutes. It reads only scheduler and
journal state (the scheduler's ``last_error``, the armed venues and the
journal) and sends ``scheduler_stale`` and ``tick_failure`` through M28.1's
occurrence store and notifiers.

Pins:

* With no tick and no workstation running, a venue whose last tick passes
  the stale threshold notifies once, at the first check past it, under the
  id ``alerts.collect`` gives it. A durable scheduler ``last_error``
  notifies ``tick_failure`` once.
* The heartbeat and the full passes (the tick and the workstation) share
  each occurrence, and the heartbeat never ends an occurrence it did not
  collect, so nothing is sent twice.
* The heartbeat respects the notifications lock: while another process
  holds it, the heartbeat sends and writes nothing.
* It never fetches candles, builds venues, reads keys, ticks or opens a
  socket. With every channel off it reads no alert and writes nothing.
* A raising notifier never crashes it; the failure is recorded.
* It cannot report a powered-off or sleeping computer, and it does not
  report on wake the ticks missed while the computer was off: the tick
  catches up first. A tick that still does not run is reported 15 minutes
  later.
* Its units fire ``service heartbeat`` every 15 minutes; the tick unit
  bytes are unchanged. The web scheduler install installs both, enable
  enables both and ``service uninstall`` removes both.

Every test uses an isolated home, a fake clock and a fake notifier: no
venue, desktop bus, SMTP host or OS scheduler is touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import plistlib
import socket
import sys
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot import notifications as kb_notifications
from krellbot.application import alerts
from krellbot.service import supervise
from krellbot.service.supervise import FakeSupervisor

TEST_PORT = 8080
BOOT_TOKEN = "boot-heartbeat-check"
EXECUTABLE = "/usr/local/bin/krellbot"
QUARTER = 15 * 60
HOUR = 3600
# The venue's last tick; every check below is dated from it.
LAST_TICK = 1_800_000_000
STALE_TITLE = "Krellbot warning: scheduler_stale"
STALE_BODY = "count=1 code=tick_stale venue=kraken ack=in-app"
FAILURE_TITLE = "Krellbot critical: tick_failure"
REFUSAL_TITLE = "Krellbot warning: live_refused"
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---- fixtures on disk -------------------------------------------------------


def _arm_paper(home: Path) -> None:
    from krellbot.config import ArmedPack, Config, save_config

    body = {
        "schema_version": 1,
        "id": "heartbeat-check",
        "version": "1.0.0",
        "label": "Heartbeat check",
        "author": "krellbot heartbeat tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 50}},
        "markets": [{"venue": "kraken", "pair": "SUIUSD"}],
    }
    pack_path = home / "heartbeat-check.json"
    pack_path.write_text(json.dumps(body), encoding="utf-8")
    save_config(
        home,
        Config(
            armed=[
                ArmedPack(
                    pack_path=str(pack_path),
                    pack_sha256="0" * 64,
                    pack_id="heartbeat-check",
                    pack_version="1.0.0",
                    venue="kraken",
                    pair="SUIUSD",
                    cap=Decimal(100),
                    stop=Decimal(5),
                    mode="paper",
                    starting_cash=Decimal(1000),
                    requires_license=False,
                    armed_at_ts=1,
                )
            ]
        ),
    )


def _journal(home: Path, record: dict) -> None:
    journal = home / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    with (journal / "2027-01.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _append_tick(home: Path, ts: int) -> None:
    """A tick the scheduler ran for kraken (no candle read, so no feed alert)."""

    _journal(
        home,
        {
            "ts": ts,
            "kind": "tick",
            "venue": "kraken",
            "pack": "heartbeat-check",
            "bar_ts": 0,
            "detail": {"reason": "no_candles", "pair": "SUIUSD"},
        },
    )


def _append_refusal(home: Path) -> None:
    """An open alert of a kind the heartbeat does not check."""

    _journal(
        home,
        {
            "ts": LAST_TICK - HOUR,
            "kind": "tick",
            "venue": "coinbase",
            "pack": "other-pack",
            "bar_ts": 0,
            "detail": {"reason": "live_refused", "code": "live_disabled", "pair": "BTCUSD"},
        },
    )


def _tick_records(home: Path) -> int:
    count = 0
    for path in sorted((home / "journal").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("kind") == "tick":
                count += 1
    return count


def _files(home: Path) -> set[str]:
    return {p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()}


def _titles(rows: list[dict[str, str]]) -> list[tuple[str, str]]:
    return [(row["channel"], row["title"]) for row in rows]


def _alert_id(home: Path, kind: str, now: int) -> str:
    found = [a for a in alerts.collect(home, now=now) if a.kind == kind]
    assert len(found) == 1, found
    return found[0].id


@pytest.fixture
def notifier(monkeypatch) -> kb_notifications.FakeNotifier:
    """The notifier production would build; never a desktop bus or SMTP host."""

    fake = kb_notifications.FakeNotifier()
    monkeypatch.setattr(kb_notifications, "default_notifier", lambda home: fake)
    return fake


def _heartbeat(monkeypatch, now: int) -> int:
    """Run the check exactly as its scheduler unit does, at a fake ``now``."""

    from krellbot import cli

    monkeypatch.setattr("krellbot.cli._now_seconds_tick", lambda: float(now))
    return cli.main(["krellbot", "service", "heartbeat"])


def _checks(monkeypatch, *, start: int, end: int) -> None:
    """One check every 15 minutes from ``start`` to ``end`` inclusive."""

    now = start
    while now <= end:
        assert _heartbeat(monkeypatch, now) == 0
        now += QUARTER


def _full_pass(home: Path, fake: kb_notifications.FakeNotifier, now: int) -> int:
    """The pass the tick and the workstation run: every alert, ends occurrences."""

    return kb_notifications.maybe_notify(home, alerts.collect(home, now=now), notifier=fake, now=now)


# ---- reporting what the tick cannot -------------------------------------------


def test_a_missed_tick_notifies_once_from_the_heartbeat_with_no_tick_or_workstation(
    home: Path, fresh_keyring, monkeypatch, notifier
) -> None:
    _arm_paper(home)
    _append_tick(home, LAST_TICK)
    kb_notifications.update_settings(home, {"desktop_enabled": True})

    sent_at: list[int] = []
    for k in range(1, 13):
        now = LAST_TICK + k * QUARTER
        assert _heartbeat(monkeypatch, now) == 0
        if notifier.sent and not sent_at:
            sent_at.append(now)

    # Sent at the first check past the stale threshold, and only then.
    assert sent_at == [LAST_TICK + 2 * HOUR], sent_at
    assert notifier.sent == [
        {
            "channel": "desktop",
            "title": STALE_TITLE,
            "body": STALE_BODY,
            "alert_id": _alert_id(home, alerts.KIND_SCHEDULER_STALE, LAST_TICK + 3 * HOUR),
        }
    ], notifier.sent
    # The heartbeat ticked nothing.
    assert _tick_records(home) == 1
    desktop = kb_notifications.delivery_status(home)["channels"]["desktop"]
    assert desktop["last_sent_ts"] == LAST_TICK + 2 * HOUR, desktop
    assert desktop["pending"] == 0, desktop


def test_a_scheduler_error_notifies_tick_failure_once(home: Path, fresh_keyring, monkeypatch, notifier) -> None:
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    supervise.set_last_error(home, "not_installed")
    written_at = LAST_TICK + 100
    os.utime(supervise.last_error_path(home), (written_at, written_at))

    # A scheduler error needs no earlier check: the first one reports it.
    assert _heartbeat(monkeypatch, LAST_TICK + QUARTER) == 0
    assert _heartbeat(monkeypatch, LAST_TICK + 2 * QUARTER) == 0

    assert notifier.sent == [
        {
            "channel": "desktop",
            "title": FAILURE_TITLE,
            "body": "count=1 code=not_installed ack=in-app",
            "alert_id": _alert_id(home, alerts.KIND_TICK_FAILURE, LAST_TICK + QUARTER),
        }
    ], notifier.sent


def test_the_heartbeat_and_the_full_passes_share_each_occurrence(
    home: Path, fresh_keyring, monkeypatch, notifier
) -> None:
    _arm_paper(home)
    _append_tick(home, LAST_TICK)
    _append_refusal(home)
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    full = kb_notifications.FakeNotifier()

    # The workstation (or the tick) sends the refusal before the tick stops.
    _full_pass(home, full, LAST_TICK + QUARTER)
    assert _titles(full.sent) == [("desktop", REFUSAL_TITLE)], full.sent

    # The heartbeat sends the missed tick, and leaves the refusal recorded.
    _checks(monkeypatch, start=LAST_TICK + QUARTER, end=LAST_TICK + 3 * HOUR)
    assert _titles(notifier.sent) == [("desktop", STALE_TITLE)], notifier.sent

    # A full pass resends neither.
    _full_pass(home, full, LAST_TICK + 3 * HOUR)
    assert _titles(full.sent) == [("desktop", REFUSAL_TITLE)], full.sent

    # The tick catches up, then stops again: this time a full pass sees the
    # new occurrence first, and the heartbeat does not send it again.
    caught_up = LAST_TICK + 3 * HOUR + 60
    _append_tick(home, caught_up)
    _checks(monkeypatch, start=caught_up + QUARTER, end=caught_up + 2 * HOUR - QUARTER)
    assert _full_pass(home, full, caught_up + 2 * HOUR) == 1
    _checks(monkeypatch, start=caught_up + 2 * HOUR, end=caught_up + 3 * HOUR)
    assert _titles(full.sent) == [("desktop", REFUSAL_TITLE), ("desktop", STALE_TITLE)], full.sent
    assert _titles(notifier.sent) == [("desktop", STALE_TITLE)], notifier.sent


def test_the_heartbeat_sends_nothing_while_another_process_holds_the_notifications_lock(
    home: Path, fresh_keyring, monkeypatch, notifier
) -> None:
    _arm_paper(home)
    _append_tick(home, LAST_TICK)
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    sent_store = home / "run" / kb_notifications.SENT_FILE

    with kb_notifications._store_lock(home) as held:
        assert held
        _checks(monkeypatch, start=LAST_TICK + QUARTER, end=LAST_TICK + 2 * HOUR)
        assert notifier.attempts == [], notifier.attempts
        assert not sent_store.exists()

    # Once the lock is free, the next check sends it.
    assert _heartbeat(monkeypatch, LAST_TICK + 2 * HOUR + QUARTER) == 0
    assert _titles(notifier.sent) == [("desktop", STALE_TITLE)], notifier.sent


def test_the_heartbeat_never_fetches_builds_venues_reads_keys_ticks_or_connects(
    home: Path, fresh_keyring, monkeypatch, notifier
) -> None:
    import krellbot.cli as kb_cli
    import krellbot.run as kb_run
    import krellbot.secrets as kb_secrets
    from krellbot.venues.coinbase import CoinbaseVenue
    from krellbot.venues.kraken import KrakenVenue
    from krellbot.venues.paper import PaperVenue

    _arm_paper(home)
    _append_tick(home, LAST_TICK)
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    # A legacy key file that any key read or migration would move.
    legacy = json.dumps({"kraken_key": "CANARY-key", "kraken_secret": "CANARY-secret"}).encode("utf-8")
    (home / "state.json").write_bytes(legacy)
    journal_before = (home / "journal" / "2027-01.jsonl").read_bytes()
    files_before = _files(home)

    tripped: list[str] = []

    def tripwire(name: str):
        def trip(*args, **kwargs):
            tripped.append(name)
            raise AssertionError(f"heartbeat called {name}")

        return trip

    for target, name in [
        (kb_cli, "_default_fetch"),
        (kb_cli, "_default_transport"),
        (kb_cli, "venue_for_tick"),
        (kb_cli, "_tick_venues"),
        (kb_cli, "_tick_one_venue"),
        (kb_run, "tick"),
        (kb_secrets, "get"),
        (kb_secrets, "store"),
        (kb_secrets, "migrate_legacy"),
        (PaperVenue, "__init__"),
        (KrakenVenue, "__init__"),
        (CoinbaseVenue, "__init__"),
        (fresh_keyring, "get_password"),
        (fresh_keyring, "set_password"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
    ]:
        monkeypatch.setattr(target, name, tripwire(name))

    _checks(monkeypatch, start=LAST_TICK + QUARTER, end=LAST_TICK + 3 * HOUR)

    assert tripped == [], tripped
    # The check ran: it sent the missed tick.
    assert _titles(notifier.sent) == [("desktop", STALE_TITLE)], notifier.sent
    assert (home / "state.json").read_bytes() == legacy
    assert (home / "journal" / "2027-01.jsonl").read_bytes() == journal_before
    # It wrote only the occurrence store, its lock and its own last check.
    assert _files(home) - files_before == {
        "run/notifications-sent.json",
        "run/notifications-sent.lock",
        "service/last_heartbeat",
    }, _files(home) - files_before


def test_with_every_channel_off_the_heartbeat_reads_no_alert_and_writes_nothing(
    home: Path, fresh_keyring, monkeypatch, notifier
) -> None:
    _arm_paper(home)
    _append_tick(home, LAST_TICK)
    reads: list[str] = []

    def counting(name, real):
        def wrapped(*args, **kwargs):
            reads.append(name)
            return real(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(alerts, "collect", counting("collect", alerts.collect))
    monkeypatch.setattr(alerts, "collect_scheduler", counting("collect_scheduler", alerts.collect_scheduler))
    files_before = _files(home)

    _checks(monkeypatch, start=LAST_TICK + QUARTER, end=LAST_TICK + 3 * HOUR)

    assert reads == [], reads
    assert notifier.attempts == [], notifier.attempts
    assert _files(home) == files_before, _files(home) - files_before
    # The alert exists; only the operator's opt-in is missing.
    assert [a.kind for a in alerts.collect(home, now=LAST_TICK + 3 * HOUR)] == [alerts.KIND_SCHEDULER_STALE]


def test_a_raising_notifier_never_crashes_the_heartbeat(home: Path, fresh_keyring, monkeypatch, capsys) -> None:
    _arm_paper(home)
    _append_tick(home, LAST_TICK)
    kb_notifications.update_settings(
        home,
        {
            "desktop_enabled": True,
            "email_enabled": True,
            "email": {"to": "ops@example.invalid", "smtp_host": "smtp.example.invalid"},
        },
    )
    attempts: list[str] = []

    class _Raising:
        def notify(self, *, channel, title, body, alert_id) -> None:
            attempts.append(channel)
            raise RuntimeError("CANARY notifier down")

    monkeypatch.setattr(kb_notifications, "default_notifier", lambda home: _Raising())

    _checks(monkeypatch, start=LAST_TICK + QUARTER, end=LAST_TICK + 2 * HOUR)

    out = capsys.readouterr()
    assert "CANARY" not in out.out + out.err
    assert "Traceback" not in out.err
    # The one check that found the alert tried each channel once.
    assert sorted(attempts) == ["desktop", "email"], attempts
    status = kb_notifications.delivery_status(home)["channels"]
    for channel in ("desktop", "email"):
        assert status[channel]["last_sent_ts"] is None, status
        assert status[channel]["pending"] == 1, status
        assert status[channel]["last_failure"]["code"] == "notifier_error", status
    assert "CANARY" not in json.dumps(status)


@pytest.mark.parametrize("tick_catches_up", [True, False], ids=["tick_catches_up", "tick_still_missing"])
def test_ticks_missed_while_the_computer_was_off_are_not_reported_on_wake(
    home: Path, fresh_keyring, monkeypatch, notifier, tick_catches_up
) -> None:
    _arm_paper(home)
    _append_tick(home, LAST_TICK)
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    _checks(monkeypatch, start=LAST_TICK + QUARTER, end=LAST_TICK + HOUR - QUARTER)

    # The computer sleeps for eight hours: neither the tick nor the heartbeat
    # runs. On wake both fire; the heartbeat finds the old tick first.
    wake = LAST_TICK + 8 * HOUR
    assert _heartbeat(monkeypatch, wake) == 0
    assert notifier.attempts == [], notifier.attempts

    if tick_catches_up:
        _append_tick(home, wake + 30)
    assert _heartbeat(monkeypatch, wake + QUARTER) == 0

    if tick_catches_up:
        assert notifier.attempts == [], notifier.attempts
    else:
        assert _titles(notifier.sent) == [("desktop", STALE_TITLE)], notifier.sent


# ---- the scheduler entry ----------------------------------------------------------


def test_heartbeat_units_fire_the_check_every_15_minutes(home: Path) -> None:
    from krellbot.service import render

    plist = plistlib.loads(render.render_heartbeat_launchd(EXECUTABLE, home))
    assert plist == {
        "Label": "dev.krellbot.heartbeat",
        "ProgramArguments": [EXECUTABLE, "service", "heartbeat"],
        "StartInterval": QUARTER,
        "StandardOutPath": f"{home}/logs/heartbeat.out.log",
        "StandardErrorPath": f"{home}/logs/heartbeat.err.log",
        "RunAtLoad": False,
        "EnvironmentVariables": {"KRELLBOT_HOME": str(home)},
    }

    units = render.render_heartbeat_systemd(EXECUTABLE, home)
    assert sorted(units) == ["krellbot-heartbeat.service", "krellbot-heartbeat.timer"]
    timer = units["krellbot-heartbeat.timer"].decode("utf-8")
    assert "OnCalendar=*-*-* *:00/15:00" in timer
    assert "Unit=krellbot-heartbeat.service" in timer
    assert "WantedBy=timers.target" in timer
    service = units["krellbot-heartbeat.service"].decode("utf-8")
    assert f"ExecStart={EXECUTABLE} service heartbeat\n" in service
    assert f"Environment=KRELLBOT_HOME={home}\n" in service
    assert "User=" not in service

    windows_exe = r"C:\Users\op\bin\krellbot.exe"
    task = ET.fromstring(render.render_heartbeat_windows(windows_exe, home))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert task.findtext("t:RegistrationInfo/t:URI", namespaces=ns) == "\\krellbot\\heartbeat"
    assert task.findtext("t:Triggers/t:CalendarTrigger/t:Repetition/t:Interval", namespaces=ns) == "PT15M"
    assert task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns) == (
        f'/d /c set "KRELLBOT_HOME={home}" && "{windows_exe}" service heartbeat'
    )
    with pytest.raises(ValueError):
        render.render_heartbeat_windows('C:\\bad"exe', home)


def test_the_tick_unit_bytes_are_unchanged() -> None:
    from krellbot.service import render

    home = Path("/home/op/.krellbot")

    def digest(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    assert digest(render.render_launchd(EXECUTABLE, home)) == (
        "0a14955353ffbf931aae8f52e89df77095515728accc55e74e00d50b95b37087"
    )
    systemd = render.render_systemd(EXECUTABLE, home)
    assert {name: digest(body) for name, body in systemd.items()} == {
        "krellbot-tick.timer": "656457f318333551d23910b2523cb57ec10b359a3e686c8369d6e5ee52b7c473",
        "krellbot-tick.service": "352dd54a6c258ae26f442237dc44ec68755bb901c26f067c6f2b7f47d8131f24",
    }
    assert digest(render.render_windows(EXECUTABLE, home)) == (
        "f6d832096072202becb78c9491dd629905fbbdac2304c29420c8efa03fc80341"
    )


def _units_for_platform(write_root: Path, home: Path, executable: str) -> tuple[dict[Path, bytes], dict[Path, bytes]]:
    """The tick and heartbeat unit files ``sys.platform`` gets, with their bytes."""

    from krellbot.service import render

    if sys.platform == "darwin":
        d = write_root / "Library" / "LaunchAgents"
        return (
            {d / "dev.krellbot.tick.plist": render.render_launchd(executable, home)},
            {d / "dev.krellbot.heartbeat.plist": render.render_heartbeat_launchd(executable, home)},
        )
    if sys.platform.startswith("linux"):
        d = write_root / ".config" / "systemd" / "user"
        return (
            {d / name: body for name, body in render.render_systemd(executable, home).items()},
            {d / name: body for name, body in render.render_heartbeat_systemd(executable, home).items()},
        )
    if sys.platform == "win32":
        d = write_root / "Tasks"
        return (
            {d / "krellbot-tick.xml": render.render_windows(executable, home)},
            {d / "krellbot-heartbeat.xml": render.render_heartbeat_windows(executable, home)},
        )
    raise RuntimeError(f"unsupported test platform: {sys.platform}")


def _asgi(app, *, method: str, path: str, body: bytes = b"", headers=None):
    headers = [("Host", f"127.0.0.1:{TEST_PORT}"), *(headers or [])]
    if body:
        headers.append(("Content-Length", str(len(body))))
        headers.append(("Content-Type", "application/json"))
    sent = {"done": False}
    captured: dict[str, object] = {"status": 500, "headers": [], "body": bytearray()}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

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
    cookies = [v.decode("latin-1") for k, v in captured["headers"] if k.decode("latin-1").lower() == "set-cookie"]
    return int(captured["status"]), bytes(captured["body"]), cookies


def _web_scheduler(home: Path, write_root: Path, supervisor: FakeSupervisor):
    """The workstation with its scheduler routes pointed at a fake OS."""

    from krellbot.api.app import create_app

    app = create_app(home=home, port=TEST_PORT, bootstrap_token=BOOT_TOKEN)
    state = app.state.krellbot
    state.scheduler_supervisor = supervisor
    state.scheduler_write_root = write_root
    state.scheduler_executable = EXECUTABLE
    origin = ("Origin", f"http://127.0.0.1:{TEST_PORT}")
    status, body, cookies = _asgi(
        app,
        method="POST",
        path="/api/v1/session/bootstrap",
        body=json.dumps({"token": BOOT_TOKEN}).encode("utf-8"),
        headers=[origin],
    )
    assert status == 200, body
    cookie = next(c for c in cookies if c.startswith("krellbot_session="))
    auth = [origin, ("X-Krellbot-CSRF", json.loads(body)["csrf_token"]), ("Cookie", cookie.split(";", 1)[0])]

    def post(path: str) -> dict:
        status, body, _cookies = _asgi(app, method="POST", path=path, body=b"{}", headers=auth)
        assert status == 200, body
        payload = json.loads(body)
        assert set(payload) == {"schema_version", "installed", "enabled", "last_error"}, payload
        return payload

    return post


def test_the_web_scheduler_install_installs_the_tick_and_the_heartbeat(home: Path, fresh_keyring) -> None:
    write_root = home / "user-home"
    fake = FakeSupervisor()
    post = _web_scheduler(home, write_root, fake)

    payload = post("/api/v1/scheduler/install")

    assert payload["installed"] is True and payload["last_error"] is None, payload
    tick_units, heartbeat_units = _units_for_platform(write_root, home, EXECUTABLE)
    for path, body in {**tick_units, **heartbeat_units}.items():
        assert path.read_bytes() == body, path

    # A second install rewrites neither; enable is still one platform call.
    assert post("/api/v1/scheduler/install")["last_error"] is None
    assert post("/api/v1/scheduler/enable")["enabled"] is True
    assert fake.enable_calls == [write_root]
    for path, body in {**tick_units, **heartbeat_units}.items():
        assert path.read_bytes() == body, path


def test_the_web_install_adds_the_heartbeat_to_a_tick_installed_earlier(home: Path, fresh_keyring) -> None:
    from krellbot import service as kb_service

    write_root = home / "user-home"
    earlier = "/opt/krellbot/bin/krellbot"
    assert kb_service.install(home, executable=earlier, write_root=write_root) == 0
    tick_units, heartbeat_units = _units_for_platform(write_root, home, earlier)
    assert not any(path.exists() for path in heartbeat_units)

    payload = _web_scheduler(home, write_root, FakeSupervisor())("/api/v1/scheduler/install")

    # No owner conflict; the heartbeat runs the executable the tick unit runs.
    assert payload["installed"] is True and payload["last_error"] is None, payload
    for path, body in {**tick_units, **heartbeat_units}.items():
        assert path.read_bytes() == body, path


@pytest.mark.parametrize("platform_supervisor", ["systemd", "launchd", "windows"])
def test_enabling_the_scheduler_enables_the_heartbeat_too(home: Path, monkeypatch, platform_supervisor) -> None:
    from krellbot.service import render

    write_root = home / "user-home"
    calls: list[list[str]] = []
    monkeypatch.setattr(supervise, "_run_checked", lambda argv, *, label: calls.append(list(argv)))
    monkeypatch.setattr(supervise, "_launchd_domain", lambda: "gui/501")

    def write(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    if platform_supervisor == "systemd":
        d = write_root / ".config" / "systemd" / "user"
        for name, body in {
            **render.render_systemd(EXECUTABLE, home),
            **render.render_heartbeat_systemd(EXECUTABLE, home),
        }.items():
            write(d / name, body)
        supervise.SystemdUserSupervisor().enable(write_root=write_root)
        assert calls == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", "krellbot-tick.timer"],
            ["systemctl", "--user", "enable", "--now", "krellbot-heartbeat.timer"],
        ], calls
    elif platform_supervisor == "launchd":
        d = write_root / "Library" / "LaunchAgents"
        write(d / "dev.krellbot.tick.plist", render.render_launchd(EXECUTABLE, home))
        write(d / "dev.krellbot.heartbeat.plist", render.render_heartbeat_launchd(EXECUTABLE, home))
        supervise.LaunchdSupervisor().enable(write_root=write_root)
        assert calls == [
            ["launchctl", "bootstrap", "gui/501", str(d / "dev.krellbot.tick.plist")],
            ["launchctl", "enable", "gui/501/dev.krellbot.tick"],
            ["launchctl", "bootstrap", "gui/501", str(d / "dev.krellbot.heartbeat.plist")],
            ["launchctl", "enable", "gui/501/dev.krellbot.heartbeat"],
        ], calls
    else:
        d = write_root / "Tasks"
        write(d / "krellbot-tick.xml", render.render_windows(EXECUTABLE, home))
        write(d / "krellbot-heartbeat.xml", render.render_heartbeat_windows(EXECUTABLE, home))
        supervise.WindowsTaskSupervisor().enable(write_root=write_root)
        assert calls == [
            ["schtasks", "/Create", "/TN", r"\krellbot\tick", "/XML", str(d / "krellbot-tick.xml"), "/F"],
            ["schtasks", "/Change", "/TN", r"\krellbot\tick", "/ENABLE"],
            ["schtasks", "/Create", "/TN", r"\krellbot\heartbeat", "/XML", str(d / "krellbot-heartbeat.xml"), "/F"],
            ["schtasks", "/Change", "/TN", r"\krellbot\heartbeat", "/ENABLE"],
        ], calls


def test_service_uninstall_removes_the_heartbeat_units_too(home: Path, fresh_keyring) -> None:
    from krellbot import cli

    write_root = home / "user-home"
    _web_scheduler(home, write_root, FakeSupervisor())("/api/v1/scheduler/install")
    tick_units, heartbeat_units = _units_for_platform(write_root, home, EXECUTABLE)
    assert all(path.is_file() for path in {**tick_units, **heartbeat_units})

    assert cli.main(["krellbot", "service", "uninstall", "--root", str(write_root)]) == 0

    assert not any(path.exists() for path in {**tick_units, **heartbeat_units})


def test_the_heartbeat_entry_takes_no_arguments(home: Path, fresh_keyring, monkeypatch, capsys) -> None:
    from krellbot import cli

    assert cli.main(["krellbot", "service", "heartbeat", "--venue", "kraken"]) == 2
    assert "usage" in capsys.readouterr().err


def test_the_service_doc_says_the_heartbeat_cannot_report_a_powered_off_computer() -> None:
    text = (REPO_ROOT / "docs" / "service.md").read_text(encoding="utf-8")
    assert "krellbot service heartbeat" in text
    assert "every 15 minutes" in text
    assert "cannot report a powered-off computer" in text
