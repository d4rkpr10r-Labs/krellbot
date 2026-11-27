"""Every research result has a run record beside it (F22).

``research-jobs/<id>.json`` gains a third top-level key, ``run``, next to
the unchanged ``{job, result}``. The record names the run's identity:

  * ``run_id``: sha256 over the canonical JSON of 13 inputs
    (``pack_sha256``, ``data_manifest_sha256``, ``engine_version``,
    ``fee_bps``, ``slippage_bps``, ``slippage_mult``, ``from_ms``,
    ``to_ms``, ``holdout_from_ms``, ``holdout_to_ms``, ``venue``, ``pair``,
    ``tf``), serialized with ``api/jobs.canonical_receipt_bytes``;
  * the ``receipt_canonical_sha256`` the default runner computes;
  * a request echo (``revision_id``, and ``strategy_id`` = the pack's id);
  * the terminal state.

Failed and cancelled jobs get a record too, with every field the run never
resolved left null. ``GET /api/v1/jobs/{id}/run`` serves the record with a
read-only "variants tried" count: distinct ``pack_sha256`` across stored
run records sharing ``strategy_id`` and ``data_manifest_sha256``, failed
and cancelled runs included.

Every candle below is synthetic local data, never market data, and every
number is a backtest fixture, never market performance.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from krellbot import __version__ as ENGINE_VERSION
from krellbot.api.jobs import canonical_receipt_bytes, compute_run_id
from krellbot.application.research import ResearchService
from krellbot.application.strategy import StrategyDraftService
from krellbot.backtest.receipt import pack_sha256 as receipt_pack_sha256
from krellbot.strategy_ir.v1 import strip_editor
from tests.test_ns06_api import TEST_PORT, asgi_call
from tests.test_ns06_jobs import _bootstrap_session, _build_app, _cancel_job, _get_job, _submit_job

HOUR_MS = 3_600_000
VOLUME = "100"
BARS = (
    (0, "90", "90", "90", "90"),
    (HOUR_MS, "90", "90", "90", "90"),
    (2 * HOUR_MS, "90", "100", "90", "100"),
    (3 * HOUR_MS, "100", "110", "95", "110"),
    (4 * HOUR_MS, "110", "110", "100", "100"),
    (5 * HOUR_MS, "100", "100", "100", "100"),
)
# A holdout after the last bar: disjoint from every scored window below.
HOLDOUT_FROM_MS = 10 * HOUR_MS
HOLDOUT_TO_MS = 12 * HOUR_MS

RUN_INPUT_KEYS = (
    "pack_sha256",
    "data_manifest_sha256",
    "engine_version",
    "fee_bps",
    "slippage_bps",
    "slippage_mult",
    "from_ms",
    "to_ms",
    "holdout_from_ms",
    "holdout_to_ms",
    "venue",
    "pair",
    "tf",
)
RECEIPT_INPUT_KEYS = ("pack_sha256", "data_manifest_sha256", "engine_version", "fee_bps", "slippage_bps")
RUN_RECORD_KEYS = {"schema_version", "run_id", "inputs", "receipt_canonical_sha256", "request", "state"}

BASE_INPUTS = {
    "pack_sha256": "a" * 64,
    "data_manifest_sha256": "b" * 64,
    "engine_version": "0.9.7",
    "fee_bps": 40,
    "slippage_bps": 5,
    "slippage_mult": 1.0,
    "from_ms": 0,
    "to_ms": 5 * HOUR_MS,
    "holdout_from_ms": None,
    "holdout_to_ms": None,
    "venue": "kraken",
    "pair": "SUIUSD",
    "tf": "1h",
}
# One changed value per input; every one must move the id.
CHANGED_INPUT = {
    "pack_sha256": "c" * 64,
    "data_manifest_sha256": "d" * 64,
    "engine_version": "0.9.8",
    "fee_bps": 41,
    "slippage_bps": 6,
    "slippage_mult": 1.5,
    "from_ms": HOUR_MS,
    "to_ms": 4 * HOUR_MS,
    "holdout_from_ms": HOLDOUT_FROM_MS,
    "holdout_to_ms": HOLDOUT_TO_MS,
    "venue": "coinbase",
    "pair": "BTCUSD",
    "tf": "4h",
}


# ---------------------------------------------------------------------------
# Synthetic inputs
# ---------------------------------------------------------------------------


def _pack(*, strategy_id: str = "synthetic-run-identity", sma_len: int = 2) -> dict:
    return {
        "schema_version": 1,
        "id": strategy_id,
        "version": "1.0.0",
        "label": "Synthetic run identity",
        "author": "krellbot tests",
        "origin": "Synthetic regression data for research run identity; not market data.",
        "timeframe": "1h",
        "indicators": {"sma": {"fn": "sma", "src": "close", "len": sma_len}},
        "entry": ["close", "crosses_above", "sma"],
        "exit": ["close", "crosses_below", "sma"],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 10}},
        "markets": [{"venue": "kraken", "pair": "SUIUSD"}],
    }


def _write_pack(directory: Path, name: str, pack: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    # Indented, unsorted bytes: the raw-byte hash differs from the receipt identity.
    path.write_text(json.dumps(pack, indent=2), encoding="utf-8")
    return path


def _write_csv(directory: Path, name: str, *, volume: str = VOLUME) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts_ms", "open", "high", "low", "close", "volume"])
        for ts, o, h, low, c in BARS:
            writer.writerow([ts, o, h, low, c, volume])
    return path


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _read_headers(csrf: str, session: str) -> list[tuple[str, str]]:
    return [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
        ("X-Krellbot-CSRF", csrf),
        ("Cookie", f"krellbot_session={session}"),
    ]


def _get(app, path: str, headers: list[tuple[str, str]]):
    status, _hdrs, body, _cookies = asgi_call(app, method="GET", path=path, headers=headers)
    return status, (json.loads(body) if body else None)


def _wait_terminal(app, csrf: str, session: str, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    snap: dict | None = None
    while time.monotonic() < deadline:
        _s, snap = _get_job(app, csrf, session, job_id)
        if snap["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished: {snap}")


def _wait_state(app, csrf: str, session: str, job_id: str, state: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    snap: dict | None = None
    while time.monotonic() < deadline:
        _s, snap = _get_job(app, csrf, session, job_id)
        if snap["state"] == state:
            return snap
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} never reached {state}: {snap}")


def _request(pack_path: Path | None, csv_path: Path, **overrides) -> dict:
    payload: dict = {
        "kind": "research.backtest",
        "correlation_id": "run-identity",
        "dataset_csv": str(csv_path),
        "venue": "kraken",
        "fee_bps": 10,
        "slippage_bps": 20,
        "from_ms": 0,
        "to_ms": 5 * HOUR_MS,
    }
    if pack_path is not None:
        payload["pack_path"] = str(pack_path)
    payload.update(overrides)
    return payload


def _run(app, csrf: str, session: str, payload: dict, *, expect: str = "succeeded") -> str:
    status, job = _submit_job(app, csrf, session, payload)
    assert status == 200, job
    final = _wait_terminal(app, csrf, session, job["id"])
    assert final["state"] == expect, final
    return job["id"]


def _run_body(app, csrf: str, session: str, job_id: str) -> dict:
    status, body = _get(app, f"/api/v1/jobs/{job_id}/run", _read_headers(csrf, session))
    assert status == 200, body
    assert set(body) == {"schema_version", "job_id", "run", "variants_tried"}, body
    assert body["schema_version"] == "1"
    assert body["job_id"] == job_id
    assert set(body["run"]) == RUN_RECORD_KEYS, body["run"]
    assert set(body["run"]["inputs"]) == set(RUN_INPUT_KEYS), body["run"]["inputs"]
    assert set(body["run"]["request"]) == {"revision_id", "strategy_id"}, body["run"]["request"]
    return body


def _job_file(home: Path, job_id: str) -> dict:
    return json.loads((home / "research-jobs" / f"{job_id}.json").read_text(encoding="utf-8"))


def _block_runs_for(monkeypatch, pack_path: Path) -> tuple[threading.Event, threading.Event]:
    """Hold the real research run for ``pack_path`` until released."""

    entered = threading.Event()
    release = threading.Event()
    original = ResearchService.run_with_cost_reruns

    def run_with_cost_reruns(self, request):
        if Path(request.pack_path) == pack_path:
            entered.set()
            assert release.wait(10), "test never released the held run"
        return original(self, request)

    monkeypatch.setattr(ResearchService, "run_with_cost_reruns", run_with_cost_reruns)
    return entered, release


# ---------------------------------------------------------------------------
# 1. compute_run_id: the public identity helper
# ---------------------------------------------------------------------------


def test_run_id_is_sha256_of_the_canonical_thirteen_inputs() -> None:
    expected = hashlib.sha256(canonical_receipt_bytes(BASE_INPUTS)).hexdigest()
    assert compute_run_id(**BASE_INPUTS) == expected
    assert compute_run_id(**BASE_INPUTS) == compute_run_id(**dict(reversed(list(BASE_INPUTS.items()))))
    # The receipt keeps slippage_mult as a float; an int multiplier names the same run.
    assert compute_run_id(**{**BASE_INPUTS, "slippage_mult": 1}) == expected


@pytest.mark.parametrize("key", RUN_INPUT_KEYS)
def test_changing_any_one_input_changes_the_run_id(key: str) -> None:
    base = compute_run_id(**BASE_INPUTS)
    changed = compute_run_id(**{**BASE_INPUTS, key: CHANGED_INPUT[key]})
    assert changed != base


def test_every_single_input_change_gives_its_own_run_id() -> None:
    ids = {compute_run_id(**BASE_INPUTS)}
    for key in RUN_INPUT_KEYS:
        ids.add(compute_run_id(**{**BASE_INPUTS, key: CHANGED_INPUT[key]}))
    assert len(ids) == len(RUN_INPUT_KEYS) + 1


@pytest.mark.parametrize(
    "key", [key for key in RUN_INPUT_KEYS if key not in {"from_ms", "to_ms", "holdout_from_ms", "holdout_to_ms"}]
)
def test_compute_run_id_refuses_an_unresolved_input(key: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        compute_run_id(**{**BASE_INPUTS, key: None})


def test_compute_run_id_refuses_wrong_types_instead_of_coercing() -> None:
    for key, bad in (("fee_bps", "40"), ("fee_bps", True), ("from_ms", 1.5), ("slippage_mult", float("nan"))):
        with pytest.raises((TypeError, ValueError)):
            compute_run_id(**{**BASE_INPUTS, key: bad})


# ---------------------------------------------------------------------------
# 2. A succeeded job: the record beside an unchanged result
# ---------------------------------------------------------------------------


def test_succeeded_job_persists_a_run_record_beside_the_unchanged_result(home: Path) -> None:
    inputs = home / "inputs"
    pack = _pack()
    pack_path = _write_pack(inputs, "pack.json", pack)
    csv_path = _write_csv(inputs, "candles.csv")
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    job_id = _run(app, csrf, session, _request(pack_path, csv_path, slippage_mult=1.5))

    # The stored result and its route are unchanged.
    status, stored = _get(app, f"/api/v1/jobs/{job_id}/result", _read_headers(csrf, session))
    assert status == 200
    assert set(stored) == {"legacy_receipt", "trace"}
    receipt = stored["legacy_receipt"]

    # The job file gains exactly one key, and no other *.json lands under research-jobs/.
    persisted = _job_file(home, job_id)
    assert set(persisted) == {"job", "result", "run"}
    assert persisted["result"] == stored
    assert sorted(p.name for p in (home / "research-jobs").rglob("*.json")) == [f"{job_id}.json"]

    body = _run_body(app, csrf, session, job_id)
    record = body["run"]
    assert persisted["run"] == record
    assert record["schema_version"] == "1"
    assert record["state"] == "succeeded"
    assert record["receipt_canonical_sha256"] == hashlib.sha256(canonical_receipt_bytes(receipt)).hexdigest()
    assert record["request"] == {"revision_id": None, "strategy_id": "synthetic-run-identity"}

    run_inputs = record["inputs"]
    for key in RECEIPT_INPUT_KEYS:
        assert run_inputs[key] == receipt[key], key
    assert run_inputs["slippage_mult"] == receipt["slippage_mult"] == 1.5
    assert (run_inputs["venue"], run_inputs["pair"], run_inputs["tf"]) == (
        receipt["venue"],
        receipt["pair"],
        receipt["tf"],
    )
    assert run_inputs["engine_version"] == ENGINE_VERSION
    assert (run_inputs["from_ms"], run_inputs["to_ms"]) == (0, 5 * HOUR_MS)
    # Window inputs the request did not send stay null.
    assert run_inputs["holdout_from_ms"] is None and run_inputs["holdout_to_ms"] is None

    # pack_sha256 is the receipt identity, not the pack file's raw-byte hash.
    assert run_inputs["pack_sha256"] == receipt_pack_sha256(strip_editor(pack))
    assert run_inputs["pack_sha256"] != hashlib.sha256(pack_path.read_bytes()).hexdigest()
    assert run_inputs["data_manifest_sha256"] == hashlib.sha256(csv_path.read_bytes()).hexdigest()

    # The id is the public helper over the recorded inputs.
    assert record["run_id"] == compute_run_id(**run_inputs)
    assert record["run_id"] == hashlib.sha256(canonical_receipt_bytes(run_inputs)).hexdigest()
    assert body["variants_tried"] == 1


def test_a_revision_run_echoes_its_revision_id_and_the_pack_id(home: Path) -> None:
    pack = _pack(strategy_id="synthetic-revision-run")
    summary = StrategyDraftService(home=home).create(pack)
    csv_path = _write_csv(home / "inputs", "candles.csv")
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    job_id = _run(app, csrf, session, _request(None, csv_path, revision_id=summary["revision_id"]))

    record = _run_body(app, csrf, session, job_id)["run"]
    _s, stored = _get(app, f"/api/v1/jobs/{job_id}/result", _read_headers(csrf, session))
    assert record["request"] == {"revision_id": summary["revision_id"], "strategy_id": "synthetic-revision-run"}
    assert record["inputs"]["pack_sha256"] == stored["legacy_receipt"]["pack_sha256"]
    assert record["run_id"] == compute_run_id(**record["inputs"])


# ---------------------------------------------------------------------------
# 3. Identical inputs, identical ids; any change, a new id
# ---------------------------------------------------------------------------


def test_identical_inputs_give_identical_run_ids_across_runs_and_restarts(home: Path) -> None:
    inputs = home / "inputs"
    pack_path = _write_pack(inputs, "pack.json", _pack())
    csv_path = _write_csv(inputs, "candles.csv")
    payload = _request(pack_path, csv_path, holdout_from_ms=HOLDOUT_FROM_MS, holdout_to_ms=HOLDOUT_TO_MS)

    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    first = _run(app, csrf, session, payload)
    second = _run(app, csrf, session, payload)
    first_record = _run_body(app, csrf, session, first)["run"]
    second_record = _run_body(app, csrf, session, second)["run"]
    assert first != second
    assert first_record["run_id"] is not None
    assert second_record["run_id"] == first_record["run_id"]
    assert first_record["inputs"]["holdout_from_ms"] == HOLDOUT_FROM_MS
    assert first_record["inputs"]["holdout_to_ms"] == HOLDOUT_TO_MS

    restarted = _build_app(home)
    csrf2, session2 = _bootstrap_session(restarted)
    # The restored record is served unchanged after a restart...
    restored = _run_body(restarted, csrf2, session2, first)
    assert restored["run"] == first_record
    assert restored["variants_tried"] == 1
    # ...and the same inputs run again on the restarted workstation give the same id.
    third = _run(restarted, csrf2, session2, payload)
    assert _run_body(restarted, csrf2, session2, third)["run"]["run_id"] == first_record["run_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"fee_bps": 11},
        {"slippage_bps": 21},
        {"slippage_mult": 1.5},
        {"from_ms": HOUR_MS},
        {"to_ms": 4 * HOUR_MS},
        {"holdout_from_ms": HOLDOUT_FROM_MS, "holdout_to_ms": HOLDOUT_TO_MS},
        {"pack": "sma3"},
        {"dataset": "other bytes"},
    ],
    ids=lambda change: "+".join(sorted(change)),
)
def test_a_changed_request_input_changes_the_run_id(home: Path, change: dict) -> None:
    inputs = home / "inputs"
    pack_path = _write_pack(inputs, "pack.json", _pack())
    csv_path = _write_csv(inputs, "candles.csv")
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    base = _run_body(app, csrf, session, _run(app, csrf, session, _request(pack_path, csv_path)))["run"]

    change = dict(change)
    changed_pack = pack_path
    changed_csv = csv_path
    if change.pop("pack", None):
        changed_pack = _write_pack(inputs, "pack-sma3.json", _pack(sma_len=3))
    if change.pop("dataset", None):
        changed_csv = _write_csv(inputs, "candles-other.csv", volume="101")
    changed_id = _run(app, csrf, session, _request(changed_pack, changed_csv, **change))
    changed = _run_body(app, csrf, session, changed_id)["run"]
    assert changed["run_id"] is not None
    assert changed["run_id"] != base["run_id"]
    assert changed["run_id"] == compute_run_id(**changed["inputs"])


# ---------------------------------------------------------------------------
# 4. Failed and cancelled jobs: a record with unresolved fields null
# ---------------------------------------------------------------------------


def test_a_failed_job_records_what_it_resolved_and_null_for_the_rest(home: Path) -> None:
    inputs = home / "inputs"
    pack = _pack()
    pack_path = _write_pack(inputs, "pack.json", pack)
    csv_path = _write_csv(inputs, "candles.csv")
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    # The holdout overlaps the scored window: the service refuses after reading
    # the pack and the dataset, before any receipt exists.
    job_id = _run(
        app,
        csrf,
        session,
        _request(pack_path, csv_path, holdout_from_ms=HOUR_MS, holdout_to_ms=2 * HOUR_MS),
        expect="failed",
    )
    _s, snap = _get_job(app, csrf, session, job_id)
    assert snap["error"]["code"] == "holdout_overlap"

    body = _run_body(app, csrf, session, job_id)
    record = body["run"]
    assert _job_file(home, job_id)["run"] == record
    assert _job_file(home, job_id)["result"] is None
    assert record["state"] == "failed"
    assert record["receipt_canonical_sha256"] is None
    assert record["run_id"] is None
    assert record["request"] == {"revision_id": None, "strategy_id": "synthetic-run-identity"}
    assert record["inputs"] == {
        "pack_sha256": receipt_pack_sha256(strip_editor(pack)),
        "data_manifest_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "engine_version": ENGINE_VERSION,
        "fee_bps": 10,
        "slippage_bps": 20,
        # Not sent: the engine default is resolved by the run, which stopped first.
        "slippage_mult": None,
        "from_ms": 0,
        "to_ms": 5 * HOUR_MS,
        "holdout_from_ms": HOUR_MS,
        "holdout_to_ms": 2 * HOUR_MS,
        "venue": "kraken",
        # Never resolved: the run refused before it matched a market or a timeframe.
        "pair": None,
        "tf": None,
    }
    assert body["variants_tried"] == 1


def test_a_job_that_never_found_its_pack_records_no_pack_identity(home: Path) -> None:
    csv_path = _write_csv(home / "inputs", "candles.csv")
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    job_id = _run(app, csrf, session, _request(None, csv_path, revision_id="no-such-revision"), expect="failed")

    body = _run_body(app, csrf, session, job_id)
    record = body["run"]
    assert record["state"] == "failed"
    assert record["request"] == {"revision_id": "no-such-revision", "strategy_id": None}
    assert record["inputs"]["pack_sha256"] is None
    assert record["inputs"]["data_manifest_sha256"] is None
    assert record["run_id"] is None
    assert record["receipt_canonical_sha256"] is None
    # No strategy, no count: never a guessed zero.
    assert body["variants_tried"] is None


def test_cancelled_jobs_get_a_record_queued_with_nulls_running_with_resolved_inputs(home: Path, monkeypatch) -> None:
    inputs = home / "inputs"
    pack_path = _write_pack(inputs, "pack.json", _pack())
    csv_path = _write_csv(inputs, "candles.csv")
    entered, release = _block_runs_for(monkeypatch, pack_path)
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)

    status, running = _submit_job(app, csrf, session, _request(pack_path, csv_path))
    assert status == 200 and running["state"] == "running", running
    assert entered.wait(5)
    status, queued = _submit_job(app, csrf, session, _request(pack_path, csv_path, fee_bps=12))
    assert status == 200 and queued["state"] == "queued", queued

    # A queued job never ran: nothing is resolved.
    status, snap = _cancel_job(app, csrf, session, queued["id"])
    assert status == 200 and snap["state"] == "cancelled", snap
    queued_record = _run_body(app, csrf, session, queued["id"])["run"]
    assert queued_record["state"] == "cancelled"
    assert queued_record["run_id"] is None
    assert queued_record["receipt_canonical_sha256"] is None
    assert queued_record["request"] == {"revision_id": None, "strategy_id": None}
    assert queued_record["inputs"] == {key: None for key in RUN_INPUT_KEYS}
    assert _job_file(home, queued["id"])["run"] == queued_record

    # A running job cancelled mid-run keeps the inputs it resolved; no receipt is stored.
    status, snap = _cancel_job(app, csrf, session, running["id"])
    assert status == 200 and snap["state"] == "cancelling", snap
    release.set()
    final = _wait_terminal(app, csrf, session, running["id"])
    assert final["state"] == "cancelled", final
    status, body = _get(app, f"/api/v1/jobs/{running['id']}/result", _read_headers(csrf, session))
    assert status == 404 and body == {"code": "result_unavailable"}

    record = _run_body(app, csrf, session, running["id"])["run"]
    assert record["state"] == "cancelled"
    assert record["receipt_canonical_sha256"] is None
    assert record["request"] == {"revision_id": None, "strategy_id": "synthetic-run-identity"}
    assert all(record["inputs"][key] is not None for key in RUN_INPUT_KEYS if "holdout" not in key)
    assert record["inputs"]["fee_bps"] == 10
    assert record["run_id"] == compute_run_id(**record["inputs"])
    assert _job_file(home, running["id"])["result"] is None


# ---------------------------------------------------------------------------
# 5. Variants tried
# ---------------------------------------------------------------------------


def test_variants_tried_counts_distinct_packs_on_the_same_data_including_failed_and_cancelled(
    home: Path, monkeypatch
) -> None:
    inputs = home / "inputs"
    v1 = _write_pack(inputs, "v1.json", _pack(sma_len=2))
    v2 = _write_pack(inputs, "v2.json", _pack(sma_len=3))
    v3 = _write_pack(inputs, "v3.json", _pack(sma_len=4))
    other_strategy = _write_pack(inputs, "other.json", _pack(strategy_id="another-strategy", sma_len=5))
    data = _write_csv(inputs, "candles.csv")
    other_data = _write_csv(inputs, "candles-other.csv", volume="101")
    entered, release = _block_runs_for(monkeypatch, v3)
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)

    first = _run(app, csrf, session, _request(v1, data))
    assert _run_body(app, csrf, session, first)["variants_tried"] == 1
    # The same pack again at another fee is not another variant.
    _run(app, csrf, session, _request(v1, data, fee_bps=30))
    assert _run_body(app, csrf, session, first)["variants_tried"] == 1
    # A failed variant counts.
    _run(app, csrf, session, _request(v2, data, holdout_from_ms=0, holdout_to_ms=HOUR_MS), expect="failed")
    assert _run_body(app, csrf, session, first)["variants_tried"] == 2
    # A variant cancelled mid-run counts.
    status, held = _submit_job(app, csrf, session, _request(v3, data))
    assert status == 200 and entered.wait(5)
    _cancel_job(app, csrf, session, held["id"])
    release.set()
    assert _wait_terminal(app, csrf, session, held["id"])["state"] == "cancelled"
    assert _run_body(app, csrf, session, first)["variants_tried"] == 3
    # Another strategy on the same data, and the same strategy on other data, do not.
    other_strategy_job = _run(app, csrf, session, _request(other_strategy, data))
    other_data_job = _run(app, csrf, session, _request(v2, other_data))
    assert _run_body(app, csrf, session, first)["variants_tried"] == 3
    assert _run_body(app, csrf, session, held["id"])["variants_tried"] == 3
    assert _run_body(app, csrf, session, other_strategy_job)["variants_tried"] == 1
    assert _run_body(app, csrf, session, other_data_job)["variants_tried"] == 1

    # The count is read from stored records, so a restart gives the same answer.
    restarted = _build_app(home)
    csrf2, session2 = _bootstrap_session(restarted)
    assert _run_body(restarted, csrf2, session2, first)["variants_tried"] == 3


# ---------------------------------------------------------------------------
# 6. Older files and restarts
# ---------------------------------------------------------------------------


def _job_dict(job_id: str, state: str, created_at: str) -> dict:
    terminal = state in {"succeeded", "failed", "cancelled", "interrupted"}
    return {
        "schema_version": "1",
        "id": job_id,
        "kind": "research.backtest",
        "state": state,
        "created_at": created_at,
        "started_at": created_at,
        "finished_at": created_at if terminal else None,
        "progress": None,
        "result_ref": f"backtest:old:{job_id}" if state == "succeeded" else None,
        "error": None,
        "correlation_id": "old",
    }


def test_an_older_job_file_without_run_loads_and_nothing_is_requeued(home: Path) -> None:
    directory = home / "research-jobs"
    directory.mkdir(parents=True)
    old_result = {"legacy_receipt": {"engine_version": "0.9.0", "pack_sha256": "abc"}, "trace": []}
    (directory / "old-succeeded.json").write_text(
        json.dumps({"job": _job_dict("old-succeeded", "succeeded", "2026-01-01T00:00:00Z"), "result": old_result}),
        encoding="utf-8",
    )
    (directory / "old-running.json").write_text(
        json.dumps({"job": _job_dict("old-running", "running", "2026-01-01T00:00:01Z"), "result": None}),
        encoding="utf-8",
    )
    # A run record never makes a job runnable again, even beside a queued state.
    replayable = copy.deepcopy(BASE_INPUTS)
    (directory / "queued-with-run.json").write_text(
        json.dumps(
            {
                "job": _job_dict("queued-with-run", "queued", "2026-01-01T00:00:02Z"),
                "result": None,
                "run": {
                    "schema_version": "1",
                    "run_id": compute_run_id(**replayable),
                    "inputs": replayable,
                    "receipt_canonical_sha256": None,
                    "request": {"revision_id": None, "strategy_id": "synthetic-run-identity"},
                    "state": "queued",
                },
            }
        ),
        encoding="utf-8",
    )

    calls: list[dict] = []

    def runner(request):
        calls.append(dict(request))
        return {"ok": True, "result_ref": "fresh", "legacy_receipt": {}, "trace": []}

    app = _build_app(home, runner=runner)
    csrf, session = _bootstrap_session(app)
    headers = _read_headers(csrf, session)
    time.sleep(0.3)
    assert calls == []

    # The older succeeded job still loads and serves its result; it has no run record.
    _s, snap = _get_job(app, csrf, session, "old-succeeded")
    assert snap["state"] == "succeeded"
    status, body = _get(app, "/api/v1/jobs/old-succeeded/result", headers)
    assert status == 200 and body == old_result
    status, body = _get(app, "/api/v1/jobs/old-succeeded/run", headers)
    assert status == 404 and body == {"code": "run_unavailable"}
    assert "run" not in _job_file(home, "old-succeeded")

    # Unfinished jobs become interrupted, never queued or running again.
    for job_id in ("old-running", "queued-with-run"):
        _s, snap = _get_job(app, csrf, session, job_id)
        assert snap["state"] == "interrupted", snap
        record = _run_body(app, csrf, session, job_id)["run"]
        assert record["state"] == "interrupted"
        assert record["run_id"] is None
        assert record["receipt_canonical_sha256"] is None
        assert record["inputs"] == {key: None for key in RUN_INPUT_KEYS}

    # A fresh submission runs at once: no restored job holds the running slot or the queue.
    status, job = _submit_job(app, csrf, session, {"correlation_id": "fresh"})
    assert status == 200 and job["state"] == "running", job
    assert _wait_terminal(app, csrf, session, job["id"])["state"] == "succeeded"
    assert [call["correlation_id"] for call in calls] == ["fresh"]


# ---------------------------------------------------------------------------
# 7. The route
# ---------------------------------------------------------------------------


def test_run_route_keeps_the_session_and_origin_gate(home: Path) -> None:
    app = _build_app(home, runner=lambda _request: {"ok": True, "result_ref": "r", "legacy_receipt": {}, "trace": []})
    csrf, session = _bootstrap_session(app)
    _s, job = _submit_job(app, csrf, session, {"correlation_id": "gate"})
    _wait_terminal(app, csrf, session, job["id"])
    path = f"/api/v1/jobs/{job['id']}/run"
    status, _body = _get(app, path, _read_headers(csrf, session))
    assert status == 200

    no_session = [("Host", f"127.0.0.1:{TEST_PORT}"), ("Origin", f"http://127.0.0.1:{TEST_PORT}")]
    status, body = _get(app, path, no_session)
    assert status == 403 and body == {"detail": "session required"}

    status, body = _get(app, path, no_session + [("Cookie", "krellbot_session=invented")])
    assert status == 403 and body == {"detail": "session required"}

    foreign_origin = [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", "http://evil.example"),
        ("Cookie", f"krellbot_session={session}"),
    ]
    status, body = _get(app, path, foreign_origin)
    assert status == 403

    foreign_host = [("Host", "evil.example"), ("Cookie", f"krellbot_session={session}")]
    status, body = _get(app, path, foreign_host)
    assert status == 403


def test_run_route_is_unavailable_until_the_job_ends(home: Path) -> None:
    release = threading.Event()

    def runner(_request):
        assert release.wait(5)
        return {"ok": True, "result_ref": "r", "legacy_receipt": {}, "trace": []}

    app = _build_app(home, runner=runner)
    csrf, session = _bootstrap_session(app)
    headers = _read_headers(csrf, session)

    status, body = _get(app, "/api/v1/jobs/no-such-job/run", headers)
    assert status == 404 and body["code"] == "not_found"

    _s, job = _submit_job(app, csrf, session, {"correlation_id": "pending"})
    _wait_state(app, csrf, session, job["id"], "running")
    status, body = _get(app, f"/api/v1/jobs/{job['id']}/run", headers)
    assert status == 404 and body == {"code": "run_unavailable"}
    assert _job_file(home, job["id"])["run"] is None

    release.set()
    assert _wait_terminal(app, csrf, session, job["id"])["state"] == "succeeded"
    record = _run_body(app, csrf, session, job["id"])["run"]
    # An injected runner reports no identity: nothing is guessed from it.
    assert record["state"] == "succeeded"
    assert record["run_id"] is None
    assert record["receipt_canonical_sha256"] is None
    assert record["request"] == {"revision_id": None, "strategy_id": None}
