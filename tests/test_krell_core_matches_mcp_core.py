"""Krell's core and the MCP server's core offer the same tools, and Krell's tickets follow its clock.

Both front doors build their agent tool core with
:func:`krellbot.agent.assemble.build_agent_core`; Krell's
:func:`~krellbot.agent.krell_loop.build_krell_core` adds only the engine's
confirm ledger. So whatever one actor kind is offered, it is offered the
same way through either door, and no agent through either door is ever
offered a tool that places or closes an order or touches live trading.
Ticket expiry follows the clock the core was built with. Fakes only: no
model and no network.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from krellbot.agent.actors import KRELL, Actor
from krellbot.agent.assemble import build_agent_core
from krellbot.agent.confirm import CODE_CONFIRM_UNKNOWN, TICKET_TTL_S, ConfirmLedger
from krellbot.agent.krell_loop import build_krell_core

NOW = 1_700_000_000
CURSOR = Actor.mcp("cursor")
# Only a read may name orders or positions (``unresolved_orders``, ``positions``); nothing else may.
_ORDER_OR_LIVE = re.compile(r"order|close|position|live")


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
    # The read, research and paper change tools are all there.
    assert {"read", "catalogue", "backtest", "compare", "paper_arm", "pause_entries", "disarm"} <= shared_krell


def test_no_agent_is_offered_a_tool_that_places_or_closes_an_order(home: Path) -> None:
    ledger = ConfirmLedger(clock=lambda: NOW)
    krell_core = build_krell_core(home, clock=lambda: NOW, ledger=ledger)
    mcp_core = build_agent_core(home, clock=lambda: NOW)

    # Everything each actor is offered, including a tool offered to one actor kind alone.
    for core in (krell_core, mcp_core):
        for actor in (KRELL, CURSOR):
            offered = core.specs(actor)
            assert "close_position" not in [spec.name for spec in offered], actor.label
            for spec in offered:
                if spec.effect != "read":
                    assert not _ORDER_OR_LIVE.search(spec.name), (actor.label, spec.name)


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


def test_ticket_expiry_follows_the_clock_krells_core_was_built_with(home: Path) -> None:
    # The ledger's own clock is the wall clock, which stays put during the test; only the
    # core's clock moves. If the core did not hand its clock to the ledger, nothing would expire.
    ledger = ConfirmLedger(clock=time.time)
    now = [float(NOW)]
    core = build_krell_core(home, clock=lambda: now[0], ledger=ledger)
    market = {"venue": "kraken", "pair": "SUIUSD"}

    early = core.call(KRELL, "pause_entries", market)["confirm"]
    late = core.call(KRELL, "pause_entries", market)["confirm"]
    assert late["expires_at"] == NOW + TICKET_TTL_S

    now[0] = NOW + TICKET_TTL_S - 1
    ran = core.call(KRELL, "pause_entries", market, confirm_token=early["ticket_id"])
    assert ran["code"] != CODE_CONFIRM_UNKNOWN  # still live: it reached the paper service

    now[0] = NOW + TICKET_TTL_S + 1
    expired = core.call(KRELL, "pause_entries", market, confirm_token=late["ticket_id"])
    assert (expired["effect"], expired["code"]) == ("refused", CODE_CONFIRM_UNKNOWN)
