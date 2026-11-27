"""Agent tool core: actors, result labels and the ``agent_action`` audit trail.

Every agent tool call goes through one core with an actor (``krell`` or
``mcp:<client>``). Each call appends exactly one ``kind=agent_action`` journal
record, secret-shaped params are refused before any handler runs, long values
are truncated in the record, and every result carries ``actor`` and
``labels``. Hard live/key refusals keep their typed MCP codes.
"""

from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from krellbot.agent.actors import KRELL, Actor
from krellbot.agent.audit import is_secret_param_name, read_agent_activity
from krellbot.agent.core import LABEL_NOT_ADVICE, LABEL_PAPER, LABEL_SIMULATED, AgentToolCore, ToolSpec
from krellbot.journal import browse, read_records_lenient, summarize_record
from krellbot.mcp import ALLOWED_TOOLS, CODE_KEY_READ_REFUSED, CODE_LIVE_ARM_REFUSED, McpTools

_RESULT_KEYS = {"schema_version", "ok", "effect", "tool", "actor", "labels"}


def _agent_rows(home: Path) -> list[dict]:
    return [r for r in read_records_lenient(home) if r.get("kind") == "agent_action"]


def _journal_text(home: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted((home / "journal").glob("*.jsonl")))


def _valid_pack_dict() -> dict:
    return {
        "schema_version": 1,
        "id": "trend-follow",
        "version": "1.0.0",
        "label": "Trend follow",
        "author": "krellbot agent core tests",
        "timeframe": "1h",
        "indicators": {"sma20": {"fn": "sma", "src": "close", "len": 20}},
        "entry": ["close", ">", "sma20"],
        "exit": ["close", "<", "sma20"],
        "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": "kraken", "pair": "SUIUSD"}],
    }


def test_mcp_call_appends_one_agent_action_record_per_call(home: Path):
    mcp = McpTools(home, clock=lambda: 1_700_000_100)

    result = mcp.call("validate", revision_id="nope")
    assert result["ok"] is False

    rows = _agent_rows(home)
    assert len(rows) == 1
    row = rows[0]
    assert row["ts"] == 1_700_000_100
    assert (row["venue"], row["pack"], row["bar_ts"]) == ("-", "-", 0)
    assert row["detail"] == {
        "actor": "mcp:local",
        "tool": "validate",
        "params": {"revision_id": "nope"},
        "outcome": "refused",
        "code": "mcp_invalid_request",
        "confirm_ticket": None,
        "tokens": None,
    }

    snapshot = mcp.call("read")
    assert snapshot["ok"] is True
    rows = _agent_rows(home)
    assert len(rows) == 2
    assert (rows[1]["detail"]["tool"], rows[1]["detail"]["outcome"]) == ("read", "ok")

    journal_file = home / "journal" / "2023-11.jsonl"
    assert journal_file.is_file()
    if sys.platform != "win32":
        assert journal_file.stat().st_mode & 0o777 == 0o600


def test_live_and_key_tool_names_are_refused_and_audited(home: Path, monkeypatch: pytest.MonkeyPatch):
    secrets_get = MagicMock(side_effect=AssertionError("secrets.get must not be called"))
    monkeypatch.setattr("krellbot.secrets.get", secrets_get, raising=False)

    import keyring

    get_password = MagicMock(side_effect=AssertionError("keyring.get_password must not be called"))
    monkeypatch.setattr(keyring, "get_password", get_password)

    mcp = McpTools(home)
    live = mcp.call("arm_live", venue="kraken")
    keys = mcp.call("read_keys")
    krell_live = AgentToolCore(home).call(KRELL, "live.arm", {})

    assert (live["ok"], live["effect"], live["code"]) == (False, "refused", CODE_LIVE_ARM_REFUSED)
    assert (keys["ok"], keys["effect"], keys["code"]) == (False, "refused", CODE_KEY_READ_REFUSED)
    assert krell_live["code"] == CODE_LIVE_ARM_REFUSED
    assert krell_live["actor"] == "krell"

    audited = [
        (r["detail"]["actor"], r["detail"]["tool"], r["detail"]["outcome"], r["detail"]["code"])
        for r in _agent_rows(home)
    ]
    assert audited == [
        ("mcp:local", "arm_live", "refused", CODE_LIVE_ARM_REFUSED),
        ("mcp:local", "read_keys", "refused", CODE_KEY_READ_REFUSED),
        ("krell", "live.arm", "refused", CODE_LIVE_ARM_REFUSED),
    ]
    assert _agent_rows(home)[0]["venue"] == "kraken"
    secrets_get.assert_not_called()
    get_password.assert_not_called()


def test_secret_shaped_params_are_refused_before_dispatch(home: Path):
    secret_value = "sk-fake-secret-value-0123456789"
    mcp = McpTools(home)

    result = mcp.call("draft", pack=_valid_pack_dict(), api_key=secret_value)
    assert result["ok"] is False
    assert result["effect"] == "refused"
    assert result["code"] == "agent_secret_param_refused"
    assert result["tool"] == "draft"
    assert secret_value not in json.dumps(result)
    # The draft handler never ran.
    assert not (home / "drafts").exists()

    # Lowercased, substring markers: any of the six marker words refuses.
    for name in ("Authorization", "session_token", "db_password", "credential_id", "client_secret", "apiKey"):
        refused = mcp.call("validate", revision_id="nope", **{name: secret_value})
        assert refused["code"] == "agent_secret_param_refused", name

    rows = _agent_rows(home)
    assert len(rows) == 7
    first = rows[0]["detail"]
    assert first["outcome"] == "refused"
    assert first["code"] == "agent_secret_param_refused"
    assert first["params"]["api_key"] == "***"
    assert rows[1]["detail"]["params"] == {"revision_id": "nope", "Authorization": "***"}
    assert secret_value not in _journal_text(home)


def test_existing_verb_params_are_not_secret_shaped():
    legitimate = (
        "pack",
        "parent_revision_id",
        "revision_id",
        "pack_path",
        "dataset_csv",
        "venue",
        "pair",
        "timeframe",
        "allow_gaps",
        "mode",
        "paper_balance",
        "correlation_id",
    )
    assert [name for name in legitimate if is_secret_param_name(name)] == []


def test_long_param_values_are_truncated_in_the_audit_record(home: Path):
    mcp = McpTools(home)
    big_pack = _valid_pack_dict()
    big_pack["label"] = "L" * 5000

    mcp.call("read", note="x" * 5000)
    mcp.call("draft", pack=big_pack)

    rows = _agent_rows(home)
    stored = rows[0]["detail"]["params"]["note"]
    assert len(stored) <= 220
    assert stored.startswith("x" * 200)
    assert stored.endswith("…(+4800)")

    stored_pack = rows[1]["detail"]["params"]["pack"]
    assert len(json.dumps(stored_pack, ensure_ascii=False)) <= 240
    assert "L" * 1000 not in _journal_text(home)


def test_results_carry_actor_and_labels(home: Path):
    mcp = McpTools(home, actor=Actor.mcp("Cursor"))

    backtest = mcp.call("backtest")
    assert backtest["ok"] is False
    assert backtest["code"] == "mcp_invalid_request"
    assert backtest["labels"] == [LABEL_SIMULATED]
    assert backtest["actor"] == "mcp:cursor"

    snapshot = mcp.call("read")
    assert snapshot["ok"] is True
    assert snapshot["labels"] == []
    assert snapshot["actor"] == "mcp:cursor"

    arm = mcp.call("paper_arm", venue="kraken")
    assert arm["ok"] is False
    assert arm["labels"] == [LABEL_PAPER]

    unknown = mcp.call("close_position")
    assert unknown["code"] == "mcp_unknown_tool"
    assert unknown["labels"] == []

    krell_read = AgentToolCore(home).call(KRELL, "read", {})
    assert krell_read["ok"] is True
    assert krell_read["actor"] == "krell"

    for result in (backtest, snapshot, arm, unknown, krell_read):
        assert _RESULT_KEYS <= set(result)
    assert (LABEL_SIMULATED, LABEL_NOT_ADVICE, LABEL_PAPER) == ("simulated", "not_advice", "paper")


def test_core_registers_the_five_verbs_with_effect_and_cost_classes(home: Path):
    core = AgentToolCore(home)
    specs = {spec.name: spec for spec in core.specs(Actor.mcp(None))}
    assert list(specs) == list(ALLOWED_TOOLS)
    assert [spec.name for spec in core.specs(KRELL)] == list(ALLOWED_TOOLS)
    classes = {name: (spec.effect, spec.cost, spec.labels) for name, spec in specs.items()}
    assert classes == {
        "read": ("read", "light", ()),
        "draft": ("research", "light", ()),
        "validate": ("research", "light", ()),
        "backtest": ("research", "heavy", ("simulated",)),
        "paper_arm": ("change", "heavy", ("paper",)),
    }
    for spec in specs.values():
        assert spec.input_schema["type"] == "object"
        assert spec.description


def test_tools_are_scoped_to_their_actors_and_handler_errors_never_raise(home: Path):
    core = AgentToolCore(home, clock=lambda: 1_700_000_300)

    def boom(_ctx, _params):
        raise RuntimeError("handler exploded with /private/path detail")

    core.register(
        ToolSpec(
            name="krell_only_probe",
            effect="read",
            cost="light",
            input_schema={"type": "object", "properties": {}},
            labels=(),
            description="Test-only tool that always fails.",
            handler=boom,
            actors=frozenset({"krell"}),
        )
    )
    assert "krell_only_probe" in [spec.name for spec in core.specs(KRELL)]
    assert "krell_only_probe" not in [spec.name for spec in core.specs(Actor.mcp("cursor"))]

    hidden = core.call(Actor.mcp("cursor"), "krell_only_probe", {})
    assert hidden["code"] == "mcp_unknown_tool"

    failed = core.call(KRELL, "krell_only_probe", {})
    assert failed["ok"] is False
    assert failed["effect"] == "error"
    assert failed["code"] == "agent_tool_error"
    assert "/private/path" not in failed["message"]
    assert _RESULT_KEYS <= set(failed)

    bad_params = core.call(KRELL, "read", ["not", "an", "object"])  # type: ignore[arg-type]
    assert bad_params["code"] == "mcp_invalid_request"

    outcomes = [(r["detail"]["actor"], r["detail"]["outcome"], r["detail"]["code"]) for r in _agent_rows(home)]
    assert outcomes == [
        ("mcp:cursor", "refused", "mcp_unknown_tool"),
        ("krell", "error", "agent_tool_error"),
        ("krell", "refused", "mcp_invalid_request"),
    ]

    with pytest.raises(ValueError):
        core.register(
            ToolSpec(
                name="read_keys",
                effect="read",
                cost="light",
                input_schema={"type": "object"},
                labels=(),
                description="A key-shaped tool name can never be registered.",
                handler=boom,
            )
        )


def test_actor_mcp_sanitizes_client_names():
    assert Actor.mcp("Claude Desktop!").label == "mcp:claudedesktop"
    assert Actor.mcp(None).label == "mcp:local"
    assert Actor.mcp("").label == "mcp:local"
    assert Actor.mcp("!!!").label == "mcp:local"
    assert Actor.mcp("Cursor-1.2_beta").label == "mcp:cursor-1.2_beta"

    long_name = Actor.mcp("A" * 100)
    assert long_name.client == "a" * 40
    assert long_name.label == "mcp:" + "a" * 40

    assert KRELL == Actor("krell")
    assert KRELL.label == "krell"
    with pytest.raises(FrozenInstanceError):
        KRELL.kind = "mcp"  # type: ignore[misc]


def test_journal_summary_reads_agent_actions(home: Path):
    McpTools(home, clock=lambda: 1_700_000_100).call("validate", revision_id="nope")
    McpTools(home, clock=lambda: 1_700_000_101).call("read")
    rows = _agent_rows(home)

    assert summarize_record(rows[0]) == "mcp:local validate: refused (mcp_invalid_request)"
    assert summarize_record(rows[1]) == "mcp:local read: ok"

    browsed = browse(home, tail=2, now=1_700_000_200)
    assert [r["summary"] for r in browsed] == [
        "mcp:local validate: refused (mcp_invalid_request)",
        "mcp:local read: ok",
    ]


def test_agent_activity_reads_newest_first_with_filters(home: Path):
    McpTools(home, clock=lambda: 1_700_000_100).call("validate", revision_id="nope")
    McpTools(home, clock=lambda: 1_700_000_200, actor=Actor.mcp("cursor")).call("read")
    AgentToolCore(home, clock=lambda: 1_700_000_300).call(KRELL, "arm_live", {})

    every = read_agent_activity(home)
    assert [r["ts"] for r in every] == [1_700_000_300, 1_700_000_200, 1_700_000_100]

    assert [r["detail"]["tool"] for r in read_agent_activity(home, actor="mcp:cursor")] == ["read"]
    assert [r["detail"]["actor"] for r in read_agent_activity(home, outcome="refused")] == ["krell", "mcp:local"]
    assert [r["detail"]["tool"] for r in read_agent_activity(home, tool="validate")] == ["validate"]
    assert [r["ts"] for r in read_agent_activity(home, since_ts=1_700_000_200)] == [1_700_000_300, 1_700_000_200]
    assert [r["ts"] for r in read_agent_activity(home, limit=1)] == [1_700_000_300]
