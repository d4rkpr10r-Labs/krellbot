"""Cost estimates for agent tool calls: light or heavy, before anything runs.

Heavy work can require a human confirm (M38.7). A backtest's size is its
bars times its packs, where ``bars`` is the count of data rows in the dataset
CSV the call names: above :data:`HEAVY_BAR_PACKS` it is heavy. ``compare``
and ``paper_arm`` are always heavy. A backtest whose dataset cannot be
counted (none named, so the run would fetch; or one the research tools would
refuse) has an unknown size and is heavy.

The dataset is read exactly as the research tools read it: confined to the
home's data folders and bounded (:func:`krellbot.agent.tools_research.dataset_bars`),
so an estimate never reads a file a call could not use. ``seconds`` and
``result_tokens`` are ``None`` (not estimated) until there is a measured
basis for them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from krellbot.agent.core import Cost, normalize_tool_name
from krellbot.agent.limits import TOOL_PAPER_ARM
from krellbot.agent.tools_research import TOOL_BACKTEST, TOOL_COMPARE, dataset_bars

# Work above this many bars x packs is heavy.
HEAVY_BAR_PACKS = 20000
ALWAYS_HEAVY_TOOLS: frozenset[str] = frozenset({TOOL_COMPARE, TOOL_PAPER_ARM})


@dataclass(frozen=True)
class CostEstimate:
    """How much work one tool call is, as far as can be told before it runs."""

    cost: Cost
    bars: int | None
    packs: int
    seconds: float | None = None
    result_tokens: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def estimate(tool: str, params: dict, *, home: Path) -> CostEstimate:
    """Estimate one call of ``tool`` with ``params``. Reads at most one bounded dataset; never raises for bad params."""
    name = normalize_tool_name(tool)
    params = params if isinstance(params, dict) else {}
    if name == TOOL_BACKTEST:
        bars = dataset_bars(home, params.get("dataset_csv"))
        packs = 1
        heavy = bars is None or bars * packs > HEAVY_BAR_PACKS
        return CostEstimate("heavy" if heavy else "light", bars, packs)
    if name == TOOL_COMPARE:
        ids = params.get("revision_ids")
        packs = len(ids) if isinstance(ids, list | tuple) else 0
        return CostEstimate("heavy", dataset_bars(home, params.get("dataset_csv")), packs)
    if name in ALWAYS_HEAVY_TOOLS:
        return CostEstimate("heavy", None, 1)
    return CostEstimate("light", None, 0)


__all__ = [
    "ALWAYS_HEAVY_TOOLS",
    "HEAVY_BAR_PACKS",
    "CostEstimate",
    "estimate",
]
