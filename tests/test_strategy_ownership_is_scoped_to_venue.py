"""Strategy ownership belongs to one trading identity: pack, venue, pair and mode.

The same pack and pair can be armed on two venues. Each venue's exit may
consume only what that venue's arm bought; external holdings on either venue
are never strategy inventory. Journal history that cannot be attributed to an
identity is never credited to whichever arm is configured now, and it holds
new entries instead of making the strategy look flat.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot import journal as kb_journal
from krellbot import run
from krellbot.application import alerts as kb_alerts
from krellbot.application.paper import PaperService
from krellbot.execution import reservations
from krellbot.run.reconcile import OwnershipIdentity, project_ownership
from tests.test_ns14_reservations import _PACK_BODY, _candle, _FakeVenue

PAIR = "BTCUSDT"
ENTRY_BARS = [_candle(0, "10"), _candle(3_600_000, "9"), _candle(7_200_000, "12")]
EXIT_BARS = [*ENTRY_BARS, _candle(10_800_000, "8")]
EXTERNAL = Decimal(100)
REPO_ROOT = Path(__file__).resolve().parents[1]


class _BtcUsdtVenue(_FakeVenue):
    @staticmethod
    def _split(pair: str) -> tuple[str, str]:
        return ("BTC", "USDT")


def _write_pack(home: Path, *, pack_id: str, venue: str, version: str = "1.0.0") -> Path:
    body = {
        **_PACK_BODY,
        "id": pack_id,
        "version": version,
        "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 50}},
        "markets": [{"venue": venue, "pair": PAIR}],
    }
    path = home / f"{pack_id}-{venue}-{version}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _arm(home: Path, *, pack_id: str, venue: str, cash: Decimal, version: str = "1.0.0") -> None:
    path = _write_pack(home, pack_id=pack_id, venue=venue, version=version)
    result = PaperService(home).arm(path, venue=venue, mode="paper", paper_balance=cash)
    assert result.ok, result.to_dict()


def _tick(home: Path, venue: str, fake: _FakeVenue, bars: list, *, clock: float) -> None:
    assert run.tick(venue=venue, venue_obj=fake, reader=lambda *_: bars, home=home, clock=lambda: clock) == 0


def _exits(fake: _FakeVenue) -> list[Decimal]:
    return [call.qty for call in fake.calls if call.method == "exit"]


def _enter_on_two_venues(home: Path, *, second_pack_id: str = "shared-pack") -> dict[str, _BtcUsdtVenue]:
    """Arm and enter on Binance (1000 USDT) and Bybit (400 USDT): unequal fills."""
    assert os.environ["KRELLBOT_HOME"] == str(home), "journal writes and ownership reads must share one home"
    venues = {
        "binance": _BtcUsdtVenue(venue="binance", pair=PAIR, initial_balances={"USDT": Decimal(1000)}),
        "bybit": _BtcUsdtVenue(venue="bybit", pair=PAIR, initial_balances={"USDT": Decimal(400)}),
    }
    _arm(home, pack_id="shared-pack", venue="binance", cash=Decimal(1000))
    _arm(home, pack_id=second_pack_id, venue="bybit", cash=Decimal(400))
    for venue, fake in venues.items():
        _tick(home, venue, fake, ENTRY_BARS, clock=1_000_000)
    return venues


def _identity(pack_id: str, venue: str, mode: str = "paper") -> OwnershipIdentity:
    return OwnershipIdentity(pack_id=pack_id, venue=venue, pair=PAIR, mode=mode)


def test_same_pack_on_two_venues_exits_only_each_venues_own_inventory(home):
    venues = _enter_on_two_venues(home)
    owned = {venue: fake._balances["BTC"] for venue, fake in venues.items()}
    assert owned["binance"] != owned["bybit"]
    for fake in venues.values():
        fake._balances["BTC"] += EXTERNAL

    _tick(home, "binance", venues["binance"], EXIT_BARS, clock=1_003_600)
    # Bybit's attribution must survive Binance's exit on journal evidence alone.
    assert project_ownership(home, _identity("shared-pack", "bybit")).attributable_qty == owned["bybit"]
    _tick(home, "bybit", venues["bybit"], EXIT_BARS, clock=1_003_600)

    assert _exits(venues["binance"]) == [owned["binance"]]
    assert _exits(venues["bybit"]) == [owned["bybit"]]
    assert venues["binance"]._balances["BTC"] == EXTERNAL
    assert venues["bybit"]._balances["BTC"] == EXTERNAL


def test_different_packs_on_two_venues_keep_their_own_inventory(home):
    venues = _enter_on_two_venues(home, second_pack_id="other-pack")
    owned = {venue: fake._balances["BTC"] for venue, fake in venues.items()}
    for fake in venues.values():
        fake._balances["BTC"] += EXTERNAL

    for venue, fake in venues.items():
        _tick(home, venue, fake, EXIT_BARS, clock=1_003_600)

    assert _exits(venues["binance"]) == [owned["binance"]]
    assert _exits(venues["bybit"]) == [owned["bybit"]]
    assert all(fake._balances["BTC"] == EXTERNAL for fake in venues.values())


_CHILD_EXIT = """
import json, os, sys
from decimal import Decimal
from pathlib import Path
from krellbot import run
from tests.test_strategy_ownership_is_scoped_to_venue import EXIT_BARS, PAIR, _BtcUsdtVenue
venue, btc = sys.argv[1], Decimal(sys.argv[2])
fake = _BtcUsdtVenue(venue=venue, pair=PAIR, initial_balances={"BTC": btc, "USDT": Decimal(0)})
home = Path(os.environ["KRELLBOT_HOME"])
assert run.tick(venue=venue, venue_obj=fake, reader=lambda *_: EXIT_BARS, home=home, clock=lambda: 1_003_600) == 0
print(json.dumps([str(c.qty) for c in fake.calls if c.method == "exit"]))
"""


def _exit_in_fresh_process(home: Path, venue: str, btc: Decimal) -> list[Decimal]:
    # Import this checkout's engine in the child, not whatever is installed.
    paths = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    env = {**os.environ, "KRELLBOT_HOME": str(home), "PYTHONPATH": paths}
    out = subprocess.run(
        [sys.executable, "-c", _CHILD_EXIT, venue, str(btc)],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [Decimal(q) for q in json.loads(out.stdout.strip().splitlines()[-1])]


def test_attribution_survives_a_fresh_process_restart(home):
    venues = _enter_on_two_venues(home)
    owned = {venue: fake._balances["BTC"] for venue, fake in venues.items()}

    assert _exit_in_fresh_process(home, "binance", owned["binance"] + EXTERNAL) == [owned["binance"]]
    assert _exit_in_fresh_process(home, "bybit", owned["bybit"] + EXTERNAL) == [owned["bybit"]]


def test_attribution_survives_disarm_and_rearm(home):
    venues = _enter_on_two_venues(home)
    owned = venues["binance"]._balances["BTC"]
    assert PaperService(home).disarm(venue="binance", pair=PAIR).ok
    _arm(home, pack_id="shared-pack", venue="binance", cash=Decimal(1000))
    venues["binance"]._balances["BTC"] += EXTERNAL

    _tick(home, "binance", venues["binance"], EXIT_BARS, clock=1_003_600)

    assert _exits(venues["binance"]) == [owned]
    assert venues["binance"]._balances["BTC"] == EXTERNAL


def test_pack_version_change_keeps_inventory_manageable(home):
    venues = _enter_on_two_venues(home)
    owned = venues["binance"]._balances["BTC"]
    assert PaperService(home).disarm(venue="binance", pair=PAIR).ok
    _arm(home, pack_id="shared-pack", venue="binance", cash=Decimal(1000), version="1.1.0")
    venues["binance"]._balances["BTC"] += EXTERNAL

    _tick(home, "binance", venues["binance"], EXIT_BARS, clock=1_003_600)

    assert _exits(venues["binance"]) == [owned]
    assert venues["binance"]._balances["BTC"] == EXTERNAL


# ---- projection contract ----------------------------------------------------


def _record(*, pack: str = "p", venue: str | None = "kraken", **detail) -> None:
    rec: dict = {"ts": 1, "kind": "tick", "pack": pack, "bar_ts": 0, "detail": detail}
    if venue is not None:
        rec["venue"] = venue
        kb_journal.append(rec)
        return
    # journal.append now requires a venue; a venue-less row can only be a
    # record written by an older format, so put it on disk as one.
    path = Path(os.environ["KRELLBOT_HOME"]) / "journal" / "1970-01.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _kraken(mode: str = "paper") -> OwnershipIdentity:
    return OwnershipIdentity(pack_id="p", venue="kraken", pair="SUIUSD", mode=mode)


def test_paper_history_is_not_live_ownership(home):
    _record(pair="SUIUSD", mode="paper", entry_qty="4")
    _record(pair="SUIUSD", mode="live", entry_qty="1.5")

    assert project_ownership(home, _kraken("paper")).attributable_qty == Decimal(4)
    assert project_ownership(home, _kraken("live")).attributable_qty == Decimal("1.5")
    assert not project_ownership(home, _kraken("live")).ambiguous


def test_paper_trace_marker_attributes_records_written_before_mode_was_journaled(home):
    _record(pair="SUIUSD", entry_qty="4", decision_trace_bars=3)

    paper = project_ownership(home, _kraken("paper"))
    assert (paper.attributable_qty, paper.ambiguous) == (Decimal(4), False)
    live = project_ownership(home, _kraken("live"))
    assert (live.attributable_qty, live.ambiguous) == (Decimal(0), False)


@pytest.mark.parametrize(
    "legacy",
    [
        {"venue": None, "pair": "SUIUSD", "mode": "paper", "entry_qty": "3"},
        {"pair": "SUIUSD", "entry_qty": "3"},
        {"mode": "paper", "entry_qty": "3"},
        {"pair": "SUIUSD", "mode": "paper", "entry_qty": "not-a-number"},
    ],
    ids=["no-venue", "no-mode", "no-pair", "unreadable-quantity"],
)
def test_unattributable_history_is_never_credited_and_is_reported(home, legacy):
    _record(pair="SUIUSD", mode="paper", entry_qty="4")
    _record(**legacy)

    projection = project_ownership(home, _kraken("paper"))

    assert projection.attributable_qty == Decimal(4)
    assert projection.ambiguous


def test_unattributable_decrease_cannot_make_room_to_sell_external_holdings(home):
    _record(pair="SUIUSD", mode="paper", entry_qty="4")
    _record(venue=None, pair="SUIUSD", mode="paper", exit_qty="4")

    projection = project_ownership(home, _kraken("paper"))

    assert projection.attributable_qty == Decimal(0)
    assert projection.ambiguous


def test_other_identities_neither_add_ownership_nor_ambiguity(home):
    _record(pair="SUIUSD", mode="paper", entry_qty="4")
    _record(pack="other", venue=None, pair="SUIUSD", entry_qty="9")
    _record(venue="coinbase", pair="SUIUSD", entry_qty="9")
    _record(pair="BTCUSD", entry_qty="9")
    _record(pair="SUIUSD", mode="live", entry_qty="9")
    _record(pair="SUIUSD", reason="no_candles")

    projection = project_ownership(home, _kraken("paper"))

    assert (projection.attributable_qty, projection.ambiguous) == (Decimal(4), False)


def test_flatten_authorization_uses_the_same_identity(home):
    _record(pair="SUIUSD", mode="paper", entry_qty="5")
    _record(venue="coinbase", pair="SUIUSD", mode="paper", entry_qty="7")

    def flatten(qty: str, **identity) -> str:
        return reservations.flatten_result(home, pack_id="p", pair="SUIUSD", qty=Decimal(qty), **identity)

    assert flatten("5", venue="kraken", mode="paper") == reservations.CODE_FLATTEN
    assert flatten("6", venue="kraken", mode="paper") == reservations.CODE_FLATTEN_NOT_OWNED
    assert flatten("5", venue="kraken", mode="live") == reservations.CODE_FLATTEN_NOT_OWNED

    _record(venue=None, pair="SUIUSD", mode="paper", entry_qty="1")
    assert flatten("5", venue="kraken", mode="paper") == reservations.CODE_FLATTEN_NOT_OWNED


# ---- tick behavior with ambiguous history -----------------------------------


def test_ambiguous_history_holds_new_entries_and_is_surfaced(home):
    fake = _BtcUsdtVenue(venue="binance", pair=PAIR, initial_balances={"USDT": Decimal(1000)})
    _arm(home, pack_id="shared-pack", venue="binance", cash=Decimal(1000))
    _record(pack="shared-pack", venue=None, pair=PAIR, mode="paper", entry_qty="2")

    _tick(home, "binance", fake, ENTRY_BARS, clock=1_000_000)

    assert [call for call in fake.calls if call.method == "entry"] == []
    ticks = [r for r in kb_journal.read_records_lenient(home) if r.get("kind") == "tick" and r.get("bar_ts")]
    assert ticks[-1]["detail"]["intent_refused"] == "ownership_ambiguous"
    assert any(alert.code == "ownership_ambiguous" for alert in kb_alerts.collect(home, now=1_000_000))


def test_ambiguous_history_still_exits_attributable_inventory_only(home):
    fake = _BtcUsdtVenue(venue="binance", pair=PAIR, initial_balances={"USDT": Decimal(1000)})
    _arm(home, pack_id="shared-pack", venue="binance", cash=Decimal(1000))
    _tick(home, "binance", fake, ENTRY_BARS, clock=1_000_000)
    owned = fake._balances["BTC"]
    _record(pack="shared-pack", venue=None, pair=PAIR, mode="paper", entry_qty="2")
    fake._balances["BTC"] += EXTERNAL

    _tick(home, "binance", fake, EXIT_BARS, clock=1_003_600)

    assert _exits(fake) == [owned]
    assert fake._balances["BTC"] == EXTERNAL
