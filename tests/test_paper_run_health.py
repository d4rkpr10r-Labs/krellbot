"""Health reports evidence for the currently armed paper run only."""

import json
from decimal import Decimal

import pytest

from krellbot import config
from tests.test_multi_venue_paper_status import (
    _arm,
    _authenticated_headers,
    _bootstrap_session,
    _get,
    _write_pack,
)
from tests.test_ns06_api import _build_app


@pytest.fixture
def armed_health(home):
    path = _write_pack(home, pack_id="wave", venue="kraken", pair="SUIUSD")
    _arm(home, path, venue="kraken", paper_balance=Decimal(1000))
    saved = config.load_config(home)
    saved.armed[0].armed_at_ts = 100000
    config.save_config(home, saved)
    app = _build_app(home)
    app.state.krellbot.clock = lambda: 100001
    session = _bootstrap_session(app, "boot-test-token")
    headers = _authenticated_headers(session)

    def health():
        status, body = _get(app, "/api/v1/paper/health", headers=headers)
        assert status == 200
        return body

    health.app = app

    def tick(ts, reason="flat", **detail):
        directory = home / "journal"
        directory.mkdir(exist_ok=True)
        record = {
            "kind": "tick",
            "ts": ts,
            "venue": "kraken",
            "pack": "wave",
            "bar_ts": (ts - 3600) * 1000,
            "detail": {"pair": "SUIUSD", "reason": reason, **detail},
        }
        with (directory / "test.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    return health, tick


def test_health_requires_tick_after_current_arm(armed_health):
    health, tick = armed_health
    tick(99999)
    tick(100000)
    assert health()["state"] == "waiting"
    assert health()["last_success"] is None
    tick(100001)
    assert health()["state"] == "checked"
    assert health()["last_success"] == 100001


@pytest.mark.parametrize(
    ("reason", "detail", "expected"),
    [
        ("entry", {"entry_qty": "0.1"}, "filled"),
        ("flat", {}, "checked"),
        ("no_candles", {}, "no_candles"),
        ("fetch_error", {}, "feed_error"),
        ("entry", {"metadata_refusal": "metadata_stale"}, "stale_rules"),
        ("entry", {"metadata_refusal": "metadata_symbol_mismatch"}, "refused"),
        ("entry", {"kill_switch": True}, "blocked"),
        ("entry", {"ensure_stop_failed": True}, "blocked"),
        ("entry", {"entries_blocked": True}, "blocked"),
    ],
)
def test_health_explains_each_tick_outcome(armed_health, reason, detail, expected):
    health, tick = armed_health
    tick(100001, reason, **detail)
    assert health()["state"] == expected


def test_health_reports_pause_and_scheduler_status(home, armed_health):
    health, _ = armed_health
    saved = config.load_config(home)
    saved.armed[0].entries_paused = True
    config.save_config(home, saved)
    body = health()
    assert body["state"] == "paused"
    assert body["scheduler_installed"] is False
    assert body["scheduler_enabled"] is False


def test_health_rejects_outdated_check_and_stale_candle(home, armed_health):
    health, tick = armed_health
    tick(100001)
    journal = home / "journal" / "test.jsonl"
    record = json.loads(journal.read_text())
    record["bar_ts"] = (record["ts"] - 10800) * 1000
    journal.write_text(json.dumps(record) + "\n")
    assert health()["state"] == "stale_candles"
    assert health()["last_success"] is None
    record["bar_ts"] = (record["ts"] - 3600) * 1000
    journal.write_text(json.dumps(record) + "\n")
    assert health()["state"] == "checked"
    health.app.state.krellbot.clock = lambda: 110801
    assert health()["state"] == "stale_check"


@pytest.mark.parametrize("timeframe,seconds", [("4h", 14400), ("1d", 86400)])
def test_health_waits_for_next_long_timeframe_bar(home, armed_health, timeframe, seconds):
    health, tick = armed_health
    pack = home / "packs" / "wave.json"
    content = json.loads(pack.read_text())
    content["timeframe"] = timeframe
    pack.write_text(json.dumps(content))
    tick(100001)
    health.app.state.krellbot.clock = lambda: 100001 + seconds
    assert health()["state"] == "checked"
    health.app.state.krellbot.clock = lambda: 100001 + seconds + 7200
    assert health()["state"] == "stale_check"


def test_zero_quantity_is_not_reported_as_a_fill(armed_health):
    health, tick = armed_health
    tick(100001, "entry", entry_qty="0.00000000")
    assert health()["state"] == "checked"


def test_malformed_tick_timestamps_are_ignored(home, armed_health):
    health, tick = armed_health
    for value in (float("nan"), float("inf"), True, 10**400):
        tick(value)
    assert health()["state"] == "waiting"


def test_slow_scheduler_probe_does_not_block_status(home, armed_health, monkeypatch):
    import asyncio
    import threading

    from krellbot.service import supervise

    health, _ = armed_health
    app = health.app
    started = threading.Event()
    release = threading.Event()

    def slow_scheduler(*args, **kwargs):
        started.set()
        release.wait(5)
        return {"installed": True, "enabled": True, "last_error": None}

    monkeypatch.setattr(supervise, "scheduler_status", slow_scheduler)
    session = next(iter(app.state.krellbot.sessions))

    async def call(path):
        response = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "server": ("127.0.0.1", 8080),
            "client": ("127.0.0.1", 12345),
            "headers": [(b"host", b"127.0.0.1:8080"), (b"cookie", f"krellbot_session={session}".encode())],
            "root_path": "",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            response.append(message)

        await app(scope, receive, send)
        return next(item["status"] for item in response if item["type"] == "http.response.start")

    async def exercise():
        task = asyncio.create_task(call("/api/v1/paper/health"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert await asyncio.wait_for(call("/api/v1/paper/status"), timeout=1) == 200
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(exercise())
