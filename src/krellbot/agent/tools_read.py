"""Agent read tools: positions, journal, alerts, orders, market, catalogue, bar decisions, tour.

Read-only and light. Krell and MCP clients see Krellbot data only through
tools, and a tool result goes to the user's AI provider, so every result is
bounded and secret-free:

* One helper, :func:`cap_result`, builds every result. It drops secret-shaped
  fields (``key``, ``secret``, ``token``, ``password``, ``credential``,
  ``authorization``, plus ``license`` and ``balance``) at any depth, redacts
  registered secrets, and caps the JSON at :data:`MAX_RESULT_BYTES` (32 KiB,
  with headroom for the ``actor`` and ``labels`` keys the core adds) by
  trimming list entries and setting ``truncated: true``. A result that does
  not fit even with its lists empty is refused (``agent_result_too_large``).
* Paper only. ``positions`` and the paper form of ``explain_bar`` read paper
  arms; a venue and pair armed live is refused (``agent_live_read_refused``),
  and an unfiltered list leaves live arms out and counts them
  (``live_excluded``). ``journal_recent`` keeps paper-classified records only.
* Handlers call application facades only (``paper``, ``journal``, ``alerts``,
  ``operations``, ``packs``, ``pack_library``, ``data.cache``,
  ``domain.trace``). Nothing here imports ``run``, ``venues``, ``execution``,
  ``keys`` or ``secrets``. The catalogue is the cached document: this module
  never calls ``refresh_catalogue``, which uses the network.
* Ids and names an agent sends are checked before they reach a path: a
  ``job_id`` must match the job id format, and ``venue``, ``pair`` and
  ``timeframe`` must be plain names. Stored backtest traces are found by
  listing ``research-jobs/`` and matching the file's stem, never by joining
  the agent's text into a path, and they are read without constructing a
  ``JobManager``, whose start-up rewrites unfinished job records that a
  running workstation still owns.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from krellbot import journal, sanitize
from krellbot.agent import tour
from krellbot.agent.audit import is_secret_param_name
from krellbot.agent.core import (
    LABEL_NOT_ADVICE,
    LABEL_PAPER,
    LABEL_SIMULATED,
    MAX_PACK_BYTES,
    SCHEMA_VERSION,
    AgentToolCore,
    ToolContext,
    ToolRefusal,
    ToolSpec,
    invalid_request,
    read_regular_file_bounded,
)
from krellbot.application import alerts as alerts_app
from krellbot.application import operations, pack_library
from krellbot.application import packs as packs_app
from krellbot.application.paper import project_owned_position
from krellbot.config import ArmedPack, load_config
from krellbot.data import cache
from krellbot.domain.trace import paper_decision_trace

MAX_RESULT_BYTES = 32 * 1024
# Room for the ``actor`` and ``labels`` keys the core adds after the handler.
ENVELOPE_RESERVE_BYTES = 512

JOURNAL_DEFAULT_TAIL = 20
JOURNAL_MAX_TAIL = 50
SNAPSHOT_DEFAULT_BARS = 50
SNAPSHOT_MAX_BARS = 200
MAX_STORED_JOB_BYTES = 32 * 1024 * 1024
MAX_DISCLOSURES = 8

CODE_LIVE_READ_REFUSED = "agent_live_read_refused"
CODE_RESULT_TOO_LARGE = "agent_result_too_large"
CODE_UNRESOLVED_ORDERS_UNAVAILABLE = "unresolved_orders_unavailable"
CODE_MARKET_SNAPSHOT_NOT_FOUND = "market_snapshot_not_found"
CODE_EXPLAIN_BAR_NOT_FOUND = "explain_bar_not_found"
CODE_TOUR_STEP_NOT_FOUND = "tour_step_not_found"
CODE_EXPLAIN_BAR_PACK_CHANGED = "explain_bar_pack_changed"

MESSAGE_LIVE_READ_REFUSED = "this market is armed live; agent read tools show paper markets only"
MESSAGE_RESULT_TOO_LARGE = "the result does not fit in 32 KiB even with its lists emptied"
MESSAGE_UNRESOLVED_ORDERS_UNAVAILABLE = "the operational store could not be read, so unresolved orders are unknown"
MESSAGE_MARKET_SNAPSHOT_NOT_FOUND = "no cached candles for this venue, pair and timeframe"
MESSAGE_EXPLAIN_BAR_NOT_FOUND = "no decision found for this bar"
MESSAGE_EXPLAIN_BAR_PACK_CHANGED = "the armed pack file no longer matches the pack that was armed"
MESSAGE_BAD_JOB_ID = "job_id must be 1-64 characters of letters, digits, '-' and '_'"
MESSAGE_BAD_VENUE = "venue must be 1-32 characters of a-z, 0-9, '-' and '_'"
MESSAGE_BAD_PAIR = "pair must be 1-32 characters of letters, digits, '.', '-', '_' and '/'"
MESSAGE_BAD_TIMEFRAME = "timeframe must look like 15m, 1h, 4h or 1d"

# Same format as ``api.jobs`` job ids (``secrets.token_urlsafe`` text).
_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_VENUE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_PAIR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,31}")
_TIMEFRAME_RE = re.compile(r"[0-9]{1,3}[mhdw]")

# Dropped at any depth from every result, on top of the secret-shaped names.
_DROPPED_FIELDS = frozenset({"key", "secret", "license", "balance"})

_TOOL_ERROR_UNAVAILABLE = "ObligationsUnavailable"
_TOOL_ERROR_MODULE = "krellbot.execution.obligations"


# ---- one result builder, one cap ----------------------------------------


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _scrub(v)
            for k, v in value.items()
            if not (isinstance(k, str) and (k in _DROPPED_FIELDS or is_secret_param_name(k)))
        }
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    return value


def _json_size(value: Any) -> int:
    # ASCII-escaped, so a consumer that escapes non-ASCII text stays under the cap too.
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _refused(tool: str, code: str, message: str) -> dict:
    return ToolRefusal(code, message, tool).to_dict()


def cap_result(
    tool: str,
    body: dict,
    *,
    drop_front: tuple[str, ...] = (),
    drop_back: tuple[str, ...] = (),
) -> dict:
    """Build a read tool's result and keep it under :data:`MAX_RESULT_BYTES` of JSON.

    ``drop_front`` names lists ordered oldest first, trimmed from the front
    (the oldest end); ``drop_back`` names lists whose last entries matter
    least, trimmed from the back (newest-first lists, and lists whose oldest
    entry is the one to keep). The largest list shrinks first, by an eighth
    each step. ``truncated`` is always present, and ``True`` when the body
    already said so or any entry was cut. When no list is left to trim and
    the result is still too large, it is refused as ``agent_result_too_large``.
    """
    result: dict = {"schema_version": SCHEMA_VERSION, "ok": True, "effect": "unchanged", "tool": tool}
    result.update(_scrub(body))
    result = sanitize.redact(result)
    truncated = bool(result.get("truncated", False))
    limit = MAX_RESULT_BYTES - ENVELOPE_RESERVE_BYTES
    while True:
        result["truncated"] = truncated
        if _json_size(result) <= limit:
            return result
        candidates = [(name, True) for name in drop_front if isinstance(result.get(name), list) and result[name]] + [
            (name, False) for name in drop_back if isinstance(result.get(name), list) and result[name]
        ]
        if not candidates:
            return _refused(tool, CODE_RESULT_TOO_LARGE, MESSAGE_RESULT_TOO_LARGE)
        name, front = max(candidates, key=lambda item: _json_size(result[item[0]]))
        entries = result[name]
        cut = max(1, len(entries) // 8)
        result[name] = entries[cut:] if front else entries[:-cut]
        truncated = True


# ---- param checks -------------------------------------------------------


def _optional_name(tool: str, params: dict, name: str, pattern: re.Pattern[str], message: str):
    """``(value, None)``, ``(None, None)`` when absent, or ``(None, refusal)``."""
    value = params.get(name)
    if value is None:
        return None, None
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        return None, invalid_request(tool, f"{name}: {message}").to_dict()
    return value, None


def _count(tool: str, params: dict, name: str, maximum: int) -> tuple[int | None, dict | None]:
    value = params.get(name)
    if value is None:
        return None, None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None, invalid_request(tool, f"{name} must be an integer of at least 1 (at most {maximum})").to_dict()
    return value, None


def _bar_ts(tool: str, params: dict) -> tuple[int | None, dict | None]:
    value = params.get("bar_ts")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None, invalid_request(tool, "bar_ts must be the bar's open time in milliseconds (an integer)").to_dict()
    return value, None


# ---- positions ----------------------------------------------------------

TOOL_POSITIONS = "positions"


def _positions(ctx: ToolContext, params: dict) -> dict:
    venue, refusal = _optional_name(TOOL_POSITIONS, params, "venue", _VENUE_RE, MESSAGE_BAD_VENUE)
    if refusal:
        return refusal
    pair, refusal = _optional_name(TOOL_POSITIONS, params, "pair", _PAIR_RE, MESSAGE_BAD_PAIR)
    if refusal:
        return refusal
    selected = [
        armed
        for armed in load_config(ctx.home).armed
        if (venue is None or armed.venue == venue) and (pair is None or armed.pair == pair)
    ]
    paper = [armed for armed in selected if armed.mode == "paper"]
    live = [armed for armed in selected if armed.mode != "paper"]
    if venue is not None and pair is not None and live and not paper:
        return _refused(TOOL_POSITIONS, CODE_LIVE_READ_REFUSED, MESSAGE_LIVE_READ_REFUSED)
    positions = [
        project_owned_position(
            ctx.home,
            venue=armed.venue,
            pair=armed.pair,
            mode=armed.mode,
            as_of_run=None,
            starting_cash=armed.starting_cash,
        )
        for armed in paper
    ]
    return cap_result(TOOL_POSITIONS, {"positions": positions, "live_excluded": len(live)}, drop_back=("positions",))


# ---- journal_recent -----------------------------------------------------

TOOL_JOURNAL_RECENT = "journal_recent"


def _agent_record(record: dict) -> dict:
    """One journal record for an agent, with its summary rebuilt from the scrubbed record.

    ``journal.browse`` builds ``summary`` before :func:`_scrub` runs, and a record
    with no reason or event summarizes as the JSON of its whole ``detail``, which
    would carry back the secret-shaped values the scrub removes.
    """
    mode = record.get("mode")
    clean = _scrub(journal.drop_export_secret_fields({k: v for k, v in record.items() if k != "summary"}))
    clean["summary"] = journal.summarize_record(clean, mode=mode if isinstance(mode, str) else None)
    return clean


def _journal_recent(ctx: ToolContext, params: dict) -> dict:
    tool = TOOL_JOURNAL_RECENT
    since = params.get("since")
    if since is not None and since not in journal.WINDOW_SECONDS:
        return invalid_request(tool, "since must be 24h or 14d").to_dict()
    tail, refusal = _count(tool, params, "tail", JOURNAL_MAX_TAIL)
    if refusal:
        return refusal
    asked = tail if tail is not None else (JOURNAL_MAX_TAIL if since else JOURNAL_DEFAULT_TAIL)
    wanted = min(asked, JOURNAL_MAX_TAIL)
    try:
        # Paper-classified records only: live records are out of scope for agents.
        # One extra record is read so "more existed than were returned" is known.
        records = journal.browse(
            ctx.home, tail=None if since else wanted + 1, since=since, mode="paper", now=ctx.clock()
        )
    except journal.BrowseError:
        return invalid_request(tool, "the journal selection was refused").to_dict()
    # Truncated means the 50-record limit hid records the call would otherwise have returned.
    truncated = len(records) > wanted and (since is not None or asked > JOURNAL_MAX_TAIL)
    records = [_agent_record(record) for record in records[-wanted:]]
    return cap_result(tool, {"records": records, "truncated": truncated}, drop_front=("records",))


# ---- alerts -------------------------------------------------------------

TOOL_ALERTS = "alerts"


def _alerts(ctx: ToolContext, params: dict) -> dict:
    rows = [alert.to_dict() for alert in alerts_app.collect(ctx.home, now=int(ctx.clock()))]
    # Critical first, then most recent first: the oldest, mildest rows are at the back.
    return cap_result(TOOL_ALERTS, {"alerts": rows}, drop_back=("alerts",))


# ---- unresolved_orders --------------------------------------------------

TOOL_UNRESOLVED_ORDERS = "unresolved_orders"


def _unresolved_orders(ctx: ToolContext, params: dict) -> dict:
    try:
        rows = operations.list_unresolved_orders(ctx.home, now=int(ctx.clock()))
    except RuntimeError as exc:
        # ``execution`` is off limits here, so the facade's error is matched by name.
        if type(exc).__name__ != _TOOL_ERROR_UNAVAILABLE or type(exc).__module__ != _TOOL_ERROR_MODULE:
            raise
        return _refused(
            TOOL_UNRESOLVED_ORDERS, CODE_UNRESOLVED_ORDERS_UNAVAILABLE, MESSAGE_UNRESOLVED_ORDERS_UNAVAILABLE
        )
    # Oldest first, and the oldest are the ones that matter, so the newest are cut first.
    return cap_result(TOOL_UNRESOLVED_ORDERS, {"orders": rows}, drop_back=("orders",))


# ---- market_snapshot ----------------------------------------------------

TOOL_MARKET_SNAPSHOT = "market_snapshot"


def _candle_row(candle: Any) -> dict:
    return {
        "ts_ms": candle.ts_ms,
        "open": str(candle.open),
        "high": str(candle.high),
        "low": str(candle.low),
        "close": str(candle.close),
        "volume": str(candle.volume),
    }


def _market_names(tool: str, params: dict, *, timeframe: bool) -> tuple[tuple | None, dict | None]:
    venue = params.get("venue")
    pair = params.get("pair")
    if not isinstance(venue, str) or _VENUE_RE.fullmatch(venue) is None:
        return None, invalid_request(tool, f"venue: {MESSAGE_BAD_VENUE}").to_dict()
    if not isinstance(pair, str) or _PAIR_RE.fullmatch(pair) is None:
        return None, invalid_request(tool, f"pair: {MESSAGE_BAD_PAIR}").to_dict()
    if not timeframe:
        return (venue, pair, None), None
    tf = params.get("timeframe")
    if not isinstance(tf, str) or _TIMEFRAME_RE.fullmatch(tf) is None:
        return None, invalid_request(tool, f"timeframe: {MESSAGE_BAD_TIMEFRAME}").to_dict()
    return (venue, pair, tf), None


def _paper_arm_of(ctx: ToolContext, venue: str, pair: str) -> tuple[ArmedPack | None, bool]:
    """``(paper arm, True when the market is armed live instead)``."""
    for armed in load_config(ctx.home).armed:
        if armed.venue == venue and armed.pair == pair:
            return (armed, False) if armed.mode == "paper" else (None, True)
    return None, False


def _market_snapshot(ctx: ToolContext, params: dict) -> dict:
    tool = TOOL_MARKET_SNAPSHOT
    names, refusal = _market_names(tool, params, timeframe=True)
    if refusal:
        return refusal
    venue, pair, tf = names
    bars, refusal = _count(tool, params, "bars", SNAPSHOT_MAX_BARS)
    if refusal:
        return refusal
    asked = bars if bars is not None else SNAPSHOT_DEFAULT_BARS
    wanted = min(asked, SNAPSHOT_MAX_BARS)
    try:
        cached = cache.read_cache(ctx.home, venue, pair, tf)
    except (OSError, ValueError, KeyError, TypeError):
        cached = None
    if cached is None:
        return _refused(tool, CODE_MARKET_SNAPSHOT_NOT_FOUND, MESSAGE_MARKET_SNAPSHOT_NOT_FOUND)
    candles, digest = cached

    armed, _live = _paper_arm_of(ctx, venue, pair)
    if armed is not None:
        ticks = alerts_app.current_arm_ticks(journal.read_records_lenient(ctx.home), armed)
        timeframe_seconds = alerts_app.pack_timeframe_seconds(armed.pack_path)
    else:
        ticks = []
        timeframe_seconds = alerts_app.TIMEFRAME_SECONDS.get(tf)
    freshness = dataclasses.asdict(alerts_app.feed_freshness(ticks, timeframe_seconds=timeframe_seconds))

    body = {
        "venue": venue,
        "pair": pair,
        "timeframe": tf,
        "dataset_sha256": digest,
        "rows_total": len(candles),
        "armed_on_paper": armed is not None,
        "freshness": freshness,
        "candles": [_candle_row(c) for c in candles[-wanted:]],
        "truncated": len(candles) > wanted and asked > wanted,
    }
    return cap_result(tool, body, drop_front=("candles",))


# ---- catalogue ----------------------------------------------------------

TOOL_CATALOGUE = "catalogue"

NOTICE_NOT_ADVICE = (
    "Pack information only. Not personalized advice. No returns are promised, and backtest "
    "or fixture results are not market performance."
)


def _disclosures(row: Any) -> dict | None:
    value = row.get("disclosures") if isinstance(row, dict) else None
    if not isinstance(value, dict):
        return None
    out = {
        str(k)[:64]: v if isinstance(v, bool) else sanitize.text(str(v), max_len=200)
        for k, v in list(value.items())[:MAX_DISCLOSURES]
        if isinstance(v, bool | str)
    }
    return out or None


def _catalogue_entry(entry: dict) -> dict:
    out: dict = {}
    for name in ("id", "tier", "label", "version", "description"):
        if isinstance(entry.get(name), str):
            out[name] = sanitize.text(entry[name], max_len=600)
    permissions = entry.get("permissions")
    if isinstance(permissions, list):
        out["permissions"] = [sanitize.text(p, max_len=64) for p in permissions if isinstance(p, str)]
    disclosures = _disclosures(entry)
    if disclosures:
        out["disclosures"] = disclosures
    return out


def _catalogue(ctx: ToolContext, params: dict) -> dict:
    document = pack_library.list_catalogue(ctx.home)
    entries = [e for e in (document.get("packs") or []) if isinstance(e, dict)]
    by_id = {e.get("id"): e for e in entries}
    installed = []
    for row in packs_app.list_packs(ctx.home):
        out = {k: row.get(k) for k in ("bucket", "pack_id", "version", "permissions", "rollback_ref")}
        disclosures = _disclosures(row) or _disclosures(by_id.get(row.get("pack_id")))
        if disclosures:
            out["disclosures"] = disclosures
        installed.append(out)
    body = {
        "notice": NOTICE_NOT_ADVICE,
        "catalogue_version": str(document.get("catalogue_version", "0")),
        "installed": installed,
        "catalogue": [_catalogue_entry(e) for e in entries],
    }
    return cap_result(TOOL_CATALOGUE, body, drop_back=("catalogue", "installed"))


# ---- explain_bar --------------------------------------------------------

TOOL_EXPLAIN_BAR = "explain_bar"


def _read_job_file(path: Path) -> bytes | None:
    """Read one stored job file: a regular file only, never through a symlink, bounded."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return None
        fd = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STORED_JOB_BYTES:
            return None
        data = os.read(fd, MAX_STORED_JOB_BYTES + 1)
        chunks = [data]
        total = len(data)
        while data and total <= MAX_STORED_JOB_BYTES:
            data = os.read(fd, MAX_STORED_JOB_BYTES + 1 - total)
            chunks.append(data)
            total += len(data)
    except OSError:
        return None
    finally:
        os.close(fd)
    return b"".join(chunks) if total <= MAX_STORED_JOB_BYTES else None


def _stored_job_trace(home: Path, job_id: str) -> list | None:
    """The stored trace of a succeeded backtest job, or ``None``.

    The file is found by listing ``research-jobs/`` and matching its stem, so
    the agent's text never becomes part of a path. Nothing is written.
    """
    try:
        paths = sorted((home / "research-jobs").glob("*.json"))
    except OSError:
        return None
    for path in paths:
        if path.stem != job_id:
            continue
        raw = _read_job_file(path)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        job = payload.get("job") if isinstance(payload, dict) else None
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(job, dict) or job.get("id") != job_id or job.get("state") != "succeeded":
            return None
        trace = result.get("trace") if isinstance(result, dict) else None
        return trace if isinstance(trace, list) else None
    return None


def _explain_backtest_bar(ctx: ToolContext, job_id: str, bar_ts: int) -> dict:
    trace = _stored_job_trace(ctx.home, job_id) or []
    for row in trace:
        if isinstance(row, dict) and row.get("bar_ts") == bar_ts:
            return cap_result(TOOL_EXPLAIN_BAR, {"source": "backtest_job", "job_id": job_id, "bar": row})
    return _refused(TOOL_EXPLAIN_BAR, CODE_EXPLAIN_BAR_NOT_FOUND, MESSAGE_EXPLAIN_BAR_NOT_FOUND)


def _explain_paper_bar(ctx: ToolContext, venue: str, pair: str, bar_ts: int) -> dict:
    tool = TOOL_EXPLAIN_BAR
    armed, live = _paper_arm_of(ctx, venue, pair)
    if live:
        return _refused(tool, CODE_LIVE_READ_REFUSED, MESSAGE_LIVE_READ_REFUSED)
    if armed is None:
        return _refused(tool, CODE_EXPLAIN_BAR_NOT_FOUND, MESSAGE_EXPLAIN_BAR_NOT_FOUND)
    raw = read_regular_file_bounded(Path(armed.pack_path), MAX_PACK_BYTES)
    if raw is None or hashlib.sha256(raw).hexdigest() != armed.pack_sha256:
        return _refused(tool, CODE_EXPLAIN_BAR_PACK_CHANGED, MESSAGE_EXPLAIN_BAR_PACK_CHANGED)
    try:
        pack = json.loads(raw) if raw is not None else None
    except ValueError:
        pack = None
    timeframe = pack.get("timeframe") if isinstance(pack, dict) else None
    if not isinstance(timeframe, str) or _TIMEFRAME_RE.fullmatch(timeframe) is None:
        return _refused(tool, CODE_EXPLAIN_BAR_NOT_FOUND, MESSAGE_EXPLAIN_BAR_NOT_FOUND)
    try:
        cached = cache.read_cache(ctx.home, venue, pair, timeframe)
    except (OSError, ValueError, KeyError, TypeError):
        cached = None
    if cached is None:
        return _refused(tool, CODE_EXPLAIN_BAR_NOT_FOUND, MESSAGE_EXPLAIN_BAR_NOT_FOUND)
    candles = cached[0]
    index = next((i for i, c in enumerate(candles) if c.ts_ms == bar_ts), None)
    if index is None:
        return _refused(tool, CODE_EXPLAIN_BAR_NOT_FOUND, MESSAGE_EXPLAIN_BAR_NOT_FOUND)
    # A bar's decision reads only that bar and earlier ones.
    row = paper_decision_trace(pack, candles[: index + 1])[index]
    body = {"source": "paper_recompute", "venue": venue, "pair": pair, "timeframe": timeframe, "bar": row}
    return cap_result(tool, body)


def _explain_bar(ctx: ToolContext, params: dict) -> dict:
    tool = TOOL_EXPLAIN_BAR
    bar_ts, refusal = _bar_ts(tool, params)
    if refusal:
        return refusal
    has_job = "job_id" in params
    has_market = "venue" in params or "pair" in params
    if has_job == has_market:
        return invalid_request(tool, "pass job_id, or venue and pair, with bar_ts").to_dict()
    if has_job:
        job_id = params["job_id"]
        if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
            return invalid_request(tool, f"job_id: {MESSAGE_BAD_JOB_ID}").to_dict()
        return _explain_backtest_bar(ctx, job_id, bar_ts)
    names, refusal = _market_names(tool, params, timeframe=False)
    if refusal:
        return refusal
    return _explain_paper_bar(ctx, names[0], names[1], bar_ts)


# ---- tour_step ----------------------------------------------------------

TOOL_TOUR_STEP = "tour_step"


def _tour_step(ctx: ToolContext, params: dict) -> dict:
    tool = TOOL_TOUR_STEP
    step_id = params.get("step_id")
    listing = [{"id": s.id, "title": s.title} for s in tour.TOUR_STEPS]
    if step_id is None:
        return cap_result(tool, {"tour_version": tour.TOUR_VERSION, "steps": listing})
    if not isinstance(step_id, str):
        return invalid_request(tool, "step_id must be a string").to_dict()
    for step in tour.TOUR_STEPS:
        if step.id == step_id:
            body = {
                "tour_version": tour.TOUR_VERSION,
                "step": {"id": step.id, "title": step.title, "facts": list(step.facts)},
            }
            return cap_result(tool, body)
    available = ", ".join(s.id for s in tour.TOUR_STEPS)
    return _refused(tool, CODE_TOUR_STEP_NOT_FOUND, f"unknown step_id; available: {available}")


# ---- registration -------------------------------------------------------


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    schema: dict = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = required
    return schema


def read_specs() -> tuple[ToolSpec, ...]:
    """The eight read tools, in registration order. Schemas are built fresh per call."""

    def spec(name: str, schema: dict, labels: tuple[str, ...], description: str, handler) -> ToolSpec:
        return ToolSpec(
            name=name,
            effect="read",
            cost="light",
            input_schema=schema,
            labels=labels,
            description=description,
            handler=handler,
        )

    venue = {"type": "string", "description": "Venue name, for example kraken."}
    pair = {"type": "string", "description": "Venue pair, for example SUIUSD."}
    return (
        spec(
            TOOL_POSITIONS,
            _schema({"venue": venue, "pair": pair}),
            (LABEL_PAPER,),
            "Paper positions: free cash and holdings per armed paper market. Live markets are never shown.",
            _positions,
        ),
        spec(
            TOOL_JOURNAL_RECENT,
            _schema(
                {
                    "since": {"type": "string", "enum": list(journal.WINDOW_SECONDS)},
                    "tail": {"type": "integer", "minimum": 1, "maximum": JOURNAL_MAX_TAIL},
                }
            ),
            (),
            f"Recent paper journal records (at most {JOURNAL_MAX_TAIL}), secret fields removed.",
            _journal_recent,
        ),
        spec(TOOL_ALERTS, _schema(), (), "Current alerts: kill switch, reconcile, stale feeds and refusals.", _alerts),
        spec(
            TOOL_UNRESOLVED_ORDERS,
            _schema(),
            (),
            "Orders still awaiting reconcile, oldest first, with their age and last lookup result.",
            _unresolved_orders,
        ),
        spec(
            TOOL_MARKET_SNAPSHOT,
            _schema(
                {
                    "venue": venue,
                    "pair": pair,
                    "timeframe": {"type": "string", "description": "For example 1h."},
                    "bars": {"type": "integer", "minimum": 1, "maximum": SNAPSHOT_MAX_BARS},
                },
                ["venue", "pair", "timeframe"],
            ),
            (),
            "The last cached candles for a market (at most 200) and how fresh the paper feed is. No network call.",
            _market_snapshot,
        ),
        spec(
            TOOL_CATALOGUE,
            _schema(),
            (LABEL_NOT_ADVICE,),
            "Installed packs and the cached pack catalogue with each pack's disclosures. Not advice.",
            _catalogue,
        ),
        spec(
            TOOL_EXPLAIN_BAR,
            _schema(
                {
                    "job_id": {"type": "string", "description": "A stored backtest job."},
                    "venue": venue,
                    "pair": pair,
                    "bar_ts": {"type": "integer", "description": "The bar's open time in milliseconds."},
                },
                ["bar_ts"],
            ),
            (LABEL_SIMULATED,),
            (
                "Why one bar decided what it did: its candle, indicators, condition outcomes and target. "
                "Pass job_id for a stored backtest, or venue and pair for the armed paper strategy. "
                "Simulated, not market performance."
            ),
            _explain_bar,
        ),
        spec(
            TOOL_TOUR_STEP,
            _schema({"step_id": {"type": "string"}}),
            (),
            "Krell's curated tour: one step's facts, or the list of steps when no step_id is given.",
            _tour_step,
        ),
    )


def register_read_tools(core: AgentToolCore) -> None:
    """Add the read tools to ``core``, each unless it is already there.

    Idempotent, so every front door (the MCP server, Krell) can call it on a
    shared core. A malformed spec still raises from ``register``.
    """
    for spec in read_specs():
        if not core.has(spec.name):
            core.register(spec)


__all__ = [
    "CODE_EXPLAIN_BAR_NOT_FOUND",
    "CODE_EXPLAIN_BAR_PACK_CHANGED",
    "CODE_LIVE_READ_REFUSED",
    "CODE_MARKET_SNAPSHOT_NOT_FOUND",
    "CODE_RESULT_TOO_LARGE",
    "CODE_TOUR_STEP_NOT_FOUND",
    "CODE_UNRESOLVED_ORDERS_UNAVAILABLE",
    "ENVELOPE_RESERVE_BYTES",
    "MAX_RESULT_BYTES",
    "NOTICE_NOT_ADVICE",
    "cap_result",
    "read_specs",
    "register_read_tools",
]
