"""Diagnostic exports contain only bounded, authenticated paper evidence."""

import json
from decimal import Decimal

from krellbot.application.paper import PaperService
from tests.test_community_pack_paper_arm import _auth, _bootstrap, _get, _pack_dict
from tests.test_ns06_api import TEST_PORT, _build_app, asgi_call


def test_report_requires_loopback_session_and_is_no_store(home):
    app = _build_app(home)
    status, _, _, _ = asgi_call(
        app, method="GET", path="/api/v1/diagnostics/report", headers=[("Host", f"127.0.0.1:{TEST_PORT}")]
    )
    assert status == 403
    headers = _auth(*_bootstrap(app))
    status, response_headers, body, _ = asgi_call(
        app, method="GET", path="/api/v1/diagnostics/report", headers=[("Host", f"127.0.0.1:{TEST_PORT}"), *headers]
    )
    assert status == 200
    assert ("cache-control", "no-store") in response_headers
    report = json.loads(body)
    assert report["events"] == []
    assert report["reproduction_steps"] == ""
    assert set(report) == {"schema_version", "build", "os", "scheduler", "events", "reproduction_steps"}


def test_report_bounds_and_allowlists_evidence(home):
    pack = _pack_dict("paper-report")
    path = home / "pack.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    result = PaperService(home=home).arm(path, venue="kraken", mode="paper", paper_balance=Decimal(1000))
    assert result.ok
    from krellbot.config import load_config

    armed = load_config(home).armed[0]
    journal = home / "journal"
    journal.mkdir(exist_ok=True)
    private = "sensitive-key-/private/user/path"
    records = []
    for n in range(105):
        records.append(
            {
                "kind": "tick",
                "ts": armed.armed_at_ts + n,
                "venue": "kraken",
                "pack": "paper-report",
                "detail": {
                    "pair": "SUIUSD",
                    "mode": "paper",
                    "metadata_refusal": "metadata_stale",
                    "balance": private,
                    "secret": private,
                },
                "headers": private,
            }
        )
    records.append(
        {
            "kind": "tick",
            "ts": armed.armed_at_ts + 200,
            "venue": "kraken",
            "pack": "paper-report",
            "detail": {"pair": "SUIUSD", "mode": "paper", "metadata_refusal": private},
        }
    )
    records.append({"kind": "tick", "ts": 0, "venue": [], "detail": {"pair": "SUIUSD"}})
    (journal / "test.jsonl").write_text("broken json\n" + "\n".join(json.dumps(x) for x in records), encoding="utf-8")
    app = _build_app(home)
    status, body = _get(app, "/api/v1/diagnostics/report", headers=_auth(*_bootstrap(app)))
    assert status == 200
    assert private not in body.decode()
    report = json.loads(body)
    assert len(report["events"]) == 100
    assert report["events"][0]["ts"] == armed.armed_at_ts + 6
    assert report["events"][-1]["refusal_code"] == "other"
    assert report["events"][-2]["refusal_code"] == "metadata_stale"
    assert all(
        set(event) == {"ts", "venue", "pair", "source", "classification", "refusal_code"} for event in report["events"]
    )


def test_unavailable_scheduler_and_malformed_files_do_not_break_report(home, monkeypatch):
    from krellbot.service import supervise

    def unavailable(*args, **kwargs):
        raise OSError("sensitive-error")

    monkeypatch.setattr(supervise, "scheduler_status", unavailable)
    journal = home / "journal"
    journal.mkdir(exist_ok=True)
    (journal / "malformed.jsonl").write_bytes(b"\xff\xfe")
    app = _build_app(home)
    status, body = _get(app, "/api/v1/diagnostics/report", headers=_auth(*_bootstrap(app)))
    assert status == 200
    assert b"sensitive-error" not in body
    assert json.loads(body)["scheduler"] is None


def test_scheduler_probe_does_not_block_other_requests(home, monkeypatch):
    import asyncio
    import threading
    import time

    from krellbot.service import supervise

    entered = threading.Event()
    release = threading.Event()

    def slow_scheduler(*args, **kwargs):
        entered.set()
        release.wait(timeout=5)
        return {"installed": True, "enabled": True}

    monkeypatch.setattr(supervise, "scheduler_status", slow_scheduler)
    app = _build_app(home)
    headers = [("Host", f"127.0.0.1:{TEST_PORT}"), *_auth(*_bootstrap(app))]

    async def get(path):
        sent = False
        response = {"status": None}

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "server": ("127.0.0.1", TEST_PORT),
            "client": ("127.0.0.1", 12345),
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers],
            "root_path": "",
        }
        await app(scope, receive, send)
        return response["status"]

    async def exercise():
        task = asyncio.create_task(get("/api/v1/diagnostics/report"))
        started = time.monotonic()
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert await get("/api/v1/version") == 200
            assert time.monotonic() - started < 2
        finally:
            release.set()
            assert await task == 200

    asyncio.run(exercise())


def test_stopped_paper_checks_keep_safe_classifications_and_exclude_live(home, monkeypatch):
    from krellbot.service import supervise

    monkeypatch.setattr(
        supervise,
        "scheduler_status",
        lambda *a, **kw: {"installed": True, "enabled": False, "last_error": "private scheduler error"},
    )
    journal = home / "journal"
    journal.mkdir(exist_ok=True)
    details = [
        {"reason": "fetch_error", "exception": "private exchange error"},
        {"reason": "no_candles"},
        {"reason": "entry", "ensure_stop_failed": True},
        {"reason": "entry", "kill_switch": True},
        {"reason": "flat", "entries_paused": True},
        {"reason": "flat", "entries_blocked": True},
        {"reason": "flat"},
        {"reason": "unexpected private error"},
    ]
    records = [
        {
            "kind": "tick",
            "ts": n + 1,
            "venue": "kraken",
            "pack": "p",
            "detail": {"pair": "SUIUSD", "mode": "paper", **detail},
        }
        for n, detail in enumerate(details)
    ]
    records.extend(
        [
            {
                "kind": "tick",
                "ts": 9,
                "venue": "kraken",
                "pack": "p",
                "detail": {"pair": "SUIUSD", "mode": "live", "reason": "fetch_error"},
            },
            {
                "kind": "tick",
                "ts": 10,
                "venue": "kraken",
                "pack": "p",
                "detail": {"pair": "SUIUSD", "reason": "fetch_error"},
            },
        ]
    )
    (journal / "evidence.jsonl").write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
    app = _build_app(home)
    status, body = _get(app, "/api/v1/diagnostics/report", headers=_auth(*_bootstrap(app)))
    assert status == 200
    assert b"private" not in body
    report = json.loads(body)
    assert report["scheduler"] == {"installed": True, "enabled": False, "has_error": True}
    assert [event["classification"] for event in report["events"]] == [
        "fetch_error",
        "no_candles",
        "stop_repair_failed",
        "kill_switch",
        "paused",
        "entries_blocked",
        "flat",
        "other",
    ]
    assert [event["ts"] for event in report["events"]] == list(range(1, 9))


def test_engine_journal_records_distinguish_paper_and_live(home, fresh_keyring):
    from fakes.fake_kraken import FakeKrakenTransport

    from krellbot.cli import cmd_tick
    from krellbot.journal import read_records_lenient
    from krellbot.run import tick
    from tests.test_live_tick_wiring import _arm, _candle, _fake_fetch, _write_pack

    pack = _write_pack(home, pack_id="mode-evidence")
    _arm(home, pack, mode="paper", pack_id="mode-evidence")
    candles = [_candle(0, "10"), _candle(3_600_000, "10"), _candle(7_200_000, "12")]
    assert cmd_tick(["--venue", "kraken"], fetch=_fake_fetch(candles), transport=FakeKrakenTransport()) == 0
    paper_ticks = [row for row in read_records_lenient(home) if row.get("kind") == "tick"]
    assert paper_ticks
    assert all(row["detail"]["mode"] == "paper" for row in paper_ticks)

    _arm(home, pack, mode="live", pack_id="mode-evidence")
    refused = []
    assert (
        tick(
            venue="kraken",
            venue_obj=object(),
            reader=lambda *_: [],
            journal_sink=lambda row: refused.append(row),
            home=home,
        )
        == 1
    )
    assert refused[-1]["detail"]["mode"] == "live"
    assert refused[-1]["detail"]["reason"] == "live_refused"
