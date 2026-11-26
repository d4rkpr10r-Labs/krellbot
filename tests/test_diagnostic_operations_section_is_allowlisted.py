"""The diagnostic Operations section is built by allowlist (M28.15, D20).

``GET /api/v1/diagnostics/operations`` sits behind the same session gate as
``GET /api/v1/diagnostics/report`` and is served on its own route, so the
report's six keys stay as they are. It carries only:

* current alerts as ``id``, ``kind``, ``severity``, ``code``, ``count``,
  ``first_ts`` and ``last_ts``, with a closed kind set and a closed code set
  per kind (an identifier or free-text code is never exported);
* the unresolved-order count and ages;
* paper pack id, version and mode per venue and pair.

Live events stay out until a privacy review: no ``live_refused`` alert, no
alert tied to a live feed, no live arm. Keys, balances, quantities, paths,
hosts and free text never reach the body, and a store failure reads
``unavailable``. Reading the section sends nothing.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from krellbot.application.paper import PaperService
from tests.test_community_pack_paper_arm import _auth, _bootstrap, _get, _pack_dict
from tests.test_ns06_api import TEST_PORT, _build_app, asgi_call

ROUTE = "/api/v1/diagnostics/operations"
SECRET = "sk-live-hunter2-SECRET"
HOST = "api.private-venue.example"
PRIVATE_PATH = "/home/alice-private/.krellbot"
STARTING_CASH = "1234.5678"
OWNED_QTY = "0.4242"
STORE_CODES = {"store_error", "store_busy", "store_corrupt", "store_full"}
ALERT_KEYS = {"id", "kind", "severity", "code", "count", "first_ts", "last_ts"}


def _arm_paper(home: Path) -> int:
    pack = _pack_dict("ops-report")
    path = home / "pack.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    result = PaperService(home=home).arm(path, venue="kraken", mode="paper", paper_balance=Decimal(1000))
    assert result.ok
    from krellbot.config import load_config

    return load_config(home).armed[0].armed_at_ts


def _add_records(home: Path) -> None:
    """Add a live arm, a paper arm with free-text identity and one on a host."""

    from krellbot.config import ArmedPack, load_config, save_config

    cfg = load_config(home)

    def armed(*, venue: str, pair: str, pack_id: str, pack_version: str, mode: str) -> ArmedPack:
        return ArmedPack(
            pack_path=f"{PRIVATE_PATH}/{SECRET}.json",
            pack_sha256="f" * 64,
            pack_id=pack_id,
            pack_version=pack_version,
            venue=venue,
            pair=pair,
            cap=Decimal(25),
            stop=Decimal("0.5"),
            mode=mode,
            starting_cash=Decimal(STARTING_CASH),
            requires_license=False,
            armed_at_ts=cfg.armed[0].armed_at_ts,
            owned_qty=Decimal(OWNED_QTY),
        )

    cfg.armed.append(
        armed(venue="coinbase", pair="BTC-USD", pack_id="live-only-pack", pack_version="2.0.0", mode="live")
    )
    cfg.armed.append(
        armed(venue="binance", pair="BTCUSDT", pack_id=f"Alice {SECRET}", pack_version=f"v1 {HOST}", mode="paper")
    )
    cfg.armed.append(armed(venue=HOST, pair="BTCUSDT", pack_id="host-pack", pack_version="1.0.0", mode="paper"))
    save_config(home, cfg)


def _write_journal(home: Path, *, start: int) -> None:
    private = {"balance": STARTING_CASH, "secret": SECRET, "headers": HOST, "exception": PRIVATE_PATH}
    paper = {"pair": "SUIUSD", "mode": "paper", **private}
    live = {"pair": "BTC-USD", "mode": "live", **private}
    records = [
        {
            "kind": "tick",
            "ts": start + 1,
            "venue": "kraken",
            "pack": "ops-report",
            "detail": {**paper, "metadata_refusal": "metadata_stale"},
        },
        {
            "kind": "tick",
            "ts": start + 2,
            "venue": "kraken",
            "pack": "ops-report",
            "detail": {**paper, "intent_refused": f"{SECRET} at {HOST}"},
        },
        {
            "kind": "tick",
            "ts": start + 3,
            "venue": "kraken",
            "pack": "ops-report",
            "detail": {**paper, "reason": "store_refused", "code": "store_busy", "intent_refused": "store_busy"},
        },
        {
            "kind": "tick",
            "ts": start + 4,
            "venue": "coinbase",
            "pack": "live-only-pack",
            "detail": {**live, "reason": "live_refused", "code": "live_disabled"},
        },
        {
            "kind": "tick",
            "ts": start + 5,
            "venue": "coinbase",
            "pack": "live-only-pack",
            "detail": {**live, "metadata_refusal": "metadata_invalid"},
        },
    ]
    journal = home / "journal"
    journal.mkdir(exist_ok=True)
    (journal / "ops.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")


def _seed_unresolved(home: Path, *, now: int) -> None:
    from krellbot.storage.database import OperationalStore

    store = OperationalStore(home / "ops.sqlite")
    store.connect()
    with store.transaction() as conn:
        for coid, age in ((f"{SECRET}-old", 7200), (f"{HOST}-new", 30)):
            stamp = now - age
            outbox = {
                "coid": coid,
                "body": json.dumps({"qty": OWNED_QTY, "pair": "SUIUSD"}),
                "sent": False,
                "ts": stamp,
            }
            conn.execute("INSERT INTO ledger (kind, payload) VALUES ('outbox', ?)", (json.dumps(outbox),))
            audit = {"coid": coid, "ts": stamp}
            conn.execute("INSERT INTO ledger (kind, payload) VALUES ('needs_reconcile', ?)", (json.dumps(audit),))


def _fix_now(monkeypatch, now: int) -> None:
    import krellbot.api.app as app_module

    monkeypatch.setattr(app_module, "_now_seconds", lambda: now)


def _read(app) -> tuple[int, bytes]:
    return _get(app, ROUTE, headers=_auth(*_bootstrap(app)))


def test_operations_section_uses_the_report_gate_and_is_no_store(home):
    app = _build_app(home)
    status, _, _, _ = asgi_call(app, method="GET", path=ROUTE, headers=[("Host", f"127.0.0.1:{TEST_PORT}")])
    assert status == 403
    headers = _auth(*_bootstrap(app))
    status, _, _, _ = asgi_call(
        app, method="GET", path=ROUTE, headers=[("Host", f"127.0.0.1:{TEST_PORT}"), ("Origin", "http://evil.example")]
    )
    assert status == 403
    status, _, _, _ = asgi_call(app, method="GET", path=ROUTE, headers=[("Host", "evil.example"), *headers])
    assert status == 403
    status, response_headers, body, _ = asgi_call(
        app, method="GET", path=ROUTE, headers=[("Host", f"127.0.0.1:{TEST_PORT}"), *headers]
    )
    assert status == 200
    assert ("cache-control", "no-store") in response_headers
    section = json.loads(body)
    assert set(section) == {"schema_version", "alerts", "unresolved_orders", "paper_runs"}
    assert section["alerts"] == []
    assert section["unresolved_orders"] == {"count": 0, "ages_seconds": []}
    assert section["paper_runs"] == []

    status, report = _get(app, "/api/v1/diagnostics/report", headers=headers)
    assert status == 200
    assert set(json.loads(report)) == {"schema_version", "build", "os", "scheduler", "events", "reproduction_steps"}


def test_operations_section_allowlists_alerts_orders_and_paper_runs(home, monkeypatch):
    from krellbot.application import live_gate
    from krellbot.doctor import TICK_STALE_SECONDS
    from krellbot.service import supervise

    armed_at = _arm_paper(home)
    _add_records(home)
    _write_journal(home, start=armed_at)
    now = armed_at + int(TICK_STALE_SECONDS) + 100
    _fix_now(monkeypatch, now)
    _seed_unresolved(home, now=now)
    supervise.set_last_error(home, f"tick failed at {PRIVATE_PATH} via {HOST} key={SECRET}")
    live_gate.engage_kill(home, reason=f"home restored from {PRIVATE_PATH} {SECRET}", now=now - 5)

    status, body = _read(_build_app(home))
    assert status == 200
    text = body.decode()
    for leaked in (SECRET, HOST, PRIVATE_PATH, str(home), STARTING_CASH, OWNED_QTY, "f" * 64):
        assert leaked not in text, leaked
    for field in (
        "pack_path",
        "pack_sha256",
        "starting_cash",
        "owned_qty",
        "balance",
        "coid",
        "client_order_id",
        "qty",
    ):
        assert field not in text, field

    section = json.loads(body)
    for alert in section["alerts"]:
        assert set(alert) == ALERT_KEYS
        assert re.fullmatch(r"[0-9a-f]{16}", alert["id"])
        assert alert["severity"] in {"critical", "warning"}
        assert all(type(alert[key]) is int and alert[key] >= 0 for key in ("count", "first_ts", "last_ts"))
    assert Counter((alert["kind"], alert["code"]) for alert in section["alerts"]) == Counter(
        {
            ("kill_switch", "kill_switch_engaged"): 1,
            ("restore_fence", "restore_fence_engaged"): 1,
            ("needs_reconcile", None): 2,
            ("tick_failure", None): 1,
            ("unresolved_order_aging", None): 1,
            ("scheduler_stale", "tick_stale"): 1,
            ("metadata_refusal", "metadata_stale"): 1,
            ("intent_refused", "other"): 1,
            ("intent_refused", "store_busy"): 1,
            ("store_refused", "store_busy"): 1,
        }
    )
    assert section["unresolved_orders"] == {"count": 2, "ages_seconds": [7200, 30]}
    assert section["paper_runs"] == [
        {"venue": "binance", "pair": "BTCUSDT", "pack_id": None, "pack_version": None, "mode": "paper"},
        {"venue": "kraken", "pair": "SUIUSD", "pack_id": "ops-report", "pack_version": "1.0.0", "mode": "paper"},
    ]


def test_live_events_stay_out_of_the_operations_section(home, monkeypatch):
    from krellbot.doctor import TICK_STALE_SECONDS

    armed_at = _arm_paper(home)
    _add_records(home)
    _write_journal(home, start=armed_at)
    _fix_now(monkeypatch, armed_at + int(TICK_STALE_SECONDS) + 100)

    status, body = _read(_build_app(home))
    assert status == 200
    section = json.loads(body)
    text = body.decode()
    for live in (
        "live_refused",
        "live_disabled",
        "metadata_invalid",
        "live-only-pack",
        "coinbase",
        "BTC-USD",
        '"live"',
    ):
        assert live not in text, live
    assert [alert["kind"] for alert in section["alerts"]].count("scheduler_stale") == 1
    assert all(run["mode"] == "paper" for run in section["paper_runs"])


def test_store_failure_reads_unavailable_without_error_text(home, monkeypatch):
    _arm_paper(home)
    (home / "ops.sqlite").write_bytes(b"definitely not a sqlite database\n")
    app = _build_app(home)
    headers = _auth(*_bootstrap(app))
    status, body = _get(app, ROUTE, headers=headers)
    assert status == 200
    section = json.loads(body)
    assert section["unresolved_orders"] == "unavailable"
    faults = [alert for alert in section["alerts"] if alert["kind"] == "store_fault"]
    assert len(faults) == 1
    assert faults[0]["code"] in STORE_CODES

    from krellbot import config as kb_config
    from krellbot.application import alerts as alerts_app

    def broken(*args, **kwargs):
        raise OSError(f"cannot read {PRIVATE_PATH} {SECRET}")

    monkeypatch.setattr(alerts_app, "collect", broken)
    monkeypatch.setattr(kb_config, "load_config", broken)
    status, body = _get(app, ROUTE, headers=headers)
    assert status == 200
    assert SECRET.encode() not in body
    assert PRIVATE_PATH.encode() not in body
    section = json.loads(body)
    assert section["alerts"] == "unavailable"
    assert section["paper_runs"] == "unavailable"
    assert section["unresolved_orders"] == "unavailable"


def test_reading_the_operations_section_sends_nothing(home, monkeypatch):
    from krellbot import notifications as kb_notifications
    from krellbot import telemetry as kb_telemetry
    from krellbot.service import supervise

    telemetry_calls: list[str] = []
    monkeypatch.setattr(kb_telemetry, "maybe_send", lambda *a, **kw: telemetry_calls.append("fill"))
    monkeypatch.setattr(kb_telemetry, "maybe_send_funnel", lambda *a, **kw: telemetry_calls.append("funnel"))
    _arm_paper(home)
    supervise.set_last_error(home, "tick failed")
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    app = _build_app(home)
    notifier = kb_notifications.FakeNotifier()
    app.state.krellbot.alert_notifier = notifier
    headers = _auth(*_bootstrap(app))

    status, body = _get(app, ROUTE, headers=headers)
    assert status == 200
    assert [alert["kind"] for alert in json.loads(body)["alerts"]] == ["tick_failure"]
    assert notifier.attempts == []
    assert telemetry_calls == []

    # The opted-in Operations read does notify, so the setup above would send.
    status, _ = _get(app, "/api/v1/operations", headers=headers)
    assert status == 200
    assert notifier.attempts
