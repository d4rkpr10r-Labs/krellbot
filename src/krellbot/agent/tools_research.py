"""Agent research tools: ``backtest`` and ``compare``, with the M29.1 run identity.

Both run in-process through :class:`~krellbot.application.research.ResearchService`
and never create a research job: roadmap decision 16 forbids a second
``JobManager`` over the home and job creation over MCP.

Run identity. Every completed run returns
``run: {run_id, receipt_canonical_sha256, pack_sha256, data_manifest_sha256}``,
built by the web job's own helpers (``api.jobs.build_run_record`` and
``api.jobs.receipt_canonical_sha256``): the receipt states the pack identity,
the data manifest, the engine version, the costs, the venue, pair and
timeframe; the four window bounds are what the call sent (``None`` when not
sent). The same inputs therefore give the same ``run_id`` whether a web job or
an agent ran them, so an agent's claim can be checked in the workstation.

Input files. Agents pass untrusted, possibly prompt-injected text, and tool
results go to an AI provider. A pack named by ``pack_path`` must be a
``.json`` file under the home's ``packs/``, ``drafts/`` or ``agent-packs/``;
``revision_id`` names a draft revision instead. A dataset must be a ``.csv``
file in the home folder itself or under its ``cache/`` or
``datasets/imported/``. Paths are resolved (symlinks and ``..`` included)
before the check, and each file is read once, bounded and regular-file only
(:func:`krellbot.agent.core.read_regular_file_bounded`). The bytes read are
copied to a private temporary folder and the run reads only those copies, so
a file swapped after the check cannot change or flood the run. A refusal
names the param but never echoes the path, and research refusals carry a
fixed message per code, never engine text that may quote the file. A hard
link inside an allowed folder, or a swap between the check and the read,
needs write access inside the home, which is out of scope.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from krellbot.agent.core import (
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
from krellbot.api.jobs import JOB_STATE_SUCCEEDED, build_run_record, receipt_canonical_sha256
from krellbot.application import research
from krellbot.application.research import ResearchRequest, ResearchService
from krellbot.application.strategy import RevisionNotFound, StrategyDraftService

TOOL_BACKTEST = "backtest"
TOOL_COMPARE = "compare"

COMPARE_MIN_REVISIONS = 2
COMPARE_MAX_REVISIONS = 4

# A dataset CSV is read whole, once, before the run.
MAX_DATASET_BYTES = 64 * 1024 * 1024

# Home-relative folders an agent may name files in, per kind.
PACK_ROOTS: tuple[str, ...] = ("packs", "drafts", "agent-packs")
DATASET_ROOTS: tuple[str, ...] = ("cache", "datasets/imported")

MESSAGE_PACK_INPUT = (
    "pack_path must name a readable .json file of at most 1 MiB under the Krellbot home's "
    "packs/, drafts/ or agent-packs/ folder; or pass revision_id"
)
MESSAGE_DATASET_INPUT = (
    "dataset_csv must name a readable .csv file of at most 64 MiB in the Krellbot home folder "
    "or under its cache/ or datasets/imported/ folder"
)
MESSAGE_REVISION_UNREADABLE = "the draft revision could not be read"
MESSAGE_AMBIGUOUS_PACK = "pass pack_path or revision_id, not both"
MESSAGE_COMPARE_COUNT = (
    f"compare takes {COMPARE_MIN_REVISIONS} to {COMPARE_MAX_REVISIONS} distinct revision_ids in a list"
)

CODE_RESEARCH_REFUSED = "research_refused"
MESSAGE_RESEARCH_REFUSED = "the backtest was refused"

# One fixed message per research refusal code. Engine refusal text can quote
# what it read (timestamps, pack values, paths), so it never reaches an agent.
_REFUSAL_MESSAGES: dict[str, str] = {
    research.CODE_MISSING_PACK: "the pack is not a JSON object",
    research.CODE_INVALID_PACK: "the pack failed validation; save it as a draft and run validate to see why",
    research.CODE_LEGACY_PACK_NOT_RUNNABLE: "legacy packs are not runnable",
    research.CODE_PACK_HAS_NO_MARKET: "the pack has no market for this venue and pair",
    research.CODE_UNKNOWN_VENUE: "unknown venue; use kraken or coinbase",
    research.CODE_UNSUPPORTED_TIMEFRAME: "unsupported timeframe",
    research.CODE_GAPPED_DATA: (
        "the dataset has out-of-order or missing candles; allow_gaps true accepts missing ones"
    ),
    research.CODE_EMPTY_DATASET: "no candles for this pair in the dataset and window",
    research.CODE_NO_DATASET_AND_NO_FETCH: "backtest needs dataset_csv",
    research.CODE_INVALID_DATASET: "the dataset could not be read as candles",
    research.CODE_INVALID_HOLDOUT: "holdout_from_ms and holdout_to_ms must be sent together",
    research.CODE_HOLDOUT_OVERLAP: "the scored window overlaps the holdout window",
    research.CODE_FUTURE_DATA: "the strategy read data after its decision bar",
    research.CODE_INVALID_NODES: "nodes must be a list",
    research.CODE_NUMERIC_OUT_OF_RANGE: research.NUMERIC_OUT_OF_RANGE_MESSAGE,
}

_WINDOW_PARAMS: tuple[str, ...] = ("from_ms", "to_ms", "holdout_from_ms", "holdout_to_ms")
_COST_PARAMS: tuple[str, ...] = ("fee_bps", "slippage_bps")
_WORKDIR_PREFIX = "krellbot-agent-research-"

# Dataset line shapes for counting bars (whitespace as ``bytes.strip`` sees it, newline aside).
_BLANK_LINE = re.compile(rb"^[ \t\r\f\v]*$", re.MULTILINE)
_HEADER_LINE = re.compile(rb"^[ \t\r\f\v]*ts_ms,", re.MULTILINE)


# ---- input files ----------------------------------------------------------


def _home_file(home: Path, value: object, *, suffix: str, roots: tuple[str, ...], top_level: bool) -> Path | None:
    """Resolve an agent-named file to its real path inside an allowed home folder, or ``None``.

    A relative name is taken from the home. The real path (symlinks and
    ``..`` resolved) must end in ``suffix`` and lie under one of ``roots``,
    or, with ``top_level``, directly in the home folder.
    """
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        home_real = Path(os.path.realpath(home))
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = home_real / candidate
        real = Path(os.path.realpath(candidate))
        parts = real.relative_to(home_real).parts
    except (OSError, ValueError):
        return None
    if not parts or real.suffix.lower() != suffix:
        return None
    if len(parts) == 1:
        return real if top_level else None
    for root in roots:
        root_parts = tuple(root.split("/"))
        if len(parts) > len(root_parts) and parts[: len(root_parts)] == root_parts:
            return real
    return None


def read_pack_input(home: Path, value: object) -> bytes | None:
    """Bytes of an agent-named pack file in an allowed home folder, or ``None``."""
    path = _home_file(home, value, suffix=".json", roots=PACK_ROOTS, top_level=False)
    return None if path is None else read_regular_file_bounded(path, MAX_PACK_BYTES)


def read_dataset_input(home: Path, value: object) -> bytes | None:
    """Bytes of an agent-named dataset CSV in the home or an allowed home folder, or ``None``.

    A ``.csv`` directly in the home folder is accepted: Krellbot writes none
    there itself, so it is one the user put there.
    """
    path = _home_file(home, value, suffix=".csv", roots=DATASET_ROOTS, top_level=True)
    return None if path is None else read_regular_file_bounded(path, MAX_DATASET_BYTES)


def count_csv_bars(raw: bytes) -> int:
    """Count the data rows (bars) of dataset CSV bytes without splitting them into lines.

    Lines end at ``\\n`` (so ``\\r\\n`` too); a last line without one still
    counts. Blank or whitespace-only lines do not count, and neither does one
    ``ts_ms,`` header line. Memory stays flat: the newlines are counted in
    place and the blank lines are matched one at a time.
    """
    if not raw:
        return 0
    lines = raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1)
    blank = sum(1 for _ in _BLANK_LINE.finditer(raw))
    if raw.endswith(b"\n"):
        # In multiline mode ``^...$`` also matches the empty tail after the last newline: not a line.
        blank -= 1
    header = 1 if _HEADER_LINE.search(raw) is not None else 0
    return max(0, lines - blank - header)


def dataset_bars(home: Path, value: object) -> int | None:
    """Bars in an agent-named dataset, read bounded; ``None`` when it may not or cannot be read."""
    raw = read_dataset_input(home, value)
    return None if raw is None else count_csv_bars(raw)


# ---- run inputs ---------------------------------------------------------------


@dataclass(frozen=True)
class RunInputs:
    """The run inputs an agent call sends, checked; unsent costs are the engine's defaults."""

    venue: str
    pair: str | None
    timeframe: str | None
    allow_gaps: bool
    fee_bps: int | None
    slippage_bps: int | None
    slippage_mult: float | None
    from_ms: int | None
    to_ms: int | None
    holdout_from_ms: int | None
    holdout_to_ms: int | None

    def request(self, pack_path: Path, dataset_csv: Path | None) -> ResearchRequest:
        return ResearchRequest(
            pack_path=pack_path,
            venue=self.venue,
            pair=self.pair,
            timeframe=self.timeframe,
            dataset_csv=dataset_csv,
            fee_bps=self.fee_bps,
            slippage_bps=self.slippage_bps,
            slippage_mult=self.slippage_mult,
            from_ms=self.from_ms,
            to_ms=self.to_ms,
            holdout_from_ms=self.holdout_from_ms,
            holdout_to_ms=self.holdout_to_ms,
            allow_gaps=self.allow_gaps,
        )

    def window(self) -> dict:
        """The run-id window inputs: a bound the call did not send is ``None``, as for a web job."""
        return {name: getattr(self, name) for name in _WINDOW_PARAMS}


def _run_inputs(tool: str, params: dict) -> RunInputs | dict:
    """Check the run inputs of a ``backtest`` or ``compare`` call; the inputs, or a refusal."""
    venue = params.get("venue")
    if venue is None or venue == "":
        venue = "kraken"
    if not isinstance(venue, str):
        return invalid_request(tool, "venue must be a string").to_dict()
    text: dict[str, str | None] = {}
    for name in ("pair", "timeframe"):
        value = params.get(name)
        if value is not None and not isinstance(value, str):
            return invalid_request(tool, f"{name} must be a string").to_dict()
        text[name] = value or None
    numbers: dict[str, int | None] = {}
    for name in _COST_PARAMS:
        value = params.get(name)
        if value is not None and (type(value) is not int or value < 0):
            return invalid_request(tool, f"{name} must be a whole number of basis points, 0 or more").to_dict()
        numbers[name] = value
    for name in _WINDOW_PARAMS:
        value = params.get(name)
        if value is not None and type(value) is not int:
            return invalid_request(tool, f"{name} must be a whole number of epoch milliseconds").to_dict()
        numbers[name] = value
    mult = params.get("slippage_mult")
    if mult is not None:
        if type(mult) not in (int, float) or not math.isfinite(mult) or mult < 0:
            return invalid_request(tool, "slippage_mult must be a finite number, 0 or more").to_dict()
        mult = float(mult)
    return RunInputs(
        venue=venue,
        pair=text["pair"],
        timeframe=text["timeframe"],
        # As for a web job, only the literal boolean true allows gaps.
        allow_gaps=params.get("allow_gaps") is True,
        fee_bps=numbers["fee_bps"],
        slippage_bps=numbers["slippage_bps"],
        slippage_mult=mult,
        from_ms=numbers["from_ms"],
        to_ms=numbers["to_ms"],
        holdout_from_ms=numbers["holdout_from_ms"],
        holdout_to_ms=numbers["holdout_to_ms"],
    )


def _revision_bytes(ctx: ToolContext, tool: str, revision_id: str) -> bytes | dict:
    """The stored pack bytes of a draft revision (its id is checked by the core), or a refusal."""
    try:
        path = StrategyDraftService(ctx.home, clock=ctx.clock).revision_path(revision_id)
    except RevisionNotFound:
        return invalid_request(tool, f"revision not found: {revision_id}").to_dict()
    raw = read_pack_input(ctx.home, path)
    return raw if raw is not None else invalid_request(tool, MESSAGE_REVISION_UNREADABLE).to_dict()


def _pack_bytes(ctx: ToolContext, tool: str, params: dict) -> bytes | dict:
    revision_id = params.get("revision_id")
    pack_path = params.get("pack_path")
    if revision_id and pack_path:
        return invalid_request(tool, MESSAGE_AMBIGUOUS_PACK).to_dict()
    if revision_id:
        return _revision_bytes(ctx, tool, str(revision_id))
    if not pack_path:
        return invalid_request(tool, f"{tool} requires pack_path or revision_id").to_dict()
    raw = read_pack_input(ctx.home, pack_path)
    return raw if raw is not None else invalid_request(tool, MESSAGE_PACK_INPUT).to_dict()


def _dataset_bytes(ctx: ToolContext, tool: str, params: dict, *, required: bool) -> bytes | dict | None:
    value = params.get("dataset_csv")
    if value is None or value == "":
        return invalid_request(tool, f"{tool} requires dataset_csv").to_dict() if required else None
    raw = read_dataset_input(ctx.home, value)
    return raw if raw is not None else invalid_request(tool, MESSAGE_DATASET_INPUT).to_dict()


# ---- runs ---------------------------------------------------------------------


def run_identity(receipt: dict, window: dict) -> dict:
    """``{run_id, receipt_canonical_sha256, pack_sha256, data_manifest_sha256}`` of a completed run.

    Built by the web job's run-record helper, so the same inputs give the
    same ``run_id``. The receipt states every input but the window bounds.
    """
    record = build_run_record(
        state=JOB_STATE_SUCCEEDED,
        request={},
        identity=window,
        receipt=receipt,
        receipt_digest=receipt_canonical_sha256(receipt),
    )
    inputs = record["inputs"]
    return {
        "run_id": record["run_id"],
        "receipt_canonical_sha256": record["receipt_canonical_sha256"],
        "pack_sha256": inputs["pack_sha256"],
        "data_manifest_sha256": inputs["data_manifest_sha256"],
    }


def _write(directory: Path, name: str, raw: bytes) -> Path:
    path = directory / name
    path.write_bytes(raw)
    return path


def _run(ctx: ToolContext, inputs: RunInputs, pack: Path, dataset: Path | None) -> dict:
    """One in-process research run over private copies.

    Returns ``{"ok": True, "receipt", "detail", "run"}``, or
    ``{"ok": False, "code", "message"}`` with a fixed message.
    """
    result = ResearchService(home=ctx.home, fetch=ctx.fetch).run(inputs.request(pack, dataset))
    receipt = result.legacy_receipt
    if not result.ok or not isinstance(receipt, dict):
        code = (result.refusal or {}).get("code")
        if code not in _REFUSAL_MESSAGES:
            return {"ok": False, "code": CODE_RESEARCH_REFUSED, "message": MESSAGE_RESEARCH_REFUSED}
        return {"ok": False, "code": code, "message": _REFUSAL_MESSAGES[code]}
    return {"ok": True, "receipt": receipt, "detail": result.detail, "run": run_identity(receipt, inputs.window())}


def _backtest(ctx: ToolContext, params: dict) -> dict:
    inputs = _run_inputs(TOOL_BACKTEST, params)
    if isinstance(inputs, dict):
        return inputs
    pack = _pack_bytes(ctx, TOOL_BACKTEST, params)
    if isinstance(pack, dict):
        return pack
    dataset = _dataset_bytes(ctx, TOOL_BACKTEST, params, required=False)
    if isinstance(dataset, dict):
        return dataset
    with tempfile.TemporaryDirectory(prefix=_WORKDIR_PREFIX) as workdir:
        pack_path = _write(Path(workdir), "pack.json", pack)
        dataset_path = None if dataset is None else _write(Path(workdir), "dataset.csv", dataset)
        # Offline unless the core was given a fetch: no network and no keyring.
        outcome = _run(ctx, inputs, pack_path, dataset_path)
    if not outcome["ok"]:
        return ToolRefusal(outcome["code"], outcome["message"], TOOL_BACKTEST).to_dict()
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "effect": "unchanged",
        "tool": TOOL_BACKTEST,
        "legacy_receipt": outcome["receipt"],
        "detail": outcome["detail"],
        "run": outcome["run"],
    }


def _compare_row(revision_id: str, outcome: dict) -> dict:
    if outcome["ok"]:
        return {
            "revision_id": revision_id,
            "ok": True,
            "metrics": outcome["receipt"].get("metrics"),
            "run": outcome["run"],
        }
    return {
        "revision_id": revision_id,
        "ok": False,
        "code": outcome["code"],
        "message": outcome["message"],
        "metrics": None,
        "run": None,
    }


def _compare(ctx: ToolContext, params: dict) -> dict:
    """Backtest each revision in turn on one dataset; one row per revision, in the order given.

    Every revision and the dataset are read before anything runs, so a bad
    input refuses the whole call. A revision the engine refuses (no market
    for the venue, say) gets a refused row and the others still run.
    """
    ids = params.get("revision_ids")
    if not isinstance(ids, list | tuple) or not COMPARE_MIN_REVISIONS <= len(ids) <= COMPARE_MAX_REVISIONS:
        return invalid_request(TOOL_COMPARE, MESSAGE_COMPARE_COUNT).to_dict()
    revision_ids = [str(value) for value in ids]
    if len(set(revision_ids)) != len(revision_ids):
        return invalid_request(TOOL_COMPARE, MESSAGE_COMPARE_COUNT).to_dict()
    inputs = _run_inputs(TOOL_COMPARE, params)
    if isinstance(inputs, dict):
        return inputs
    # One pinned dataset, so every row scores the same candles.
    dataset = _dataset_bytes(ctx, TOOL_COMPARE, params, required=True)
    if isinstance(dataset, dict):
        return dataset
    assert dataset is not None
    packs: list[bytes] = []
    for revision_id in revision_ids:
        pack = _revision_bytes(ctx, TOOL_COMPARE, revision_id)
        if isinstance(pack, dict):
            return pack
        packs.append(pack)
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix=_WORKDIR_PREFIX) as workdir:
        dataset_path = _write(Path(workdir), "dataset.csv", dataset)
        for index, (revision_id, pack) in enumerate(zip(revision_ids, packs, strict=True)):
            pack_path = _write(Path(workdir), f"pack-{index}.json", pack)
            rows.append(_compare_row(revision_id, _run(ctx, inputs, pack_path, dataset_path)))
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "effect": "unchanged",
        "tool": TOOL_COMPARE,
        "rows": rows,
    }


# ---- specs --------------------------------------------------------------------


def _run_input_properties() -> dict:
    return {
        "venue": {"type": "string", "default": "kraken"},
        "pair": {"type": "string"},
        "timeframe": {"type": "string"},
        "allow_gaps": {"type": "boolean", "default": False},
        "fee_bps": {"type": "integer", "minimum": 0, "description": "Taker fee; the venue default when unsent."},
        "slippage_bps": {"type": "integer", "minimum": 0},
        "slippage_mult": {"type": "number", "minimum": 0},
        "from_ms": {"type": "integer", "description": "First scored bar, epoch ms."},
        "to_ms": {"type": "integer", "description": "Last scored bar, epoch ms."},
        "holdout_from_ms": {"type": "integer"},
        "holdout_to_ms": {"type": "integer"},
    }


_DATASET_PROPERTY_DESCRIPTION = (
    "Local OHLCV CSV: a .csv in the Krellbot home folder or under its cache/ or datasets/imported/ folder."
)


def backtest_spec() -> ToolSpec:
    """The ``backtest`` verb (registered by the core on construction)."""
    return ToolSpec(
        name=TOOL_BACKTEST,
        effect="research",
        cost="heavy",
        input_schema={
            "type": "object",
            "properties": {
                "pack_path": {
                    "type": "string",
                    "description": "A pack .json under the Krellbot home's packs/, drafts/ or agent-packs/ folder.",
                },
                "revision_id": {"type": "string", "description": "A draft revision, instead of pack_path."},
                "dataset_csv": {"type": "string", "description": _DATASET_PROPERTY_DESCRIPTION},
                **_run_input_properties(),
            },
        },
        labels=(LABEL_SIMULATED,),
        description=(
            "Backtest a pack file or a draft revision offline against a local dataset. Returns the receipt and "
            "the run identity (run_id and receipt digest) the workstation records for the same inputs. "
            "Results are simulated, not market performance."
        ),
        handler=_backtest,
    )


def compare_spec() -> ToolSpec:
    """The ``compare`` tool: 2 to 4 draft revisions side by side on one dataset."""
    return ToolSpec(
        name=TOOL_COMPARE,
        effect="research",
        cost="heavy",
        input_schema={
            "type": "object",
            "properties": {
                "revision_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": COMPARE_MIN_REVISIONS,
                    "maxItems": COMPARE_MAX_REVISIONS,
                    "uniqueItems": True,
                },
                "dataset_csv": {"type": "string", "description": _DATASET_PROPERTY_DESCRIPTION},
                **_run_input_properties(),
            },
            "required": ["revision_ids", "dataset_csv"],
        },
        labels=(LABEL_SIMULATED,),
        description=(
            "Backtest 2 to 4 draft revisions one after another on one local dataset and return each one's "
            "metrics and run identity side by side. Results are simulated, not market performance."
        ),
        handler=_compare,
    )


def register_research_tools(core: AgentToolCore) -> None:
    """Add ``compare`` to ``core`` unless it is already there; ``backtest`` is a built-in verb.

    Idempotent, so every front door (the MCP server, Krell) can call it on a
    shared core. A malformed spec still raises from ``register``.
    """
    if not core.has(TOOL_COMPARE):
        core.register(compare_spec())


__all__ = [
    "CODE_RESEARCH_REFUSED",
    "COMPARE_MAX_REVISIONS",
    "COMPARE_MIN_REVISIONS",
    "DATASET_ROOTS",
    "MAX_DATASET_BYTES",
    "MESSAGE_AMBIGUOUS_PACK",
    "MESSAGE_COMPARE_COUNT",
    "MESSAGE_DATASET_INPUT",
    "MESSAGE_PACK_INPUT",
    "MESSAGE_RESEARCH_REFUSED",
    "MESSAGE_REVISION_UNREADABLE",
    "PACK_ROOTS",
    "TOOL_BACKTEST",
    "TOOL_COMPARE",
    "RunInputs",
    "backtest_spec",
    "compare_spec",
    "count_csv_bars",
    "dataset_bars",
    "read_dataset_input",
    "read_pack_input",
    "register_research_tools",
    "run_identity",
]
