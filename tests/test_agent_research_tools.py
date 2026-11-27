"""Agent research tools: backtest with run identity, compare and cost estimates.

An agent backtest (Krell or an MCP client) returns the same run identity a
web research job records (M29.1): ``run_id``, ``receipt_canonical_sha256``,
``pack_sha256`` and ``data_manifest_sha256``, so an agent's claim can be
checked in the workstation. ``compare`` backtests 2 to 4 draft revisions on
one dataset, labelled simulated. Both run in-process and never create a job
(roadmap decision 16). Cost estimates mark work heavy above 20000 bar x pack.

Agent-supplied files are confined to the Krellbot home's data folders and
read bounded; refusals name the param, never the path or any file content.

Every candle below is synthetic local data, never market data, and every
number is a backtest fixture, never market performance.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import random
from pathlib import Path

import pytest

from krellbot.agent import estimate as agent_estimate
from krellbot.agent import tools_research
from krellbot.agent.actors import KRELL, Actor
from krellbot.agent.core import CODE_INVALID_REQUEST, LABEL_SIMULATED, AgentToolCore
from krellbot.agent.tools_research import (
    MESSAGE_DATASET_INPUT,
    MESSAGE_PACK_INPUT,
    register_research_tools,
)
from krellbot.api.jobs import JobManager, canonical_receipt_bytes
from krellbot.application.strategy import StrategyDraftService
from krellbot.mcp import McpTools
from tests.test_ns06_jobs import _bootstrap_session, _build_app
from tests.test_research_run_identity import HOUR_MS, _pack, _request, _run, _run_body, _write_csv, _write_pack

CURSOR = Actor.mcp("cursor")
RUN_KEYS = {"run_id", "receipt_canonical_sha256", "pack_sha256", "data_manifest_sha256"}


def _core(home: Path) -> AgentToolCore:
    core = AgentToolCore(home, clock=lambda: 1_700_000_000)
    register_research_tools(core)
    return core


def _cache_csv(home: Path) -> Path:
    return _write_csv(home / "cache", "kraken__SUIUSD__1h.csv")


def _bars_csv(path: Path, rows: int) -> Path:
    """Write ``rows`` flat synthetic hourly candles (data rows, plus a header)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts_ms", "open", "high", "low", "close", "volume"])
        for index in range(rows):
            writer.writerow([index * HOUR_MS, "90", "90", "90", "90", "100"])
    return path


def _revision(home: Path, *, sma_len: int, venue: str = "kraken") -> str:
    pack = _pack(strategy_id="synthetic-compare", sma_len=sma_len)
    pack["markets"] = [{"venue": venue, "pair": "SUIUSD"}]
    return StrategyDraftService(home=home).create(pack)["revision_id"]


def _run_of(record: dict) -> dict:
    return {
        "run_id": record["run_id"],
        "receipt_canonical_sha256": record["receipt_canonical_sha256"],
        "pack_sha256": record["inputs"]["pack_sha256"],
        "data_manifest_sha256": record["inputs"]["data_manifest_sha256"],
    }


# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------


def test_mcp_backtest_returns_the_same_run_id_as_a_web_job(home: Path) -> None:
    pack_path = _write_pack(home / "packs", "run-identity.json", _pack())
    csv_path = _cache_csv(home)
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    web = _run_body(app, csrf, session, _run(app, csrf, session, _request(pack_path, csv_path)))["run"]
    assert web["run_id"] is not None

    window = {"fee_bps": 10, "slippage_bps": 20, "from_ms": 0, "to_ms": 5 * HOUR_MS}
    mcp = McpTools(home, actor=CURSOR)
    result = mcp.call("backtest", pack_path=str(pack_path), dataset_csv=str(csv_path), venue="kraken", **window)
    assert result["ok"] is True, result
    assert result["labels"] == [LABEL_SIMULATED]
    assert set(result["run"]) == RUN_KEYS
    assert result["run"] == _run_of(web)
    # The digest names the receipt the tool returned.
    receipt = result["legacy_receipt"]
    assert result["run"]["receipt_canonical_sha256"] == hashlib.sha256(canonical_receipt_bytes(receipt)).hexdigest()
    assert result["run"]["data_manifest_sha256"] == hashlib.sha256(csv_path.read_bytes()).hexdigest()

    # A draft revision named by id gives the id a web job on that revision records.
    revision_id = StrategyDraftService(home=home).create(_pack(strategy_id="synthetic-revision-run"))["revision_id"]
    web_revision = _run_body(
        app, csrf, session, _run(app, csrf, session, _request(None, csv_path, revision_id=revision_id))
    )
    by_revision = McpTools(home, actor=CURSOR).call(
        "backtest", revision_id=revision_id, dataset_csv=str(csv_path), venue="kraken", **window
    )
    assert by_revision["ok"] is True, by_revision
    assert by_revision["run"] == _run_of(web_revision["run"])

    # Any changed run input gives a new id, as it does for a web job.
    shifted = mcp.call(
        "backtest",
        pack_path=str(pack_path),
        dataset_csv=str(csv_path),
        venue="kraken",
        **{**window, "from_ms": HOUR_MS},
    )
    assert shifted["ok"] is True, shifted
    assert shifted["run"]["run_id"] not in {None, result["run"]["run_id"]}


def test_backtest_with_default_inputs_matches_a_web_job_sending_none(home: Path) -> None:
    # No window, no costs, no holdout: the window bounds are null and the
    # engine's default costs come from the receipt, for both.
    pack_path = _write_pack(home / "packs", "defaults.json", _pack())
    csv_path = _cache_csv(home)
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    payload = {
        "kind": "research.backtest",
        "correlation_id": "run-identity-defaults",
        "pack_path": str(pack_path),
        "dataset_csv": str(csv_path),
    }
    web = _run_body(app, csrf, session, _run(app, csrf, session, payload))["run"]
    assert web["run_id"] is not None
    assert {key: web["inputs"][key] for key in ("from_ms", "to_ms", "holdout_from_ms", "holdout_to_ms")} == {
        "from_ms": None,
        "to_ms": None,
        "holdout_from_ms": None,
        "holdout_to_ms": None,
    }

    result = McpTools(home, actor=CURSOR).call("backtest", pack_path=str(pack_path), dataset_csv=str(csv_path))
    assert result["ok"] is True, result
    assert result["run"]["run_id"] == web["run_id"]
    assert result["run"]["receipt_canonical_sha256"] == web["receipt_canonical_sha256"]
    assert result["run"] == _run_of(web)


def test_registering_the_research_tools_twice_offers_one_compare(home: Path) -> None:
    core = AgentToolCore(home)
    assert not core.has("compare")
    assert core.has("backtest")
    register_research_tools(core)
    register_research_tools(core)
    assert core.has("compare")
    for actor in (CURSOR, KRELL):
        assert [spec.name for spec in core.specs(actor)].count("compare") == 1


def test_backtest_refuses_bad_run_inputs_and_an_ambiguous_pack_source(home: Path) -> None:
    pack_path = _write_pack(home / "packs", "run-identity.json", _pack())
    csv_path = _cache_csv(home)
    revision_id = StrategyDraftService(home=home).create(_pack(strategy_id="synthetic-ambiguous"))["revision_id"]
    core = _core(home)
    base = {"pack_path": str(pack_path), "dataset_csv": str(csv_path)}
    for bad in (
        {"fee_bps": "10"},
        {"fee_bps": True},
        {"fee_bps": -1},
        {"slippage_bps": 1.5},
        {"slippage_mult": float("nan")},
        {"slippage_mult": "1"},
        {"from_ms": 1.5},
        {"holdout_to_ms": "x"},
        {"venue": 7},
        {"pair": ["SUIUSD"]},
        {"timeframe": 60},
        {"revision_id": revision_id},
    ):
        refused = core.call(CURSOR, "backtest", {**base, **bad})
        assert refused["ok"] is False, bad
        assert refused["code"] == CODE_INVALID_REQUEST, bad
        assert refused["labels"] == [LABEL_SIMULATED]


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------


def test_compare_runs_each_revision_and_labels_rows_simulated(home: Path) -> None:
    csv_path = _cache_csv(home)
    revisions = [_revision(home, sma_len=2), _revision(home, sma_len=3), _revision(home, sma_len=4)]
    core = _core(home)
    spec = {s.name: s for s in core.specs(CURSOR)}["compare"]
    assert (spec.effect, spec.cost, spec.labels) == ("research", "heavy", (LABEL_SIMULATED,))
    assert "compare" in [s.name for s in core.specs(KRELL)]

    result = core.call(CURSOR, "compare", {"revision_ids": revisions, "dataset_csv": str(csv_path), "venue": "kraken"})
    assert result["ok"] is True, result
    assert result["tool"] == "compare"
    assert result["labels"] == [LABEL_SIMULATED]
    assert [row["revision_id"] for row in result["rows"]] == revisions

    for row in result["rows"]:
        single = core.call(
            CURSOR, "backtest", {"revision_id": row["revision_id"], "dataset_csv": str(csv_path), "venue": "kraken"}
        )
        assert single["ok"] is True, single
        assert row["ok"] is True
        assert row["metrics"] == single["legacy_receipt"]["metrics"]
        assert row["run"] == single["run"]
        assert set(row["run"]) == RUN_KEYS
    # One dataset for every row; each revision is its own run.
    assert len({row["run"]["data_manifest_sha256"] for row in result["rows"]}) == 1
    assert len({row["run"]["run_id"] for row in result["rows"]}) == len(revisions)


def test_compare_reports_a_refused_revision_in_its_row(home: Path) -> None:
    csv_path = _cache_csv(home)
    runnable = _revision(home, sma_len=2)
    no_market = _revision(home, sma_len=3, venue="coinbase")
    result = _core(home).call(
        CURSOR, "compare", {"revision_ids": [runnable, no_market], "dataset_csv": str(csv_path), "venue": "kraken"}
    )
    assert result["ok"] is True, result
    ok_row, refused_row = result["rows"]
    assert ok_row["ok"] is True and ok_row["run"]["run_id"]
    assert refused_row == {
        "revision_id": no_market,
        "ok": False,
        "code": "pack_has_no_market",
        "message": refused_row["message"],
        "metrics": None,
        "run": None,
    }


def test_compare_rejects_fewer_than_two_or_more_than_four_revisions(home: Path) -> None:
    csv_path = _cache_csv(home)
    revisions = [_revision(home, sma_len=length) for length in (2, 3, 4, 5, 6)]
    core = _core(home)
    for bad in (None, [], revisions[:1], revisions[:5], revisions[0], [revisions[0], revisions[0]]):
        params: dict = {"dataset_csv": str(csv_path), "venue": "kraken"}
        if bad is not None:
            params["revision_ids"] = bad
        refused = core.call(CURSOR, "compare", params)
        assert refused["ok"] is False, bad
        assert refused["code"] == CODE_INVALID_REQUEST, bad
        assert refused["labels"] == [LABEL_SIMULATED]

    # A malformed or unknown id is refused before anything runs.
    for bad_ids in ([revisions[0], "nope"], [revisions[0], "f" * 64]):
        refused = core.call(CURSOR, "compare", {"revision_ids": bad_ids, "dataset_csv": str(csv_path)})
        assert refused["code"] == CODE_INVALID_REQUEST, bad_ids
    # Compare needs one pinned dataset, so every row scores the same candles.
    assert core.call(CURSOR, "compare", {"revision_ids": revisions[:2]})["code"] == CODE_INVALID_REQUEST

    accepted = core.call(CURSOR, "compare", {"revision_ids": revisions[:4], "dataset_csv": str(csv_path)})
    assert accepted["ok"] is True, accepted
    assert len(accepted["rows"]) == 4


# ---------------------------------------------------------------------------
# Cost estimates
# ---------------------------------------------------------------------------


def test_estimate_marks_long_backtests_heavy_and_short_ones_light(home: Path) -> None:
    long_csv = _bars_csv(home / "cache" / "long.csv", 20001)
    edge_csv = _bars_csv(home / "cache" / "edge.csv", 20000)
    short_csv = _bars_csv(home / "cache" / "short.csv", 100)

    heavy = agent_estimate.estimate("backtest", {"dataset_csv": str(long_csv)}, home=home)
    assert (heavy.cost, heavy.bars, heavy.packs) == ("heavy", 20001, 1)
    edge = agent_estimate.estimate("backtest", {"dataset_csv": str(edge_csv)}, home=home)
    assert (edge.cost, edge.bars, edge.packs) == ("light", 20000, 1)
    light = agent_estimate.estimate("backtest", {"dataset_csv": str(short_csv)}, home=home)
    assert (light.cost, light.bars, light.packs) == ("light", 100, 1)
    assert light.to_dict() == {"cost": "light", "bars": 100, "packs": 1, "seconds": None, "result_tokens": None}

    # Compare and paper arm are always heavy; bars x packs is still reported.
    revisions = ["a" * 64, "b" * 64, "c" * 64]
    compare = agent_estimate.estimate("compare", {"revision_ids": revisions, "dataset_csv": str(short_csv)}, home=home)
    assert (compare.cost, compare.bars, compare.packs) == ("heavy", 100, 3)
    assert agent_estimate.estimate("paper_arm", {"venue": "kraken"}, home=home).cost == "heavy"
    assert agent_estimate.estimate("read", {}, home=home).cost == "light"
    # Unknown size (no dataset to count, or one that may not be read) is heavy.
    unknown = agent_estimate.estimate("backtest", {}, home=home)
    assert (unknown.cost, unknown.bars) == ("heavy", None)


def test_bar_count_ignores_the_header_blank_lines_and_a_missing_trailing_newline(home: Path) -> None:
    rows = [f"{index * HOUR_MS},90,90,90,90,100" for index in range(5)]
    header = "ts_ms,open,high,low,close,volume"
    with_newline = ("\n".join([header, *rows]) + "\n").encode()
    without_newline = "\n".join([header, *rows]).encode()
    crlf_with_blanks = ("\r\n".join([header, "", *rows[:2], "  \t", *rows[2:], ""]) + "\r\n\r\n").encode()
    no_header = "\n".join(rows).encode()
    for raw in (with_newline, without_newline, crlf_with_blanks, no_header):
        assert tools_research.count_csv_bars(raw) == 5, raw
    assert tools_research.count_csv_bars(b"") == 0
    assert tools_research.count_csv_bars(b"\n\n  \n") == 0
    assert tools_research.count_csv_bars(header.encode()) == 0
    assert tools_research.count_csv_bars(b"ts_ms,open\n1,2") == 1

    # The estimate counts the file the same way, trailing newline or not.
    (home / "cache").mkdir(parents=True, exist_ok=True)
    for name, raw in (("with.csv", with_newline), ("without.csv", without_newline)):
        (home / "cache" / name).write_bytes(raw)
        assert agent_estimate.estimate("backtest", {"dataset_csv": f"cache/{name}"}, home=home).bars == 5


def test_a_long_cr_only_dataset_is_counted_and_heavy(home: Path) -> None:
    # The engine reads CR-only line breaks (classic Mac exports), so the
    # estimate must count them, or a heavy run would skip the confirm gate.
    header = "ts_ms,open,high,low,close,volume"
    rows = [f"{index * HOUR_MS},90,90,90,90,100" for index in range(20001)]
    (home / "cache").mkdir(parents=True, exist_ok=True)
    long_cr = home / "cache" / "long-cr.csv"
    long_cr.write_bytes(("\r".join([header, *rows]) + "\r").encode())
    heavy = agent_estimate.estimate("backtest", {"dataset_csv": str(long_cr)}, home=home)
    assert (heavy.cost, heavy.bars) == ("heavy", 20001)

    short_cr = home / "cache" / "short-cr.csv"
    short_cr.write_bytes("\r".join([header, *rows[:6]]).encode())
    assert agent_estimate.estimate("backtest", {"dataset_csv": str(short_cr)}, home=home).bars == 6
    pack_path = _write_pack(home / "packs", "cr.json", _pack())
    ran = _core(home).call(CURSOR, "backtest", {"pack_path": str(pack_path), "dataset_csv": str(short_cr)})
    assert ran["ok"] is True, ran
    assert ran["detail"]["bar_count"] == 6


def test_bar_count_never_undercounts_what_the_engine_loader_reads() -> None:
    from krellbot.data.cache import _parse_csv

    breaks = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]
    pieces = ["ts_ms,open,high,low,close,volume", "", "  ", "\t", " ts_ms,o,h,l,c,v", "x,y", " "]
    rng = random.Random(38)
    for _ in range(3000):
        lines = [
            rng.choice(pieces) if rng.random() < 0.4 else f"{rng.randint(0, 10**12)},1,2,0.5,1.5,10"
            for _ in range(rng.randint(0, 12))
        ]
        text = "".join(line + rng.choice(breaks) for line in lines)
        if lines and rng.random() < 0.5:
            text = text[: -1 if not text.endswith("\r\n") else -2]
        raw = text.encode("utf-8")
        assert tools_research.count_csv_bars(raw) >= len(_parse_csv(raw)), raw
    # One kind of break, header first, no blank lines: exact.
    for brk in breaks:
        rows = [f"{index},1,2,0.5,1.5,10" for index in range(7)]
        raw = brk.join(["ts_ms,open,high,low,close,volume", *rows]).encode("utf-8")
        assert tools_research.count_csv_bars(raw) == len(_parse_csv(raw)) == 7, brk


# ---------------------------------------------------------------------------
# No jobs over MCP (decision 16)
# ---------------------------------------------------------------------------


def test_mcp_research_tools_create_no_jobs(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_jobs(*_args, **_kwargs):
        raise AssertionError("agent research must not create a job")

    monkeypatch.setattr(JobManager, "__init__", no_jobs)
    monkeypatch.setattr(JobManager, "submit", no_jobs)
    pack_path = _write_pack(home / "packs", "run-identity.json", _pack())
    csv_path = _cache_csv(home)
    revisions = [_revision(home, sma_len=2), _revision(home, sma_len=3)]
    core = _core(home)

    backtest = core.call(CURSOR, "backtest", {"pack_path": str(pack_path), "dataset_csv": str(csv_path)})
    assert backtest["ok"] is True, backtest
    compare = core.call(CURSOR, "compare", {"revision_ids": revisions, "dataset_csv": str(csv_path)})
    assert compare["ok"] is True, compare
    krell = core.call(KRELL, "backtest", {"revision_id": revisions[0], "dataset_csv": str(csv_path)})
    assert krell["ok"] is True, krell

    for name in ("research-jobs", "research-fills", "research-costs"):
        directory = home / name
        assert not directory.exists() or not any(directory.iterdir()), name


# ---------------------------------------------------------------------------
# Input paths: confined to the home, bounded, never echoed
# ---------------------------------------------------------------------------


def _outside_paths(home: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(pack_paths, dataset_paths)`` that must be refused, with a content marker each."""
    marker = "OUTSIDE-SECRET-MARKER"
    outside_pack = home.parent / f"{home.name}-outside.json"
    outside_pack.write_text(json.dumps({**_pack(), "label": marker}), encoding="utf-8")
    outside_csv = home.parent / f"{home.name}-outside.csv"
    _bars_csv(outside_csv, 10)

    packs: list[tuple[str, str]] = [
        ("/etc/passwd", "root:"),
        (str(outside_pack), marker),
        (str(home / "packs" / ".." / ".." / outside_pack.name), marker),
        (f"packs/../../{outside_pack.name}", marker),
    ]
    datasets: list[tuple[str, str]] = [
        ("/etc/passwd", "root:"),
        (str(outside_csv), "ts_ms"),
        (str(home / "cache" / ".." / ".." / outside_csv.name), "ts_ms"),
    ]
    # Inside the home but outside the allowed folders, or the wrong kind of file.
    (home / "config.json").write_text(json.dumps({"label": marker}), encoding="utf-8")
    packs.append((str(home / "config.json"), marker))
    datasets.append((str(home / "config.json"), marker))
    (home / "journal").mkdir(exist_ok=True)
    (home / "journal" / "notes.csv").write_text(f"{marker}\n", encoding="utf-8")
    datasets.append((str(home / "journal" / "notes.csv"), marker))
    packs.append((str(_write_csv(home / "cache", "kraken__SUIUSD__1h.csv")), "ts_ms"))

    if os.name != "nt":
        (home / "packs").mkdir(exist_ok=True)
        (home / "cache").mkdir(exist_ok=True)
        pack_link = home / "packs" / "linked.json"
        pack_link.symlink_to(outside_pack)
        csv_link = home / "cache" / "linked.csv"
        csv_link.symlink_to(outside_csv)
        packs.append((str(pack_link), marker))
        datasets.append((str(csv_link), "ts_ms"))
        fifo = home / "cache" / "pipe.csv"
        os.mkfifo(fifo)
        datasets.append((str(fifo), ""))
        if Path("/dev/zero").exists():
            zero_pack = home / "packs" / "zero.json"
            zero_pack.symlink_to("/dev/zero")
            zero_csv = home / "cache" / "zero.csv"
            zero_csv.symlink_to("/dev/zero")
            packs.extend([("/dev/zero", ""), (str(zero_pack), "")])
            datasets.extend([("/dev/zero", ""), (str(zero_csv), "")])
    return packs, datasets


def _assert_refused_without_echo(result: dict, message: str, path: str, marker: str) -> None:
    assert result["ok"] is False, (path, result)
    assert result["code"] == CODE_INVALID_REQUEST, (path, result)
    assert result["message"] == message, (path, result)
    text = json.dumps(result)
    assert path not in text
    if marker:
        assert marker not in text


def test_research_tools_refuse_files_outside_the_home_data_folders_without_echoing_them(home: Path) -> None:
    pack_paths, dataset_paths = _outside_paths(home)
    good_pack = str(_write_pack(home / "packs", "good.json", _pack()))
    good_csv = str(home / "cache" / "kraken__SUIUSD__1h.csv")
    revisions = [_revision(home, sma_len=2), _revision(home, sma_len=3)]
    core = _core(home)

    for path, marker in pack_paths:
        result = core.call(CURSOR, "backtest", {"pack_path": path, "dataset_csv": good_csv})
        _assert_refused_without_echo(result, MESSAGE_PACK_INPUT, path, marker)
    for path, marker in dataset_paths:
        for tool, params in (
            ("backtest", {"pack_path": good_pack, "dataset_csv": path}),
            ("compare", {"revision_ids": revisions, "dataset_csv": path}),
        ):
            _assert_refused_without_echo(core.call(CURSOR, tool, params), MESSAGE_DATASET_INPUT, path, marker)
        # The estimate never reads them either.
        assert agent_estimate.estimate("backtest", {"dataset_csv": path}, home=home).bars is None
    assert not (home / "research-jobs").exists()

    # Files in the allowed folders (and a .csv in the home folder itself) run.
    top_level_csv = _write_csv(home, "data.csv")
    for params in (
        {"pack_path": good_pack, "dataset_csv": good_csv},
        {"pack_path": "packs/good.json", "dataset_csv": "cache/kraken__SUIUSD__1h.csv"},
        {"pack_path": good_pack, "dataset_csv": str(top_level_csv)},
        {"revision_id": revisions[0], "dataset_csv": good_csv},
    ):
        assert core.call(CURSOR, "backtest", params)["ok"] is True, params


def test_research_reads_are_bounded(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_path = _write_pack(home / "packs", "good.json", _pack())
    csv_path = _bars_csv(home / "cache" / "kraken__SUIUSD__1h.csv", 6)
    monkeypatch.setattr(tools_research, "MAX_DATASET_BYTES", csv_path.stat().st_size - 1)
    core = _core(home)
    refused = core.call(CURSOR, "backtest", {"pack_path": str(pack_path), "dataset_csv": str(csv_path)})
    assert refused["code"] == CODE_INVALID_REQUEST
    assert refused["message"] == MESSAGE_DATASET_INPUT
    assert agent_estimate.estimate("backtest", {"dataset_csv": str(csv_path)}, home=home).bars is None

    monkeypatch.setattr(tools_research, "MAX_DATASET_BYTES", csv_path.stat().st_size)
    assert core.call(CURSOR, "backtest", {"pack_path": str(pack_path), "dataset_csv": str(csv_path)})["ok"] is True
    assert agent_estimate.estimate("backtest", {"dataset_csv": str(csv_path)}, home=home).bars == 6


def test_backtest_refusals_carry_fixed_messages_never_file_content(home: Path) -> None:
    marker = "PACK-CONTENT-MARKER"
    bad_pack = _pack()
    bad_pack["timeframe"] = marker
    bad_pack["indicators"] = {"sma": {"fn": marker, "src": "close", "len": 2}}
    pack_path = _write_pack(home / "packs", "bad.json", bad_pack)
    csv_path = _cache_csv(home)
    result = _core(home).call(CURSOR, "backtest", {"pack_path": str(pack_path), "dataset_csv": str(csv_path)})
    assert result["ok"] is False
    assert result["code"] == "invalid_pack"
    assert marker not in json.dumps(result)
    assert str(pack_path) not in json.dumps(result)

    unsupported = _pack()
    unsupported["timeframe"] = "7h"
    tf_path = _write_pack(home / "packs", "tf.json", unsupported)
    tf = _core(home).call(CURSOR, "backtest", {"pack_path": str(tf_path), "dataset_csv": str(csv_path)})
    assert tf["ok"] is False
    assert "7h" not in json.dumps(tf)

    # Out-of-order candles: the engine's refusal text quotes the timestamps it read.
    good_pack = _write_pack(home / "packs", "good.json", _pack())
    unordered = home / "cache" / "unordered.csv"
    unordered.write_text(
        "ts_ms,open,high,low,close,volume\n987654321000,90,90,90,90,100\n3600000,90,90,90,90,100\n",
        encoding="utf-8",
    )
    gapped = _core(home).call(CURSOR, "backtest", {"pack_path": str(good_pack), "dataset_csv": str(unordered)})
    assert gapped["ok"] is False
    assert gapped["code"] == "gapped_data"
    assert "987654321000" not in json.dumps(gapped)
