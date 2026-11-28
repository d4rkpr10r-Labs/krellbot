"""The draft tool carries the pack format, so a model never has to guess it.

Its ``pack`` schema is derived from ``pack/schema.json`` in a provider-safe
keyword set, its description holds one runnable example, a missing id is made
from the label, and one call saves and validates.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

import jsonschema
import pytest

from krellbot.agent.actors import KRELL, Actor
from krellbot.agent.core import PACK_ID_PATTERN, AgentToolCore
from krellbot.application.strategy import StrategyDraftService
from krellbot.mcp import McpTools
from krellbot.pack import lint

# The keywords every provider we support accepts in a tool schema.
PROVIDER_SAFE_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "anyOf",
        "description",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
    }
)


def _source_schema() -> dict:
    return json.loads(resources.files("krellbot.pack").joinpath("schema.json").read_text(encoding="utf-8"))


def _draft_spec(home: Path, actor: Actor = KRELL):
    return next(spec for spec in AgentToolCore(home).specs(actor) if spec.name == "draft")


def _pack_schema(home: Path) -> dict:
    return _draft_spec(home).input_schema["properties"]["pack"]


def _example_pack(home: Path) -> dict:
    description = _draft_spec(home).description
    _, _, example = description.partition("Example pack: ")
    return json.loads(example)


def _starter_packs() -> list[dict]:
    examples = resources.files("krellbot.packs").joinpath("examples")
    return [
        json.loads(item.read_text(encoding="utf-8"))
        for item in sorted(examples.iterdir(), key=lambda item: item.name)
        if item.name.startswith("starter-") and item.name.endswith(".json")
    ]


def _pack(**overrides) -> dict:
    pack = {
        "schema_version": 1,
        "id": "format-demo",
        "version": "1.0.0",
        "label": "Format demo",
        "author": "tests",
        "timeframe": "1h",
        "indicators": {"sma20": {"fn": "sma", "src": "close", "len": 20}},
        "entry": ["close", ">", "sma20"],
        "exit": ["close", "<", "sma20"],
        "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": "kraken", "pair": "SUIUSD"}],
    }
    pack.update(overrides)
    return {key: value for key, value in pack.items() if value is not None}


def _draft(home: Path, pack: dict, **params) -> dict:
    result = AgentToolCore(home).call(KRELL, "draft", {"pack": pack, **params})
    assert result["ok"] is True, result
    return result["result"]


def _off_allowlist(node: object, path: str = "pack") -> list[str]:
    if not isinstance(node, dict):
        return [f"{path}: not an object schema"]
    found = [f"{path}.{key}" for key in node if key not in PROVIDER_SAFE_KEYWORDS]
    for name, sub in node.get("properties", {}).items():
        found += _off_allowlist(sub, f"{path}.properties.{name}")
    if "items" in node:
        found += _off_allowlist(node["items"], f"{path}.items")
    for index, sub in enumerate(node.get("anyOf", [])):
        found += _off_allowlist(sub, f"{path}.anyOf[{index}]")
    return found


def _branch(node: dict, kind: str) -> dict:
    return next(branch for branch in node["anyOf"] if branch.get("type") == kind)


# ---- the schema ----------------------------------------------------------


def test_draft_tool_schema_uses_only_provider_safe_keywords(home: Path):
    input_schema = _draft_spec(home).input_schema

    assert _off_allowlist(input_schema, "input_schema") == []
    dumped = json.dumps(input_schema)
    for key in ("$ref", "$defs", "$schema", "$id"):
        assert f'"{key}"' not in dumped


def test_draft_pack_schema_requires_every_top_level_required_field(home: Path):
    source = _source_schema()
    pack_schema = _pack_schema(home)

    assert pack_schema["type"] == "object"
    assert set(source["required"]) <= set(pack_schema["required"])
    assert set(pack_schema["properties"]) == set(source["properties"])


def test_draft_pack_schema_follows_the_pack_schema_file(home: Path):
    source = _source_schema()
    pack_schema = _pack_schema(home)

    assert pack_schema["properties"]["timeframe"]["enum"] == source["properties"]["timeframe"]["enum"]
    venues = pack_schema["properties"]["markets"]["items"]["properties"]["venue"]["enum"]
    assert venues == source["$defs"]["market"]["properties"]["venue"]["enum"]
    stop = pack_schema["properties"]["risk"]["properties"]["stop"]
    assert [branch["required"] for branch in stop["anyOf"]] == [["type", "pct"], ["type", "len", "mult"]]
    assert pack_schema["properties"]["schema_version"] == {"enum": [1]}


def test_draft_pack_schema_states_dropped_rules_in_descriptions(home: Path):
    pack_schema = _pack_schema(home)
    properties = pack_schema["properties"]

    assert PACK_ID_PATTERN in properties["id"]["description"]
    indicators = properties["indicators"]["description"]
    for word in ("fn", "src", "len", "sma", "atr", "power_mean"):
        assert word in indicators
    leaf = _branch(properties["entry"], "array")
    assert "[left, op, right]" in leaf["description"]
    assert leaf["minItems"] == leaf["maxItems"] == 3
    pct = properties["risk"]["properties"]["stop"]["anyOf"][0]["properties"]["pct"]
    assert "above 0" in pct["description"].lower()


@pytest.mark.parametrize("pack", _starter_packs(), ids=lambda pack: pack["id"])
def test_draft_pack_schema_accepts_every_shipped_starter_pack(home: Path, pack: dict):
    assert lint.check(pack) == []
    jsonschema.Draft202012Validator(_pack_schema(home)).validate(pack)


def test_condition_groups_are_typed_two_levels_deep_then_cut_off(home: Path):
    entry = _pack_schema(home)["properties"]["entry"]

    first = _branch(entry, "object")
    second = _branch(first["properties"]["all"]["items"], "object")
    innermost = second["properties"]["any"]["items"]
    cutoff = _branch(innermost, "object")
    assert set(cutoff) == {"type", "description"}
    assert _branch(innermost, "array")["minItems"] == 3

    # Two group levels are the deepest the validator accepts.
    leaf = ["close", ">", "sma20"]
    two_levels = _pack(entry={"all": [{"any": [leaf, leaf]}, leaf]})
    three_levels = _pack(entry={"all": [{"any": [{"all": [leaf]}]}]})
    assert lint.check(two_levels) == []
    assert any("nesting depth" in error["message"] for error in lint.check(three_levels))
    jsonschema.Draft202012Validator(_pack_schema(home)).validate(two_levels)


# ---- the example -----------------------------------------------------------


def test_draft_description_says_it_validates_and_never_trades(home: Path):
    description = _draft_spec(home).description

    assert "validate" in description
    assert "never trades" in description


def test_draft_description_example_is_a_runnable_pack(home: Path):
    example = _example_pack(home)

    assert lint.check(example) == []
    jsonschema.Draft202012Validator(_pack_schema(home)).validate(example)
    result = _draft(home, example)
    assert result["runnable"] is True
    assert result["errors"] == []
    assert result["state"] == "validated"


# ---- missing ids -----------------------------------------------------------


def test_missing_id_is_made_from_the_label(home: Path):
    result = _draft(home, _pack(id=None, label="Café Breakout: BTC/USD!!"))

    assert result["strategy_id"] == "cafe-breakout-btc-usd"
    assert result["pack"]["id"] == "cafe-breakout-btc-usd"
    assert result["runnable"] is True


def test_empty_id_is_made_from_the_label(home: Path):
    result = _draft(home, _pack(id="", label="Dip buyer"))

    assert result["strategy_id"] == "dip-buyer"


def test_generated_id_takes_the_next_free_suffix(home: Path):
    first = _draft(home, _pack(id=None, label="Trend rider"))
    second = _draft(home, _pack(id=None, label="Trend rider", version="1.0.1"))
    third = _draft(home, _pack(id=None, label="Trend rider", version="1.0.2"))

    assert [first["strategy_id"], second["strategy_id"], third["strategy_id"]] == [
        "trend-rider",
        "trend-rider-2",
        "trend-rider-3",
    ]


def test_generated_id_fits_the_pack_id_pattern(home: Path):
    label = "A very long strategy label that keeps on going well past the limit"
    first = _draft(home, _pack(id=None, label=label))
    second = _draft(home, _pack(id=None, label=label, version="1.0.1"))

    for result in (first, second):
        assert re.fullmatch(PACK_ID_PATTERN, result["strategy_id"]), result["strategy_id"]
        assert result["runnable"] is True
    assert second["strategy_id"].endswith("-2")
    assert first["strategy_id"] == "a-very-long-strategy-label-that"


def test_label_without_letters_or_digits_falls_back_to_strategy(home: Path):
    result = _draft(home, _pack(id=None, label="!!!"))

    assert result["strategy_id"] == "strategy"
    assert result["runnable"] is True


def test_edit_without_id_keeps_the_parent_strategy(home: Path):
    parent = _draft(home, _pack(id="kept-strategy"))

    edited = _draft(home, _pack(id=None, label="Renamed"), parent_revision_id=parent["revision_id"])

    assert edited["outcome"] == "created"
    assert edited["strategy_id"] == "kept-strategy"
    assert edited["parent_revision_id"] == parent["revision_id"]
    assert edited["pack"]["label"] == "Renamed"


def test_direct_service_callers_still_need_an_id(home: Path):
    with pytest.raises(ValueError, match="pack must have a non-empty id"):
        StrategyDraftService(home).create(_pack(id=None))


# ---- one call saves and validates -------------------------------------------


def test_malformed_pack_returns_the_validators_errors_in_one_call(home: Path):
    malformed = _pack(
        indicators={"sma20": {"src": "close", "len": 20}},
        risk={"max_account_pct": 25, "stop": {"type": "pct"}},
    )

    drafted = _draft(home, malformed)

    assert drafted["runnable"] is False
    assert drafted["state"] == "draft"
    assert drafted["errors"]
    validated = AgentToolCore(home).call(KRELL, "validate", {"revision_id": drafted["revision_id"]})
    assert drafted["errors"] == validated["result"]["errors"]
    assert drafted["runnable"] is validated["result"]["runnable"]


def test_redrafting_a_deployed_revision_keeps_it_deployed(home: Path):
    service = StrategyDraftService(home)
    revision_id = service.create(_pack())["revision_id"]
    service.validate(revision_id)
    service.mark_deployed(revision_id)

    again = _draft(home, _pack())

    assert again["outcome"] == "existing"
    assert again["state"] == "deployed"
    assert again["runnable"] is True


def test_mcp_clients_get_the_same_draft_tool(home: Path):
    mcp = Actor.mcp("cursor")
    assert _draft_spec(home, mcp).input_schema == _draft_spec(home).input_schema
    assert _draft_spec(home, mcp).description == _draft_spec(home).description

    result = McpTools(home, actor=mcp).call("draft", pack=_pack(id=None, label="From an MCP client"))

    assert result["ok"] is True
    assert result["result"]["strategy_id"] == "from-an-mcp-client"
    assert result["result"]["runnable"] is True
