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
