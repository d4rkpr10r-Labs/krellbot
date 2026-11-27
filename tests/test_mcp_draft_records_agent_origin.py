"""Draft origin: who drafted a revision lives in meta, never in pack bytes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from krellbot.agent.actors import KRELL, Actor
from krellbot.agent.core import AgentToolCore
from krellbot.application.strategy import StrategyDraftService
from krellbot.mcp import McpTools


def _pack(strategy_id: str = "origin-demo") -> dict:
    return {
        "schema_version": 1,
        "id": strategy_id,
        "version": "1.0.0",
        "label": "Origin demo",
        "author": "tests",
        "timeframe": "1h",
        "indicators": {"sma20": {"fn": "sma", "src": "close", "len": 20}},
        "entry": ["close", ">", "sma20"],
        "exit": ["close", "<", "sma20"],
        "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": "kraken", "pair": "SUIUSD"}],
    }


def _meta(home: Path, strategy_id: str, revision_id: str) -> dict:
    path = home / "drafts" / strategy_id / f"{revision_id}.meta.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_mcp_draft_records_client_origin_in_meta_not_pack_bytes(home: Path, tmp_path_factory):
    other_home = tmp_path_factory.mktemp("workstation-home")
    workstation_rev = StrategyDraftService(other_home).create(_pack())["revision_id"]

    result = McpTools(home, actor=Actor.mcp("cursor")).call("draft", pack=_pack())

    assert result["ok"] is True
    revision_id = result["result"]["revision_id"]
    assert revision_id == workstation_rev
    assert result["result"]["origin"] == "mcp:cursor"
    assert _meta(home, "origin-demo", revision_id)["origin"] == "mcp:cursor"
    pack_bytes = (home / "drafts" / "origin-demo" / f"{revision_id}.json").read_text(encoding="utf-8")
    assert "mcp:cursor" not in pack_bytes
    assert "origin" not in json.loads(pack_bytes)


def test_krell_actor_records_krell_origin(home: Path):
    result = AgentToolCore(home).call(KRELL, "draft", {"pack": _pack()})
    assert result["result"]["origin"] == "krell"


def test_resaving_identical_bytes_keeps_the_first_origin(home: Path):
    first = StrategyDraftService(home).create(_pack())
    assert first["origin"] == "workstation"

    again = McpTools(home, actor=Actor.mcp("cursor")).call("draft", pack=_pack())

    assert again["result"]["outcome"] == "existing"
    assert again["result"]["origin"] == "workstation"
    assert _meta(home, "origin-demo", first["revision_id"])["origin"] == "workstation"


def test_old_meta_without_origin_reads_as_workstation(home: Path):
    svc = StrategyDraftService(home)
    rev = svc.create(_pack())["revision_id"]
    meta_path = home / "drafts" / "origin-demo" / f"{rev}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta["origin"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert svc.create(_pack())["origin"] == "workstation"
    assert [row["origin"] for row in svc.list_owned_summaries()] == ["workstation"]


@pytest.mark.parametrize(
    "bad",
    ["", "agent", "mcp:", "mcp:Cursor", "mcp:a b", "mcp:<b>x</b>", "mcp:" + "a" * 41, "krell\n"],
)
def test_crafted_origin_is_refused_and_writes_nothing(home: Path, bad: str):
    svc = StrategyDraftService(home)
    with pytest.raises(ValueError):
        svc.create(_pack(), origin=bad)
    assert not (home / "drafts" / "origin-demo").exists() or not list(
        (home / "drafts" / "origin-demo").glob("*.meta.json")
    )
    rev = svc.create(_pack())["revision_id"]
    changed = _pack()
    changed["label"] = "changed"
    with pytest.raises(ValueError):
        svc.edit(rev, changed, origin=bad)
    assert len(list((home / "drafts" / "origin-demo").glob("*.meta.json"))) == 1


def test_list_owned_summaries_carry_origin(home: Path):
    svc = StrategyDraftService(home)
    svc.create(_pack(), origin="krell")
    assert [row["origin"] for row in svc.list_owned_summaries()] == ["krell"]
