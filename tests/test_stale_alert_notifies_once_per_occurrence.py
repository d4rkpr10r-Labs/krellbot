"""An ongoing alert notifies once per occurrence, however often Operations polls.

``scheduler_stale``, ``unresolved_order_aging`` and ``tick_failure`` are
conditions that stay true between polls. Each Operations read (every 5 s
while the tab is open) re-collects them, so their ``last_ts`` must not make
every read look like a new occurrence.

Pins:

* Notifications dedupe on ``(alert_id, first_ts)``: repeated polls of the
  same stale venue, aged order or scheduler error send one notification.
* A later occurrence (ticks resume, then go stale again; a new
  ``last_error`` write) notifies again.
* ``tick_failure`` is dated from its ``last_error`` write, not the read.
* An acknowledgement holds across later polls of the same occurrence.

Every test uses ``FakeNotifier`` and an isolated home: no desktop bus or SMTP
host is touched.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
from decimal import Decimal
from pathlib import Path

from krellbot import notifications as kb_notifications
from krellbot.application import alerts
from krellbot.doctor import TICK_STALE_SECONDS
from krellbot.service import supervise as kb_supervise

TEST_PORT = 8080
BOOT_TOKEN = "boot-stale-alerts"
POLL_SECONDS = 5


# ---- fixtures on disk -------------------------------------------------------


def _append_journal_record(home: Path, record: dict) -> None:
    journal_dir = home / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    utc = datetime.datetime.fromtimestamp(int(record["ts"]), tz=datetime.timezone.utc)
    file_path = journal_dir / f"{utc:%Y-%m}.jsonl"
    fd = os.open(str(file_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _append_tick(home: Path, *, ts: int, venue: str = "kraken") -> None:
    _append_journal_record(
        home,
        {
            "ts": ts,
            "kind": "tick",
            "venue": venue,
            "pack": "trend-follow",
            "bar_ts": ts,
            "detail": {"pair": "SUIUSD", "decision": "hold"},
        },
    )


def _arm_paper(home: Path, *, venue: str = "kraken") -> None:
    from krellbot.application.paper import PaperService

    packs = home / "packs"
    packs.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 1,
        "id": "trend-follow",
        "version": "1.0.0",
        "label": "trend-follow",
        "author": "krellbot stale alert tests",
        "timeframe": "1h",
        "indicators": {"sma20": {"fn": "sma", "src": "close", "len": 20}},
        "entry": ["close", ">", "sma20"],
        "exit": ["close", "<", "sma20"],
        "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": venue, "pair": "SUIUSD"}],
    }
    pack_path = packs / "trend-follow.json"
    pack_path.write_text(json.dumps(body), encoding="utf-8")
    result = PaperService(home=home).arm(
        pack_path,
        venue=venue,
        mode="paper",
        paper_balance=Decimal(1000),
        correlation_id="seed-stale-alerts",
    )
    assert result.ok, result.message


def _seed_unresolved(home: Path, *, coid: str, ts: int) -> None:
    from krellbot.storage.database import OperationalStore

    store = OperationalStore(home / "ops.sqlite")
    store.connect()
    with store.transaction() as conn:
        payload = json.dumps({"coid": coid, "ts": ts}, separators=(",", ":"))
        conn.execute(
            "INSERT INTO ledger (kind, payload) VALUES ('needs_reconcile', ?)",
            (payload,),
        )


def _write_last_error(home: Path, message: str, *, written_at: int) -> None:
    kb_supervise.set_last_error(home, message)
    os.utime(kb_supervise.last_error_path(home), (written_at, written_at))


def _poll(home: Path, fake: kb_notifications.FakeNotifier, *, now: int) -> list[alerts.Alert]:
    collected = alerts.collect(home, now=now)
    kb_notifications.maybe_notify(home, collected, notifier=fake)
    return collected


def _sent_for(fake: kb_notifications.FakeNotifier, kind: str) -> list[dict[str, str]]:
    return [row for row in fake.sent if row["title"].endswith(f": {kind}")]


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
    cookies = [v.decode("latin-1") for k, v in captured["headers"] if k.decode("latin-1").lower() == "set-cookie"]
    return int(captured["status"]), bytes(captured["body"]), cookies


def _session(app) -> list[tuple[str, str]]:
    body = json.dumps({"token": BOOT_TOKEN}).encode("utf-8")
    status, raw, cookies = _asgi_call(
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


# ---- scheduler_stale ---------------------------------------------------------


def test_stale_venue_notifies_once_across_operations_polls(home: Path, monkeypatch) -> None:
    from krellbot.api.app import create_app

    _arm_paper(home)
    last_tick = 1_700_000_000
    _append_tick(home, ts=last_tick)
    kb_notifications.update_settings(home, {"desktop_enabled": True})

    app = create_app(home=home, port=TEST_PORT, bootstrap_token=BOOT_TOKEN)
    fake = kb_notifications.FakeNotifier()
    app.state.krellbot.alert_notifier = fake
    auth = _session(app)

    clock = [last_tick + TICK_STALE_SECONDS]
    monkeypatch.setattr("krellbot.api.app._now_seconds", lambda: clock[0])
    for step in range(6):
        clock[0] = last_tick + TICK_STALE_SECONDS + step * POLL_SECONDS
        status, body, _c = _asgi_call(app, method="GET", path="/api/v1/operations", headers=auth)
        assert status == 200, body
        kinds = {a["kind"] for a in json.loads(body)["alerts"]}
        assert alerts.KIND_SCHEDULER_STALE in kinds, kinds

    assert len(_sent_for(fake, alerts.KIND_SCHEDULER_STALE)) == 1, fake.sent


def test_stale_venue_notifies_again_after_ticks_resume_and_stop(home: Path) -> None:
    _arm_paper(home)
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()

    first_tick = 1_700_000_000
    _append_tick(home, ts=first_tick)
    stale_at = first_tick + TICK_STALE_SECONDS
    for step in range(3):
        _poll(home, fake, now=stale_at + step * POLL_SECONDS)
    assert len(_sent_for(fake, alerts.KIND_SCHEDULER_STALE)) == 1

    # Ticks resume: the alert clears.
    second_tick = stale_at + 600
    _append_tick(home, ts=second_tick)
    fresh = _poll(home, fake, now=second_tick + 60)
    assert [a for a in fresh if a.kind == alerts.KIND_SCHEDULER_STALE] == []

    # Ticks stop again: a new occurrence, one more notification.
    restale_at = second_tick + TICK_STALE_SECONDS
    for step in range(3):
        _poll(home, fake, now=restale_at + step * POLL_SECONDS)
    assert len(_sent_for(fake, alerts.KIND_SCHEDULER_STALE)) == 2, fake.sent


def test_stale_venue_ack_holds_across_later_polls(home: Path) -> None:
    _arm_paper(home)
    last_tick = 1_700_000_000
    _append_tick(home, ts=last_tick)
    now = last_tick + TICK_STALE_SECONDS

    stale = next(a for a in alerts.collect(home, now=now) if a.kind == alerts.KIND_SCHEDULER_STALE)
    alerts.acknowledge(home, stale.id, now=now + 1)

    for later in (now + POLL_SECONDS, now + 60, now + 3600):
        again = next(a for a in alerts.collect(home, now=later) if a.id == stale.id)
        assert again.acknowledged is True, (later, again)


def test_acknowledged_stale_venue_is_not_notified_on_later_polls(home: Path) -> None:
    _arm_paper(home)
    last_tick = 1_700_000_000
    _append_tick(home, ts=last_tick)
    now = last_tick + TICK_STALE_SECONDS
    stale = next(a for a in alerts.collect(home, now=now) if a.kind == alerts.KIND_SCHEDULER_STALE)
    alerts.acknowledge(home, stale.id, now=now)

    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()
    for step in range(1, 4):
        _poll(home, fake, now=now + step * POLL_SECONDS)
    assert _sent_for(fake, alerts.KIND_SCHEDULER_STALE) == []


def test_stale_venue_notified_under_the_earlier_store_is_not_resent(home: Path) -> None:
    _arm_paper(home)
    last_tick = 1_700_000_000
    _append_tick(home, ts=last_tick)
    now = last_tick + TICK_STALE_SECONDS
    stale = next(a for a in alerts.collect(home, now=now) if a.kind == alerts.KIND_SCHEDULER_STALE)

    # The earlier store kept a flat {alert_id: last_ts} map of what it sent.
    sent_file = home / "run" / kb_notifications.SENT_FILE
    sent_file.parent.mkdir(parents=True, exist_ok=True)
    sent_file.write_text(json.dumps({stale.id: now}), encoding="utf-8")

    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()
    for step in range(1, 4):
        _poll(home, fake, now=now + step * POLL_SECONDS)
    assert _sent_for(fake, alerts.KIND_SCHEDULER_STALE) == []


# ---- unresolved_order_aging --------------------------------------------------


def test_aged_unresolved_order_notifies_once_and_ack_holds(home: Path) -> None:
    started = 1_700_000_000
    _seed_unresolved(home, coid="aged-coid-1", ts=started)
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()

    aged_at = started + alerts.UNRESOLVED_AGING_SECONDS
    for step in range(4):
        _poll(home, fake, now=aged_at + step * POLL_SECONDS)
    assert len(_sent_for(fake, alerts.KIND_UNRESOLVED_ORDER_AGING)) == 1, fake.sent

    aging = next(a for a in alerts.collect(home, now=aged_at) if a.kind == alerts.KIND_UNRESOLVED_ORDER_AGING)
    alerts.acknowledge(home, aging.id, now=aged_at + 30)
    later = alerts.collect(home, now=aged_at + 600)
    assert next(a for a in later if a.id == aging.id).acknowledged is True


# ---- tick_failure ------------------------------------------------------------


def test_tick_failure_is_dated_from_its_last_error_write(home: Path) -> None:
    written_at = 1_700_000_000
    _write_last_error(home, "tick exited rc=1", written_at=written_at)

    for now in (written_at + 10, written_at + 500):
        failure = next(a for a in alerts.collect(home, now=now) if a.kind == alerts.KIND_TICK_FAILURE)
        assert failure.first_ts == written_at, failure
        assert failure.last_ts == written_at, failure


def test_tick_failure_notifies_once_per_last_error_write(home: Path) -> None:
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()

    written_at = 1_700_000_000
    _write_last_error(home, "tick exited rc=1", written_at=written_at)
    for step in range(4):
        _poll(home, fake, now=written_at + 10 + step * POLL_SECONDS)
    assert len(_sent_for(fake, alerts.KIND_TICK_FAILURE)) == 1, fake.sent

    # The same failure written again later is a new occurrence.
    rewritten_at = written_at + 3600
    _write_last_error(home, "tick exited rc=1", written_at=rewritten_at)
    for step in range(3):
        _poll(home, fake, now=rewritten_at + 10 + step * POLL_SECONDS)
    assert len(_sent_for(fake, alerts.KIND_TICK_FAILURE)) == 2, fake.sent


def test_tick_failure_ack_holds_until_the_next_write(home: Path) -> None:
    written_at = 1_700_000_000
    _write_last_error(home, "tick exited rc=1", written_at=written_at)
    failure = next(a for a in alerts.collect(home, now=written_at + 10) if a.kind == alerts.KIND_TICK_FAILURE)
    alerts.acknowledge(home, failure.id, now=written_at + 20)

    for later in (written_at + 25, written_at + 600):
        again = next(a for a in alerts.collect(home, now=later) if a.id == failure.id)
        assert again.acknowledged is True, (later, again)

    _write_last_error(home, "tick exited rc=1", written_at=written_at + 3600)
    renewed = next(a for a in alerts.collect(home, now=written_at + 3610) if a.id == failure.id)
    assert renewed.acknowledged is False, renewed
