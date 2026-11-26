"""Persisted alerts surface for the live-operations view.

Sources (all read-only):

  1. ``live_gate.kill_state`` — kill switch engaged ⇒ one critical
     ``kill_switch`` alert.
  2. ``<home>/ops.sqlite`` ledger rows of kind ``needs_reconcile`` —
     one critical ``needs_reconcile`` alert per ``coid`` (i.e. the
     payload's ``coid`` is the alert code). The store is opened only
     when ``path.exists()`` is true so collecting alerts never creates
     the database.
  3. Journal tick records whose ``detail.reason`` is
     ``live_refused`` or ``store_refused``, or whose ``detail`` carries
     an ``intent_refused`` or ``metadata_refusal`` key — one warning
     alert per ``(kind, code, venue, pair)`` triple, deduped with a
     count and ``first_ts`` / ``last_ts``. A ``metadata_refusal`` tick
     must never be a silent skip: it surfaces as an unacknowledged
     Operations alert.
  4. Scheduler / tick supervision (M16.3): durable ``service/last_error``
     ⇒ ``tick_failure``; armed venues whose last journal tick is older
     than ``doctor.TICK_STALE_SECONDS`` ⇒ ``scheduler_stale``.
  5. Unresolved order aging (M16.3): operational ledger orders awaiting
     reconcile whose age is at least ``UNRESOLVED_AGING_SECONDS`` ⇒
     ``unresolved_order_aging``.
  6. Restore fence (M16.3): kill switch engaged with the home-restore
     reason ⇒ typed ``restore_fence`` (in addition to ``kill_switch``).
  7. Feed freshness (M28.4): an armed feed whose latest candle read was
     older than two timeframes ⇒ ``feed_stale``. The rule
     (:func:`candle_is_fresh`) is the one ``/api/v1/paper/health`` and
     ``/api/v1/paper/fleet/freshness`` use.

Alerts are deduped, severity-sorted (critical first), and ordered by
``last_ts`` descending inside each severity band. Acknowledgements
persist in ``<home>/run/alerts-ack.json`` keyed by alert id; a new
occurrence with a greater ``last_ts`` un-acknowledges the alert. For the
ongoing ``scheduler_stale``, ``unresolved_order_aging`` and ``feed_stale``
conditions the occurrence is ``first_ts``, so an ack holds until the
condition clears and recurs. ``tick_failure`` is dated from its
``last_error`` write. A corrupt ack file is treated as empty and
overwritten on the next ack.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from collections.abc import Mapping as _Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from krellbot import paths as kb_paths
from krellbot.application import live_gate
from krellbot.doctor import TICK_STALE_SECONDS

SCHEMA_VERSION = "1"
ACK_FILE = "alerts-ack.json"

REASON_LIVE_REFUSED = "live_refused"
REASON_STORE_REFUSED = "store_refused"
INTENT_REFUSED_KEY = "intent_refused"
METADATA_REFUSAL_KEY = "metadata_refusal"

KIND_TICK_FAILURE = "tick_failure"
KIND_SCHEDULER_STALE = "scheduler_stale"
KIND_UNRESOLVED_ORDER_AGING = "unresolved_order_aging"
KIND_RESTORE_FENCE = "restore_fence"
KIND_FEED_STALE = "feed_stale"
FEED_STALE_CODE = "candle_stale"

# Unresolved orders older than this surface as operator-supervision alerts.
UNRESOLVED_AGING_SECONDS = 3600

# Pack timeframes the tick reads, in seconds. A candle read more than
# ``STALE_CANDLE_TIMEFRAMES`` timeframes after it opened is stale.
TIMEFRAME_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}
STALE_CANDLE_TIMEFRAMES = 2

# Conditions that stay true between reads. Their ``last_ts`` is the read time
# (``now``) or the latest read that still shows them; the occurrence began at
# ``first_ts`` (the venue's last tick, the order's first stamp, the tick that
# first read a stale candle) and lasts until the condition clears.
_ONGOING_KINDS = frozenset({KIND_SCHEDULER_STALE, KIND_UNRESOLVED_ORDER_AGING, KIND_FEED_STALE})

RESTORE_FENCE_REASON_PREFIX = "home restored"

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"

OPS_DB_FILENAME = "ops.sqlite"


@dataclass(frozen=True)
class Alert:
    """One deduped alert row."""

    id: str
    kind: str
    severity: str
    code: str | None
    venue: str | None
    pair: str | None
    count: int
    first_ts: int
    last_ts: int
    acknowledged: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "code": self.code,
            "venue": self.venue,
            "pair": self.pair,
            "count": self.count,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "acknowledged": self.acknowledged,
        }


def _alert_id(kind: str, code: str | None, venue: str | None, pair: str | None) -> str:
    seed = json.dumps([kind, code, venue, pair], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _ack_path(home: Path) -> Path:
    return Path(home) / "run" / ACK_FILE


def _load_acks(home: Path) -> dict[str, int]:
    """Read the ack map. A missing or corrupt file yields {} so a corrupt
    file does not raise and the next ack overwrites it cleanly."""

    path = _ack_path(home)
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
        decoded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in decoded.items():
        if isinstance(k, str) and isinstance(v, int):
            out[k] = v
    return out


def _save_acks(home: Path, acks: dict[str, int]) -> None:
    run_dir = Path(home) / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(acks, sort_keys=True, separators=(",", ":")).encode("utf-8")
    kb_paths.atomic_write(_ack_path(home), payload, mode=0o600)


def _collect_kill_switch(home: Path, *, now: int) -> list[Alert]:
    state = live_gate.kill_state(home)
    if not state.engaged:
        return []
    ts = state.engaged_at if state.engaged_at is not None else now
    return [
        Alert(
            id=_alert_id("kill_switch", "kill_switch_engaged", None, None),
            kind="kill_switch",
            severity=SEVERITY_CRITICAL,
            code="kill_switch_engaged",
            venue=None,
            pair=None,
            count=1,
            first_ts=ts,
            last_ts=ts,
            acknowledged=False,
        )
    ]


def _collect_needs_reconcile(home: Path) -> list[Alert]:
    """Read ``<home>/ops.sqlite`` for ``needs_reconcile`` ledger rows.

    The store is opened only when the file exists; collecting alerts
    never creates the database. Any read failure yields a single
    ``store_fault`` critical alert; the file bytes are not modified
    (the helper only opens an existing file in read mode through
    :class:`OperationalStore`).
    """

    path = Path(home) / OPS_DB_FILENAME
    if not path.exists():
        return []
    # Imported lazily so the cold-path does not pull sqlite3 for tests
    # that never touch the database.
    from krellbot.storage.database import (
        OperationalStore,
        StoreBusy,
        StoreCorrupt,
    )

    store = OperationalStore(path)
    try:
        rows = store.read_ledger()
    except (StoreBusy, StoreCorrupt, sqlite3.Error, OSError) as exc:
        code = getattr(exc, "code", "store_error")
        code = code if isinstance(code, str) else "store_error"
        return [
            Alert(
                id=_alert_id("store_fault", code, None, None),
                kind="store_fault",
                severity=SEVERITY_CRITICAL,
                code=code,
                venue=None,
                pair=None,
                count=1,
                first_ts=0,
                last_ts=0,
                acknowledged=False,
            )
        ]

    from krellbot.execution.obligations import RESOLVED_KIND

    resolved: set[str] = set()
    for _id, kind, payload in rows:
        if kind != RESOLVED_KIND:
            continue
        try:
            record = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("coid"), str):
            resolved.add(record["coid"])

    out: list[Alert] = []
    for _id, kind, payload in rows:
        if kind != "needs_reconcile":
            continue
        try:
            record = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        coid = record.get("coid")
        if not isinstance(coid, str) or not coid or coid in resolved:
            continue
        out.append(
            Alert(
                id=_alert_id("needs_reconcile", coid, None, None),
                kind="needs_reconcile",
                severity=SEVERITY_CRITICAL,
                code=coid,
                venue=None,
                pair=None,
                count=1,
                first_ts=0,
                last_ts=0,
                acknowledged=False,
            )
        )
    return out


def _read_journal_dicts(home: Path) -> list[dict]:
    """Read every JSON object line of every ``<home>/journal/*.jsonl`` file.

    Unreadable files and unparseable lines are skipped silently; nothing is
    created on a missing home. Records come back in the order the journal
    wrote them.
    """

    journal_dir = Path(home) / "journal"
    if not journal_dir.exists():
        return []
    out: list[dict] = []
    files = sorted(p for p in journal_dir.iterdir() if p.is_file() and p.suffix == ".jsonl")
    for file_path in files:
        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                out.append(record)
    return out


def _iter_journal_records(home: Path) -> list[tuple[int, str, dict]]:
    """Read every tick of every ``<home>/journal/*.jsonl`` file.

    Unparseable lines are skipped silently; the file must not be
    created on a missing home. Returns ``(ts, venue, detail_dict)``
    tuples in the order the journal wrote them.
    """

    out: list[tuple[int, str, dict]] = []
    for record in _read_journal_dicts(home):
        ts_raw = record.get("ts")
        kind = record.get("kind")
        venue = record.get("venue")
        detail = record.get("detail")
        if not isinstance(ts_raw, int):
            continue
        if kind != "tick":
            continue
        if not isinstance(venue, str):
            continue
        if not isinstance(detail, dict):
            continue
        out.append((ts_raw, venue, detail))
    return out


def _bucket_journal_refusal(
    buckets: dict[tuple[str, str | None, str | None, str | None], dict[str, object]],
    *,
    kind: str,
    code: str | None,
    venue: str,
    pair: str | None,
    ts: int,
) -> None:
    key = (kind, code, venue, pair)
    bucket = buckets.get(key)
    if bucket is None:
        bucket = {"count": 0, "first_ts": ts, "last_ts": ts}
        buckets[key] = bucket
    bucket["count"] = int(bucket["count"]) + 1
    first_ts = int(bucket["first_ts"])
    last_ts = int(bucket["last_ts"])
    if ts < first_ts:
        bucket["first_ts"] = ts
    if ts > last_ts:
        bucket["last_ts"] = ts


def _collect_journal_refusals(home: Path) -> list[Alert]:
    """Group ``live_refused`` / ``store_refused`` / ``intent_refused`` /
    ``metadata_refusal`` records by ``(kind, code, venue, pair)``.

    A tick may carry more than one refusal key (for example
    ``intent_refused`` and ``metadata_refusal``); each non-null string
    key produces its own alert so a ``metadata_refusal`` is never a
    silent skip.
    """

    buckets: dict[tuple[str, str | None, str | None, str | None], dict[str, object]] = {}
    for ts, venue, detail in _iter_journal_records(home):
        reason = detail.get("reason")
        raw_pair = detail.get("pair")
        pair = raw_pair if isinstance(raw_pair, str) else None
        if reason == REASON_LIVE_REFUSED:
            raw_code = detail.get("code")
            code = raw_code if isinstance(raw_code, str) else None
            _bucket_journal_refusal(
                buckets,
                kind=REASON_LIVE_REFUSED,
                code=code,
                venue=venue,
                pair=pair,
                ts=ts,
            )
        elif reason == REASON_STORE_REFUSED:
            raw_code = detail.get("code")
            code = raw_code if isinstance(raw_code, str) else None
            _bucket_journal_refusal(
                buckets,
                kind=REASON_STORE_REFUSED,
                code=code,
                venue=venue,
                pair=pair,
                ts=ts,
            )
        intent_code = detail.get(INTENT_REFUSED_KEY)
        if isinstance(intent_code, str) and intent_code:
            _bucket_journal_refusal(
                buckets,
                kind=INTENT_REFUSED_KEY,
                code=intent_code,
                venue=venue,
                pair=pair,
                ts=ts,
            )
        metadata_code = detail.get(METADATA_REFUSAL_KEY)
        if isinstance(metadata_code, str) and metadata_code:
            _bucket_journal_refusal(
                buckets,
                kind=METADATA_REFUSAL_KEY,
                code=metadata_code,
                venue=venue,
                pair=pair,
                ts=ts,
            )

    out: list[Alert] = []
    for (kind, code, venue, pair), bucket in buckets.items():
        out.append(
            Alert(
                id=_alert_id(kind, code, venue, pair),
                kind=kind,
                severity=SEVERITY_WARNING,
                code=code,
                venue=venue,
                pair=pair,
                count=int(bucket["count"]),
                first_ts=int(bucket["first_ts"]),
                last_ts=int(bucket["last_ts"]),
                acknowledged=False,
            )
        )
    return out


def _occurrence_ts(alert: Alert) -> int:
    """The time of the latest event an ack must postdate.

    For an ongoing condition ``last_ts`` is the read time, so comparing an
    ack against it would revert the ack on the next poll. Its occurrence
    starts at ``first_ts`` and lasts until the condition clears.
    """

    if alert.kind in _ONGOING_KINDS:
        return alert.first_ts
    return alert.last_ts


def _apply_acks(alerts: list[Alert], acks: _Mapping[str, int]) -> list[Alert]:
    """Mark each alert acknowledged when the ack ts is ``>=`` its occurrence.

    A new occurrence (greater ``last_ts``, or a greater ``first_ts`` for an
    ongoing condition) un-acknowledges the alert, so the comparison uses
    ``>=`` so the alert stays acknowledged until a strictly later event
    arrives.
    """

    if not acks:
        return alerts
    out: list[Alert] = []
    for alert in alerts:
        ack_ts = acks.get(alert.id)
        acknowledged = ack_ts is not None and ack_ts >= _occurrence_ts(alert)
        out.append(
            Alert(
                id=alert.id,
                kind=alert.kind,
                severity=alert.severity,
                code=alert.code,
                venue=alert.venue,
                pair=alert.pair,
                count=alert.count,
                first_ts=alert.first_ts,
                last_ts=alert.last_ts,
                acknowledged=acknowledged,
            )
        )
    return out


def _order(alerts: list[Alert]) -> list[Alert]:
    return sorted(
        alerts,
        key=lambda a: (
            0 if a.severity == SEVERITY_CRITICAL else 1,
            -a.last_ts,
            a.kind,
        ),
    )


def _collect_tick_failure(home: Path, *, now: int) -> list[Alert]:
    """Surface a durable scheduler ``last_error`` as ``tick_failure``.

    The alert is dated from the ``last_error`` write, not from the read, so
    each write is one occurrence however often Operations polls. A write
    dated after the read (the clock moved back) is clamped to the read.
    """

    from krellbot.service import supervise as kb_supervise

    message = kb_supervise.get_last_error(home)
    if message is None:
        return []
    code = message if len(message) <= 64 else message[:61] + "..."
    written_at = kb_supervise.last_error_ts(home)
    occurred_at = 0 if written_at is None else min(written_at, int(now))
    return [
        Alert(
            id=_alert_id(KIND_TICK_FAILURE, code, None, None),
            kind=KIND_TICK_FAILURE,
            severity=SEVERITY_CRITICAL,
            code=code,
            venue=None,
            pair=None,
            count=1,
            first_ts=occurred_at,
            last_ts=occurred_at,
            acknowledged=False,
        )
    ]


def _collect_scheduler_stale(home: Path, *, now: int) -> list[Alert]:
    """Warn when an armed venue's last tick is older than the stale threshold.

    Uses the same journal scan as scheduler supervision
    (``supervise.last_tick_ts``) and ``doctor.TICK_STALE_SECONDS``. A
    venue that has never ticked is not treated as stale here — that is
    a just-armed fleet state, not a missed scheduler wake.
    """

    from krellbot import config as kb_config
    from krellbot.service import supervise as kb_supervise

    try:
        cfg = kb_config.load_config(home)
    except (OSError, ValueError):
        return []
    venues = sorted({record.venue for record in cfg.armed})
    out: list[Alert] = []
    for venue in venues:
        last = kb_supervise.last_tick_ts(home, venue)
        if last is None:
            continue
        age = int(now) - int(last)
        if age < int(TICK_STALE_SECONDS):
            continue
        out.append(
            Alert(
                id=_alert_id(KIND_SCHEDULER_STALE, "tick_stale", venue, None),
                kind=KIND_SCHEDULER_STALE,
                severity=SEVERITY_WARNING,
                code="tick_stale",
                venue=venue,
                pair=None,
                count=1,
                first_ts=int(last),
                last_ts=int(now),
                acknowledged=False,
            )
        )
    return out


def _collect_unresolved_order_aging(home: Path, *, now: int) -> list[Alert]:
    """Warn for each unresolved order whose age meets the aging threshold."""

    from krellbot.application import operations as operations_view_app
    from krellbot.execution.obligations import ObligationsUnavailable

    try:
        rows = operations_view_app.list_unresolved_orders(home, now=now)
    except ObligationsUnavailable:
        return []  # the store_fault alert from _collect_needs_reconcile covers it
    out: list[Alert] = []
    for row in rows:
        age = row.get("age_seconds")
        if not isinstance(age, int) or age < UNRESOLVED_AGING_SECONDS:
            continue
        coid = row.get("client_order_id")
        if not isinstance(coid, str) or not coid:
            continue
        out.append(
            Alert(
                id=_alert_id(KIND_UNRESOLVED_ORDER_AGING, coid, None, None),
                kind=KIND_UNRESOLVED_ORDER_AGING,
                severity=SEVERITY_WARNING,
                code=coid,
                venue=None,
                pair=None,
                count=1,
                first_ts=int(now) - int(age),
                last_ts=int(now),
                acknowledged=False,
            )
        )
    return out


def _collect_restore_fence(home: Path, *, now: int) -> list[Alert]:
    """Typed alert when the kill switch was engaged by a home restore fence."""

    state = live_gate.kill_state(home)
    if not state.engaged:
        return []
    reason = state.reason or ""
    if not reason.startswith(RESTORE_FENCE_REASON_PREFIX):
        return []
    ts = state.engaged_at if state.engaged_at is not None else now
    return [
        Alert(
            id=_alert_id(KIND_RESTORE_FENCE, "restore_fence_engaged", None, None),
            kind=KIND_RESTORE_FENCE,
            severity=SEVERITY_CRITICAL,
            code="restore_fence_engaged",
            venue=None,
            pair=None,
            count=1,
            first_ts=ts,
            last_ts=ts,
            acknowledged=False,
        )
    ]


class ArmedFeed(Protocol):
    """The armed-record fields that identify one feed and its current arm."""

    venue: str
    pack_id: str
    pair: str
    armed_at_ts: int


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def pack_timeframe_seconds(pack_path: str | Path) -> int | None:
    """The armed pack's timeframe in seconds, or ``None`` when unreadable."""

    try:
        pack = json.loads(Path(pack_path).read_text(encoding="utf-8"))
        return TIMEFRAME_SECONDS.get(pack.get("timeframe"))
    except (OSError, ValueError, AttributeError, TypeError, RecursionError):
        return None


def candle_is_fresh(*, bar_ts_ms: object, read_at: object, timeframe_seconds: int | None) -> bool:
    """The stale-candle rule, shared by paper health, fleet freshness and
    ``feed_stale``.

    A tick read a fresh candle when the candle opened (``bar_ts_ms``) no
    more than ``STALE_CANDLE_TIMEFRAMES`` timeframes before the tick
    (``read_at``, unix seconds) and not after it. Without a known timeframe
    or a valid bar time nothing is fresh.
    """

    return (
        timeframe_seconds is not None
        and _is_number(bar_ts_ms)
        and _is_number(read_at)
        and 0 <= bar_ts_ms < 1_000_000_000_000_000
        and 0 <= read_at * 1000 - bar_ts_ms <= timeframe_seconds * STALE_CANDLE_TIMEFRAMES * 1000
    )


def current_arm_ticks(records: Iterable[object], armed: ArmedFeed) -> list[dict]:
    """Tick records of ``armed``'s current arm, oldest first.

    A tick counts when it has the arm's venue, pack and pair and was written
    after the arm. A legacy arm without a start time has none, so it never
    claims earlier evidence.
    """

    armed_at = armed.armed_at_ts
    out: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        detail = record.get("detail")
        ts = record.get("ts")
        if (
            record.get("kind") == "tick"
            and record.get("venue") == armed.venue
            and record.get("pack") == armed.pack_id
            and isinstance(detail, dict)
            and detail.get("pair") == armed.pair
            and _is_number(ts)
            and 0 < ts < 1_000_000_000_000
            and armed_at > 0
            and ts > armed_at
        ):
            out.append(record)
    out.sort(key=lambda rec: rec["ts"])
    return out


def _read_candle(record: dict) -> int | float | None:
    """The ``bar_ts`` of the candle a tick read, or ``None`` when it read none.

    The tick journals ``bar_ts`` 0 when it fetched no candle (a feed error,
    no candles, a refusal before any fetch).
    """

    bar = record.get("bar_ts")
    if not _is_number(bar) or not 0 < bar < 1_000_000_000_000_000:
        return None
    return bar


@dataclass(frozen=True)
class FeedFreshness:
    """Candle evidence of one armed feed.

    ``last_bar_ts_ms``, ``candle_age_s`` and ``candle_stale`` describe the
    candle the current arm's latest tick read, measured when it was read.
    They are ``None`` when that tick read no candle; ``candle_stale`` is also
    ``None`` when the timeframe is unreadable, and ``candle_age_s`` when the
    candle is dated after the tick.

    ``stale_*`` describe the open stale occurrence: the stale reads since the
    last fresh one, dated by the tick that first read a stale candle. A tick
    that read no candle neither starts nor ends it.
    """

    last_bar_ts_ms: int | None = None
    candle_age_s: int | None = None
    candle_stale: bool | None = None
    stale_first_ts: int | None = None
    stale_last_ts: int | None = None
    stale_count: int = 0


def feed_freshness(ticks: list[dict], *, timeframe_seconds: int | None) -> FeedFreshness:
    """Judge one feed from its current arm's ticks (``current_arm_ticks``)."""

    stale_first: int | None = None
    stale_last: int | None = None
    stale_count = 0
    if timeframe_seconds is not None:
        for record in ticks:
            bar = _read_candle(record)
            if bar is None:
                continue
            if candle_is_fresh(bar_ts_ms=bar, read_at=record["ts"], timeframe_seconds=timeframe_seconds):
                stale_first, stale_last, stale_count = None, None, 0
                continue
            if stale_first is None:
                stale_first = int(record["ts"])
            stale_last = int(record["ts"])
            stale_count += 1

    latest = ticks[-1] if ticks else None
    bar = _read_candle(latest) if latest is not None else None
    if latest is None or bar is None:
        return FeedFreshness(stale_first_ts=stale_first, stale_last_ts=stale_last, stale_count=stale_count)
    age_ms = latest["ts"] * 1000 - bar
    return FeedFreshness(
        last_bar_ts_ms=int(bar),
        candle_age_s=int(age_ms // 1000) if age_ms >= 0 else None,
        candle_stale=None
        if timeframe_seconds is None
        else not candle_is_fresh(bar_ts_ms=bar, read_at=latest["ts"], timeframe_seconds=timeframe_seconds),
        stale_first_ts=stale_first,
        stale_last_ts=stale_last,
        stale_count=stale_count,
    )


def _collect_feed_stale(home: Path) -> list[Alert]:
    """Warn for each armed feed whose latest candle read was stale.

    One occurrence runs from the tick that first read a stale candle until a
    fresh candle is read, so the alert keeps that ``first_ts`` across later
    reads and notifies once.
    """

    from krellbot import config as kb_config

    try:
        cfg = kb_config.load_config(home)
    except (OSError, ValueError):
        return []
    if not cfg.armed:
        return []
    records = _read_journal_dicts(home)
    out: list[Alert] = []
    for armed in cfg.armed:
        freshness = feed_freshness(
            current_arm_ticks(records, armed),
            timeframe_seconds=pack_timeframe_seconds(armed.pack_path),
        )
        if freshness.stale_first_ts is None or freshness.stale_last_ts is None:
            continue
        out.append(
            Alert(
                id=_alert_id(KIND_FEED_STALE, FEED_STALE_CODE, armed.venue, armed.pair),
                kind=KIND_FEED_STALE,
                severity=SEVERITY_WARNING,
                code=FEED_STALE_CODE,
                venue=armed.venue,
                pair=armed.pair,
                count=freshness.stale_count,
                first_ts=freshness.stale_first_ts,
                last_ts=freshness.stale_last_ts,
                acknowledged=False,
            )
        )
    return out


def collect(home: Path, *, now: int) -> list[Alert]:
    """Collect alerts from every read-only source and return them in a
    stable order.

    Sources:

      * ``live_gate.kill_state`` (kill_switch)
      * ``<home>/ops.sqlite`` ``needs_reconcile`` rows (and one
        ``store_fault`` alert on a read failure)
      * ``<home>/journal/*.jsonl`` tick records with a refusal reason
        (``live_refused``, ``store_refused``, ``intent_refused``, or
        ``metadata_refusal``)
      * scheduler ``last_error`` (``tick_failure``) and stale last-tick
        gaps (``scheduler_stale``)
      * aged unresolved orders (``unresolved_order_aging``)
      * restore-fence kill reason (``restore_fence``)
      * armed feeds whose latest candle read was stale (``feed_stale``)

    The function never creates any file. An empty home yields ``[]``
    and leaves the filesystem untouched.
    """

    alerts: list[Alert] = []
    alerts.extend(_collect_kill_switch(Path(home), now=now))
    alerts.extend(_collect_restore_fence(Path(home), now=now))
    alerts.extend(_collect_needs_reconcile(Path(home)))
    alerts.extend(_collect_journal_refusals(Path(home)))
    alerts.extend(_collect_tick_failure(Path(home), now=now))
    alerts.extend(_collect_scheduler_stale(Path(home), now=now))
    alerts.extend(_collect_unresolved_order_aging(Path(home), now=now))
    alerts.extend(_collect_feed_stale(Path(home)))
    acks = _load_acks(Path(home))
    alerts = _apply_acks(alerts, acks)
    return _order(alerts)


# The kinds the scheduler heartbeat (``krellbot service heartbeat``) checks.
SCHEDULER_KINDS = frozenset({KIND_TICK_FAILURE, KIND_SCHEDULER_STALE})


def collect_scheduler(home: Path, *, now: int) -> list[Alert]:
    """The ``tick_failure`` and ``scheduler_stale`` alerts, as ``collect`` builds them.

    Same ids, occurrence times and acknowledgements as ``collect``, so a
    notification pass over these and one over ``collect`` share each
    occurrence. Reads only the scheduler's ``last_error``, the armed venues,
    the journal and the ack file; it opens no store, venue or key and never
    creates a file.
    """

    home = Path(home)
    alerts = [*_collect_tick_failure(home, now=now), *_collect_scheduler_stale(home, now=now)]
    return _order(_apply_acks(alerts, _load_acks(home)))


def acknowledge(home: Path, alert_id: str, *, now: int) -> Alert:
    """Record an ack for ``alert_id`` at ``now`` and return the alert.

    A :class:`KeyError` is raised when ``alert_id`` is not in the
    current ``collect(...)`` output — callers cannot ack a phantom id.
    The ack file is written atomically with mode ``0o600``; a corrupt
    existing file is treated as empty and overwritten on this call.
    """

    home = Path(home)
    alerts = collect(home, now=now)
    for alert in alerts:
        if alert.id == alert_id:
            acks = _load_acks(home)
            acks[alert_id] = now
            _save_acks(home, acks)
            return Alert(
                id=alert.id,
                kind=alert.kind,
                severity=alert.severity,
                code=alert.code,
                venue=alert.venue,
                pair=alert.pair,
                count=alert.count,
                first_ts=alert.first_ts,
                last_ts=alert.last_ts,
                acknowledged=True,
            )
    raise KeyError(alert_id)


__all__ = [
    "ACK_FILE",
    "FEED_STALE_CODE",
    "KIND_FEED_STALE",
    "KIND_RESTORE_FENCE",
    "KIND_SCHEDULER_STALE",
    "KIND_TICK_FAILURE",
    "KIND_UNRESOLVED_ORDER_AGING",
    "SCHEDULER_KINDS",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARNING",
    "STALE_CANDLE_TIMEFRAMES",
    "TIMEFRAME_SECONDS",
    "UNRESOLVED_AGING_SECONDS",
    "Alert",
    "ArmedFeed",
    "FeedFreshness",
    "acknowledge",
    "candle_is_fresh",
    "collect",
    "collect_scheduler",
    "current_arm_ticks",
    "feed_freshness",
    "pack_timeframe_seconds",
]
