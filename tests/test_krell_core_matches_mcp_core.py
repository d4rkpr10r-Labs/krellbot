"""Krell's core and the MCP server's core offer the same tools.

Both front doors build their agent tool core with
:func:`krellbot.agent.assemble.build_agent_core`; Krell's
:func:`~krellbot.agent.krell_loop.build_krell_core` adds only the engine's
confirm ledger. So whatever one actor kind is offered, it is offered the
same way through either door. Fakes only: no model and no network.
"""

from __future__ import annotations

from pathlib import Path

from krellbot.agent.actors import KRELL, Actor
from krellbot.agent.assemble import build_agent_core
from krellbot.agent.confirm import ConfirmLedger
from krellbot.agent.krell_loop import build_krell_core

NOW = 1_700_000_000
CURSOR = Actor.mcp("cursor")


def _offered(core, actor: Actor) -> list[str]:
    return [spec.name for spec in core.specs(actor)]


def test_krell_and_mcp_cores_offer_the_same_tools_to_each_actor_kind(home: Path) -> None:
    ledger = ConfirmLedger(clock=lambda: NOW)
    krell_core = build_krell_core(home, clock=lambda: NOW, ledger=ledger)
    mcp_core = build_agent_core(home, clock=lambda: NOW)

    for actor in (KRELL, CURSOR):
        assert _offered(krell_core, actor) == _offered(mcp_core, actor), actor.label
    # Across the two actor kinds, each door offers its actor the tools both kinds may use.
    shared_krell = {s.name for s in krell_core.specs(KRELL) if "mcp" in s.actors}
    shared_mcp = {s.name for s in mcp_core.specs(CURSOR) if "krell" in s.actors}
    assert shared_krell == shared_mcp
    # The read, research and paper change tools are all there, never close_position.
    assert {"read", "catalogue", "backtest", "compare", "paper_arm", "pause_entries", "disarm"} <= shared_krell
    assert "close_position" not in shared_krell


def test_krells_core_asks_for_a_confirm_through_its_ledger_and_the_mcp_core_has_none(home: Path) -> None:
    ledger = ConfirmLedger(clock=lambda: NOW)
    krell_core = build_krell_core(home, clock=lambda: NOW, ledger=ledger)
    mcp_core = build_agent_core(home, clock=lambda: NOW)
    market = {"venue": "kraken", "pair": "SUIUSD"}

    asked = krell_core.call(KRELL, "pause_entries", market)
    assert asked["effect"] == "needs_confirm"
    ticket = asked["confirm"]["ticket_id"]
    # The ticket lives in the engine's ledger, bound to the exact call.
    assert ledger.consume(ticket, "pause_entries", market, now=NOW) == "ok"
    # The MCP core holds no ledger: a Krell call through it can never get a ticket.
    refused = mcp_core.call(KRELL, "pause_entries", market)
    assert (refused["effect"], refused["code"]) == ("refused", "agent_confirm_required")
