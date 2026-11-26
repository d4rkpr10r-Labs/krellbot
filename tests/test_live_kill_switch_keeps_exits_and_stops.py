"""The live kill switch stops new entries and keeps exits and stops running.

With the kill switch engaged, a live tick behaves as a paper tick does (D6):

* no entry is sent, and the tick record carries ``kill_switch: true``;
* a signal exit is sent;
* a missing protective stop is re-placed through ``place_stop``;
* ``raise_stop`` runs;
* unresolved orders are reconciled from venue evidence.

Each of those is pinned for paper and live side by side, so paper is
unchanged and live matches it. The scheduler's venue setup (``cmd_tick``,
scheduled and ``--offline-candles``) builds the live venue under the kill
switch and sends the exit and nothing else.

What stays refused:

* The restore fence sends nothing, and touches no venue or transport, until
  the operator releases the kill switch. A later operator kill, or a cleared
  entries pause, does not lift it.
* Live arm, web live arm, promotion and live preflight stay refused with
  ``kill_switch_engaged``, as does a paper arm.
* The kill switch never stands in for the rest of the gate: a killed live
  tick without ``KRELLBOT_ENABLE_LIVE`` or an operator grant sends nothing,
  and the shipped paper-only release refuses it before any venue call.

Runs on fake venues and fake transports with the release gate patched
in-process (``live_capable_release``); ``PAPER_ONLY_RELEASE`` stays True in
source.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fakes.fake_kraken import FakeKrakenTransport
from fakes.live_grant import write_grant

from krellbot import journal as kb_journal
from krellbot import release_policy, run
from krellbot.application import live_gate
from krellbot.config import ArmedPack, Config, load_config, save_config
from krellbot.execution.obligations import read_obligations
from krellbot.pack.model import Candle
from krellbot.storage.database import OperationalStore
from krellbot.storage.outbox import Outbox
from krellbot.venues.base import UNKNOWN_OUTCOME, Balance, KeyPerms, OpenOrder, OrderOutcome, OrderRef, PairRules, Truth

pytestmark = pytest.mark.usefixtures("live_capable_release")

VENUE = "kraken"
PAIR = "SUIUSD"
BASE = "SUI"
PACK = "live-kill"
H = 3_600_000
ENTRY_BAR = 2 * H
OWNED = Decimal(5)
ENTRY_STOP = Decimal(6)  # the stop the seeded entry rested: 12 * (1 - 50/100)
KILLED_AT = 1_700_000_000

KRAKEN_KEY = "FAKE_KEY_FOR_KRAKEN_TESTS"
KRAKEN_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
TRADE_ONLY = {"permissions": ["query-funds", "query-open-trades", "modify-trades", "close-trades"]}

# The pack enters when close crosses above sma2 and exits when it crosses below.
ENTRY = ("10", "10", "12")  # entry signal on bar 2 (stop 6)
HOLD = (*ENTRY, "13")  # bar 3 neither enters nor exits
EXIT = (*HOLD, "10")  # exit signal on bar 4
REENTRY = (*ENTRY, "10", "13")  # entry signal on bar 4 (stop 6.5) while the arm still owns

MODES = pytest.mark.parametrize("mode", ["paper", "live"])


def _candle(i: int, close: str) -> Candle:
    c = Decimal(close)
    return Candle(ts_ms=i * H, open=c, high=c + 1, low=c - 1, close=c, volume=Decimal(100))


def _bars(closes: tuple[str, ...]) -> list[Candle]:
    return [_candle(i, close) for i, close in enumerate(closes)]


def _bar_ts(closes: tuple[str, ...]) -> int:
    return (len(closes) - 1) * H


def _coid(intent: str, bar_ts: int) -> str:
    return run.coid_for(pack_id=PACK, pack_version="1.0.0", venue=VENUE, pair=PAIR, bar_ts=bar_ts, intent=intent)


def _write_pack(directory: Path) -> Path:
    pack = {
        "schema_version": 1,
        "id": PACK,
        "version": "1.0.0",
        "label": "Live kill switch",
        "author": "krellbot tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 50}},
        "markets": [{"venue": VENUE, "pair": PAIR}],
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{PACK}.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    return path


def _arm(home: Path, *, mode: str, pack_dir: Path | None = None) -> None:
    armed = ArmedPack(
        pack_path=str(_write_pack(pack_dir or home)),
        pack_sha256="0" * 64,
        pack_id=PACK,
        pack_version="1.0.0",
        venue=VENUE,
        pair=PAIR,
        cap=Decimal(100),
        stop=Decimal(5),
        mode=mode,
        starting_cash=Decimal(1000) if mode == "paper" else None,
        requires_license=False,
        armed_at_ts=1,
    )
    save_config(home, Config(armed=[armed]))


def _seed_owned(home: Path, *, mode: str) -> None:
    """The arm's entry on ENTRY_BAR, journaled as the engine journals it."""
    kb_journal.append(
        {
            "ts": 1,
            "kind": "tick",
            "venue": VENUE,
            "pack": PACK,
            "bar_ts": ENTRY_BAR,
            "detail": {
                "pair": PAIR,
                "mode": mode,
                "reason": "entry",
                "entry_qty": str(OWNED),
                "exit_qty": "0",
                "stop_qty": "0",
                "entry_coid": _coid("entry", ENTRY_BAR),
            },
        }
    )


def _authorize_live(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    write_grant(home, venue=VENUE, pair=PAIR)


def _kill(home: Path, *, reason: str = "operator halt") -> None:
    live_gate.engage_kill(home, reason=reason, now=KILLED_AT)


def _tick_records(home: Path) -> list[dict]:
    return [r for r in kb_journal.read_records_lenient(home) if r.get("kind") == "tick" and r.get("pack") == PACK]


class _Venue:
    """In-memory venue that records every call the tick makes on it.

    ``base`` is the account's SUI balance. ``resting_stop`` rests one sell stop
    on the pair at that price. ``outcomes`` answers ``order_outcome`` by coid.
    """

    def __init__(
        self,
        *,
        base: Decimal = Decimal(0),
        resting_stop: Decimal | None = None,
        outcomes: dict[str, OrderOutcome] | None = None,
    ) -> None:
        self.base = base
        self.orders = (
            []
            if resting_stop is None
            else [OpenOrder(id="OSTOP1", coid="stop-1", pair=PAIR, side="sell", qty=base, stop_price=resting_stop)]
        )
        self.outcomes = dict(outcomes or {})
        self.calls: list[tuple] = []

    def sent(self) -> list[tuple]:
        """Calls that send or change an order at the venue."""
        return [c for c in self.calls if c[0] in {"entry", "exit", "stop", "cancel_stops", "raise_stop"}]

    def rules(self, pair: str) -> PairRules:
        self.calls.append(("rules",))
        return PairRules(ordermin=Decimal("0.01"), costmin=Decimal("0.5"), lot_decimals=8, price_decimals=5)

    def snapshot(self) -> Truth:
        self.calls.append(("snapshot",))
        balances = [Balance(asset="USD", free=Decimal(1000))]
        if self.base > 0:
            balances.append(Balance(asset=BASE, free=self.base))
        return Truth(balances=balances, open_orders=list(self.orders), recent_fills=[])

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        self.calls.append(("entry", coid, qty, stop))
        return OrderRef(id=coid, coid=coid, pair=pair, side="buy", qty=qty, filled_qty=qty, stop_price=stop)

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef:
        self.calls.append(("exit", coid, qty))
        return OrderRef(id=coid, coid=coid, pair=pair, side="sell", qty=qty, filled_qty=qty)

    def place_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef:
        self.calls.append(("stop", coid, qty, stop))
        return OrderRef(id=coid, coid=coid, pair=pair, side="sell", qty=qty, filled_qty=Decimal(0), stop_price=stop)

    def cancel_stops(self, pair: str) -> None:
        self.calls.append(("cancel_stops",))

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        self.calls.append(("raise_stop", new_stop))

    def order_by_coid(self, coid: str) -> OpenOrder | None:
        self.calls.append(("order_by_coid", coid))
        return None

    def order_outcome(self, coid: str, *, pair: str) -> OrderOutcome:
        self.calls.append(("order_outcome", coid))
        return self.outcomes.get(coid, UNKNOWN_OUTCOME)

    def check_key(self) -> KeyPerms:
        self.calls.append(("check_key",))
        return KeyPerms(can_trade=True, can_withdraw=False)


def _tick(home: Path, venue: _Venue, closes: tuple[str, ...]) -> int:
    return run.tick(venue=VENUE, venue_obj=venue, reader=lambda v, p: _bars(closes), home=home)


# ---- a killed tick, paper and live side by side ----------------------------------------


@MODES
def test_killed_tick_sends_no_entry(home, monkeypatch, mode):
    _authorize_live(home, monkeypatch)
    _kill(home)
    _arm(home, mode=mode)
    venue = _Venue()

    assert _tick(home, venue, ENTRY) == 0

    assert venue.sent() == []
    [record] = [r for r in _tick_records(home) if r["bar_ts"] == ENTRY_BAR]
    assert record["detail"]["reason"] == "entry", "the pack signalled an entry on this bar"
    assert record["detail"]["entry_qty"] == "0"
    assert record["detail"]["kill_switch"] is True
    assert record["detail"]["entries_paused"] is True


@MODES
def test_killed_tick_sends_a_signal_exit(home, monkeypatch, mode):
    _authorize_live(home, monkeypatch)
    _kill(home)
    _arm(home, mode=mode)
    _seed_owned(home, mode=mode)
    venue = _Venue(base=OWNED, resting_stop=ENTRY_STOP)

    assert _tick(home, venue, EXIT) == 0

    assert ("cancel_stops",) in venue.sent()
    placed = [c for c in venue.sent() if c[0] in {"entry", "exit", "stop"}]
    assert placed == [("exit", _coid("exit", _bar_ts(EXIT)), OWNED)]


@MODES
def test_killed_tick_replaces_a_missing_stop(home, monkeypatch, mode):
    _authorize_live(home, monkeypatch)
    _kill(home)
    _arm(home, mode=mode)
    _seed_owned(home, mode=mode)
    venue = _Venue(base=OWNED)  # owned, and no stop rests for it

    assert _tick(home, venue, HOLD) == 0

    placed = [c for c in venue.sent() if c[0] in {"entry", "exit", "stop"}]
    assert [c[:3] for c in placed] == [("stop", _coid("stop", _bar_ts(HOLD)), OWNED)], "re-placed, not sold"
    assert placed[0][3] > 0


@MODES
def test_killed_tick_raises_the_stop(home, monkeypatch, mode):
    _authorize_live(home, monkeypatch)
    _kill(home)
    _arm(home, mode=mode)
    _seed_owned(home, mode=mode)
    venue = _Venue(base=OWNED, resting_stop=ENTRY_STOP)

    assert _tick(home, venue, REENTRY) == 0

    assert venue.sent() == [("raise_stop", Decimal("6.5"))]


@MODES
def test_killed_tick_reconciles_an_unresolved_order(home, monkeypatch, mode):
    _authorize_live(home, monkeypatch)
    _kill(home)
    _arm(home, mode=mode)
    # An entry committed on an earlier bar whose reply was lost: its outcome
    # is unknown until the venue's own record says what happened.
    lost = _coid("entry", H)
    body = json.dumps(
        {"kind": "entry", "qty": "5", "stop": "6", "pair": PAIR, "pack_id": PACK, "pack_version": "1.0.0", "bar_ts": H},
        separators=(",", ":"),
        sort_keys=True,
    )
    store = OperationalStore(home / "ops.sqlite")
    store.connect()

    def reply_lost(_coid: str, _body: str) -> None:
        raise OSError("connection reset before the reply")

    with pytest.raises(OSError):
        Outbox(store).dispatch(lost, body, reply_lost, mode=mode, venue=VENUE)
    assert [o.coid for o in read_obligations(home).unresolved] == [lost]
    venue = _Venue(outcomes={lost: OrderOutcome(status="absent", authoritative=True)})

    assert _tick(home, venue, HOLD) == 0

    assert ("order_outcome", lost) in venue.calls
    assert read_obligations(home).unresolved == ()
    assert venue.sent() == [], "reconcile never resubmits"


# ---- the scheduler's venue setup -------------------------------------------------------


def _store_kraken_key(keyring) -> None:
    from krellbot import sanitize

    sanitize.register_secret(KRAKEN_KEY, KRAKEN_SECRET)
    keyring.set_password("krellbot:kraken", "key", KRAKEN_KEY)
    keyring.set_password("krellbot:kraken", "secret", KRAKEN_SECRET)


def _kraken_account() -> FakeKrakenTransport:
    """A Kraken account holding the arm's SUI with no resting stop."""
    return FakeKrakenTransport(
        responses=[{"error": [], "result": {"descr": {"order": "sell 5 SUIUSD"}, "txid": ["OEXIT1-AAAAA-BBBBBB"]}}],
        balances={"USD": "1000", BASE: str(OWNED)},
        asset_pairs={PAIR: {"ordermin": "0.01", "costmin": "0.5", "lot_decimals": 5, "pair_decimals": 4}},
        api_key_info=TRADE_ONLY,
        ticker_last="10",
    )


def _cmd_tick_args(home: Path, *, offline: bool, closes: tuple[str, ...]) -> list[str]:
    if not offline:
        return ["--venue", VENUE]
    csv = home / "candles.csv"
    rows = [f"{c.ts_ms},{c.open},{c.high},{c.low},{c.close},{c.volume}" for c in _bars(closes)]
    csv.write_text("ts_ms,open,high,low,close,volume\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return ["--venue", VENUE, "--offline-candles", str(csv)]


@pytest.mark.parametrize("offline", [False, True], ids=["scheduled", "offline"])
def test_scheduled_killed_live_tick_sends_the_exit_and_no_buy(home, fresh_keyring, monkeypatch, capsys, offline):
    from krellbot.cli import cmd_tick

    _authorize_live(home, monkeypatch)
    _kill(home)
    _arm(home, mode="live")
    _seed_owned(home, mode="live")
    _store_kraken_key(fresh_keyring)
    monkeypatch.setattr("krellbot.venues.kraken.time.sleep", lambda _seconds: None)
    transport = _kraken_account()

    rc = cmd_tick(
        _cmd_tick_args(home, offline=offline, closes=EXIT),
        fetch=lambda v, p, tf, t: _bars(EXIT),
        transport=transport,
    )

    assert rc == 0, capsys.readouterr().out
    adds = [c.form for c in transport.calls if c.url.endswith("/AddOrder")]
    assert [(f["type"], f["ordertype"], f["volume"], f["cl_ord_id"]) for f in adds] == [
        ("sell", "market", "5.00000", _coid("exit", _bar_ts(EXIT)))
    ]


# ---- what stays refused ----------------------------------------------------------------


def _restored_home(home: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A live arm backed up, then restored into a new home that owns OWNED.

    The restore pauses every live arm's entries and engages the kill switch
    with its ``home restored`` reason: the restore fence.
    """
    from krellbot.storage import home_backup

    _arm(home, mode="live", pack_dir=home / "packs")
    OperationalStore(home / "ops.sqlite").connect()
    archive = tmp_path_factory.mktemp("backup") / "home-backup.tar.gz"
    home_backup.create(home, archive, now=KILLED_AT)
    restored = tmp_path_factory.mktemp("restore") / "home"
    home_backup.restore(archive, restored)
    monkeypatch.setenv("KRELLBOT_HOME", str(restored))
    _authorize_live(restored, monkeypatch)
    _seed_owned(restored, mode="live")
    assert live_gate.kill_state(restored).engaged is True
    assert [a.entries_paused for a in load_config(restored).armed] == [True]
    return restored


def _fenced(home: Path) -> bool:
    return any(r["detail"].get("reason") == "live_fenced" for r in _tick_records(home))


def test_restore_fence_sends_nothing_until_the_kill_switch_is_released(home, tmp_path_factory, monkeypatch):
    restored = _restored_home(home, tmp_path_factory, monkeypatch)
    venue = _Venue(base=OWNED)

    assert _tick(restored, venue, EXIT) == 0

    assert venue.calls == [], "the restore fence touches no venue"
    assert _fenced(restored)

    live_gate.release_kill(restored)
    assert _tick(restored, venue, EXIT) == 0
    placed = [c for c in venue.sent() if c[0] in {"entry", "exit", "stop"}]
    assert placed == [("exit", _coid("exit", _bar_ts(EXIT)), OWNED)]


@pytest.mark.parametrize("change", ["operator_kill", "pause_cleared"])
def test_restore_fence_holds_after_a_new_kill_or_a_cleared_pause(home, tmp_path_factory, monkeypatch, change):
    restored = _restored_home(home, tmp_path_factory, monkeypatch)
    if change == "operator_kill":
        _kill(restored, reason="operator halt")  # the restored arm's entries stay paused
    else:
        config = load_config(restored)
        config.armed[0].entries_paused = False  # the restore kill stays engaged
        save_config(restored, config)
    venue = _Venue(base=OWNED)

    assert _tick(restored, venue, EXIT) == 0

    assert venue.calls == []
    assert _fenced(restored)


def test_scheduled_tick_behind_the_restore_fence_touches_no_transport(
    home, tmp_path_factory, fresh_keyring, monkeypatch, capsys
):
    from krellbot.cli import cmd_tick

    _restored_home(home, tmp_path_factory, monkeypatch)
    _store_kraken_key(fresh_keyring)
    transport = _kraken_account()

    rc = cmd_tick(["--venue", VENUE], fetch=lambda v, p, tf, t: _bars(EXIT), transport=transport)

    assert rc == 1
    assert "live refused: kill_switch_engaged" in capsys.readouterr().out
    assert transport.calls == [], "no key check and no order"


def test_kill_switch_still_refuses_arms_and_promotion(home, monkeypatch, capsys):
    from krellbot.application.paper import PaperService

    _authorize_live(home, monkeypatch)
    _kill(home)
    pack_path = _write_pack(home)

    def key_check(_venue: str) -> KeyPerms:
        raise AssertionError("the kill switch refuses before any key check")

    rc = run.arm_pack(pack_path, venue=VENUE, mode="live", key_check=key_check, home=home, skip_confirm=True)
    assert rc == 1
    assert "live refused: kill_switch_engaged" in capsys.readouterr().out
    for result in (
        live_gate.check_live_send(home, venue=VENUE, pair=PAIR),
        live_gate.check_web_live_arm(home, venue=VENUE, pair=PAIR),
        live_gate.check_promotion(home, venue=VENUE, pair=PAIR, revision_id="rev-1"),
    ):
        assert (result.ok, result.code) == (False, live_gate.CODE_KILL_SWITCH)
    paper = PaperService(home=home).arm(pack_path, venue=VENUE, mode="paper", paper_balance=Decimal(1000))
    assert (paper.ok, paper.code) == (False, "kill_switch_engaged")
    assert not (home / "config.json").exists()


@pytest.mark.parametrize("missing", ["env", "grant"])
def test_killed_live_tick_still_needs_env_and_grant(home, monkeypatch, capsys, missing):
    if missing == "env":
        monkeypatch.delenv("KRELLBOT_ENABLE_LIVE", raising=False)
        write_grant(home, venue=VENUE, pair=PAIR)
    else:
        monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    _kill(home)
    _arm(home, mode="live")
    _seed_owned(home, mode="live")
    venue = _Venue(base=OWNED)

    assert _tick(home, venue, EXIT) == 1

    code = live_gate.CODE_LIVE_DISABLED if missing == "env" else live_gate.CODE_LIVE_NOT_AUTHORIZED
    assert f"live refused: {code}" in capsys.readouterr().out
    assert venue.calls == []


def test_shipped_paper_only_release_still_refuses_a_killed_live_tick(home, fresh_keyring, monkeypatch, capsys):
    from krellbot.cli import PAPER_ONLY_TICK_REFUSED, cmd_tick

    monkeypatch.setattr(release_policy, "PAPER_ONLY_RELEASE", True)  # the shipped build
    _authorize_live(home, monkeypatch)
    _kill(home)
    _arm(home, mode="live")
    _seed_owned(home, mode="live")
    _store_kraken_key(fresh_keyring)
    transport = _kraken_account()

    assert cmd_tick(["--venue", VENUE], fetch=lambda v, p, tf, t: _bars(EXIT), transport=transport) == 1
    assert PAPER_ONLY_TICK_REFUSED in capsys.readouterr().out
    assert transport.calls == []

    venue = _Venue(base=OWNED)
    assert _tick(home, venue, EXIT) == 1
    assert f"live refused: {live_gate.CODE_PAPER_ONLY_RELEASE}" in capsys.readouterr().out
    assert venue.calls == []


# ---- live preflight over the loopback API ----------------------------------------------

PORT = 8080


def _asgi(app, path: str, payload: dict, headers: list[tuple[str, str]]) -> tuple[int, bytes, list[str]]:
    body = json.dumps(payload).encode("utf-8")
    raw = [("host", f"127.0.0.1:{PORT}"), ("content-type", "application/json"), ("content-length", str(len(body)))]
    received = {"done": False}
    captured: dict = {"status": 500, "headers": [], "body": b""}

    async def receive() -> dict:
        if received["done"]:
            return {"type": "http.disconnect"}
        received["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            captured["status"] = int(message["status"])
            captured["headers"] = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "server": ("127.0.0.1", PORT),
        "client": ("127.0.0.1", 12345),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in [*raw, *headers]],
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    cookies = [v.decode("latin-1") for k, v in captured["headers"] if k.decode("latin-1").lower() == "set-cookie"]
    return captured["status"], captured["body"], cookies


def test_kill_switch_still_refuses_live_preflight(home, monkeypatch):
    from krellbot.api.app import create_app

    _authorize_live(home, monkeypatch)
    _kill(home)
    token = "boot-live-kill-preflight"
    app = create_app(home=home, port=PORT, bootstrap_token=token)
    transport = MagicMock()
    app.state.krellbot.live_transport = transport
    origin = ("Origin", f"http://127.0.0.1:{PORT}")
    status, body, cookies = _asgi(app, "/api/v1/session/bootstrap", {"token": token}, [origin])
    assert status == 200, body
    session = next(c for c in cookies if c.startswith("krellbot_session=")).split(";", 1)[0]
    auth = [origin, ("X-Krellbot-CSRF", json.loads(body)["csrf_token"]), ("Cookie", session)]

    status, body, _cookies = _asgi(
        app,
        "/api/v1/commands",
        {
            "schema_version": "1",
            "command": "live.preflight",
            "payload": {"account_id": VENUE, "revision_id": "rev-1", "mode": "sandbox", "venue": VENUE},
        },
        auth,
    )

    assert status == 200, body
    decoded = json.loads(body)
    assert (decoded["code"], decoded["ok"], decoded["effect"]) == ("kill_switch_engaged", False, "refused")
    assert transport.mock_calls == []
