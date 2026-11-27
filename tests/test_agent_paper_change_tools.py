"""Agent change tools: pause, resume and disarm act on paper strategies only.

``pause_entries``, ``resume_entries`` and ``disarm`` reach Krell and external
MCP agents through the agent tool core and ``PaperService``. A strategy whose
stored mode is not paper is refused with ``agent_live_change_refused`` before
the service is called, so ``config.json`` keeps its exact bytes. External
agents may not pause, resume or disarm until the operator allows it, and
Krell's changes run only once a human confirms them. ``close_position`` sells
at market, so it is never an agent tool.

Every armed record below is a local fixture: no exchange, no order, no key.
"""

from __future__ import annotations

import ast
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot.agent import tools_change
from krellbot.agent.actors import KRELL, Actor
from krellbot.agent.audit import read_agent_activity
from krellbot.agent.confirm import CODE_CONFIRM_REQUIRED, CODE_CONFIRM_UNKNOWN, ConfirmLedger
from krellbot.agent.core import CODE_INVALID_REQUEST, CODE_UNKNOWN_TOOL, AgentToolCore
from krellbot.agent.limits import CHANGE_TOOLS, CODE_AGENT_CHANGE_NOT_ALLOWED
from krellbot.agent.tools_change import (
    CODE_AGENT_LIVE_CHANGE_REFUSED,
    MESSAGE_AGENT_LIVE_CHANGE_REFUSED,
    register_change_tools,
)
from krellbot.agent.tools_read import register_read_tools
from krellbot.agent.tools_research import register_research_tools
from krellbot.application import paper as paper_app
from krellbot.application import user_policy
from krellbot.application.paper import PaperService
from krellbot.config import ArmedPack, Config, config_path, load_config, save_config
from krellbot.journal import read_records_lenient

pytestmark = pytest.mark.usefixtures("fresh_keyring")

CURSOR = Actor.mcp("cursor")
NOW = 1_700_000_000
PAPER_MARKET = {"venue": "kraken", "pair": "SUIUSD"}
LIVE_MARKET = {"venue": "kraken", "pair": "BTCUSD"}


def _core(home: Path, *, ledger: bool = True) -> AgentToolCore:
    """A core with the change tools; with the engine's confirm ledger unless ``ledger`` is False."""
    confirm = ConfirmLedger(clock=lambda: NOW) if ledger else None
    core = AgentToolCore(home, clock=lambda: NOW, confirm=confirm)
    register_change_tools(core)
    return core


def _record(home: Path, *, mode: str, venue: str, pair: str, entries_paused: bool = False) -> ArmedPack:
    packs = home / "packs"
    packs.mkdir(parents=True, exist_ok=True)
    pack_id = f"change-{pair.lower()}"
    body = {
        "schema_version": 1,
        "id": pack_id,
        "version": "1.0.0",
        "label": "Change tools",
        "author": "krellbot change tool tests",
        "timeframe": "1h",
        "indicators": {"sma3": {"fn": "sma", "src": "close", "len": 3}},
        "entry": ["close", ">", "sma3"],
        "exit": ["close", "<", "sma3"],
        "risk": {"max_account_pct": 2, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": venue, "pair": pair}],
    }
    pack_path = packs / f"{pack_id}.json"
    pack_path.write_text(json.dumps(body), encoding="utf-8")
    return ArmedPack(
        pack_path=str(pack_path),
        pack_sha256=hashlib.sha256(pack_path.read_bytes()).hexdigest(),
        pack_id=pack_id,
        pack_version="1.0.0",
        venue=venue,
        pair=pair,
        cap=Decimal(2),
        stop=Decimal(5),
        mode=mode,
        starting_cash=Decimal(1000),
        requires_license=False,
        armed_at_ts=NOW - 10_000,
        entries_paused=entries_paused,
    )


def _save(home: Path, *records: ArmedPack) -> None:
    save_config(home, Config(armed=list(records)))


def _paper(home: Path, **extra: object) -> ArmedPack:
    return _record(home, mode="paper", **PAPER_MARKET, **extra)


def _config_bytes(home: Path) -> bytes:
    return config_path(home).read_bytes()


def _write_policy(home: Path, **agents: object) -> None:
    section = user_policy.DEFAULT_AGENT_LIMITS.to_dict()
    section.update(agents)
    payload = {
        "schema_version": "2",
        "allow_paper_arm": True,
        "allow_live": False,
        "allow_key_export": False,
        "agents": section,
    }
    user_policy.policy_path(home).write_text(json.dumps(payload), encoding="utf-8")


def _krell_confirmed(core: AgentToolCore, tool: str, params: dict) -> dict:
    """A Krell change as the human confirms it: ``needs_confirm`` first, then the same call with its ticket."""
    asked = core.call(KRELL, tool, params)
    assert asked["effect"] == "needs_confirm", asked
    assert asked["code"] == CODE_CONFIRM_REQUIRED
    return core.call(KRELL, tool, params, confirm_token=asked["confirm"]["ticket_id"])


def _spy_service(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every PaperService change call (the real method still runs)."""
    calls: list[str] = []
    for name in CHANGE_TOOLS:
        real = getattr(PaperService, name)

        def spy(self, *args, _name=name, _real=real, **kwargs):
            calls.append(_name)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(PaperService, name, spy)
    return calls


def _refused_policy_rows(home: Path) -> list[tuple[str, str, str]]:
    return [
        (r["detail"]["code"], r["detail"]["action"], r["detail"]["actor"])
        for r in read_records_lenient(home)
        if r.get("kind") == "user_policy"
        and isinstance(r.get("detail"), dict)
        and r["detail"].get("event") == "refused"
    ]


def _audit_rows(home: Path, tool: str) -> list[tuple[str, str, str, str]]:
    """``(actor, outcome, code, venue)`` of a tool's ``agent_action`` records, oldest first."""
    rows = read_agent_activity(home, tool=tool)
    return [(r["detail"]["actor"], r["detail"]["outcome"], r["detail"]["code"], r["venue"]) for r in reversed(rows)]


def _assert_live_refused(result: dict, tool: str, actor: str) -> None:
    assert result["ok"] is False, result
    assert result["effect"] == "refused"
    assert result["code"] == CODE_AGENT_LIVE_CHANGE_REFUSED
    assert result["message"] == MESSAGE_AGENT_LIVE_CHANGE_REFUSED
    assert result["tool"] == tool
    assert result["actor"] == actor
    assert result["labels"] == ["paper"]
    assert "confirm" not in result


def _swap_mode(home: Path, mode: str) -> None:
    """Re-arm the paper market's record in ``mode`` (another process writing the config)."""
    config = load_config(home)
    for armed in config.armed:
        if (armed.venue, armed.pair) == (PAPER_MARKET["venue"], PAPER_MARKET["pair"]):
            armed.mode = mode
    save_config(home, config)


# ---- live targets ----------------------------------------------------------


def test_agent_pause_on_a_live_strategy_is_refused_before_any_config_write(
    home: Path, live_capable_release, monkeypatch: pytest.MonkeyPatch
):
    _save(home, _paper(home), _record(home, mode="live", **LIVE_MARKET))
    _write_policy(home, mcp_allow_pause_resume=True, mcp_allow_disarm=True)
    calls = _spy_service(monkeypatch)
    before = _config_bytes(home)
    core = _core(home)

    by_mcp = core.call(CURSOR, "pause_entries", LIVE_MARKET)
    # Krell is refused on its first call: no ticket, so no human is asked to confirm a call that cannot run.
    by_krell = core.call(KRELL, "pause_entries", LIVE_MARKET)

    _assert_live_refused(by_mcp, "pause_entries", "mcp:cursor")
    _assert_live_refused(by_krell, "pause_entries", "krell")
    # The paper service is never called and not one config byte changes.
    assert calls == []
    assert _config_bytes(home) == before
    live = [a for a in load_config(home).armed if a.pair == "BTCUSD"]
    assert [(a.mode, a.entries_paused) for a in live] == [("live", False)]
    # Journaled with its typed code, for each actor.
    assert _audit_rows(home, "pause_entries") == [
        ("mcp:cursor", "refused", CODE_AGENT_LIVE_CHANGE_REFUSED, "kraken"),
        ("krell", "refused", CODE_AGENT_LIVE_CHANGE_REFUSED, "kraken"),
    ]


def test_agent_resume_and_disarm_on_a_non_paper_strategy_are_refused_too(
    home: Path, live_capable_release, monkeypatch: pytest.MonkeyPatch
):
    other = {"venue": "coinbase", "pair": "BTC-USD"}
    _save(
        home,
        _record(home, mode="live", entries_paused=True, **LIVE_MARKET),
        # Any stored mode other than paper is refused, not only "live".
        _record(home, mode="shadow", **other),
    )
    _write_policy(home, mcp_allow_pause_resume=True, mcp_allow_disarm=True)
    calls = _spy_service(monkeypatch)
    before = _config_bytes(home)
    core = _core(home)

    for market in (LIVE_MARKET, other):
        for tool in ("resume_entries", "disarm"):
            _assert_live_refused(core.call(CURSOR, tool, market), tool, "mcp:cursor")
            _assert_live_refused(core.call(KRELL, tool, market), tool, "krell")

    assert calls == []
    assert _config_bytes(home) == before
    assert [(a.pair, a.mode, a.entries_paused) for a in load_config(home).armed] == [
        ("BTCUSD", "live", True),
        ("BTC-USD", "shadow", False),
    ]


def test_a_paper_record_on_the_same_pair_does_not_open_a_live_record_to_agents(
    home: Path, live_capable_release, monkeypatch: pytest.MonkeyPatch
):
    # ``PaperService.disarm`` removes every record on the pair, so a paper
    # record listed first must not let an agent disarm the live one with it.
    _save(home, _paper(home), _record(home, mode="live", **PAPER_MARKET))
    _write_policy(home, mcp_allow_pause_resume=True, mcp_allow_disarm=True)
    calls = _spy_service(monkeypatch)
    before = _config_bytes(home)
    core = _core(home)

    for tool in ("disarm", "pause_entries"):
        _assert_live_refused(core.call(CURSOR, tool, PAPER_MARKET), tool, "mcp:cursor")
        _assert_live_refused(core.call(KRELL, tool, PAPER_MARKET), tool, "krell")

    assert calls == []
    assert _config_bytes(home) == before
    assert [(a.mode, a.entries_paused) for a in load_config(home).armed] == [("paper", False), ("live", False)]


def test_a_record_re_armed_live_after_the_confirm_is_refused_and_the_ticket_stays_unspent(
    home: Path, live_capable_release, monkeypatch: pytest.MonkeyPatch
):
    _save(home, _paper(home))
    calls = _spy_service(monkeypatch)
    core = _core(home)

    asked = core.call(KRELL, "pause_entries", PAPER_MARKET)
    assert asked["effect"] == "needs_confirm"
    ticket = asked["confirm"]["ticket_id"]

    _swap_mode(home, "live")
    swapped = _config_bytes(home)
    refused = core.call(KRELL, "pause_entries", PAPER_MARKET, confirm_token=ticket)
    _assert_live_refused(refused, "pause_entries", "krell")
    assert calls == []
    assert _config_bytes(home) == swapped
    assert _audit_rows(home, "pause_entries")[-1] == ("krell", "refused", CODE_AGENT_LIVE_CHANGE_REFUSED, "kraken")

    # The refusal came before the confirm step, so the ticket was not spent.
    _swap_mode(home, "paper")
    paused = core.call(KRELL, "pause_entries", PAPER_MARKET, confirm_token=ticket)
    assert (paused["ok"], paused["code"]) == (True, paper_app.CODE_ENTRIES_PAUSED)
    assert calls == ["pause_entries"]


@pytest.mark.parametrize("tool", ["pause_entries", "disarm"])
def test_a_record_re_armed_live_while_the_service_runs_is_reported_as_a_live_refusal(
    home: Path, live_capable_release, monkeypatch: pytest.MonkeyPatch, tool: str
):
    _save(home, _paper(home))
    _write_policy(home, mcp_allow_pause_resume=True, mcp_allow_disarm=True)
    real = getattr(PaperService, tool)
    swapped: list[bytes] = []

    def re_armed_live_first(self, *args, **kwargs):
        # The handler read a paper record; another process re-arms it live before the service reads it.
        _swap_mode(home, "live")
        swapped.append(_config_bytes(home))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(PaperService, tool, re_armed_live_first)
    result = _core(home).call(CURSOR, tool, PAPER_MARKET)

    # The service refused with ``stored_mode_not_paper`` and wrote nothing; agents see the live-target code.
    _assert_live_refused(result, tool, "mcp:cursor")
    assert _config_bytes(home) == swapped[0]
    assert [(a.mode, a.entries_paused) for a in load_config(home).armed] == [("live", False)]


# ---- per-actor allow flags -------------------------------------------------


def test_mcp_pause_and_disarm_are_off_by_default(home: Path, monkeypatch: pytest.MonkeyPatch):
    _save(home, _paper(home))
    calls = _spy_service(monkeypatch)
    before = _config_bytes(home)
    core = _core(home)

    for tool in CHANGE_TOOLS:
        result = core.call(CURSOR, tool, PAPER_MARKET)
        assert result["ok"] is False, result
        assert result["effect"] == "refused"
        assert result["code"] == CODE_AGENT_CHANGE_NOT_ALLOWED
        assert result["labels"] == ["paper"]
        assert "confirm" not in result

    assert calls == []
    assert _config_bytes(home) == before
    assert _refused_policy_rows(home) == [(CODE_AGENT_CHANGE_NOT_ALLOWED, tool, "mcp:cursor") for tool in CHANGE_TOOLS]


def test_mcp_pause_works_when_operator_enables_it(home: Path):
    _save(home, _paper(home))
    _write_policy(home, mcp_allow_pause_resume=True)
    core = _core(home)

    paused = core.call(CURSOR, "pause_entries", PAPER_MARKET)
    assert paused == {
        "schema_version": "1",
        "ok": True,
        "effect": "changed",
        "code": paper_app.CODE_ENTRIES_PAUSED,
        "message": "entries paused: kraken SUIUSD",
        "tool": "pause_entries",
        "venue": "kraken",
        "pair": "SUIUSD",
        "actor": "mcp:cursor",
        "labels": ["paper"],
    }
    assert load_config(home).armed[0].entries_paused is True

    again = core.call(CURSOR, "pause_entries", PAPER_MARKET)
    assert (again["ok"], again["effect"], again["code"]) == (True, "unchanged", paper_app.CODE_ALREADY_PAUSED)

    resumed = core.call(CURSOR, "resume_entries", PAPER_MARKET)
    assert (resumed["ok"], resumed["effect"], resumed["code"]) == (True, "changed", paper_app.CODE_ENTRIES_RESUMED)
    assert load_config(home).armed[0].entries_paused is False

    # Disarm has its own flag, still off.
    still_armed = _config_bytes(home)
    refused = core.call(CURSOR, "disarm", PAPER_MARKET)
    assert refused["code"] == CODE_AGENT_CHANGE_NOT_ALLOWED
    assert _config_bytes(home) == still_armed

    _write_policy(home, mcp_allow_pause_resume=True, mcp_allow_disarm=True)
    disarmed = core.call(CURSOR, "disarm", PAPER_MARKET)
    assert (disarmed["ok"], disarmed["effect"], disarmed["code"]) == (True, "changed", paper_app.CODE_DISARMED)
    assert load_config(home).armed == []
    assert [row[1:3] for row in _audit_rows(home, "disarm")] == [
        ("refused", CODE_AGENT_CHANGE_NOT_ALLOWED),
        ("ok", paper_app.CODE_DISARMED),
    ]


def test_krell_pause_resume_and_disarm_run_once_a_human_confirms(home: Path):
    _save(home, _paper(home))
    core = _core(home)
    before = _config_bytes(home)

    asked = core.call(KRELL, "pause_entries", PAPER_MARKET)
    assert asked["effect"] == "needs_confirm"
    assert asked["confirm"]["tool"] == "pause_entries"
    assert asked["confirm"]["params"] == PAPER_MARKET
    assert asked["labels"] == ["paper"]
    # Asking changes nothing.
    assert _config_bytes(home) == before

    ticket = asked["confirm"]["ticket_id"]
    paused = core.call(KRELL, "pause_entries", PAPER_MARKET, confirm_token=ticket)
    assert (paused["ok"], paused["code"], paused["actor"]) == (True, paper_app.CODE_ENTRIES_PAUSED, "krell")
    assert load_config(home).armed[0].entries_paused is True
    # The ticket runs once.
    replayed = core.call(KRELL, "pause_entries", PAPER_MARKET, confirm_token=ticket)
    assert replayed["code"] == CODE_CONFIRM_UNKNOWN

    resumed = _krell_confirmed(core, "resume_entries", PAPER_MARKET)
    assert (resumed["ok"], resumed["code"]) == (True, paper_app.CODE_ENTRIES_RESUMED)
    disarmed = _krell_confirmed(core, "disarm", PAPER_MARKET)
    assert (disarmed["ok"], disarmed["code"]) == (True, paper_app.CODE_DISARMED)
    assert load_config(home).armed == []

    # A core without a confirm ledger refuses Krell's change (fail closed).
    _save(home, _paper(home))
    rearmed = _config_bytes(home)
    no_ledger = _core(home, ledger=False).call(KRELL, "pause_entries", PAPER_MARKET)
    assert (no_ledger["ok"], no_ledger["effect"], no_ledger["code"]) == (False, "refused", CODE_CONFIRM_REQUIRED)

    # The operator stops Krell disarming: refused before any ticket is issued.
    _write_policy(home, krell_allow_disarm=False)
    refused = core.call(KRELL, "disarm", PAPER_MARKET)
    assert refused["code"] == CODE_AGENT_CHANGE_NOT_ALLOWED
    assert "confirm" not in refused
    assert _config_bytes(home) == rearmed


# ---- inputs and missing records -------------------------------------------


def test_a_market_that_is_not_armed_returns_the_service_not_armed_code(home: Path):
    _write_policy(home, mcp_allow_pause_resume=True, mcp_allow_disarm=True)
    core = _core(home)

    for tool in CHANGE_TOOLS:
        result = core.call(CURSOR, tool, PAPER_MARKET)
        assert (result["ok"], result["effect"], result["code"]) == (False, "refused", paper_app.CODE_NOT_ARMED)
        assert result["message"] == "not armed: kraken SUIUSD"
        assert result["labels"] == ["paper"]
    assert not config_path(home).exists()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"venue": "kraken"},
        {"pair": "SUIUSD"},
        {"venue": "../kraken", "pair": "SUIUSD"},
        {"venue": "Kraken", "pair": "SUIUSD"},
        {"venue": "", "pair": "SUIUSD"},
        {"venue": "kraken", "pair": "SUI USD"},
        {"venue": "kraken", "pair": "S" * 33},
        {"venue": 7, "pair": "SUIUSD"},
        {"venue": "kraken", "pair": ["SUIUSD"]},
    ],
)
def test_change_tools_check_venue_and_pair_before_reading_the_config(
    home: Path, monkeypatch: pytest.MonkeyPatch, params: dict
):
    _save(home, _paper(home))
    _write_policy(home, mcp_allow_pause_resume=True, mcp_allow_disarm=True)
    calls = _spy_service(monkeypatch)
    before = _config_bytes(home)

    def config_must_not_be_read(*_args, **_kwargs):
        raise AssertionError("the config was read before venue and pair were checked")

    monkeypatch.setattr(tools_change, "load_config", config_must_not_be_read)
    core = _core(home)

    for tool in CHANGE_TOOLS:
        result = core.call(CURSOR, tool, params)
        assert (result["ok"], result["effect"], result["code"]) == (False, "refused", CODE_INVALID_REQUEST), result
        assert result["tool"] == tool
    assert calls == []
    assert _config_bytes(home) == before


# ---- the catalogue ---------------------------------------------------------


def test_close_position_is_not_an_agent_tool(home: Path):
    _save(home, _paper(home))
    _write_policy(home, mcp_allow_pause_resume=True, mcp_allow_disarm=True)
    core = _core(home)
    register_read_tools(core)
    register_research_tools(core)
    before = _config_bytes(home)

    for actor in (KRELL, CURSOR):
        names = [spec.name for spec in core.specs(actor)]
        assert not [name for name in names if "close" in name], names
        for tool in ("close_position", "close-position", "Close Position"):
            result = core.call(actor, tool, PAPER_MARKET)
            assert (result["ok"], result["effect"], result["code"]) == (False, "refused", CODE_UNKNOWN_TOOL)
            assert result["tool"] == "close_position"
            assert result["labels"] == []
            assert "confirm" not in result
    assert _config_bytes(home) == before


def test_register_change_tools_twice_registers_each_tool_once(home: Path):
    core = AgentToolCore(home)
    built_in = [spec.name for spec in core.specs(KRELL)]
    register_change_tools(core)
    once = [spec.name for spec in core.specs(KRELL)]
    register_change_tools(core)
    twice = [spec.name for spec in core.specs(KRELL)]

    assert once == twice == [*built_in, *CHANGE_TOOLS]
    for actor in (KRELL, CURSOR):
        specs = {spec.name: spec for spec in core.specs(actor)}
        for name in CHANGE_TOOLS:
            spec = specs[name]
            assert (spec.effect, spec.cost, spec.labels) == ("change", "light", ("paper",))
            assert spec.input_schema["required"] == ["venue", "pair"]
            assert set(spec.input_schema["properties"]) == {"venue", "pair"}


def test_change_tool_handlers_do_not_import_risk_path_modules():
    tree = ast.parse(Path(tools_change.__file__).read_text(encoding="utf-8"))
    banned = {"run", "venues", "execution", "keys", "secrets"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            if node.module == "krellbot":
                imported.update(f"krellbot.{alias.name}" for alias in node.names)
    for name in imported:
        parts = name.split(".")
        assert not (parts[0] == "krellbot" and len(parts) > 1 and parts[1] in banned), name
