"""Agent read tools: what Krell and MCP clients may look at, bounded and secret-free.

Every result goes to the user's AI provider, so each one is capped at 32 KiB
of JSON (oldest list entries trimmed, ``truncated: true``) and carries no
key, secret, licence or balance-file field. ``positions`` is paper only.

Every candle and trace below is synthetic local data, never market data.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.agent import tools_read
from krellbot.agent.actors import KRELL, Actor
from krellbot.agent.core import LABEL_NOT_ADVICE, LABEL_SIMULATED, AgentToolCore
from krellbot.agent.tools_read import MAX_RESULT_BYTES, register_read_tools
from krellbot.agent.tour import TOUR_STEPS
from krellbot.application import pack_library
from krellbot.config import ArmedPack, Config, save_config
from krellbot.data import cache
from krellbot.domain.trace import paper_decision_trace
from krellbot.pack.model import Candle

CURSOR = Actor.mcp("cursor")
HOUR_MS = 3_600_000
NOW = 1_700_000_000
START_MS = ((NOW * 1000) // HOUR_MS) * HOUR_MS - 200 * HOUR_MS
READ_TOOLS = (
    "positions",
    "journal_recent",
    "alerts",
    "unresolved_orders",
    "market_snapshot",
    "catalogue",
    "explain_bar",
    "tour_step",
)


def _core(home: Path) -> AgentToolCore:
    core = AgentToolCore(home, clock=lambda: NOW)
    register_read_tools(core)
    return core


def _size(result: dict) -> int:
    return len(json.dumps(result, separators=(",", ":")).encode("utf-8"))


def _candles(count: int) -> list[Candle]:
    out = []
    for i in range(count):
        price = Decimal(100 + (i % 7) - (i % 3))
        out.append(
            Candle(
                ts_ms=START_MS + i * HOUR_MS,
                open=price,
                high=price + 2,
                low=price - 2,
                close=price + 1,
                volume=Decimal(10),
            )
        )
    return out


def _pack(pack_id: str = "trend-follow") -> dict:
    return {
        "schema_version": 1,
        "id": pack_id,
        "version": "1.0.0",
        "label": "Trend follow",
        "author": "krellbot read tool tests",
        "timeframe": "1h",
        "indicators": {"sma3": {"fn": "sma", "src": "close", "len": 3}},
        "entry": ["close", ">", "sma3"],
        "exit": ["close", "<", "sma3"],
        "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": "kraken", "pair": "SUIUSD"}],
    }


def _arm(home: Path, *, mode: str = "paper", venue: str = "kraken", pair: str = "SUIUSD") -> ArmedPack:
    packs = home / "packs"
    packs.mkdir(parents=True, exist_ok=True)
    pack_path = packs / "trend-follow.json"
    pack_path.write_text(json.dumps(_pack()), encoding="utf-8")
    armed = ArmedPack(
        pack_path=str(pack_path),
        pack_sha256="0" * 64,
        pack_id="trend-follow",
        pack_version="1.0.0",
        venue=venue,
        pair=pair,
        cap=Decimal(100),
        stop=Decimal(5),
        mode=mode,
        starting_cash=Decimal(1000),
        requires_license=False,
        armed_at_ts=NOW - 10_000,
    )
    save_config(home, Config(armed=[armed]))
    return armed


def _journal(home: Path, records: list[dict]) -> None:
    directory = home / "journal"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "2023-11.jsonl").open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _row(ts: int, **extra) -> dict:
    return {"ts": ts, "kind": "tick", "venue": "kraken", "pack": "trend-follow", "bar_ts": ts * 1000, "detail": extra}


def _stored_job(home: Path, job_id: str, trace: list[dict]) -> None:
    directory = home / "research-jobs"
    directory.mkdir(parents=True, exist_ok=True)
    job = {
        "schema_version": "1",
        "id": job_id,
        "kind": "research.backtest",
        "state": "succeeded",
        "created_at": "2023-11-14T00:00:00Z",
        "started_at": "2023-11-14T00:00:01Z",
        "finished_at": "2023-11-14T00:00:02Z",
        "progress": None,
        "result_ref": job_id,
        "error": None,
        "correlation_id": "c",
    }
    body = {"job": job, "result": {"legacy_receipt": {"fixture": True}, "trace": trace}, "run": None}
    (directory / f"{job_id}.json").write_text(json.dumps(body), encoding="utf-8")


def test_register_read_tools_twice_registers_each_tool_once(home: Path):
    core = AgentToolCore(home)
    register_read_tools(core)
    register_read_tools(core)
    names = [spec.name for spec in core.specs(KRELL)]
    assert [n for n in names if n in READ_TOOLS] == list(READ_TOOLS)
    assert len(names) == len(set(names))
    for spec in core.specs(CURSOR):
        if spec.name in READ_TOOLS:
            assert (spec.effect, spec.cost) == ("read", "light")


def test_read_tools_never_include_live_key_material_or_env(home: Path, monkeypatch: pytest.MonkeyPatch):
    from krellbot import sanitize

    monkeypatch.setenv("KRELLBOT_TEST_ENV_SENTINEL", "env-sentinel-7781")
    monkeypatch.setenv("KRAKEN_API_SECRET", "env-secret-sentinel-4402")
    sanitize.register_secret("registered-secret-9911")
    _arm(home, mode="live")
    _journal(
        home,
        [
            _row(NOW - 5, key="journal-key-sentinel-1", secret="journal-secret-sentinel-2", reason="live_refused"),
            _row(NOW - 4, license="journal-license-sentinel-3", balance="journal-balance-sentinel-4", note="ok"),
            _row(NOW - 3, api_token="journal-token-sentinel-5", note="paper tick"),
        ],
    )
    cache.write_cache(home, "kraken", "SUIUSD", "1h", _candles(5))
    core = _core(home)
    calls = {
        "positions": {},
        "journal_recent": {"since": "24h"},
        "alerts": {},
        "unresolved_orders": {},
        "market_snapshot": {"venue": "kraken", "pair": "SUIUSD", "timeframe": "1h"},
        "catalogue": {},
        "explain_bar": {"venue": "kraken", "pair": "SUIUSD", "bar_ts": START_MS},
        "tour_step": {},
    }
    assert set(calls) == set(READ_TOOLS)
    for tool, params in calls.items():
        text = json.dumps(core.call(CURSOR, tool, params))
        for sentinel in (
            "env-sentinel-7781",
            "env-secret-sentinel-4402",
            "registered-secret-9911",
            "journal-key-sentinel-1",
            "journal-secret-sentinel-2",
            "journal-license-sentinel-3",
            "journal-balance-sentinel-4",
            "journal-token-sentinel-5",
        ):
            assert sentinel not in text, (tool, sentinel)
        assert "pack_path" not in text and str(home) not in text, tool


def test_positions_are_paper_only_and_a_live_arm_is_refused(home: Path):
    _arm(home, mode="paper")
    core = _core(home)
    ok = core.call(CURSOR, "positions", {})
    assert ok["ok"] is True
    assert [(p["venue"], p["pair"], p["mode"]) for p in ok["positions"]] == [("kraken", "SUIUSD", "paper")]
    assert ok["positions"][0]["free_cash"] == "1000"
    assert ok["labels"] == ["paper"]

    live_home = home / "live-home"
    live_home.mkdir()
    _arm(live_home, mode="live")
    live_core = _core(live_home)
    asked = live_core.call(CURSOR, "positions", {"venue": "kraken", "pair": "SUIUSD"})
    assert asked["ok"] is False and asked["code"] == "agent_live_read_refused"
    listed = live_core.call(CURSOR, "positions", {})
    assert listed["ok"] is True and listed["positions"] == [] and listed["live_excluded"] == 1


def test_journal_recent_caps_tail_at_fifty_and_marks_truncation(home: Path):
    _journal(home, [_row(NOW - 1000 + i, n=i) for i in range(80)])
    core = _core(home)

    asked = core.call(KRELL, "journal_recent", {"tail": 500})
    assert asked["ok"] is True
    assert len(asked["records"]) == 50
    assert asked["truncated"] is True
    assert [r["detail"]["n"] for r in asked["records"]] == list(range(30, 80))
    assert all("summary" in r for r in asked["records"])

    small = core.call(KRELL, "journal_recent", {"tail": 5})
    assert len(small["records"]) == 5 and small["truncated"] is False

    windowed = core.call(KRELL, "journal_recent", {"since": "24h"})
    assert len(windowed["records"]) == 50 and windowed["truncated"] is True

    bad = core.call(KRELL, "journal_recent", {"since": "1y"})
    assert bad["ok"] is False and bad["code"] == "mcp_invalid_request"
    bad_tail = core.call(KRELL, "journal_recent", {"tail": 0})
    assert bad_tail["ok"] is False and bad_tail["code"] == "mcp_invalid_request"


def test_journal_recent_leaves_out_live_records(home: Path):
    _journal(
        home,
        [_row(NOW - 3, mode="paper", n=1), _row(NOW - 2, mode="live", n=2), _row(NOW - 1, reason="live_fenced", n=3)],
    )
    result = _core(home).call(KRELL, "journal_recent", {"tail": 10})
    assert [r["detail"]["n"] for r in result["records"]] == [1]


def test_alerts_list_the_collected_rows(home: Path):
    _arm(home)
    _journal(home, [_row(NOW - 100, reason="refused", pair="SUIUSD")])
    result = _core(home).call(KRELL, "alerts", {})
    assert result["ok"] is True and isinstance(result["alerts"], list)
    for alert in result["alerts"]:
        assert set(alert) >= {"id", "kind", "severity", "acknowledged"}


def test_unresolved_orders_list_and_unreadable_store_is_a_typed_refusal(home: Path):
    core = _core(home)
    empty = core.call(KRELL, "unresolved_orders", {})
    assert empty["ok"] is True and empty["orders"] == []

    (home / "ops.sqlite").write_bytes(b"this is not a sqlite database" * 50)
    refused = core.call(KRELL, "unresolved_orders", {})
    assert refused["ok"] is False
    assert refused["code"] == "unresolved_orders_unavailable"


def test_market_snapshot_returns_last_n_cached_candles_and_freshness(home: Path):
    _arm(home)
    candles = _candles(30)
    cache.write_cache(home, "kraken", "SUIUSD", "1h", candles)
    last_ts = candles[-1].ts_ms
    _journal(
        home,
        [
            {
                "ts": NOW - 60,
                "kind": "tick",
                "venue": "kraken",
                "pack": "trend-follow",
                "bar_ts": last_ts,
                "detail": {"pair": "SUIUSD", "candle_ts": last_ts},
            }
        ],
    )
    core = _core(home)
    result = core.call(CURSOR, "market_snapshot", {"venue": "kraken", "pair": "SUIUSD", "timeframe": "1h", "bars": 12})
    assert result["ok"] is True
    assert [c["ts_ms"] for c in result["candles"]] == [c.ts_ms for c in candles[-12:]]
    assert result["candles"][-1]["close"] == str(candles[-1].close)
    assert result["rows_total"] == 30
    assert result["truncated"] is False
    assert set(result["freshness"]) >= {"last_bar_ts_ms", "candle_age_s", "candle_stale"}

    capped = core.call(CURSOR, "market_snapshot", {"venue": "kraken", "pair": "SUIUSD", "timeframe": "1h", "bars": 900})
    assert len(capped["candles"]) == 30 and capped["truncated"] is True  # 900 asked, 200 is the cap

    missing = core.call(CURSOR, "market_snapshot", {"venue": "kraken", "pair": "ETHUSD", "timeframe": "1h"})
    assert missing["ok"] is False and missing["code"] == "market_snapshot_not_found"

    for venue, pair, tf in (("../x", "SUIUSD", "1h"), ("kraken", "..\\..\\x", "1h"), ("kraken", "SUIUSD", "1h/../..")):
        bad = core.call(CURSOR, "market_snapshot", {"venue": venue, "pair": pair, "timeframe": tf})
        assert bad["ok"] is False and bad["code"] == "mcp_invalid_request", (venue, pair, tf)


def test_explain_bar_from_backtest_job_is_labelled_simulated(home: Path):
    trace = [{"bar_ts": START_MS + i * HOUR_MS, "warmup": i < 2, "target": {"long": i > 3}} for i in range(10)]
    _stored_job(home, "job_abc-123", trace)
    core = _core(home)
    result = core.call(CURSOR, "explain_bar", {"job_id": "job_abc-123", "bar_ts": START_MS + 5 * HOUR_MS})
    assert result["ok"] is True
    assert result["labels"] == [LABEL_SIMULATED]
    assert result["source"] == "backtest_job"
    assert result["bar"] == trace[5]


def test_explain_bar_unknown_bar_returns_typed_code(home: Path):
    _stored_job(home, "job1", [{"bar_ts": START_MS, "warmup": False}])
    core = _core(home)
    for params in (
        {"job_id": "job1", "bar_ts": START_MS + 999},
        {"job_id": "nosuchjob", "bar_ts": START_MS},
    ):
        result = core.call(CURSOR, "explain_bar", params)
        assert result["ok"] is False and result["code"] == "explain_bar_not_found", params
    _arm(home)
    cache.write_cache(home, "kraken", "SUIUSD", "1h", _candles(10))
    paper = core.call(CURSOR, "explain_bar", {"venue": "kraken", "pair": "SUIUSD", "bar_ts": START_MS + 1})
    assert paper["ok"] is False and paper["code"] == "explain_bar_not_found"


def test_explain_bar_refuses_malformed_job_ids_and_mixed_forms(home: Path):
    (home / "secret-outside.json").write_text("{}", encoding="utf-8")
    core = _core(home)
    for job_id in ("../secret-outside", "a/b", "x" * 65, "", "job id", "..", "a.json"):
        result = core.call(CURSOR, "explain_bar", {"job_id": job_id, "bar_ts": 1})
        assert result["ok"] is False and result["code"] == "mcp_invalid_request", job_id
    for params in (
        {"bar_ts": 1},
        {"job_id": "job1", "venue": "kraken", "pair": "SUIUSD", "bar_ts": 1},
        {"job_id": "job1", "bar_ts": "1"},
        {"job_id": "job1", "bar_ts": True},
        {"venue": "kraken", "bar_ts": 1},
    ):
        result = core.call(CURSOR, "explain_bar", params)
        assert result["ok"] is False and result["code"] == "mcp_invalid_request", params


def test_explain_bar_paper_form_recomputes_the_armed_packs_trace(home: Path):
    _arm(home)
    candles = _candles(20)
    cache.write_cache(home, "kraken", "SUIUSD", "1h", candles)
    target = candles[12]
    result = _core(home).call(KRELL, "explain_bar", {"venue": "kraken", "pair": "SUIUSD", "bar_ts": target.ts_ms})
    assert result["ok"] is True
    assert result["source"] == "paper_recompute"
    expected = paper_decision_trace(_pack(), candles)[12]
    assert result["bar"] == json.loads(json.dumps(expected))

    live_home = home / "live-home"
    live_home.mkdir()
    _arm(live_home, mode="live")
    cache.write_cache(live_home, "kraken", "SUIUSD", "1h", candles)
    refused = _core(live_home).call(KRELL, "explain_bar", {"venue": "kraken", "pair": "SUIUSD", "bar_ts": target.ts_ms})
    assert refused["ok"] is False and refused["code"] == "agent_live_read_refused"


def test_catalogue_carries_pack_disclosures_and_not_advice_label(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        tools_read.pack_library,
        "list_catalogue",
        lambda _home: {
            "catalogue_version": "7",
            "packs": [
                {
                    "id": "paid-launch-dca",
                    "tier": "paid",
                    "label": "Launch DCA",
                    "version": "1.0.0",
                    "permissions": ["catalog"],
                    "description": "Teaching pack. Not personalized advice. No returns promises.",
                    "disclosures": {"not_personalized_advice": True, "no_returns_promises": True},
                    "signature": "must-not-pass-through",
                },
                {"id": "free-one", "tier": "free", "label": "Free", "permissions": []},
            ],
        },
    )
    monkeypatch.setattr(
        tools_read.packs_app,
        "list_packs",
        lambda _home: [
            {"bucket": "installed", "pack_id": "free-one", "version": "1", "permissions": [], "rollback_ref": None}
        ],
    )
    result = _core(home).call(CURSOR, "catalogue", {})
    assert result["ok"] is True
    assert result["labels"] == [LABEL_NOT_ADVICE]
    assert result["catalogue_version"] == "7"
    by_id = {p["id"]: p for p in result["catalogue"]}
    assert by_id["paid-launch-dca"]["disclosures"] == {"not_personalized_advice": True, "no_returns_promises": True}
    assert "disclosures" not in by_id["free-one"]
    assert "signature" not in json.dumps(result)
    assert [p["pack_id"] for p in result["installed"]] == ["free-one"]
    assert "not personalized advice" in result["notice"].lower()


def test_catalogue_never_refreshes_from_the_network(home: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(*args, **kwargs):
        raise AssertionError("catalogue must read the cache only")

    monkeypatch.setattr(pack_library, "refresh_catalogue", boom)
    result = _core(home).call(CURSOR, "catalogue", {})
    assert result["ok"] is True and result["catalogue"] == []


def test_tour_step_returns_one_step_or_lists_them(home: Path):
    core = _core(home)
    listed = core.call(CURSOR, "tour_step", {})
    assert [s["id"] for s in listed["steps"]] == [s.id for s in TOUR_STEPS]
    assert "facts" not in listed["steps"][0]
    one = core.call(CURSOR, "tour_step", {"step_id": TOUR_STEPS[1].id})
    assert one["ok"] is True
    assert one["step"] == {"id": TOUR_STEPS[1].id, "title": TOUR_STEPS[1].title, "facts": list(TOUR_STEPS[1].facts)}
    unknown = core.call(CURSOR, "tour_step", {"step_id": "nope"})
    assert unknown["ok"] is False and unknown["code"] == "tour_step_not_found"


def test_oversized_results_are_trimmed_to_32_kib(home: Path):
    big = "x" * 4000
    _journal(home, [_row(NOW - 1000 + i, n=i, blob=big) for i in range(50)])
    core = _core(home)
    result = core.call(KRELL, "journal_recent", {"tail": 50})
    assert result["ok"] is True
    assert result["truncated"] is True
    assert _size(result) <= MAX_RESULT_BYTES == 32 * 1024
    kept = [r["detail"]["n"] for r in result["records"]]
    assert kept and kept[-1] == 49 and kept == sorted(kept)
    assert len(kept) < 50

    cache.write_cache(home, "kraken", "SUIUSD", "1h", _candles(200))
    snap = core.call(KRELL, "market_snapshot", {"venue": "kraken", "pair": "SUIUSD", "timeframe": "1h", "bars": 200})
    assert snap["ok"] is True and _size(snap) <= MAX_RESULT_BYTES


def test_trim_helper_drops_the_oldest_end_and_refuses_when_nothing_fits():
    body = {"rows": [{"n": i, "pad": "y" * 1000} for i in range(100)], "other": []}
    trimmed = tools_read.cap_result("t", dict(body), drop_front=("rows",))
    assert trimmed["truncated"] is True and _size(trimmed) <= MAX_RESULT_BYTES
    assert trimmed["rows"][-1]["n"] == 99

    drop_back_result = tools_read.cap_result("t", dict(body), drop_back=("rows",))
    assert drop_back_result["rows"][0]["n"] == 0

    single = tools_read.cap_result("t", {"rows": [], "blob": "z" * (MAX_RESULT_BYTES * 2)})
    assert single["ok"] is False and single["code"] == "agent_result_too_large"

    small = tools_read.cap_result("t", {"rows": [1, 2, 3]}, drop_front=("rows",))
    assert small["truncated"] is False and small["rows"] == [1, 2, 3]


def test_handlers_do_not_import_risk_path_modules():
    source = Path(tools_read.__file__).read_text(encoding="utf-8")
    for name in ("krellbot.run", "krellbot.venues", "krellbot.execution", "krellbot.keys", "krellbot.secrets"):
        assert f"import {name}" not in source and f"from {name}" not in source, name
