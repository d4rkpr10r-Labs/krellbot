"""Operator policy v2: hard limits on what agents may paper-arm or change.

``user-policy.json`` schema "2" adds an ``agents`` section. A v1 file or no
file loads the safe defaults; a malformed file locks agents out. The agent
tool core enforces the limits for Krell and external MCP agents alike, and
journals every limit refusal as a ``user_policy`` refused row. Agent arms are
counted from ``agent-arms.json`` against the live config, so human arms never
count and disarmed or re-armed pairs drop out. After an arm, the stored
record is checked again and counted under a lock; anything that fails then
disarms the arm, or says plainly that it may still be running.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from itertools import count
from pathlib import Path

import pytest

from krellbot import config as kb_config
from krellbot import run as kb_run
from krellbot.agent import arm_registry
from krellbot.agent.actors import KRELL, Actor
from krellbot.agent.core import MAX_PACK_BYTES, MESSAGE_PACK_UNREADABLE, AgentToolCore, ToolSpec
from krellbot.api.app import CSRF_HEADER, SESSION_COOKIE
from krellbot.application import paper as paper_app
from krellbot.application import user_policy
from krellbot.application.paper import PaperService
from krellbot.application.strategy import StrategyDraftService
from krellbot.journal import read_records_lenient
from krellbot.mcp import McpTools
from krellbot.mcp.errors import (
    CODE_AGENT_ARM_CLEANUP_FAILED,
    CODE_AGENT_ARM_UNVERIFIED,
    CODE_AGENT_CHANGE_NOT_ALLOWED,
    CODE_AGENT_LIMIT_BALANCE,
    CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED,
    CODE_AGENT_LIMIT_MAX_ARMS,
    CODE_AGENT_LIMIT_PAIR,
    CODE_AGENT_LIMIT_RISK_CAP,
    CODE_AGENT_LIMIT_VENUE,
    CODE_INVALID_REQUEST,
)
from tests.test_ns06_api import TEST_PORT, _build_app, _do_bootstrap, asgi_call

pytestmark = pytest.mark.usefixtures("fresh_keyring")

MCP = Actor.mcp("Cursor")
CHANGE_TOOLS = ("pause_entries", "resume_entries", "disarm")


@pytest.fixture(autouse=True)
def _stepping_arm_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every arm its own ``armed_at_ts`` (run.arm_pack stamps whole seconds)."""
    ticks = count(1_700_000_000, 10)
    monkeypatch.setattr("krellbot.run._now_seconds", lambda: float(next(ticks)))


def _pack(home: Path, *, pair: str = "SUIUSD", cap: float = 2, pack_id: str | None = None) -> Path:
    work = home / "agent-work"
    work.mkdir(parents=True, exist_ok=True)
    pack_id = pack_id or f"agent-{pair.lower()}"
    body = {
        "schema_version": 1,
        "id": pack_id,
        "version": "1.0.0",
        "label": "Agent limits",
        "author": "krellbot agent limits tests",
        "timeframe": "1h",
        "indicators": {"sma20": {"fn": "sma", "src": "close", "len": 20}},
        "entry": ["close", ">", "sma20"],
        "exit": ["close", "<", "sma20"],
        "risk": {"max_account_pct": cap, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": "kraken", "pair": pair}],
    }
    target = work / f"{pack_id}.json"
    target.write_text(json.dumps(body), encoding="utf-8")
    return target


def _agent_arm(
    home: Path,
    pack: Path,
    *,
    actor: Actor = MCP,
    balance: object = "1000",
    loss: object = "5",
) -> dict:
    params: dict = {"pack_path": str(pack), "venue": "kraken", "mode": "paper", "paper_balance": balance}
    if loss is not None:
        params["daily_loss_limit_pct"] = loss
    return AgentToolCore(home).call(actor, "paper_arm", params)


def _human_arm(home: Path, pack: Path) -> None:
    result = PaperService(home).arm(pack, venue="kraken", mode="paper", paper_balance=Decimal(1000))
    assert result.ok is True, result


def _write_policy(home: Path, payload: object) -> None:
    user_policy.policy_path(home).write_text(json.dumps(payload), encoding="utf-8")


def _v2(**agents: object) -> dict:
    section = user_policy.DEFAULT_AGENT_LIMITS.to_dict()
    section.update(agents)
    return {
        "schema_version": "2",
        "allow_paper_arm": True,
        "allow_live": True,
        "allow_key_export": False,
        "agents": section,
    }


def _refused_rows(home: Path) -> list[dict]:
    return [
        r["detail"]
        for r in read_records_lenient(home)
        if r.get("kind") == "user_policy"
        and isinstance(r.get("detail"), dict)
        and r["detail"].get("event") == "refused"
    ]


def _armed(home: Path) -> list[kb_config.ArmedPack]:
    return kb_config.load_config(home).armed


def _assert_limit_refusal(result: dict, code: str, *, actor: str = "mcp:cursor") -> None:
    assert result["ok"] is False
    assert result["effect"] == "refused"
    assert result["code"] == code
    assert result["tool"] == "paper_arm"
    assert result["actor"] == actor
    assert result["labels"] == ["paper"]


# ---- the policy file -----------------------------------------------------


def test_v1_policy_file_loads_with_default_agent_limits(home: Path):
    _write_policy(
        home,
        {"schema_version": "1", "allow_paper_arm": True, "allow_live": False, "allow_key_export": False},
    )
    policy = user_policy.load_policy(home)
    assert policy.agents == user_policy.DEFAULT_AGENT_LIMITS
    assert policy.allow_live is False
    assert user_policy.check(home, user_policy.ACTION_PAPER_ARM).ok is True

    defaults = user_policy.DEFAULT_AGENT_LIMITS
    assert defaults.allowed_venues is None
    assert defaults.allowed_pairs is None
    assert defaults.max_paper_balance == Decimal(10000)
    assert defaults.max_risk_cap_pct == Decimal(2)
    assert defaults.require_daily_loss_limit is True
    assert defaults.max_agent_arms == 3
    assert (defaults.krell_allow_pause_resume, defaults.krell_allow_disarm) == (True, True)
    assert (defaults.mcp_allow_pause_resume, defaults.mcp_allow_disarm) == (False, False)
    assert defaults.token_budget_per_conversation == 250_000
    assert defaults.token_budget_per_month == 2_500_000
    assert defaults.to_dict()["max_paper_balance"] == "10000"
    assert defaults.to_dict()["allowed_venues"] is None

    # No file at all keeps the same safe defaults.
    user_policy.policy_path(home).unlink()
    assert user_policy.load_policy(home).agents == user_policy.DEFAULT_AGENT_LIMITS
    assert user_policy.load_policy(home).to_dict()["schema_version"] == "2"


def test_malformed_agents_section_locks_agents_and_reports_invalid(home: Path):
    _write_policy(home, _v2(max_agent_arms="3"))
    decision = user_policy.check(home, user_policy.ACTION_PAPER_ARM)
    assert decision.ok is False
    assert decision.code == user_policy.CODE_POLICY_INVALID
    policy = user_policy.load_policy(home)
    assert policy.agents == user_policy.LOCKED_AGENT_LIMITS
    assert policy.allow_paper_arm is False

    locked = user_policy.LOCKED_AGENT_LIMITS
    assert locked.allowed_venues == ()
    assert locked.max_agent_arms == 0
    assert not any(
        (
            locked.krell_allow_pause_resume,
            locked.krell_allow_disarm,
            locked.mcp_allow_pause_resume,
            locked.mcp_allow_disarm,
        )
    )
    assert (locked.token_budget_per_conversation, locked.token_budget_per_month) == (0, 0)

    missing_key = _v2()
    del missing_key["agents"]["mcp_allow_disarm"]
    v1_with_agents = _v2()
    v1_with_agents["schema_version"] = "1"
    unknown_top_level = _v2()
    unknown_top_level["allow_agent_close"] = True
    malformed = (
        missing_key,
        _v2(allow_agent_close=False),
        _v2(max_paper_balance=10000),
        _v2(max_paper_balance="1e4"),
        _v2(max_risk_cap_pct="NaN"),
        _v2(max_risk_cap_pct="-1"),
        _v2(max_risk_cap_pct="101"),
        _v2(allowed_venues="kraken"),
        _v2(allowed_pairs=["SUIUSD", 7]),
        _v2(require_daily_loss_limit="true"),
        _v2(token_budget_per_month=True),
        _v2(max_agent_arms=-1),
        v1_with_agents,
        unknown_top_level,
        {**_v2(), "agents": None},
    )
    for payload in malformed:
        _write_policy(home, payload)
        assert user_policy.load_policy(home).agents == user_policy.LOCKED_AGENT_LIMITS, payload
        assert user_policy.check(home, user_policy.ACTION_PAPER_ARM).code == user_policy.CODE_POLICY_INVALID

    # Fail closed: an agent arm is refused by the existing paper-arm policy
    # check, exactly as before (one refuse row, no limit row) and nothing is armed.
    result = _agent_arm(home, _pack(home))
    assert result["ok"] is False
    assert result["code"] == user_policy.CODE_POLICY_DENIED
    assert result["message"] == user_policy.MESSAGE_INVALID
    assert _armed(home) == []
    assert [r["code"] for r in _refused_rows(home)] == [user_policy.CODE_POLICY_INVALID]


def test_policy_put_round_trips_v2_and_old_v1_put_still_works(home: Path):
    app = _build_app(home)
    _decoded, csrf = _do_bootstrap(app, token="boot-test-token")
    session = next(iter(app.state.krellbot.sessions))
    headers = [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
        (CSRF_HEADER, csrf),
        ("Cookie", f"{SESSION_COOKIE}={session}"),
        ("Content-Type", "application/json"),
    ]

    def put(body: dict) -> tuple[int, dict]:
        status, _h, raw, _c = asgi_call(
            app, method="PUT", path="/api/v1/user-policy", body=json.dumps(body).encode(), headers=headers
        )
        return status, json.loads(raw)

    v2 = _v2(
        allowed_venues=["kraken"],
        allowed_pairs=["SUIUSD", "BTCUSD"],
        max_paper_balance="2500.50",
        max_risk_cap_pct="1.5",
        max_agent_arms=1,
        mcp_allow_disarm=True,
        token_budget_per_month=100_000,
    )
    status, out = put({"policy": v2})
    assert status == 200, out
    assert out["code"] == user_policy.CODE_POLICY_UPDATED
    assert out["policy"] == v2

    status, _h, raw, _c = asgi_call(app, method="GET", path="/api/v1/user-policy", headers=headers)
    assert status == 200
    assert json.loads(raw)["policy"] == v2
    assert json.loads(user_policy.policy_path(home).read_text(encoding="utf-8")) == v2
    agents = user_policy.load_policy(home).agents
    assert agents.max_paper_balance == Decimal("2500.50")
    assert agents.allowed_pairs == ("SUIUSD", "BTCUSD")

    # An old v1 client still saves. The file is written as v2, and the
    # operator's agent limits stay as they were (a v1 writer knows nothing
    # about them, so it must not reset them).
    v1 = {"schema_version": "1", "allow_paper_arm": True, "allow_live": False, "allow_key_export": False}
    status, out = put(v1)
    assert status == 200, out
    assert out["policy"]["schema_version"] == "2"
    assert out["policy"]["allow_live"] is False
    assert out["policy"]["agents"] == v2["agents"]
    on_disk = json.loads(user_policy.policy_path(home).read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == "2"
    assert on_disk["agents"] == v2["agents"]

    # A malformed v2 body is refused and the file is unchanged.
    before = user_policy.policy_path(home).read_bytes()
    status, out = put(_v2(max_agent_arms="3"))
    assert status == 400
    assert out["code"] == user_policy.CODE_BAD_INPUT
    assert user_policy.policy_path(home).read_bytes() == before


def test_agent_limit_decimals_round_trip_without_exponents(home: Path):
    payload = _v2(max_paper_balance="10000.50", max_risk_cap_pct="0.0000001")
    assert user_policy.save_policy(home, payload).ok is True
    assert json.loads(user_policy.policy_path(home).read_text(encoding="utf-8")) == payload
    loaded = user_policy.load_policy(home)
    assert user_policy.check(home, user_policy.ACTION_PAPER_ARM).ok is True
    assert loaded.agents.max_paper_balance == Decimal("10000.50")
    assert loaded.agents.max_risk_cap_pct == Decimal("0.0000001")
    assert loaded.to_dict() == payload

    # Saving the loaded policy back is a byte-identical no-op.
    before = user_policy.policy_path(home).read_bytes()
    assert user_policy.save_policy(home, loaded).code == user_policy.CODE_OK
    assert user_policy.policy_path(home).read_bytes() == before

    # Refusal messages show the operator's value the same plain way.
    refused = _agent_arm(home, _pack(home))
    assert refused["code"] == CODE_AGENT_LIMIT_RISK_CAP
    assert "0.0000001%" in refused["message"]


# ---- paper-arm limits ----------------------------------------------------


def test_a_policy_flip_between_reads_cannot_skip_the_agent_limits(home: Path, monkeypatch: pytest.MonkeyPatch):
    deny = {"schema_version": "1", "allow_paper_arm": False, "allow_live": False, "allow_key_export": False}
    _write_policy(home, deny)
    real_load = user_policy._load
    flipped: list[bool] = []

    def read_then_operator_reenables(policy_home: Path):
        loaded = real_load(policy_home)
        if not flipped:
            flipped.append(True)
            _write_policy(home, {**deny, "allow_paper_arm": True})
        return loaded

    monkeypatch.setattr(user_policy, "_load", read_then_operator_reenables)
    # 25% cap and no daily loss limit: any agent-limit check would refuse it.
    result = _agent_arm(home, _pack(home, cap=25), loss=None)
    assert result["ok"] is False
    assert result["code"] == user_policy.CODE_POLICY_DENIED
    assert result["action"] == user_policy.ACTION_PAPER_ARM
    assert result["message"] == user_policy.MESSAGE_DENY_PAPER_ARM
    assert not kb_config.config_path(home).exists()
    assert [r["code"] for r in _refused_rows(home)] == [user_policy.CODE_POLICY_DENIED]


def test_agent_paper_arm_over_balance_is_refused_and_journaled(home: Path):
    result = _agent_arm(home, _pack(home), balance="20000")
    _assert_limit_refusal(result, CODE_AGENT_LIMIT_BALANCE)
    assert _armed(home) == []

    rows = _refused_rows(home)
    assert len(rows) == 1
    assert rows[0]["code"] == CODE_AGENT_LIMIT_BALANCE
    assert rows[0]["reason"] == CODE_AGENT_LIMIT_BALANCE
    assert rows[0]["action"] == "paper_arm"
    assert rows[0]["actor"] == "mcp:cursor"
    audit = [r for r in read_records_lenient(home) if r.get("kind") == "agent_action"]
    assert audit[-1]["detail"]["code"] == CODE_AGENT_LIMIT_BALANCE

    # A balance at the limit is allowed.
    assert _agent_arm(home, _pack(home), balance="10000")["ok"] is True


def test_agent_paper_arm_without_daily_loss_limit_is_refused(home: Path):
    pack = _pack(home)
    result = _agent_arm(home, pack, loss=None)
    _assert_limit_refusal(result, CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED)
    krell = _agent_arm(home, pack, actor=KRELL, loss=None)
    _assert_limit_refusal(krell, CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED, actor="krell")
    assert _armed(home) == []
    assert [r["code"] for r in _refused_rows(home)] == [CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED] * 2
    assert [r["actor"] for r in _refused_rows(home)] == ["mcp:cursor", "krell"]

    # When the operator turns the requirement off, the arm goes through without a limit.
    _write_policy(home, _v2(require_daily_loss_limit=False))
    assert _agent_arm(home, pack, loss=None)["ok"] is True
    assert _armed(home)[0].daily_loss_limit_pct is None


def test_agent_paper_arm_risk_cap_above_two_percent_is_refused(home: Path):
    result = _agent_arm(home, _pack(home, cap=2.5))
    _assert_limit_refusal(result, CODE_AGENT_LIMIT_RISK_CAP)
    assert _armed(home) == []

    # The cap comes from the pack being armed, also for a stored draft revision.
    mcp = McpTools(home, actor=MCP)
    draft_pack = json.loads(_pack(home, cap=25, pack_id="draft-wide").read_text(encoding="utf-8"))
    revision_id = mcp.call("draft", pack=draft_pack)["result"]["revision_id"]
    assert mcp.call("validate", revision_id=revision_id)["result"]["runnable"] is True
    by_revision = mcp.call(
        "paper_arm",
        revision_id=revision_id,
        venue="kraken",
        mode="paper",
        paper_balance="1000",
        daily_loss_limit_pct="5",
    )
    _assert_limit_refusal(by_revision, CODE_AGENT_LIMIT_RISK_CAP)
    assert _armed(home) == []
    assert [r["code"] for r in _refused_rows(home)] == [CODE_AGENT_LIMIT_RISK_CAP] * 2

    # A cap at the limit is allowed and is the cap on the armed record.
    assert _agent_arm(home, _pack(home, cap=2))["ok"] is True
    assert _armed(home)[0].cap == Decimal(2)


def test_venue_and_pair_allowlists_bind_agent_arms(home: Path):
    _write_policy(home, _v2(allowed_venues=["coinbase"]))
    _assert_limit_refusal(_agent_arm(home, _pack(home)), CODE_AGENT_LIMIT_VENUE)

    _write_policy(home, _v2(allowed_venues=[]))
    _assert_limit_refusal(_agent_arm(home, _pack(home)), CODE_AGENT_LIMIT_VENUE)

    _write_policy(home, _v2(allowed_venues=["kraken"], allowed_pairs=["BTCUSD"]))
    _assert_limit_refusal(_agent_arm(home, _pack(home, pair="SUIUSD")), CODE_AGENT_LIMIT_PAIR)
    assert _armed(home) == []
    assert _agent_arm(home, _pack(home, pair="BTCUSD"))["ok"] is True

    codes = [r["code"] for r in _refused_rows(home)]
    assert codes == [CODE_AGENT_LIMIT_VENUE, CODE_AGENT_LIMIT_VENUE, CODE_AGENT_LIMIT_PAIR]


# ---- counting agent arms -------------------------------------------------


def test_fourth_running_agent_arm_is_refused_but_human_arms_do_not_count(home: Path):
    for pair in ("SUIUSD", "BTCUSD", "ETHUSD"):
        assert _agent_arm(home, _pack(home, pair=pair))["ok"] is True
    _human_arm(home, _pack(home, pair="SOLUSD", cap=25, pack_id="human-solusd"))
    _human_arm(home, _pack(home, pair="XRPUSD", cap=25, pack_id="human-xrpusd"))

    fourth = _agent_arm(home, _pack(home, pair="ADAUSD"))
    _assert_limit_refusal(fourth, CODE_AGENT_LIMIT_MAX_ARMS)
    by_krell = _agent_arm(home, _pack(home, pair="ADAUSD"), actor=KRELL)
    _assert_limit_refusal(by_krell, CODE_AGENT_LIMIT_MAX_ARMS, actor="krell")

    assert {a.pair for a in _armed(home)} == {"SUIUSD", "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"}
    running = arm_registry.running_agent_arms(home)
    assert sorted(r["pair"] for r in running) == ["BTCUSD", "ETHUSD", "SUIUSD"]
    assert {r["actor"] for r in running} == {"mcp:cursor"}
    assert [r["code"] for r in _refused_rows(home)] == [CODE_AGENT_LIMIT_MAX_ARMS] * 2


def test_disarmed_or_human_rearmed_pairs_drop_out_of_the_agent_arm_count(home: Path):
    for pair in ("SUIUSD", "BTCUSD", "ETHUSD"):
        assert _agent_arm(home, _pack(home, pair=pair))["ok"] is True

    # The human disarms one agent arm and re-arms the same pair themselves,
    # and disarms another outright.
    assert PaperService(home).disarm(venue="kraken", pair="SUIUSD").ok is True
    _human_arm(home, _pack(home, pair="SUIUSD", cap=25, pack_id="human-suiusd"))
    assert PaperService(home).disarm(venue="kraken", pair="BTCUSD").ok is True
    assert [r["pair"] for r in arm_registry.running_agent_arms(home)] == ["ETHUSD"]

    # The freed slots are usable again, and the registry drops stale entries.
    assert _agent_arm(home, _pack(home, pair="SOLUSD"))["ok"] is True
    assert _agent_arm(home, _pack(home, pair="BTCUSD"))["ok"] is True
    assert sorted(r["pair"] for r in arm_registry.running_agent_arms(home)) == ["BTCUSD", "ETHUSD", "SOLUSD"]
    stored = json.loads(arm_registry.registry_path(home).read_text(encoding="utf-8"))
    assert sorted(r["pair"] for r in stored["arms"]) == ["BTCUSD", "ETHUSD", "SOLUSD"]
    _assert_limit_refusal(_agent_arm(home, _pack(home, pair="ADAUSD")), CODE_AGENT_LIMIT_MAX_ARMS)


def test_unreadable_agent_arm_registry_refuses_agent_arms(home: Path):
    arm_registry.registry_path(home).write_text("{oops", encoding="utf-8")
    result = _agent_arm(home, _pack(home))
    _assert_limit_refusal(result, CODE_AGENT_LIMIT_MAX_ARMS)
    assert _armed(home) == []
    assert [r["code"] for r in _refused_rows(home)] == [CODE_AGENT_LIMIT_MAX_ARMS]


# ---- the daily-loss limit on agent arms ----------------------------------


def test_agent_arm_with_a_daily_loss_limit_sets_it_on_the_armed_record(home: Path):
    result = _agent_arm(home, _pack(home), loss="2.5")
    assert result["ok"] is True
    assert result["code"] == "armed"
    armed = kb_config.find_armed(kb_config.load_config(home), "kraken", "SUIUSD")
    assert armed is not None
    assert armed.daily_loss_limit_pct == Decimal("2.5")
    assert arm_registry.running_agent_arms(home) == [
        {
            "venue": "kraken",
            "pair": "SUIUSD",
            "actor": "mcp:cursor",
            "armed_at_ts": armed.armed_at_ts,
            "revision_id": None,
        }
    ]
    path = arm_registry.registry_path(home)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # Krell arming a validated draft revision records the revision.
    core = AgentToolCore(home)
    draft = core.call(KRELL, "draft", {"pack": json.loads(_pack(home, pair="BTCUSD").read_text(encoding="utf-8"))})
    revision_id = draft["result"]["revision_id"]
    assert core.call(KRELL, "validate", {"revision_id": revision_id})["ok"] is True
    armed_by_krell = core.call(
        KRELL,
        "paper_arm",
        {"revision_id": revision_id, "venue": "kraken", "paper_balance": 500, "daily_loss_limit_pct": 100},
    )
    assert armed_by_krell["ok"] is True, armed_by_krell
    btc = kb_config.find_armed(kb_config.load_config(home), "kraken", "BTCUSD")
    assert btc is not None
    assert btc.daily_loss_limit_pct == Decimal(100)
    krell_rows = [r for r in arm_registry.running_agent_arms(home) if r["actor"] == "krell"]
    assert krell_rows == [
        {
            "venue": "kraken",
            "pair": "BTCUSD",
            "actor": "krell",
            "armed_at_ts": btc.armed_at_ts,
            "revision_id": revision_id,
        }
    ]

    # A workstation arm passes no limit and writes no limit key at all.
    _human_arm(home, _pack(home, pair="SOLUSD", cap=25, pack_id="human-solusd"))
    stored = json.loads(kb_config.config_path(home).read_text(encoding="utf-8"))
    human = next(row for row in stored["armed"] if row["pair"] == "SOLUSD")
    assert "daily_loss_limit_pct" not in human


def test_invalid_daily_loss_limit_is_refused_before_any_write(home: Path):
    pack = _pack(home)
    for bad in ("0", "100.5", "abc", "-1", "1e1", " 5", True, [5]):
        result = _agent_arm(home, pack, loss=bad)
        assert result["ok"] is False, bad
        assert result["code"] == CODE_INVALID_REQUEST, bad
    assert not kb_config.config_path(home).exists()
    assert _refused_rows(home) == []

    refused = PaperService(home).arm(
        pack,
        venue="kraken",
        mode="paper",
        paper_balance=Decimal(1000),
        daily_loss_limit_pct=Decimal("NaN"),
    )
    assert refused.ok is False
    assert refused.code == paper_app.CODE_INVALID_LOSS_LIMIT
    assert refused.revision_before == refused.revision_after
    assert not kb_config.config_path(home).exists()


def test_failure_applying_the_daily_loss_limit_leaves_nothing_armed(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(paper_app, "_apply_loss_limit_to_new_arm", lambda *_a, **_k: False)
    result = _agent_arm(home, _pack(home))
    assert result["ok"] is False
    assert result["code"] == paper_app.CODE_LOSS_LIMIT_NOT_APPLIED
    # The rollback leaves the config as it was: here, no file at all.
    assert not kb_config.config_path(home).exists()
    assert arm_registry.running_agent_arms(home) == []

    # With other arms in place, only the new arm is rolled back.
    _human_arm(home, _pack(home, pair="SOLUSD", cap=25, pack_id="human-solusd"))
    before = kb_config.config_path(home).read_bytes()

    def write_fails(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(paper_app, "_apply_loss_limit_to_new_arm", write_fails)
    result = _agent_arm(home, _pack(home))
    assert result["code"] == paper_app.CODE_LOSS_LIMIT_NOT_APPLIED
    assert result["revision_before"] == result["revision_after"]
    assert kb_config.config_path(home).read_bytes() == before
    assert [a.pair for a in _armed(home)] == ["SOLUSD"]

    # Another paper command holding the config: refused before anything is written.
    @contextmanager
    def busy(_home, **_kwargs):
        yield False

    monkeypatch.setattr(kb_config, "_paper_command_transaction", busy)
    result = _agent_arm(home, _pack(home))
    assert result["code"] == paper_app.CODE_CONFIG_BUSY
    assert kb_config.config_path(home).read_bytes() == before
    assert arm_registry.running_agent_arms(home) == []


def test_rollback_of_a_failed_agent_arm_keeps_a_concurrent_human_arm(home: Path, monkeypatch: pytest.MonkeyPatch):
    def human_arms_meanwhile_then_the_limit_fails(*_args, **_kwargs):
        _human_arm(home, _pack(home, pair="SOLUSD", cap=25, pack_id="human-solusd"))
        return False

    monkeypatch.setattr(paper_app, "_apply_loss_limit_to_new_arm", human_arms_meanwhile_then_the_limit_fails)
    result = _agent_arm(home, _pack(home))
    assert result["code"] == paper_app.CODE_LOSS_LIMIT_NOT_APPLIED
    assert "rolled back" in result["message"]
    # Only the agent's own record (kraken SUIUSD) was removed.
    assert [a.pair for a in _armed(home)] == ["SOLUSD"]


# ---- after the arm: the stored record is the authority -------------------


def test_the_stored_arm_is_checked_against_the_limits_after_the_arm(home: Path, monkeypatch: pytest.MonkeyPatch):
    # The pre-check's parse reports a 1% cap, but the armed bytes carry 25%:
    # the stored record is checked again and wins.
    monkeypatch.setattr("krellbot.agent.core.arm_terms", lambda _raw, _venue: ("SUIUSD", Decimal(1)))
    result = _agent_arm(home, _pack(home, cap=25))
    _assert_limit_refusal(result, CODE_AGENT_LIMIT_RISK_CAP)
    assert "was disarmed" in result["message"]
    assert _armed(home) == []
    assert arm_registry.running_agent_arms(home) == []
    assert [r["code"] for r in _refused_rows(home)] == [CODE_AGENT_LIMIT_RISK_CAP]


def test_an_arm_whose_new_record_cannot_be_found_is_never_counted_or_disarmed(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_policy(home, _v2(require_daily_loss_limit=False))
    _human_arm(home, _pack(home, pair="SUIUSD", cap=25, pack_id="human-suiusd"))
    before = kb_config.config_path(home).read_bytes()

    # The arm reports success but wrote nothing; the record on the agent's
    # pair is the human's, from before the arm, so it is left alone and the
    # agent is told plainly that its arm could not be identified.
    monkeypatch.setattr("krellbot.run.arm_pack", lambda *_args, **_kwargs: 0)
    result = _agent_arm(home, _pack(home), loss=None)
    _assert_limit_refusal(result, CODE_AGENT_ARM_CLEANUP_FAILED)
    assert "could not be identified" in result["message"]
    assert "may still be running" in result["message"]
    assert kb_config.config_path(home).read_bytes() == before
    assert arm_registry.running_agent_arms(home) == []


def test_failure_after_an_agent_arm_disarms_it_or_says_it_may_still_run(home: Path, monkeypatch: pytest.MonkeyPatch):
    def registry_write_fails(*_args, **_kwargs):
        raise OSError("read-only home")

    monkeypatch.setattr(arm_registry, "claim_agent_arm", registry_write_fails)
    result = _agent_arm(home, _pack(home))
    _assert_limit_refusal(result, CODE_AGENT_ARM_UNVERIFIED)
    assert "was disarmed" in result["message"]
    assert _armed(home) == []
    assert arm_registry.running_agent_arms(home) == []

    # The disarm raises: the arm may still be running, and the refusal says
    # so, names it, and never claims it was disarmed.
    def disarm_raises(_self, **_kwargs):
        raise OSError("config is read-only")

    monkeypatch.setattr(PaperService, "disarm", disarm_raises)
    result = _agent_arm(home, _pack(home, pair="BTCUSD"))
    _assert_limit_refusal(result, CODE_AGENT_ARM_CLEANUP_FAILED)
    assert "kraken BTCUSD may still be running" in result["message"]
    assert "was disarmed" not in result["message"]
    assert [a.pair for a in _armed(home)] == ["BTCUSD"]

    # A disarm that is refused is a failed cleanup too.
    def disarm_refused(_self, *, venue, pair, correlation_id=""):
        return paper_app.CommandResultV1(
            schema_version="1",
            correlation_id=correlation_id,
            code=paper_app.CODE_STORED_MODE_NOT_PAPER,
            ok=False,
            message="refused",
            effect="refused",
            revision_before=None,
            revision_after=None,
        )

    monkeypatch.setattr(PaperService, "disarm", disarm_refused)
    result = _agent_arm(home, _pack(home, pair="ETHUSD"))
    _assert_limit_refusal(result, CODE_AGENT_ARM_CLEANUP_FAILED)
    assert "kraken ETHUSD may still be running" in result["message"]
    assert sorted(a.pair for a in _armed(home)) == ["BTCUSD", "ETHUSD"]
    assert [r["code"] for r in _refused_rows(home)] == [
        CODE_AGENT_ARM_UNVERIFIED,
        CODE_AGENT_ARM_CLEANUP_FAILED,
        CODE_AGENT_ARM_CLEANUP_FAILED,
    ]
    # Arms that may still be running are counted, so later agent arms see them.
    assert sorted(r["pair"] for r in arm_registry.running_agent_arms(home)) == ["BTCUSD", "ETHUSD"]
    _write_policy(home, _v2(max_agent_arms=2))
    _assert_limit_refusal(_agent_arm(home, _pack(home, pair="SOLUSD")), CODE_AGENT_LIMIT_MAX_ARMS)


def test_a_refused_twin_arm_never_undoes_the_other_calls_arm(home: Path, monkeypatch: pytest.MonkeyPatch):
    # Two overlapping agent calls arm byte-identical packs, so they share a
    # snapshot. The late call takes its "before" keys, then the twin arms
    # in full; the late call's own arm is refused as already armed and
    # wrote nothing, so it must not touch the twin's record.
    pack = _pack(home)
    real_arm = PaperService.arm
    twin: list[dict] = []

    def arm_while_a_twin_arms(self, *args, **kwargs):
        if not twin:
            twin.append({})
            twin[0] = _agent_arm(home, pack, actor=KRELL)
        return real_arm(self, *args, **kwargs)

    monkeypatch.setattr(PaperService, "arm", arm_while_a_twin_arms)
    late = _agent_arm(home, pack)
    assert twin[0]["ok"] is True, twin[0]
    assert late["ok"] is False
    assert late["code"] == paper_app.CODE_ALREADY_ARMED
    assert late["effect"] == "refused"
    [armed] = _armed(home)
    assert armed.pair == "SUIUSD"
    assert [(r["pair"], r["actor"]) for r in arm_registry.running_agent_arms(home)] == [("SUIUSD", "krell")]
    assert _refused_rows(home) == []


def test_two_agent_arms_that_both_passed_the_count_cannot_exceed_the_limit(home: Path, monkeypatch: pytest.MonkeyPatch):
    _write_policy(home, _v2(max_agent_arms=1))
    real_arm = PaperService.arm
    racing: list[dict] = []

    def arm_while_another_agent_arms(self, *args, **kwargs):
        # This arm already passed its pre-check (0 of 1 running). Before it
        # writes, a second agent passes the same pre-check and arms first.
        if not racing:
            racing.append({})
            racing[0] = _agent_arm(home, _pack(home, pair="BTCUSD"), actor=KRELL)
        return real_arm(self, *args, **kwargs)

    monkeypatch.setattr(PaperService, "arm", arm_while_another_agent_arms)
    late = _agent_arm(home, _pack(home, pair="SUIUSD"))
    assert racing[0]["ok"] is True
    _assert_limit_refusal(late, CODE_AGENT_LIMIT_MAX_ARMS)
    assert "was disarmed" in late["message"]
    assert [a.pair for a in _armed(home)] == ["BTCUSD"]
    assert [(r["pair"], r["actor"]) for r in arm_registry.running_agent_arms(home)] == [("BTCUSD", "krell")]


def test_concurrent_agent_arm_records_are_not_lost(home: Path, monkeypatch: pytest.MonkeyPatch):
    _human_arm(home, _pack(home, pair="SUIUSD"))
    _human_arm(home, _pack(home, pair="BTCUSD"))
    real_read = arm_registry._read

    def slow_read(registry_home: Path) -> list[dict]:
        rows = real_read(registry_home)
        time.sleep(0.2)  # widen the gap between a writer's read and its write
        return rows

    monkeypatch.setattr(arm_registry, "_read", slow_read)
    errors: list[BaseException] = []

    def record(armed: kb_config.ArmedPack) -> None:
        try:
            arm_registry.record_agent_arm(
                home,
                venue=armed.venue,
                pair=armed.pair,
                actor=MCP,
                armed_at_ts=armed.armed_at_ts,
                revision_id=None,
            )
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assertion below.
            errors.append(exc)

    writers = [threading.Thread(target=record, args=(armed,)) for armed in _armed(home)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    assert errors == []
    assert sorted(r["pair"] for r in arm_registry.running_agent_arms(home)) == ["BTCUSD", "SUIUSD"]


# ---- the pack that was checked is the pack that is armed -----------------


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_a_pack_swapped_after_the_check_is_not_what_gets_armed(home: Path, monkeypatch: pytest.MonkeyPatch):
    pack = _pack(home)  # kraken SUIUSD, 2% cap
    checked = pack.read_bytes()
    real_arm = PaperService.arm

    def swap_then_arm(self, *args, **kwargs):
        # An agent with file access swaps the file after the check.
        _pack(home, pair="BTCUSD", cap=25, pack_id="agent-suiusd")
        return real_arm(self, *args, **kwargs)

    monkeypatch.setattr(PaperService, "arm", swap_then_arm)
    result = _agent_arm(home, pack)
    assert result["ok"] is True, result
    assert json.loads(pack.read_bytes())["markets"][0]["pair"] == "BTCUSD"
    [armed] = _armed(home)
    assert (armed.pair, armed.cap) == ("SUIUSD", Decimal(2))
    snapshot = arm_registry.snapshot_dir(home) / f"{_sha256(checked)}.json"
    assert armed.pack_path == str(snapshot)
    assert [r["pair"] for r in arm_registry.running_agent_arms(home)] == ["SUIUSD"]


def test_an_arm_the_engine_put_on_another_pack_is_reported_and_left_alone(home: Path, monkeypatch: pytest.MonkeyPatch):
    # Simulates the engine arming a different pack (another pair) than the
    # snapshot it was given.
    other = _pack(home, pair="BTCUSD", cap=25, pack_id="other-btcusd")
    real_arm_pack = kb_run.arm_pack

    def arm_another_pack(_pack_path, **kwargs):
        return real_arm_pack(other, **kwargs)

    monkeypatch.setattr(kb_run, "arm_pack", arm_another_pack)
    result = _agent_arm(home, _pack(home))
    _assert_limit_refusal(result, CODE_AGENT_ARM_CLEANUP_FAILED)
    assert "may still be running" in result["message"]
    # A record whose pack_path is not this arm's snapshot is never touched.
    [armed] = _armed(home)
    assert (armed.pair, armed.pack_path) == ("BTCUSD", str(other))
    assert arm_registry.running_agent_arms(home) == []
    assert [r["code"] for r in _refused_rows(home)] == [CODE_AGENT_ARM_CLEANUP_FAILED]


def test_undoing_an_agent_arm_never_touches_a_human_arm_made_meanwhile(home: Path, monkeypatch: pytest.MonkeyPatch):
    real_arm = PaperService.arm

    def arm_then_a_human_arms(self, *args, **kwargs):
        result = real_arm(self, *args, **kwargs)
        human = _pack(home, pair="SOLUSD", cap=25, pack_id="human-solusd")
        assert real_arm(PaperService(home), human, venue="kraken", mode="paper", paper_balance=Decimal(1000)).ok
        return result

    def registry_write_fails(*_args, **_kwargs):
        raise OSError("read-only home")

    monkeypatch.setattr(PaperService, "arm", arm_then_a_human_arms)
    monkeypatch.setattr(arm_registry, "claim_agent_arm", registry_write_fails)
    result = _agent_arm(home, _pack(home))
    _assert_limit_refusal(result, CODE_AGENT_ARM_UNVERIFIED)
    assert "kraken SUIUSD was disarmed" in result["message"]
    assert [a.pair for a in _armed(home)] == ["SOLUSD"]


def test_agent_arms_arm_a_private_content_addressed_snapshot(home: Path):
    pack = _pack(home)
    raw = pack.read_bytes()
    assert _agent_arm(home, pack)["ok"] is True
    snapshot = arm_registry.snapshot_dir(home) / f"{_sha256(raw)}.json"
    assert _armed(home)[0].pack_path == str(snapshot)
    assert snapshot.read_bytes() == raw
    if os.name != "nt":
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
        assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700

    # The same bytes reuse the same snapshot; it is never rewritten.
    first = snapshot.stat()
    assert PaperService(home).disarm(venue="kraken", pair="SUIUSD").ok is True
    assert _agent_arm(home, pack)["ok"] is True
    again = snapshot.stat()
    assert (again.st_ino, again.st_mtime_ns) == (first.st_ino, first.st_mtime_ns)
    assert [p.name for p in snapshot.parent.iterdir()] == [snapshot.name]

    # A snapshot whose bytes no longer match its name is replaced before use.
    assert PaperService(home).disarm(venue="kraken", pair="SUIUSD").ok is True
    snapshot.write_bytes(b"{}")
    assert _agent_arm(home, pack)["ok"] is True
    assert snapshot.read_bytes() == raw


def test_concurrent_snapshots_of_the_same_bytes_never_share_a_temp_file(home: Path, monkeypatch: pytest.MonkeyPatch):
    raw = _pack(home).read_bytes()
    target = arm_registry.snapshot_dir(home) / f"{_sha256(raw)}.json"
    real_fsync = os.fsync
    started: list[bool] = []
    twin: list[Path] = []

    def fsync_then_a_twin_snapshots(fd: int) -> None:
        # The first writer has written its temp file; before it replaces the
        # target, a second call snapshots the same bytes in full.
        real_fsync(fd)
        if not started:
            started.append(True)
            twin.append(arm_registry.snapshot_pack(home, raw))

    monkeypatch.setattr(os, "fsync", fsync_then_a_twin_snapshots)
    assert arm_registry.snapshot_pack(home, raw) == target
    assert twin == [target]
    assert target.read_bytes() == raw
    assert [p.name for p in target.parent.iterdir()] == [target.name]
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_agent_pack_reads_are_bounded_to_a_regular_file_of_at_most_one_mib(home: Path):
    pack = _pack(home)
    raw = pack.read_bytes()
    too_big = home / "too-big.json"
    too_big.write_bytes(raw + b" " * (MAX_PACK_BYTES + 1 - len(raw)))
    refused = [too_big, home, home / "missing.json"]
    if os.name != "nt":
        fifo = home / "pipe.json"
        os.mkfifo(fifo)
        refused.append(fifo)
        if Path("/dev/zero").exists():
            zero_link = home / "zero.json"
            zero_link.symlink_to("/dev/zero")
            refused.extend([Path("/dev/zero"), zero_link])
    for path in refused:
        result = _agent_arm(home, path)
        assert result["ok"] is False, path
        assert result["code"] == CODE_INVALID_REQUEST, path
        assert result["message"] == MESSAGE_PACK_UNREADABLE, path
    assert not kb_config.config_path(home).exists()
    assert not arm_registry.snapshot_dir(home).exists()

    # Exactly 1 MiB (the pack padded with trailing whitespace) is read and armed.
    at_limit = home / "at-limit.json"
    at_limit.write_bytes(raw + b" " * (MAX_PACK_BYTES - len(raw)))
    assert _agent_arm(home, at_limit)["ok"] is True


def test_an_agent_arm_of_a_catalog_pack_still_requires_a_license(home: Path):
    catalog = home / "packs" / "catalog"
    catalog.mkdir(parents=True)
    paid = catalog / "agent-suiusd.json"
    paid.write_bytes(_pack(home).read_bytes())
    assert _agent_arm(home, paid)["ok"] is True
    [armed] = _armed(home)
    assert armed.pack_path.startswith(str(arm_registry.snapshot_dir(home)))
    assert armed.requires_license is True


def test_a_failure_inside_the_arm_after_the_write_is_undone(home: Path, monkeypatch: pytest.MonkeyPatch):
    real_apply = paper_app._apply_loss_limit_to_new_arm
    real_drop = paper_app._drop_new_arm

    def rollback_save_fails(*_args, **_kwargs):
        raise OSError("disk full")

    # The limit cannot be set and the rollback's save raises: PaperService.arm
    # raises with the record written.
    monkeypatch.setattr(paper_app, "_apply_loss_limit_to_new_arm", lambda *_a, **_k: False)
    monkeypatch.setattr(paper_app, "_drop_new_arm", rollback_save_fails)
    result = _agent_arm(home, _pack(home))
    _assert_limit_refusal(result, CODE_AGENT_ARM_UNVERIFIED)
    assert "kraken SUIUSD was disarmed" in result["message"]
    assert _armed(home) == []
    assert arm_registry.running_agent_arms(home) == []

    # Marking the draft deployed raises after a revision arm: undone the same way.
    monkeypatch.setattr(paper_app, "_apply_loss_limit_to_new_arm", real_apply)
    monkeypatch.setattr(paper_app, "_drop_new_arm", real_drop)
    core = AgentToolCore(home)
    draft = core.call(KRELL, "draft", {"pack": json.loads(_pack(home, pair="BTCUSD").read_text(encoding="utf-8"))})
    revision_id = draft["result"]["revision_id"]
    assert core.call(KRELL, "validate", {"revision_id": revision_id})["ok"] is True

    def mark_fails(_self, _revision_id):
        raise OSError("drafts are read-only")

    monkeypatch.setattr(StrategyDraftService, "mark_deployed", mark_fails)
    result = core.call(
        KRELL,
        "paper_arm",
        {"revision_id": revision_id, "venue": "kraken", "paper_balance": "1000", "daily_loss_limit_pct": "5"},
    )
    assert result["ok"] is False
    assert result["code"] == CODE_AGENT_ARM_UNVERIFIED
    assert _armed(home) == []


# ---- change tools (M38.6 registers them) ---------------------------------


def test_change_tools_follow_the_per_actor_allow_flags(home: Path):
    ran: list[tuple[str, str]] = []

    def handler(ctx, params):
        ran.append((ctx.actor.label, params["tool"]))
        return {"schema_version": "1", "ok": True, "effect": "changed", "tool": params["tool"]}

    core = AgentToolCore(home)
    for name in CHANGE_TOOLS:
        core.register(
            ToolSpec(
                name=name,
                effect="change",
                cost="light",
                input_schema={"type": "object", "properties": {}},
                labels=("paper",),
                description="test stand-in for an M38.6 change tool",
                handler=handler,
            )
        )

    # Defaults: Krell may pause, resume and disarm; external agents may not.
    for name in CHANGE_TOOLS:
        assert core.call(KRELL, name, {"tool": name})["ok"] is True
        refused = core.call(MCP, name, {"tool": name})
        assert refused["ok"] is False
        assert refused["code"] == CODE_AGENT_CHANGE_NOT_ALLOWED
    assert ran == [("krell", name) for name in CHANGE_TOOLS]
    rows = _refused_rows(home)
    assert [(r["code"], r["action"], r["actor"]) for r in rows] == [
        (CODE_AGENT_CHANGE_NOT_ALLOWED, name, "mcp:cursor") for name in CHANGE_TOOLS
    ]

    # The operator lets external agents pause and resume, and stops Krell disarming.
    _write_policy(home, _v2(mcp_allow_pause_resume=True, krell_allow_disarm=False))
    ran.clear()
    assert core.call(MCP, "pause_entries", {"tool": "pause_entries"})["ok"] is True
    assert core.call(MCP, "resume_entries", {"tool": "resume_entries"})["ok"] is True
    assert core.call(MCP, "disarm", {"tool": "disarm"})["code"] == CODE_AGENT_CHANGE_NOT_ALLOWED
    assert core.call(KRELL, "disarm", {"tool": "disarm"})["code"] == CODE_AGENT_CHANGE_NOT_ALLOWED
    assert ran == [("mcp:cursor", "pause_entries"), ("mcp:cursor", "resume_entries")]

    # A malformed policy file locks every agent change.
    user_policy.policy_path(home).write_text("{not-json", encoding="utf-8")
    locked = core.call(KRELL, "pause_entries", {"tool": "pause_entries"})
    assert locked["code"] == user_policy.CODE_POLICY_INVALID
    assert ran == [("mcp:cursor", "pause_entries"), ("mcp:cursor", "resume_entries")]
