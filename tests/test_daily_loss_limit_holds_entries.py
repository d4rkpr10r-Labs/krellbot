"""An arm's optional daily loss limit holds new entries while exits and stops run.

The limit is a percent of the arm's starting cash. The Workstation sets it with
``risk.set_daily_loss_limit`` through ``POST /api/v1/commands``, the ordinary
state-change path (session, CSRF, loopback origin and host, and the tailnet
read-only gate). It is off by default, and an arm without it keeps its
``config.json`` bytes.

Each tick that acts on a bar persists, per arm, its Decimal equity and the UTC
day's opening equity in ``<home>/run/daily-loss-<venue>.json``:

    equity = starting cash
             + net cash of the ownership identity's attributed fills
               (a sell's notional x (1 - fee), less a buy's notional x (1 + fee),
               at the venue's taker fee)
             + the identity's attributed quantity x the latest close

Quantities come from the journal records attributed to the identity (pack,
venue, pair, mode), reconciled records included, never from requested sizes.
Prices come from the venue fills that name those records' orders; a buy the
venue's fills cannot name (live adapters link sell fills only) is priced at the
close of the bar its entry was decided on. External inventory and other
identities' fills never count.

When the day's loss (opening equity less the day's lowest equity) reaches the
limit, the tick refuses entries with ``daily_loss_limit`` until the next UTC
day. Exits, stop repair and stop fills continue. Equity that cannot be computed
holds entries with ``loss_limit_unavailable``.

Paper runs on the paper venue; live runs on fake transports with the release
gate patched in-process (``live_capable_release``). ``PAPER_ONLY_RELEASE``
stays True in source.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fakes.live_grant import write_grant

from krellbot import journal as kb_journal
from krellbot import run
from krellbot.config import ArmedPack, Config, config_path, load_config, save_config
from krellbot.pack.model import Candle
from krellbot.venues.base import PairRules
from krellbot.venues.paper import PaperVenue
from tests.test_live_exit_residual_stays_owned import _CoinbaseLive, _KrakenLive

PACK_ID = "loss-gate"
PAIR = "SUIUSD"
H = 3_600_000
DAY = 86_400
DAY0 = 1_699_920_000  # 2023-11-14T00:00:00Z
T0 = DAY0 + 3_600  # 01:00 UTC
NEXT_DAY = DAY0 + DAY + 3_600  # 01:00 UTC the day after
KRAKEN_FEE = Decimal(40) / Decimal(10_000)
RULES = PairRules(ordermin=Decimal("0.01"), costmin=Decimal("0.5"), lot_decimals=5, price_decimals=4)
STARTING_CASH = Decimal(1000)
COMMAND = "risk.set_daily_loss_limit"
ROUTE = "/api/v1/paper/daily-loss-limit"
TEST_PORT = 8080
BOOT_TOKEN = "boot-daily-loss-limit"


def _pack(venue: str = "kraken", pair: str = PAIR) -> dict:
    """Enters when the close crosses above 11 and exits when it crosses below 9."""
    return {
        "schema_version": 1,
        "id": PACK_ID,
        "version": "1.0.0",
        "label": "Loss gate",
        "author": "krellbot tests",
        "timeframe": "1h",
        "indicators": {},
        "entry": ["close", "crosses_above", 11],
        "exit": ["close", "crosses_below", 9],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 50}},
        "markets": [{"venue": venue, "pair": pair}],
    }


def _candle(index: int, close: str) -> Candle:
    c = Decimal(close)
    return Candle(ts_ms=index * H, open=c, high=c + 1, low=c - 1, close=c, volume=Decimal(100))


def _bars(*closes: str) -> list[Candle]:
    return [_candle(i, close) for i, close in enumerate(closes)]


def _arm(
    home: Path,
    *,
    limit: str | None = None,
    venue: str = "kraken",
    pair: str = PAIR,
    mode: str = "paper",
    starting_cash: Decimal | None = STARTING_CASH,
) -> ArmedPack:
    pack_path = home / f"{PACK_ID}.json"
    pack_path.write_text(json.dumps(_pack(venue, pair)), encoding="utf-8")
    armed = ArmedPack(
        pack_path=str(pack_path),
        pack_sha256="0" * 64,
        pack_id=PACK_ID,
        pack_version="1.0.0",
        venue=venue,
        pair=pair,
        cap=Decimal(100),
        stop=Decimal(50),
        mode=mode,
        starting_cash=starting_cash,
        requires_license=False,
        armed_at_ts=1,
    )
    if limit is not None:
        armed.daily_loss_limit_pct = Decimal(limit)
    save_config(home, Config(armed=[armed]))
    return armed


def _coid(bar: int, intent: str, *, venue: str = "kraken", pair: str = PAIR) -> str:
    return run.coid_for(pack_id=PACK_ID, pack_version="1.0.0", venue=venue, pair=pair, bar_ts=bar * H, intent=intent)


def _journal_entry(home: Path, *, bar: int, qty: Decimal, coid: str, mode: str = "paper", **where) -> None:
    """An entry journaled the way the engine journals one."""
    venue = where.get("venue", "kraken")
    kb_journal.append(
        {
            "ts": 1,
            "kind": "tick",
            "venue": venue,
            "pack": where.get("pack", PACK_ID),
            "bar_ts": bar * H,
            "detail": {
                "pair": where.get("pair", PAIR),
                "mode": mode,
                "reason": "entry",
                "entry_qty": str(qty),
                "exit_qty": "0",
                "stop_qty": "0",
                "entry_coid": coid,
            },
        }
    )


def _records(home: Path) -> list[dict]:
    return [
        r
        for r in kb_journal.read_records_lenient(home)
        if r.get("kind") == "tick" and r.get("pack") == PACK_ID and isinstance(r.get("detail"), dict)
    ]


def _bar_record(home: Path, bar: int) -> dict:
    [record] = [
        r["detail"]
        for r in _records(home)
        if r.get("bar_ts") == bar * H and r["detail"].get("reason") != run.RECONCILED_REASON
    ]
    return record


def _ledger(home: Path, venue: str = "kraken") -> dict:
    return json.loads((home / "run" / f"daily-loss-{venue}.json").read_text(encoding="utf-8"))


def _arm_state(home: Path, *, venue: str = "kraken", pair: str = PAIR, mode: str = "paper") -> dict:
    return _ledger(home, venue)["arms"][f"{PACK_ID}|{pair}|{mode}"]


def _day(ts: int) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).date().isoformat()


class _Paper:
    """The paper venue and the tick, driven bar by bar at an explicit clock."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.bars = _bars("10")
        self.now = T0
        self.venue = PaperVenue(
            "kraken",
            rules_provider=lambda _pair: RULES,
            candle_reader=lambda *_: self.bars,
            home=home,
            starting_cash=STARTING_CASH,
            clock=lambda: self.now,
        )

    def tick(self, *closes: str, at: int) -> int:
        self.bars = _bars(*closes)
        self.now = at
        return run.tick(
            venue="kraken", venue_obj=self.venue, reader=lambda *_: self.bars, home=self.home, clock=lambda: self.now
        )

    def fills(self) -> list[dict]:
        state = json.loads((self.home / "run" / "paper-kraken.json").read_text(encoding="utf-8"))
        return state["recent_fills"]

    def buys(self) -> list[dict]:
        return [f for f in self.fills() if f["side"] == "buy"]

    def open_orders(self) -> list[dict]:
        state = json.loads((self.home / "run" / "paper-kraken.json").read_text(encoding="utf-8"))
        return state["open_orders"]


def _expected_equity(fills: list[dict], *, coids: set[str], owned: Decimal, close: str, fee=KRAKEN_FEE) -> Decimal:
    """Starting cash plus the net cash of the named fills, plus what is owned at the close."""
    cash = STARTING_CASH
    for fill in fills:
        if fill["coid"] not in coids:
            continue
        notional = Decimal(str(fill["qty"])) * Decimal(str(fill["price"]))
        cash += notional * (1 - fee) if fill["side"] == "sell" else -notional * (1 + fee)
    return cash + owned * Decimal(close)


# ---- a minimal ASGI client -------------------------------------------------------------


def _call(app, *, method: str, path: str, body: bytes = b"", headers: list[tuple[str, str]], query: str = ""):
    sent = {"done": False}
    captured: dict = {"status": 500, "headers": [], "body": b""}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = int(message["status"])
            captured["headers"] = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "server": ("127.0.0.1", 0),
        "client": ("127.0.0.1", 12345),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    flat = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in captured["headers"]]
    return captured["status"], flat, captured["body"]


def _base_headers(body: bytes = b"") -> list[tuple[str, str]]:
    return [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]


def _app(home: Path, *, now: int = T0):
    from krellbot.api.app import create_app

    app = create_app(home=home, port=TEST_PORT, bootstrap_token=BOOT_TOKEN)
    app.state.krellbot.clock = lambda: now
    return app


def _session(app) -> dict[str, str]:
    body = json.dumps({"token": BOOT_TOKEN}).encode("utf-8")
    status, headers, raw = _call(
        app, method="POST", path="/api/v1/session/bootstrap", body=body, headers=_base_headers(body)
    )
    assert status == 200, raw
    cookie = next(v for k, v in headers if k == "set-cookie" and v.startswith("krellbot_session="))
    return {"cookie": cookie.split(";", 1)[0], "csrf": json.loads(raw)["csrf_token"]}


def _set_limit(app, session: dict[str, str] | None, payload: dict, *, csrf: bool = True) -> tuple[int, dict]:
    body = json.dumps({"schema_version": "1", "command": COMMAND, "payload": payload}).encode("utf-8")
    headers = _base_headers(body)
    if session is not None:
        headers.append(("Cookie", session["cookie"]))
        if csrf:
            headers.append(("X-Krellbot-CSRF", session["csrf"]))
    status, _headers, raw = _call(app, method="POST", path="/api/v1/commands", body=body, headers=headers)
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"raw": raw.decode("utf-8", "replace")}


def _read_limit(app, session: dict[str, str] | None, *, venue: str = "kraken", pair: str = PAIR) -> tuple[int, dict]:
    headers = [("Host", f"127.0.0.1:{TEST_PORT}")]
    if session is not None:
        headers.append(("Cookie", session["cookie"]))
    status, _headers, raw = _call(
        app, method="GET", path=ROUTE, headers=headers, query=urlencode({"venue": venue, "pair": pair})
    )
    return status, json.loads(raw)


# ---- off by default ------------------------------------------------------------------------


def test_the_limit_is_off_by_default_and_an_arm_without_it_keeps_its_config_bytes(home):
    _arm(home)
    before = config_path(home).read_bytes()

    assert load_config(home).armed[0].daily_loss_limit_pct is None
    assert b"daily_loss_limit" not in before
    save_config(home, load_config(home))
    assert config_path(home).read_bytes() == before  # a load and save round trip changes nothing

    paper = _Paper(home)
    assert paper.tick("10", "10.5", at=T0) == 0  # a tick that trades nothing
    assert config_path(home).read_bytes() == before
    assert _bar_record(home, 1)["intent_refused"] is None

    assert paper.tick("10", "10.5", "12", at=T0 + 3_600) == 0  # an entry: no limit holds it
    assert len(paper.buys()) == 1
    assert _bar_record(home, 2)["intent_refused"] is None
    assert b"daily_loss_limit" not in config_path(home).read_bytes()


# ---- set from the Workstation ----------------------------------------------------------------


def test_the_workstation_command_sets_and_clears_the_limit(home):
    _arm(home)
    before = config_path(home).read_bytes()
    app = _app(home)
    session = _session(app)

    status, body = _read_limit(app, session)
    assert status == 200, body
    assert (body["armed"], body["daily_loss_limit_pct"], body["code"]) == (True, None, None)

    status, body = _set_limit(app, session, {"venue": "kraken", "pair": PAIR, "pct": "5"})
    assert status == 200, body
    assert (body["ok"], body["code"], body["effect"], body["daily_loss_limit_pct"]) == (
        True,
        "daily_loss_limit_set",
        "changed",
        "5",
    )
    assert load_config(home).armed[0].daily_loss_limit_pct == Decimal(5)
    assert json.loads(config_path(home).read_bytes())["armed"][0]["daily_loss_limit_pct"] == "5"

    status, body = _read_limit(app, session)
    assert (body["daily_loss_limit_pct"], body["limit_amount"], body["starting_cash"]) == ("5", "50", "1000")

    status, body = _set_limit(app, session, {"venue": "kraken", "pair": PAIR, "pct": "5"})
    assert (body["ok"], body["effect"]) == (True, "unchanged")

    status, body = _set_limit(app, session, {"venue": "kraken", "pair": PAIR, "pct": None})
    assert status == 200, body
    assert (body["ok"], body["code"], body["effect"], body["daily_loss_limit_pct"]) == (
        True,
        "daily_loss_limit_off",
        "changed",
        None,
    )
    assert config_path(home).read_bytes() == before  # off again: the field is gone, byte for byte


@pytest.mark.parametrize("pct", ["0", "100.5", "-1", "abc", "1e1", ".5", "5 ", 5, 2.5, True, [], {}])
def test_the_command_refuses_a_limit_that_is_not_a_plain_percent(home, pct):
    _arm(home)
    before = config_path(home).read_bytes()
    app = _app(home)

    status, body = _set_limit(app, _session(app), {"venue": "kraken", "pair": PAIR, "pct": pct})

    assert status == 200, body
    assert (body["ok"], body["code"], body["effect"]) == (False, "invalid_loss_limit", "refused")
    assert config_path(home).read_bytes() == before


def test_the_command_refuses_unknown_arms_missing_starting_cash_and_bad_requests(home):
    _arm(home, starting_cash=None)
    before = config_path(home).read_bytes()
    app = _app(home)
    session = _session(app)

    def code(payload: dict) -> str:
        status, body = _set_limit(app, session, payload)
        assert status == 200, body
        assert body["ok"] is False
        return body["code"]

    assert code({"venue": "kraken", "pair": PAIR, "pct": "5"}) == "starting_cash_unknown"
    assert code({"venue": "kraken", "pair": "XBTUSD", "pct": "5"}) == "not_armed"
    assert code({"venue": "kraken", "pct": "5"}) == "bad_input"
    assert code({"venue": 1, "pair": PAIR, "pct": "5"}) == "bad_input"
    assert code({"venue": "kraken", "pair": PAIR}) == "bad_input"  # off is an explicit null
    assert config_path(home).read_bytes() == before

    # The ordinary state-change gate: no CSRF, no session.
    assert _set_limit(app, session, {"venue": "kraken", "pair": PAIR, "pct": "5"}, csrf=False)[0] == 403
    assert _set_limit(app, None, {"venue": "kraken", "pair": PAIR, "pct": "5"})[0] == 403
    assert _read_limit(app, None)[0] == 403
    assert config_path(home).read_bytes() == before


def test_the_limit_can_be_set_on_a_live_arm_with_reserved_cash(home):
    _arm(home, mode="live", starting_cash=Decimal(100))
    app = _app(home)

    status, body = _set_limit(app, _session(app), {"venue": "kraken", "pair": PAIR, "pct": "2.5"})

    assert status == 200, body
    assert (body["ok"], body["code"]) == (True, "daily_loss_limit_set")
    assert load_config(home).armed[0].daily_loss_limit_pct == Decimal("2.5")


ORIGIN = "https://workstation.example.ts.net"
TAILNET_HOST = "workstation.example.ts.net"
USER = "owner@example.com"


def _http(port: int, method: str, path: str, headers: dict[str, str], payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_a_read_only_tailnet_session_cannot_set_the_limit(home, tmp_path):
    from krellbot.api.serve import WorkstationServer

    _arm(home)
    before = config_path(home).read_bytes()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><head></head><body>Workstation</body></html>")
    server = WorkstationServer(home=home, dist_dir=dist, tailscale_origin=ORIGIN, tailscale_user=USER)
    server.start()
    try:
        remote = {"Host": TAILNET_HOST, "Tailscale-User-Login": USER}
        status, headers, _ = _http(server.bound_port, "POST", "/login", {**remote, "Origin": ORIGIN})
        assert status == 303
        authed = {**remote, "Cookie": headers["set-cookie"].split(";", 1)[0], "Origin": ORIGIN}
        status, _, raw = _http(server.bound_port, "GET", "/api/v1/session/csrf", authed)
        assert status == 200, raw
        authed["X-Krellbot-CSRF"] = json.loads(raw)["csrf_token"]
        authed["Content-Type"] = "application/json"

        status, _, raw = _http(
            server.bound_port,
            "POST",
            "/api/v1/commands",
            authed,
            {"command": COMMAND, "payload": {"venue": "kraken", "pair": PAIR, "pct": "5"}},
        )

        assert status == 403, raw
        assert json.loads(raw)["code"] == "remote_read_only"
        status, _, raw = _http(server.bound_port, "GET", f"{ROUTE}?venue=kraken&pair={PAIR}", authed)
        assert status == 200, raw  # reads keep working
    finally:
        server.stop()
    assert config_path(home).read_bytes() == before


# ---- equity from attributed fills -----------------------------------------------------------


def test_equity_counts_only_the_identitys_attributed_fills(home):
    _arm(home)
    paper = _Paper(home)
    paper.bars = _bars("10", "12")
    entry = _coid(1, "entry")
    paper.venue.place_entry_with_stop(entry, Decimal(5), Decimal(6), pair=PAIR)
    _journal_entry(home, bar=1, qty=Decimal(5), coid=entry)
    # External inventory on the same pair and wallet: never the strategy's.
    paper.venue.place_entry_with_stop("external-buy", Decimal(3), Decimal(1), pair=PAIR)
    # The same pack as another identity, and another pack on the same pair.
    _journal_entry(home, bar=1, qty=Decimal(7), coid="live-entry", mode="live")
    _journal_entry(home, bar=1, qty=Decimal(9), coid="other-entry", pack="other-pack")
    _journal_entry(home, bar=1, qty=Decimal(4), coid="coinbase-entry", venue="coinbase")

    assert paper.tick("10", "12", "11", at=T0) == 0  # no signal: nothing trades

    state = _arm_state(home)
    expected = _expected_equity(paper.fills(), coids={entry}, owned=Decimal(5), close="11")
    assert Decimal(state["equity"]) == expected
    assert expected == STARTING_CASH - Decimal(5) * Decimal("12.0060") * (1 + KRAKEN_FEE) + Decimal(55)
    assert Decimal(state["day_open"]) == expected
    assert state["day"] == _day(T0)


def test_the_utc_day_open_is_recorded_once_per_day(home):
    _arm(home)  # no limit: equity is still persisted for the arm
    paper = _Paper(home)

    assert paper.tick("10", "10.5", at=T0) == 0
    assert (_arm_state(home)["day"], Decimal(_arm_state(home)["day_open"])) == (_day(T0), STARTING_CASH)

    assert paper.tick("10", "10.5", "12", at=T0 + 3_600) == 0  # an entry, then the mark moves
    assert paper.tick("10", "10.5", "12", "11.5", at=T0 + 7_200) == 0
    entry = _coid(2, "entry")
    owned = Decimal(_bar_record(home, 2)["entry_qty"])
    state = _arm_state(home)
    assert Decimal(state["day_open"]) == STARTING_CASH  # recorded once, at the day's first tick
    assert Decimal(state["equity"]) == _expected_equity(paper.fills(), coids={entry}, owned=owned, close="11.5")
    assert state["equity_ts"] == T0 + 7_200

    assert paper.tick("10", "10.5", "12", "11.5", "11.4", at=NEXT_DAY) == 0
    state = _arm_state(home)
    next_open = _expected_equity(paper.fills(), coids={entry}, owned=owned, close="11.4")
    assert (state["day"], Decimal(state["day_open"]), Decimal(state["equity"])) == (
        _day(NEXT_DAY),
        next_open,
        next_open,
    )


# ---- at the limit -------------------------------------------------------------------------------


def test_at_the_limit_entries_stop_while_exits_and_stops_run_until_the_next_utc_day(home):
    _arm(home, limit="5")  # 5% of 1000: 50
    paper = _Paper(home)
    entry, exit_coid = _coid(1, "entry"), _coid(3, "exit")

    assert paper.tick("10", "12", at=T0) == 0  # entry
    owned = Decimal(_bar_record(home, 1)["entry_qty"])
    assert owned > 0 and [b["coid"] for b in paper.buys()] == [entry]
    assert Decimal(_arm_state(home)["day_open"]) == STARTING_CASH

    # The mark falls past the limit. The arm holds a position, so no entry is
    # attempted; its missing stop is still re-placed.
    paper.venue.cancel_stops(PAIR)
    assert paper.tick("10", "12", "11.2", at=T0 + 3_600) == 0
    state = _arm_state(home)
    equity = _expected_equity(paper.fills(), coids={entry}, owned=owned, close="11.2")
    assert Decimal(state["equity"]) == equity
    assert STARTING_CASH - equity >= 50
    assert [(o["coid"], Decimal(str(o["stop_price"]))) for o in paper.open_orders()] == [
        (_coid(2, "stop"), Decimal("5.6"))
    ]
    assert _bar_record(home, 2)["intent_refused"] is None
    app = _app(home, now=T0 + 3_600)
    status, body = _read_limit(app, _session(app))
    assert status == 200, body
    assert (body["code"], body["day_open"], body["limit_amount"]) == ("daily_loss_limit", "1000", "50")

    assert paper.tick("10", "12", "11.2", "8.5", at=T0 + 7_200) == 0  # the exit still sells
    [sold] = [f for f in paper.fills() if f["side"] == "sell"]
    assert (sold["coid"], Decimal(str(sold["qty"]))) == (exit_coid, owned)
    assert Decimal(_bar_record(home, 3)["exit_qty"]) == owned

    assert paper.tick("10", "12", "11.2", "8.5", "12", at=T0 + 10_800) == 0  # an entry signal, refused
    record = _bar_record(home, 4)
    assert (record["intent_refused"], record["entry_qty"]) == ("daily_loss_limit", "0")
    assert [b["coid"] for b in paper.buys()] == [entry]  # nothing bought
    realized = _expected_equity(paper.fills(), coids={entry, exit_coid}, owned=Decimal(0), close="12")
    state = _arm_state(home)
    assert (Decimal(state["day_open"]), Decimal(state["equity"])) == (STARTING_CASH, realized)

    # The next UTC day opens at the equity carried into it, and entries resume.
    assert paper.tick("10", "12", "11.2", "8.5", "12", "10", "12", at=NEXT_DAY) == 0
    record = _bar_record(home, 6)
    assert record["intent_refused"] is None
    assert [b["coid"] for b in paper.buys()] == [entry, _coid(6, "entry")]
    state = _arm_state(home)
    assert (state["day"], Decimal(state["day_open"])) == (_day(NEXT_DAY), realized)


def test_a_stop_fill_while_entries_are_held_is_journaled_and_counted_at_its_fill_price(home):
    _arm(home, limit="5")
    paper = _Paper(home)
    entry = _coid(1, "entry")
    assert paper.tick("10", "12", at=T0) == 0  # entry, stop resting at 6
    owned = Decimal(_bar_record(home, 1)["entry_qty"])
    assert paper.tick("10", "12", "11.2", at=T0 + 3_600) == 0  # past the limit, position held
    assert STARTING_CASH - Decimal(_arm_state(home)["equity"]) >= 50

    # The next bar trades through the stop: the paper venue fills it before the tick decides.
    assert paper.tick("10", "12", "11.2", "6.5", at=T0 + 7_200) == 0

    [stop_fill] = [f for f in paper.fills() if f.get("kind") == "stop"]
    record = _bar_record(home, 3)
    assert (Decimal(record["stop_qty"]), record["stop_fill_ids"]) == (owned, [stop_fill["id"]])
    expected = _expected_equity(paper.fills(), coids={entry, stop_fill["coid"]}, owned=Decimal(0), close="6.5")
    assert Decimal(_arm_state(home)["equity"]) == expected  # the fill's own price, not the close

    assert paper.tick("10", "12", "11.2", "6.5", "12", at=T0 + 10_800) == 0
    assert _bar_record(home, 4)["intent_refused"] == "daily_loss_limit"
    assert Decimal(_arm_state(home)["equity"]) == expected


def test_an_unreadable_state_file_holds_limited_entries_and_is_left_as_it_is(home):
    _arm(home, limit="5")
    (home / "run").mkdir(exist_ok=True)
    state_file = home / "run" / "daily-loss-kraken.json"
    state_file.write_bytes(b"{not json")
    paper = _Paper(home)

    assert paper.tick("10", "12", at=T0) == 0

    assert _bar_record(home, 1)["intent_refused"] == "loss_limit_unavailable"
    assert paper.buys() == []
    assert state_file.read_bytes() == b"{not json"


def test_raising_the_limit_above_the_days_worst_loss_lets_entries_resume_that_day(home):
    _arm(home, limit="5")
    paper = _Paper(home)
    assert paper.tick("10", "12", at=T0) == 0
    assert paper.tick("10", "12", "8.5", at=T0 + 3_600) == 0  # sold at a loss past 5%
    assert paper.tick("10", "12", "8.5", "12", at=T0 + 7_200) == 0
    assert _bar_record(home, 3)["intent_refused"] == "daily_loss_limit"

    app = _app(home, now=T0 + 7_200)
    status, body = _set_limit(app, _session(app), {"venue": "kraken", "pair": PAIR, "pct": "50"})
    assert (status, body["ok"]) == (200, True), body

    assert paper.tick("10", "12", "8.5", "12", "10", "12", at=T0 + 10_800) == 0
    assert _bar_record(home, 5)["intent_refused"] is None
    assert len(paper.buys()) == 2


def test_the_same_inputs_give_the_same_equity(home, tmp_path, monkeypatch):
    def scenario(where: Path) -> bytes:
        where.mkdir()
        monkeypatch.setenv("KRELLBOT_HOME", str(where))
        _arm(where, limit="5")
        paper = _Paper(where)
        assert paper.tick("10", "12", at=T0) == 0
        assert paper.tick("10", "12", "11.2", at=T0 + 3_600) == 0
        assert paper.tick("10", "12", "11.2", "8.5", at=T0 + 7_200) == 0
        return (where / "run" / "daily-loss-kraken.json").read_bytes()

    assert scenario(tmp_path / "first") == scenario(tmp_path / "second")


# ---- equity that cannot be computed ---------------------------------------------------------------


def test_a_sell_without_venue_evidence_of_its_price_holds_entries_with_loss_limit_unavailable(home):
    _arm(home, limit="5")
    paper = _Paper(home)
    paper.bars = _bars("10", "12")
    entry = _coid(1, "entry")
    paper.venue.place_entry_with_stop(entry, Decimal(5), Decimal(6), pair=PAIR)
    paper.venue.cancel_stops(PAIR)
    _journal_entry(home, bar=1, qty=Decimal(5), coid=entry)
    # A stop fill journaled for the identity whose fill the venue no longer shows.
    kb_journal.append(
        {
            "ts": 2,
            "kind": "tick",
            "venue": "kraken",
            "pack": PACK_ID,
            "bar_ts": 2 * H,
            "detail": {
                "pair": PAIR,
                "mode": "paper",
                "reason": "flat",
                "entry_qty": "0",
                "exit_qty": "0",
                "stop_qty": "5",
                "stop_fill_ids": ["fill-the-venue-does-not-show"],
            },
        }
    )

    assert paper.tick("10", "12", "10", "12", at=T0) == 0  # an entry signal while flat

    record = _bar_record(home, 3)
    assert (record["intent_refused"], record["entry_qty"]) == ("loss_limit_unavailable", "0")
    assert [b["coid"] for b in paper.buys()] == [entry]
    state = _arm_state(home)
    assert state["equity"] is None and state["unavailable"] == "fill_price_unknown"

    # The same history without a limit trades as before.
    config = load_config(home)
    config.armed[0].daily_loss_limit_pct = None
    save_config(home, config)
    assert paper.tick("10", "12", "10", "12", "10", "12", at=T0 + 3_600) == 0
    assert _bar_record(home, 5)["intent_refused"] is None
    assert len(paper.buys()) == 2


@pytest.mark.parametrize("case", ["no_starting_cash", "unreadable_limit"])
def test_a_limit_without_a_usable_base_holds_entries_with_loss_limit_unavailable(home, case):
    _arm(home, limit="5", starting_cash=None if case == "no_starting_cash" else STARTING_CASH)
    if case == "unreadable_limit":
        raw = json.loads(config_path(home).read_text(encoding="utf-8"))
        raw["armed"][0]["daily_loss_limit_pct"] = "five"
        config_path(home).write_text(json.dumps(raw), encoding="utf-8")
    paper = _Paper(home)

    assert paper.tick("10", "12", at=T0) == 0

    assert _bar_record(home, 1)["intent_refused"] == "loss_limit_unavailable"
    assert paper.buys() == []


# ---- live ---------------------------------------------------------------------------------------


LIVE = {"kraken": _KrakenLive, "coinbase": _CoinbaseLive}
LIVE_FEE = {"kraken": Decimal(40) / Decimal(10_000), "coinbase": Decimal(120) / Decimal(10_000)}
LIVE_FILL_PRICE = {"kraken": Decimal(6), "coinbase": Decimal("6.5")}  # what each fake exchange fills a sell at
LIVE_CASH = Decimal(100)


@pytest.fixture(params=["kraken", "coinbase"])
def live(request, home, monkeypatch, live_capable_release):
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    cls = LIVE[request.param]
    write_grant(home, venue=cls.venue, pair=cls.pair)
    _arm(home, venue=cls.venue, pair=cls.pair, mode="live", starting_cash=LIVE_CASH, limit="1")
    world = cls(home)
    # The strategy's entry on bar 1 (close 12), journaled as the engine journals it.
    _journal_entry(
        home,
        bar=1,
        qty=world.qty,
        coid=_coid(1, "entry", venue=cls.venue, pair=cls.pair),
        mode="live",
        venue=cls.venue,
        pair=cls.pair,
    )
    return world


def _live_tick(live, *closes: str, at: int) -> int:
    bars = _bars(*closes)
    return run.tick(
        venue=live.venue, venue_obj=live.make_venue(), reader=lambda *_: bars, home=live.home, clock=lambda: at
    )


@pytest.mark.parametrize("executed", ["all", "part"])
def test_a_live_reconciled_exit_counts_what_executed_at_its_fill_price(live, executed):
    fee = LIVE_FEE[live.venue]
    sold = live.qty if executed == "all" else live.qty * 2 / 5
    live.exchange.exit_executes = sold
    # The live entry's buy fill does not name its order, so it is priced at its bar's close.
    cost = live.qty * Decimal(12) * (1 + fee)

    assert _live_tick(live, "10", "12", "8.5", at=T0) == 0  # the exit is sent; its outcome is not known yet
    assert _bar_record(live.home, 2)["exit_qty"] == "0"
    state = _arm_state(live.home, venue=live.venue, pair=live.pair, mode="live")
    day_open = LIVE_CASH - cost + live.qty * Decimal("8.5")
    assert (Decimal(state["day_open"]), Decimal(state["equity"])) == (day_open, day_open)

    assert _live_tick(live, "10", "12", "8.5", "8", at=T0 + 3_600) == 0  # order_outcome settles it

    [reconciled] = [r["detail"] for r in _records(live.home) if r["detail"].get("reason") == run.RECONCILED_REASON]
    assert Decimal(reconciled["exit_qty"]) == sold  # what executed, never the requested size
    proceeds = sold * LIVE_FILL_PRICE[live.venue] * (1 - fee)
    state = _arm_state(live.home, venue=live.venue, pair=live.pair, mode="live")
    assert Decimal(state["equity"]) == LIVE_CASH - cost + proceeds + (live.qty - sold) * Decimal(8)
    assert Decimal(state["day_open"]) == day_open


def test_the_limit_holds_live_entries_and_lifts_at_the_next_utc_day(live):
    assert _live_tick(live, "10", "12", "8.5", at=T0) == 0
    assert _live_tick(live, "10", "12", "8.5", "8", at=T0 + 3_600) == 0  # the exit settles at a loss past 1%
    assert live.buys() == []

    assert _live_tick(live, "10", "12", "8.5", "8", "12", at=T0 + 7_200) == 0  # an entry signal, refused
    record = _bar_record(live.home, 4)
    assert (record["intent_refused"], record["entry_qty"]) == ("daily_loss_limit", "0")
    assert live.buys() == []  # nothing sent to the exchange
    assert live.base_balance() == 100  # external units untouched

    assert _live_tick(live, "10", "12", "8.5", "8", "12", "10", "12", at=NEXT_DAY) == 0
    assert _bar_record(live.home, 6)["intent_refused"] != "daily_loss_limit"
    assert len(live.buys()) == 1  # the next UTC day's entry is sent
