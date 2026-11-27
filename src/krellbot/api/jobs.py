"""Bounded research jobs and the SSE event buffer.

This module owns the queue, cancellation, and event buffer that the
v1 API exposes at:

  * ``POST /api/v1/research/jobs``
  * ``GET  /api/v1/jobs/{id}``
  * ``POST /api/v1/jobs/{id}/cancel``
  * ``GET  /api/v1/jobs/{id}/fills``
  * ``GET  /api/v1/jobs/{id}/costs`` (side-car, see ``costs_for``)
  * ``GET  /api/v1/jobs/{id}/run`` (run record, see ``run_for``)
  * ``GET  /api/v1/events``

Jobs expose bounded execution and typed observable status.
Job and Event dataclasses are versioned and stable. The buffer is bounded
because events are a notification channel, not the ledger — authoritative
state lives on disk and a client that misses a window may receive a
``resync_required`` marker and re-query the snapshot endpoints.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import math
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from krellbot.paths import atomic_write

SCHEMA_VERSION = "1"

DATASET_CATALOG_FILENAME = "datasets.json"

JOB_KIND_RESEARCH_BACKTEST = "research.backtest"
JOB_KIND_EXPORT_REPRODUCIBILITY = "export.reproducibility"

JOB_STATE_QUEUED = "queued"
JOB_STATE_RUNNING = "running"
JOB_STATE_CANCELLING = "cancelling"
JOB_STATE_CANCELLED = "cancelled"
JOB_STATE_SUCCEEDED = "succeeded"
JOB_STATE_FAILED = "failed"
JOB_STATE_INTERRUPTED = "interrupted"

JOB_STATES_TERMINAL = frozenset({JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED, JOB_STATE_INTERRUPTED})
JOB_STATES_CANCEL = frozenset({JOB_STATE_RUNNING, JOB_STATE_CANCELLING})

JOB_ERROR_QUEUE_FULL = "queue_full"
JOB_ERROR_JOB_FAILED = "job_failed"
JOB_ERROR_MISSING_RESULT_REF = "missing_result_ref"
JOB_ERROR_NOT_FOUND = "not_found"
JOB_ERROR_RESULT_UNAVAILABLE = "result_unavailable"
JOB_ERROR_FILLS_UNAVAILABLE = "fills_unavailable"
JOB_ERROR_COSTS_UNAVAILABLE = "costs_unavailable"
JOB_ERROR_RUN_UNAVAILABLE = "run_unavailable"

# Research side-cars live outside ``research-jobs/``: ``_restore_jobs``
# parses every ``*.json`` file there as a job record.
FILLS_DIRNAME = "research-fills"
COSTS_DIRNAME = "research-costs"
# Job ids are ``secrets.token_urlsafe`` text; a side-car path is only ever
# built from such an id, so a restored record cannot point outside its dir.
_SIDE_CAR_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

CODE_INVALID_DATASET = "invalid_dataset"
CODE_NO_DATASET_AND_NO_FETCH = "no_dataset_and_no_fetch"
CODE_INVALID_STARTING_CASH = "invalid_starting_cash"

# Allowed concurrency per the brief: one running + up to four queued.
MAX_RUNNING_JOBS = 1
MAX_QUEUED_JOBS = 4

# Bounded event buffer; per the contract, "events are not the ledger".
EVENT_BUFFER_LIMIT = 256

EVENT_KIND_JOB_CHANGED = "job.changed"
EVENT_KIND_RESYNC_REQUIRED = "resync_required"


@dataclass
class JobV1:
    """Versioned snapshot of a single job's state."""

    schema_version: str = SCHEMA_VERSION
    id: str = ""
    kind: str = JOB_KIND_RESEARCH_BACKTEST
    state: str = JOB_STATE_QUEUED
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict | None = None
    result_ref: str | None = None
    error: dict | None = None
    correlation_id: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "result_ref": self.result_ref,
            "error": self.error,
            "correlation_id": self.correlation_id,
        }


@dataclass
class EventV1:
    """Versioned event emitted by the manager."""

    schema_version: str = SCHEMA_VERSION
    seq: int = 0
    event_id: str = ""
    kind: str = ""
    occurred_at: str = ""
    subject_id: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "seq": self.seq,
            "event_id": self.event_id,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "subject_id": self.subject_id,
            "payload": dict(self.payload),
        }


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_job_id() -> str:
    return secrets.token_urlsafe(12)


def _extract_result(outcome: dict) -> dict:
    """Build the stored result object from a successful runner outcome.

    Returns a dict with ``legacy_receipt`` and ``trace`` keys. The bytes
    are kept exactly as the runner returned them — no key injection, no
    zero-substitution for missing fee / equity / drawdown values. The
    contract is that the receipt is byte-identical to the runner's view.
    """

    receipt = outcome.get("legacy_receipt")
    trace = outcome.get("trace")
    out: dict = {}
    if isinstance(receipt, dict):
        out["legacy_receipt"] = receipt
    if isinstance(trace, list):
        out["trace"] = trace
    return out


def canonical_receipt_bytes(receipt: dict) -> bytes:
    """Return the canonical UTF-8 bytes of a ``legacy_receipt`` dict.

    The runner's result path uses this function to compute the
    ``receipt_canonical_sha256`` digest that ships with the stored result.
    The contract is that the bytes are byte-identical to
    ``json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")``.
    Missing keys stay absent; the runner never adds ``equity`` or
    ``fill_price`` here.
    """

    return json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------

RUN_RECORD_SCHEMA_VERSION = "1"

# The 13 inputs a run id hashes.
RUN_ID_INPUT_KEYS: tuple[str, ...] = (
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
_RUN_ID_TEXT_KEYS = frozenset({"pack_sha256", "data_manifest_sha256", "engine_version", "venue", "pair", "tf"})
_RUN_ID_INT_KEYS = frozenset({"fee_bps", "slippage_bps"})
# A window bound the request did not send is null, and still a known input.
_RUN_ID_WINDOW_KEYS = frozenset({"from_ms", "to_ms", "holdout_from_ms", "holdout_to_ms"})
# The run inputs the receipt itself states (the window bounds are not among them).
_RECEIPT_RUN_INPUT_KEYS: tuple[str, ...] = (
    "pack_sha256",
    "data_manifest_sha256",
    "engine_version",
    "fee_bps",
    "slippage_bps",
    "slippage_mult",
    "venue",
    "pair",
    "tf",
)


def _run_input_value(key: str, value: Any) -> Any:
    """Return ``value`` as the run id serializes it; raise on a wrong type."""

    if key in _RUN_ID_TEXT_KEYS:
        if not isinstance(value, str) or not value:
            raise TypeError(f"{key} must be a non-empty string")
        return value
    if key in _RUN_ID_INT_KEYS:
        if type(value) is not int:
            raise TypeError(f"{key} must be an int")
        return value
    if key in _RUN_ID_WINDOW_KEYS:
        if value is not None and type(value) is not int:
            raise TypeError(f"{key} must be an int or None")
        return value
    if key == "slippage_mult":
        if type(value) not in (int, float):
            raise TypeError("slippage_mult must be a number")
        mult = float(value)
        if not math.isfinite(mult):
            raise ValueError("slippage_mult must be finite")
        # The receipt keeps the multiplier as a float, so 1 and 1.0 name one run.
        return mult
    raise KeyError(key)


def compute_run_id(
    *,
    pack_sha256: str,
    data_manifest_sha256: str,
    engine_version: str,
    fee_bps: int,
    slippage_bps: int,
    slippage_mult: float,
    from_ms: int | None,
    to_ms: int | None,
    holdout_from_ms: int | None,
    holdout_to_ms: int | None,
    venue: str,
    pair: str,
    tf: str,
) -> str:
    """Return a research run's id: hex sha256 over its 13 canonical inputs.

    The inputs are serialized with ``canonical_receipt_bytes``, so identical
    inputs give the identical id in any process and any input change gives
    a new one. Every input must be resolved: only the four window bounds
    may be ``None``, meaning the request did not send that bound. A wrong
    type raises instead of being coerced.

    How the job layer fills a run record (``_build_run_record``):

      * ``pack_sha256`` is the receipt identity,
        ``backtest.receipt.pack_sha256(strip_editor(pack))``, and
        ``data_manifest_sha256`` is the sha256 of the dataset CSV bytes.
        The default runner resolves both itself before the run, so a failed
        run still records them and counts toward "variants tried".
      * ``run_id`` is null unless all 13 inputs are known. A window bound
        the request did not send stays null and counts as known.
      * A job cancelled mid-run keeps the inputs it resolved; its
        ``receipt_canonical_sha256`` is null because no result is stored.
    """

    values = {
        "pack_sha256": pack_sha256,
        "data_manifest_sha256": data_manifest_sha256,
        "engine_version": engine_version,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "slippage_mult": slippage_mult,
        "from_ms": from_ms,
        "to_ms": to_ms,
        "holdout_from_ms": holdout_from_ms,
        "holdout_to_ms": holdout_to_ms,
        "venue": venue,
        "pair": pair,
        "tf": tf,
    }
    canonical = {key: _run_input_value(key, values[key]) for key in RUN_ID_INPUT_KEYS}
    return hashlib.sha256(canonical_receipt_bytes(canonical)).hexdigest()


def _build_run_record(
    *,
    state: str,
    request: dict,
    identity: dict | None,
    receipt: dict | None,
    receipt_digest: str | None,
) -> dict:
    """Build the terminal run record stored as ``run`` in the job file.

    ``identity`` holds what the runner resolved (only keys it resolved are
    present); ``receipt`` is the run's own receipt when it completed and
    overrides ``identity`` for the inputs it states. Fields the run never
    resolved are null, never guessed, and ``run_id`` is null unless every
    input is known (see ``compute_run_id``).
    """

    known: dict[str, Any] = {}
    if isinstance(identity, dict):
        for key in RUN_ID_INPUT_KEYS:
            if key in identity:
                try:
                    known[key] = _run_input_value(key, identity[key])
                except (TypeError, ValueError):
                    continue
    if isinstance(receipt, dict):
        for key in _RECEIPT_RUN_INPUT_KEYS:
            if key not in receipt:
                continue
            try:
                known[key] = _run_input_value(key, receipt[key])
            except (TypeError, ValueError):
                known.pop(key, None)

    strategy_id = identity.get("strategy_id") if isinstance(identity, dict) else None
    if not isinstance(strategy_id, str) or not strategy_id:
        strategy_id = None
    elif isinstance(receipt, dict) and identity.get("pack_sha256") != known.get("pack_sha256"):
        # The run scored other pack bytes than the job layer read: not this id.
        strategy_id = None

    # The default runner prefers ``pack_path``; a revision is echoed only when it was the source.
    revision_id = request.get("revision_id")
    if not isinstance(revision_id, str) or not revision_id or request.get("pack_path"):
        revision_id = None

    complete = len(known) == len(RUN_ID_INPUT_KEYS)
    return {
        "schema_version": RUN_RECORD_SCHEMA_VERSION,
        "run_id": compute_run_id(**known) if complete else None,
        "inputs": {key: known.get(key) for key in RUN_ID_INPUT_KEYS},
        "receipt_canonical_sha256": receipt_digest,
        "request": {"revision_id": revision_id, "strategy_id": strategy_id},
        "state": state,
    }


def _record_text(record: Any, section: str, key: str) -> str | None:
    """Read one non-empty string from a stored run record, or ``None``."""

    if not isinstance(record, dict):
        return None
    part = record.get(section)
    if not isinstance(part, dict):
        return None
    value = part.get(key)
    return value if isinstance(value, str) and value else None


def _pack_identity(path: Path) -> dict:
    """Resolve a pack file's receipt identity and strategy id, or nothing.

    ``pack_sha256`` is the receipt's identity: canonical JSON of the
    editor-stripped pack (``backtest.receipt.pack_sha256``), never the
    file's raw-byte hash. A file that is not a JSON object resolves nothing.
    """

    from krellbot.backtest.receipt import pack_sha256
    from krellbot.strategy_ir.v1 import strip_editor

    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(pack, dict):
        return {}
    out: dict = {"pack_sha256": pack_sha256(strip_editor(pack))}
    strategy_id = pack.get("id")
    if isinstance(strategy_id, str) and strategy_id:
        out["strategy_id"] = strategy_id
    return out


def _dataset_identity(path: Path) -> dict:
    """Resolve a dataset CSV's manifest digest (sha256 of its bytes), or nothing."""

    from krellbot.data.cache import sha256_bytes

    try:
        body = path.read_bytes()
    except OSError:
        return {}
    return {"data_manifest_sha256": sha256_bytes(body)}


def _resolve_dataset_id(home: Path, dataset_id: Any, dataset_csv: Any) -> tuple[Any, dict | None]:
    """Resolve a ``dataset_id`` to its catalog fixture CSV path.

    Returns ``(resolved_csv, refusal)``. ``refusal`` is ``None`` on a
    successful resolution. A truthy non-string ``dataset_id`` is not
    coerced into a path: it returns ``invalid_dataset``.
    """

    if dataset_id is None or (isinstance(dataset_id, str) and not dataset_id):
        return dataset_csv, None
    if not isinstance(dataset_id, str):
        refusal = {"code": CODE_INVALID_DATASET, "message": "dataset_id must be a string"}
        return None, refusal

    catalog_path = home / DATASET_CATALOG_FILENAME
    if not catalog_path.exists():
        refusal = {"code": CODE_INVALID_DATASET, "message": "dataset catalog not found"}
        return None, refusal
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        refusal = {"code": CODE_INVALID_DATASET, "message": "dataset catalog unreadable"}
        return None, refusal
    if not isinstance(catalog, dict):
        refusal = {"code": CODE_INVALID_DATASET, "message": "dataset catalog malformed"}
        return None, refusal

    resolved = catalog.get(dataset_id)
    if not isinstance(resolved, str) or not resolved:
        refusal = {"code": CODE_INVALID_DATASET, "message": f"dataset_id {dataset_id!r} not in catalog"}
        return None, refusal

    if isinstance(dataset_csv, str) and dataset_csv and dataset_csv != resolved:
        refusal = {
            "code": CODE_INVALID_DATASET,
            "message": "dataset_csv does not match the catalog fixture",
        }
        return None, refusal

    return resolved, None


def _coerce_starting_cash(value: Any) -> tuple[Decimal, dict | None]:
    """Validate ``value`` at the job boundary and return a Decimal or refusal.

    A missing ``value`` defaults to ``Decimal(10000)``. Any other type —
    bool, list, dict, bytes, etc. — is rejected with ``invalid_starting_cash``.
    Strings must parse as a finite, positive Decimal: ``"abc"``, ``"NaN"``,
    ``"Infinity"``, ``"-5"``, ``"0"`` all refuse. ``True`` (bool) refuses
    because Python treats it as an int subclass but never as money.
    """
    from decimal import Decimal as _Decimal

    if value is None:
        return _Decimal(10000), None
    if type(value) is bool:
        return _Decimal(0), {
            "code": CODE_INVALID_STARTING_CASH,
            "message": "starting_cash must be a number or numeric string",
        }
    if isinstance(value, int):
        if value <= 0:
            return _Decimal(0), {
                "code": CODE_INVALID_STARTING_CASH,
                "message": "starting_cash must be greater than zero",
            }
        return _Decimal(value), None
    if isinstance(value, float):
        if value <= 0 or not math.isfinite(value):
            return _Decimal(0), {
                "code": CODE_INVALID_STARTING_CASH,
                "message": "starting_cash must be a finite number greater than zero",
            }
        return _Decimal(str(value)), None
    if isinstance(value, str):
        try:
            parsed = _Decimal(value)
        except (ArithmeticError, ValueError, TypeError):
            return _Decimal(0), {
                "code": CODE_INVALID_STARTING_CASH,
                "message": "starting_cash must be a numeric string",
            }
        if not parsed.is_finite() or parsed <= 0:
            return _Decimal(0), {
                "code": CODE_INVALID_STARTING_CASH,
                "message": "starting_cash must be a finite number greater than zero",
            }
        return parsed, None
    return _Decimal(0), {
        "code": CODE_INVALID_STARTING_CASH,
        "message": "starting_cash must be a number or numeric string",
    }


# ---------------------------------------------------------------------------
# Event buffer
# ---------------------------------------------------------------------------


class EventBuffer:
    """Thread-safe bounded ring buffer of :class:`EventV1`.

    The buffer is the M1 source for SSE replay. It is not the ledger:
    authoritative state lives on disk, and a client that misses a window
    may receive a ``resync_required`` and a fresh snapshot query.

    A separate asyncio lock is used inside the buffer; callers that hold
    the :class:`JobManager` lock must not re-enter the buffer's lock from
    elsewhere in a way that would invert the order. The manager itself
    only acquires the buffer lock *while already holding* the manager
    lock, so no cycle is possible.
    """

    def __init__(self, limit: int = EVENT_BUFFER_LIMIT) -> None:
        self._limit = int(limit)
        self._events: list[EventV1] = []
        self._lock = threading.Lock()
        self._seq = 0

    def append(self, *, kind: str, subject_id: str, payload: dict) -> EventV1:
        with self._lock:
            self._seq += 1
            event = EventV1(
                schema_version=SCHEMA_VERSION,
                seq=self._seq,
                event_id=str(self._seq),
                kind=kind,
                occurred_at=_now_iso(),
                subject_id=subject_id,
                payload=dict(payload),
            )
            self._events.append(event)
            if len(self._events) > self._limit:
                self._events = self._events[-self._limit :]
            return event

    def replay(self, last_event_id: int | None) -> list[EventV1]:
        """Return the events to replay for ``last_event_id``.

        With ``last_event_id=None`` every retained event is returned. With
        an integer, only events with ``seq > last_event_id`` are returned.
        """

        with self._lock:
            events = list(self._events)
        if last_event_id is None:
            return events
        return [e for e in events if e.seq > int(last_event_id)]

    def oldest_seq(self) -> int | None:
        """Return the smallest seq still retained, or ``None`` if empty."""

        with self._lock:
            return self._events[0].seq if self._events else None

    def is_gap(self, last_event_id: int) -> bool:
        """Return True if ``last_event_id`` is older than the oldest retained event."""

        with self._lock:
            if not self._events:
                return False
            return int(last_event_id) < self._events[0].seq

    def is_empty(self) -> bool:
        with self._lock:
            return not self._events


# ---------------------------------------------------------------------------
# Job manager
# ---------------------------------------------------------------------------


RunnerFn = Callable[[dict], Any]


class QueueFull(Exception):
    """Raised when the manager cannot accept a new job."""

    def __init__(self, code: str = JOB_ERROR_QUEUE_FULL, message: str | None = None) -> None:
        self.code = code
        self.message = message or "queue is full"
        super().__init__(self.message)


@dataclass
class _InternalJob:
    """Mutable internal record for one in-flight or terminal job."""

    id: str
    kind: str
    correlation_id: str
    created_at: str
    request: dict = field(default_factory=dict)
    job: JobV1 = field(default_factory=lambda: JobV1(state=JOB_STATE_QUEUED))
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Stored only on success. ``None`` for queued, running, cancelling,
    # cancelled, and failed jobs. The result route reads this directly.
    result: dict | None = None
    # The run record, set once the job reaches a terminal state (any of
    # them). ``None`` before that, and for jobs stored before records were
    # kept. Evidence only: never read to queue or replay a job.
    run: dict | None = None

    def snapshot(self) -> JobV1:
        return copy.deepcopy(self.job)

    def is_cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    def result_snapshot(self) -> dict | None:
        """Return a deep copy of the stored result, or ``None``."""

        if self.result is None:
            return None
        return copy.deepcopy(self.result)


class JobManager:
    """Owns the bounded queue and the worker thread for one FastAPI app.

    The default runner delegates to ``ResearchService(home=home).run(request)``;
    tests inject their own runner so queue, cancel, and concurrency proofs
    do not need a network or a real backtest.
    """

    def __init__(
        self,
        *,
        home: Path,
        runner: RunnerFn | None = None,
        events: EventBuffer | None = None,
    ) -> None:
        self._home = Path(home)
        self._events = events if events is not None else EventBuffer()
        self._runner = runner
        self._jobs: dict[str, _InternalJob] = {}
        self._queue_order: list[str] = []
        self._running_id: str | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_started = False
        self._restore_jobs()

    def _persist_job(self, internal: _InternalJob) -> None:
        """Persist observable evidence, never a request to replay on restart."""
        directory = self._home / "research-jobs"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"job": internal.job.to_dict(), "result": internal.result, "run": internal.run}
        atomic_write(directory / f"{internal.id}.json", json.dumps(payload).encode("utf-8"))

    def _record_run(self, internal: _InternalJob, *, outcome: Any = None, identity: dict | None = None) -> None:
        """Attach the run record for the terminal state ``internal`` has just reached.

        The receipt of a completed run (``outcome``) states most inputs; a
        job cancelled mid-run keeps them, but only a succeeded job, whose
        result is stored, names its ``receipt_canonical_sha256``.
        """

        receipt: dict | None = None
        digest: str | None = None
        if isinstance(outcome, dict) and outcome.get("ok") is True and isinstance(outcome.get("legacy_receipt"), dict):
            receipt = outcome["legacy_receipt"]
            claimed = outcome.get("receipt_canonical_sha256")
            if internal.job.state == JOB_STATE_SUCCEEDED and internal.result is not None and isinstance(claimed, str):
                try:
                    actual = hashlib.sha256(canonical_receipt_bytes(receipt)).hexdigest()
                except (TypeError, ValueError):
                    actual = None
                # Only a digest that names the stored receipt is recorded.
                digest = claimed if claimed == actual else None
        internal.run = _build_run_record(
            state=internal.job.state,
            request=internal.request,
            identity=identity,
            receipt=receipt,
            receipt_digest=digest,
        )

    def _costs_path(self, job_id: str) -> Path | None:
        if _SIDE_CAR_JOB_ID.fullmatch(job_id) is None:
            return None
        return self._home / COSTS_DIRNAME / f"{job_id}.json"

    def _persist_costs(self, internal: _InternalJob, costs: dict) -> None:
        """Write the research costs side-car for a succeeded job.

        The side-car is evidence beside the stored result, not part of it:
        a failed write leaves the result untouched and the costs route
        reports the costs unavailable.
        """
        path = self._costs_path(internal.id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(path, json.dumps(costs).encode("utf-8"))
        except (OSError, TypeError, ValueError):
            return

    def _restore_jobs(self) -> None:
        records = []
        for path in (self._home / "research-jobs").glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            job = JobV1(**payload["job"])
            # Older files have no ``run``; a record is evidence, never a replay request.
            run = payload.get("run")
            internal = _InternalJob(
                id=job.id,
                kind=job.kind,
                correlation_id=job.correlation_id,
                created_at=job.created_at,
                job=job,
                result=payload["result"],
                run=run if isinstance(run, dict) else None,
            )
            if job.state not in JOB_STATES_TERMINAL:
                job.state = JOB_STATE_INTERRUPTED
                job.finished_at = _now_iso()
                job.result_ref = None
                job.error = {
                    "code": "interrupted",
                    "message": "Workstation stopped before the job finished; submit a new run to retry.",
                }
                internal.result = None
                # The request is not persisted, so nothing the run resolved is known.
                self._record_run(internal)
                self._persist_job(internal)
            records.append(internal)
        for internal in sorted(records, key=lambda record: record.created_at):
            self._jobs[internal.id] = internal
        # No recovered job is queued or started; only a new explicit submission runs.

    def _fills_path(self, job_id: str) -> Path | None:
        if not isinstance(job_id, str) or _SIDE_CAR_JOB_ID.fullmatch(job_id) is None:
            return None
        return self._home / FILLS_DIRNAME / f"{job_id}.json"

    def _write_fills(self, job_id: str, fills: dict) -> None:
        """Persist a succeeded job's per-bar fills beside, never inside, its job record.

        A write failure leaves no side-car; the fills route then reports
        ``fills_unavailable`` instead of the job failing or fills being guessed.
        """
        path = self._fills_path(job_id)
        if path is None:
            return
        payload = {"schema_version": SCHEMA_VERSION, "job_id": job_id, "fills": fills}
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(path, json.dumps(payload).encode("utf-8"))
        except OSError:
            return

    # ---- public ----------------------------------------------------------

    def event_buffer(self) -> EventBuffer:
        return self._events

    def submit(self, *, kind: str, correlation_id: str, request: dict | None = None) -> JobV1:
        """Create a new job, enqueue it, and return its JobV1.

        Raises :class:`QueueFull` if one job is already running and four
        are queued. The exception path creates no job and emits no event.

        When no other job is running, the freshly submitted job is
        transitioned to ``running`` synchronously inside this call so the
        returned snapshot reflects the post-submit state immediately.
        The worker thread picks up the runner afterwards.
        """

        with self._lock:
            if self._running_id is not None and len(self._queue_order) >= MAX_QUEUED_JOBS:
                raise QueueFull()
            job_id = _new_job_id()
            created_at = _now_iso()
            internal = _InternalJob(
                id=job_id,
                kind=kind,
                correlation_id=correlation_id,
                created_at=created_at,
                request=dict(request or {}),
            )
            internal.job = JobV1(
                id=job_id,
                kind=kind,
                state=JOB_STATE_QUEUED,
                created_at=created_at,
                correlation_id=correlation_id,
            )
            self._jobs[job_id] = internal

            if self._running_id is None:
                # Claim the running slot synchronously so the response
                # already reflects ``running`` instead of a transient
                # ``queued``. The worker thread will invoke the runner.
                self._running_id = job_id
                internal.job.state = JOB_STATE_RUNNING
                internal.job.started_at = _now_iso()
                self._events.append(
                    kind=EVENT_KIND_JOB_CHANGED,
                    subject_id=job_id,
                    payload={"state": JOB_STATE_QUEUED, "kind": kind},
                )
                self._events.append(
                    kind=EVENT_KIND_JOB_CHANGED,
                    subject_id=job_id,
                    payload={"state": JOB_STATE_RUNNING, "kind": kind},
                )
                if self._worker is None:
                    self._worker = threading.Thread(
                        target=self._worker_loop,
                        name="krellbot-jobs",
                        daemon=True,
                    )
                    self._worker.start()
            else:
                self._queue_order.append(job_id)
                self._events.append(
                    kind=EVENT_KIND_JOB_CHANGED,
                    subject_id=job_id,
                    payload={"state": JOB_STATE_QUEUED, "kind": kind},
                )

            self._persist_job(internal)
            snapshot = internal.snapshot()

        self._wake.set()
        return snapshot

    def get(self, job_id: str) -> JobV1 | None:
        with self._lock:
            internal = self._jobs.get(job_id)
            if internal is None:
                return None
            return internal.snapshot()

    def list_jobs(self) -> list[JobV1]:
        """Return known jobs, newest first. Does not create a job."""

        with self._lock:
            jobs = [internal.snapshot() for internal in self._jobs.values()]
        jobs.reverse()
        return jobs

    def result_for(self, job_id: str) -> dict | None:
        """Return the stored result for ``job_id`` or ``None``.

        Returns ``None`` when the job is unknown, or when no result has
        been stored yet (queued, running, cancelling, cancelled, failed).
        The caller can distinguish those by combining this with
        :meth:`get`: a known job with ``state == "succeeded"`` and a
        ``None`` value indicates a missing receipt, which the contract
        treats as a 500-class internal failure.
        """

        with self._lock:
            internal = self._jobs.get(job_id)
            if internal is None:
                return None
            return internal.result_snapshot()

    def fills_for(self, job_id: str) -> dict | None:
        """Return a succeeded job's per-bar fills keyed by ``bar_ts``, or ``None``.

        ``None`` covers an unknown or unfinished job and a succeeded job
        whose runner recorded no fills (for example one stored before fills
        were kept). The stored ``{legacy_receipt, trace}`` result is never
        read or changed here.
        """

        with self._lock:
            internal = self._jobs.get(job_id)
            if internal is None or internal.job.state != JOB_STATE_SUCCEEDED:
                return None
        path = self._fills_path(job_id)
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("job_id") != job_id:
            return None
        fills = payload.get("fills")
        return fills if isinstance(fills, dict) else None

    def costs_for(self, job_id: str) -> dict | None:
        """Return the research costs side-car for ``job_id`` or ``None``.

        Only a known, succeeded job with a stored result has costs. The
        path is built from the known job's own id, never from the caller's
        text. Jobs that ran before costs were recorded, injected runners
        that return none, and unreadable side-cars all read as ``None``.
        """

        with self._lock:
            internal = self._jobs.get(job_id)
            if internal is None or internal.job.state != JOB_STATE_SUCCEEDED or internal.result is None:
                return None
            path = self._costs_path(internal.id)
        if path is None:
            return None
        try:
            costs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return costs if isinstance(costs, dict) else None

    def run_for(self, job_id: str) -> dict | None:
        """Return ``{"run", "variants_tried"}`` for ``job_id``, or ``None``.

        ``None`` covers an unknown job, a job that has not ended, and a job
        stored before run records were kept. ``variants_tried`` is read-only:
        the number of distinct ``pack_sha256`` across stored run records that
        share this record's ``strategy_id`` and ``data_manifest_sha256``,
        failed and cancelled runs included. It is ``None`` when this record
        names no strategy or no dataset, never a guessed zero.
        """

        with self._lock:
            internal = self._jobs.get(job_id)
            if internal is None or not isinstance(internal.run, dict):
                return None
            record = copy.deepcopy(internal.run)
            strategy_id = _record_text(record, "request", "strategy_id")
            manifest = _record_text(record, "inputs", "data_manifest_sha256")
            variants: int | None = None
            if strategy_id is not None and manifest is not None:
                packs = {
                    _record_text(other.run, "inputs", "pack_sha256")
                    for other in self._jobs.values()
                    if _record_text(other.run, "request", "strategy_id") == strategy_id
                    and _record_text(other.run, "inputs", "data_manifest_sha256") == manifest
                }
                packs.discard(None)
                variants = len(packs)
        return {"run": record, "variants_tried": variants}

    def cancel(self, job_id: str) -> JobV1 | None:
        """Cancel ``job_id`` if known and not already terminal. Idempotent.

        Cancelling a queued job removes it from the queue and finalizes
        the state as ``cancelled``. Cancelling a running job flips the
        state to ``cancelling`` and sets the cancel flag; the worker
        finalizes the state as ``cancelled`` when the runner returns.
        A cancel issued after the job has reached a terminal state is a
        no-op and returns the existing snapshot.
        """

        with self._lock:
            internal = self._jobs.get(job_id)
            if internal is None:
                return None
            if internal.job.state in JOB_STATES_TERMINAL:
                return internal.snapshot()

            internal.cancel_event.set()

            if internal.job.state == JOB_STATE_QUEUED:
                try:
                    self._queue_order.remove(internal.id)
                except ValueError:
                    pass
                internal.job.state = JOB_STATE_CANCELLED
                internal.job.finished_at = _now_iso()
                internal.job.result_ref = None
                # A queued job never ran: nothing is resolved.
                self._record_run(internal)
                self._persist_job(internal)
                self._events.append(
                    kind=EVENT_KIND_JOB_CHANGED,
                    subject_id=internal.id,
                    payload={"state": JOB_STATE_CANCELLED, "kind": internal.job.kind},
                )
                # A queue slot opened up; nudge the worker in case the
                # job was dequeued and about to be claimed.
                self._wake.set()
                return internal.snapshot()

            if internal.job.state == JOB_STATE_RUNNING:
                internal.job.state = JOB_STATE_CANCELLING
                self._persist_job(internal)
                self._events.append(
                    kind=EVENT_KIND_JOB_CHANGED,
                    subject_id=internal.id,
                    payload={"state": JOB_STATE_CANCELLING, "kind": internal.job.kind},
                )
                return internal.snapshot()

            # Already cancelling — idempotent, return the current snapshot.
            return internal.snapshot()

    # ---- worker ----------------------------------------------------------

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._pick_or_claim()
            if job_id is None:
                self._wake.wait(timeout=0.1)
                self._wake.clear()
                continue
            self._run_job(job_id)

    def _pick_or_claim(self) -> str | None:
        """Return the running job id, or claim the next queued job.

        Honors cancel-by-queue: a queued job whose cancel flag was set
        while it sat in the queue is finalized as ``cancelled`` and never
        runs. The next queued job is then claimed if any remain.
        """

        with self._lock:
            if self._stop.is_set():
                return None
            if self._running_id is None and not self._queue_order:
                return None

            if self._running_id is not None:
                return self._running_id

            # Skip already-cancelled queued entries; finalize them and
            # return None so the loop waits for the next live job.
            while self._queue_order:
                next_id = self._queue_order[0]
                internal = self._jobs.get(next_id)
                if internal is None:
                    self._queue_order.pop(0)
                    continue
                if internal.is_cancel_requested() and internal.job.state == JOB_STATE_QUEUED:
                    self._queue_order.pop(0)
                    internal.job.state = JOB_STATE_CANCELLED
                    internal.job.finished_at = _now_iso()
                    self._record_run(internal)
                    self._persist_job(internal)
                    self._events.append(
                        kind=EVENT_KIND_JOB_CHANGED,
                        subject_id=internal.id,
                        payload={"state": JOB_STATE_CANCELLED, "kind": internal.job.kind},
                    )
                    continue
                break

            if not self._queue_order:
                return None

            next_id = self._queue_order.pop(0)
            internal = self._jobs.get(next_id)
            if internal is None:
                return None
            self._running_id = next_id
            internal.job.state = JOB_STATE_RUNNING
            internal.job.started_at = _now_iso()
            self._persist_job(internal)
            self._events.append(
                kind=EVENT_KIND_JOB_CHANGED,
                subject_id=internal.id,
                payload={"state": JOB_STATE_RUNNING, "kind": internal.job.kind},
            )
            return next_id

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            internal = self._jobs.get(job_id)
            if internal is None:
                self._running_id = None
                self._wake.set()
                return
            cancel_event = internal.cancel_event
            request_payload = dict(internal.request)
            request_payload["id"] = internal.id
            request_payload["kind"] = internal.job.kind
            request_payload["correlation_id"] = internal.job.correlation_id

        outcome: Any = None
        runner_error: Exception | None = None
        # What the default runner resolved so far; kept even when it raises.
        resolved: dict = {}

        try:
            if self._runner is not None:
                outcome = self._runner(request_payload)
            else:
                outcome = self._default_runner(request_payload, resolved)
        except Exception:  # noqa: BLE001 — coerced to a closed JobV1 error code
            runner_error = RuntimeError("runner raised")
        finally:
            with self._lock:
                identity = resolved
                if isinstance(outcome, dict) and isinstance(outcome.get("run_identity"), dict):
                    identity = outcome["run_identity"]
                cancel_requested = cancel_event.is_set()
                if cancel_requested and internal.job.state in JOB_STATES_CANCEL:
                    self._finalize_cancelled(internal, outcome=outcome, identity=identity)
                elif runner_error is not None:
                    self._finalize_failed(internal, code=JOB_ERROR_JOB_FAILED, message="job failed", identity=identity)
                elif isinstance(outcome, dict) and outcome.get("ok") is True:
                    ref = outcome.get("result_ref")
                    if isinstance(ref, str) and ref:
                        internal.job.result_ref = ref
                        # Store the receipt + trace exactly as the runner
                        # returned them. We deliberately do not add keys
                        # and do not coerce missing fields to ``0``. The
                        # contract keeps the receipt bytes identical to
                        # the runner's output. The result route reads
                        # this object verbatim.
                        internal.result = _extract_result(outcome)
                        # Per-bar fills and costs go to side-cars, written
                        # before the job reads as succeeded so their routes
                        # serve them as soon as the result route does.
                        fills = outcome.get("fills")
                        if isinstance(fills, dict):
                            self._write_fills(internal.id, fills)
                        costs = outcome.get("costs")
                        if isinstance(costs, dict):
                            self._persist_costs(internal, costs)
                        self._finalize_state(internal, JOB_STATE_SUCCEEDED, outcome=outcome, identity=identity)
                    else:
                        self._finalize_failed(
                            internal,
                            code=JOB_ERROR_MISSING_RESULT_REF,
                            message="runner returned no result_ref",
                            outcome=outcome,
                            identity=identity,
                        )
                else:
                    code = outcome.get("code") if isinstance(outcome, dict) else None
                    message = outcome.get("message") if isinstance(outcome, dict) else None
                    if isinstance(code, str) and code:
                        self._finalize_failed(
                            internal,
                            code=code,
                            message=str(message) if isinstance(message, str) else "job failed",
                            identity=identity,
                        )
                    else:
                        self._finalize_failed(
                            internal, code=JOB_ERROR_JOB_FAILED, message="job failed", identity=identity
                        )

                if self._running_id == internal.id:
                    self._running_id = None

        # Outside the lock: wake any waiters so the next queued job gets
        # its turn on the worker thread.
        self._wake.set()

    # ---- finalize helpers ------------------------------------------------

    def _finalize_state(
        self, internal: _InternalJob, state: str, *, outcome: Any = None, identity: dict | None = None
    ) -> None:
        internal.job.state = state
        internal.job.finished_at = _now_iso()
        self._record_run(internal, outcome=outcome, identity=identity)
        self._persist_job(internal)
        self._events.append(
            kind=EVENT_KIND_JOB_CHANGED,
            subject_id=internal.id,
            payload={"state": state, "kind": internal.job.kind},
        )

    def _finalize_cancelled(self, internal: _InternalJob, *, outcome: Any = None, identity: dict | None = None) -> None:
        internal.job.state = JOB_STATE_CANCELLED
        internal.job.finished_at = _now_iso()
        internal.job.result_ref = None
        internal.job.error = None
        self._record_run(internal, outcome=outcome, identity=identity)
        self._persist_job(internal)
        self._events.append(
            kind=EVENT_KIND_JOB_CHANGED,
            subject_id=internal.id,
            payload={"state": JOB_STATE_CANCELLED, "kind": internal.job.kind},
        )

    def _finalize_failed(
        self,
        internal: _InternalJob,
        *,
        code: str,
        message: str,
        outcome: Any = None,
        identity: dict | None = None,
    ) -> None:
        internal.job.state = JOB_STATE_FAILED
        internal.job.finished_at = _now_iso()
        internal.job.result_ref = None
        internal.job.error = {"code": code, "message": message}
        self._record_run(internal, outcome=outcome, identity=identity)
        self._persist_job(internal)
        self._events.append(
            kind=EVENT_KIND_JOB_CHANGED,
            subject_id=internal.id,
            payload={"state": JOB_STATE_FAILED, "kind": internal.job.kind},
        )

    # ---- default runner --------------------------------------------------

    def _default_runner(self, request_payload: dict, resolved: dict | None = None) -> dict:
        """Default runner for the production path.

        Translates the API request dict into a :class:`ResearchRequest`
        and runs it through :class:`ResearchService`. Raises any
        exception the service raises; the worker captures it as a closed
        JobV1 error code.

        Run inputs are written to ``resolved`` as they are resolved, so a
        run that refuses or raises still leaves what it resolved for its
        run record; every returned outcome also carries a copy as
        ``run_identity``. A key is present only once resolved.
        """

        identity = resolved if resolved is not None else {}
        outcome = self._run_default(request_payload, identity)
        outcome["run_identity"] = dict(identity)
        return outcome

    def _run_default(self, request_payload: dict, identity: dict) -> dict:
        from krellbot import __version__ as engine_version
        from krellbot.application.research import ResearchRequest, ResearchService, fills_to_json

        identity["engine_version"] = engine_version
        pack_path = request_payload.get("pack_path", "")
        revision_id = request_payload.get("revision_id")
        if not pack_path and isinstance(revision_id, str) and revision_id:
            from krellbot.application.strategy import RevisionNotFound, StrategyDraftService

            try:
                pack_path = str(StrategyDraftService(home=self._home).revision_path(revision_id))
            except RevisionNotFound:
                return {
                    "ok": False,
                    "code": JOB_ERROR_NOT_FOUND,
                    "message": "revision not found",
                }
        if pack_path:
            identity.update(_pack_identity(Path(str(pack_path))))
        venue = request_payload.get("venue") or "kraken"
        identity["venue"] = str(venue)
        pair = request_payload.get("pair")
        timeframe = request_payload.get("timeframe")
        dataset_id = request_payload.get("dataset_id")
        dataset_csv, refusal = _resolve_dataset_id(self._home, dataset_id, request_payload.get("dataset_csv"))
        if refusal is not None:
            return {"ok": False, **refusal}
        if dataset_csv:
            identity.update(_dataset_identity(Path(str(dataset_csv))))
        starting_cash = request_payload.get("starting_cash")
        fee_bps = request_payload.get("fee_bps")
        slippage_bps = request_payload.get("slippage_bps")
        slippage_mult = request_payload.get("slippage_mult")
        from_ms = request_payload.get("from_ms")
        to_ms = request_payload.get("to_ms")
        # API contract: only the literal boolean ``True``
        # may enable the allow_gaps override. Omission, ``False``,
        # ``None``, strings (including "true"/"false"), numbers, arrays
        # and objects are treated as ``False`` at this conversion
        # boundary — never a new schema rejection.
        allow_gaps = request_payload.get("allow_gaps") is True
        holdout_from_ms_raw = request_payload.get("holdout_from_ms")
        holdout_to_ms_raw = request_payload.get("holdout_to_ms")

        starting_cash_decimal, cash_refusal = _coerce_starting_cash(starting_cash)
        if cash_refusal is not None:
            return {"ok": False, **cash_refusal}

        kwargs: dict = {
            "pack_path": Path(str(pack_path)) if pack_path else Path("."),
            "venue": str(venue),
            "pair": str(pair) if isinstance(pair, str) else None,
            "timeframe": str(timeframe) if isinstance(timeframe, str) else None,
            "dataset_csv": Path(str(dataset_csv)) if dataset_csv else None,
            "starting_cash": starting_cash_decimal,
            "fee_bps": int(fee_bps) if fee_bps is not None else None,
            "slippage_bps": int(slippage_bps) if slippage_bps is not None else None,
            "slippage_mult": float(slippage_mult) if slippage_mult is not None else None,
            "from_ms": int(from_ms) if from_ms is not None else None,
            "to_ms": int(to_ms) if to_ms is not None else None,
            "allow_gaps": allow_gaps,
        }
        # Costs the request sent are what the run uses; an unsent one is the
        # engine default, which only the run resolves (its receipt states it).
        for key in ("fee_bps", "slippage_bps", "slippage_mult"):
            if kwargs[key] is not None:
                identity[key] = kwargs[key]
        identity["from_ms"] = kwargs["from_ms"]
        identity["to_ms"] = kwargs["to_ms"]
        # A holdout bound that is not an int is refused by the run, so it stays unresolved.
        for key, raw in (("holdout_from_ms", holdout_from_ms_raw), ("holdout_to_ms", holdout_to_ms_raw)):
            if raw is None or type(raw) is int:
                identity[key] = raw
        if "holdout_from_ms" in request_payload:
            kwargs["holdout_from_ms"] = holdout_from_ms_raw
        if "holdout_to_ms" in request_payload:
            kwargs["holdout_to_ms"] = holdout_to_ms_raw
        if "nodes" in request_payload:
            kwargs["nodes"] = request_payload.get("nodes")
        request = ResearchRequest(**kwargs)
        service = ResearchService(home=self._home)
        # The nominal run is the stored result; the zero-cost and stressed
        # reruns only complete the costs side-car.
        result = service.run_with_cost_reruns(request)
        if not result.ok:
            refusal = result.refusal if isinstance(result.refusal, dict) else {}
            code = refusal.get("code") if isinstance(refusal.get("code"), str) else None
            message = refusal.get("message") if isinstance(refusal.get("message"), str) else "job failed"
            payload: dict = {"ok": False}
            if code is not None:
                payload["code"] = code
            payload["message"] = message
            return payload
        correlation_id = str(request_payload.get("correlation_id") or "")
        job_id = str(request_payload.get("id") or "unknown")
        # Pass the receipt + trace back to the worker verbatim. The worker
        # stores them under ``_InternalJob.result`` and the result route
        # reads them back. We do not add keys and do not coerce missing
        # fields to ``0``; the contract keeps the receipt bytes
        # byte-identical to the runner's view.
        detail = result.detail if isinstance(result.detail, dict) else {}
        trace = detail.get("trace")
        legacy_receipt = result.legacy_receipt if isinstance(result.legacy_receipt, dict) else {}
        receipt_bytes = canonical_receipt_bytes(legacy_receipt)
        receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
        outcome = {
            "ok": True,
            "result_ref": f"backtest:{correlation_id}:{job_id}",
            "legacy_receipt": legacy_receipt,
            "trace": trace,
            "receipt_canonical_sha256": receipt_digest,
            # Side-car only: ``_extract_result`` never stores this key.
            "fills": fills_to_json(result.fills) if result.fills is not None else None,
        }
        costs = getattr(result, "costs", None)
        if isinstance(costs, dict):
            # The side-car names the receipt it describes.
            outcome["costs"] = {**costs, "receipt_canonical_sha256": receipt_digest}
        return outcome
