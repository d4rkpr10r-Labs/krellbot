"""What a paper run assumes, read from the values the paper engine uses.

``GET /api/v1/paper/assumptions`` serves the venue's taker fee and the
paper slippage, with the research fee-model label, and, for an armed paper
record, the pack's timeframe and stop rule and the run's starting cash.
It is a session-gated read: it never constructs a venue, never writes and
never reaches the network. Fake keyring and an isolated home only.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot import config as kb_config
from krellbot import secrets as kb_secrets
from krellbot.application import paper as paper_app
from krellbot.application import research as research_app
from krellbot.application.paper import PaperService
from krellbot.venues import paper as paper_venue
from tests.test_ns06_api import TEST_PORT, _build_app, _do_bootstrap, _write_dsl_pack, asgi_call

pytestmark = pytest.mark.usefixtures("fresh_keyring")
ASSUMPTIONS_PATH = "/api/v1/paper/assumptions"
ARMED_FIELDS = ("timeframe", "stop", "starting_cash", "quote")


def _session(app) -> str:
    _do_bootstrap(app, token="boot-test-token")
    return next(iter(app.state.krellbot.sessions))


def _get(app, path: str, headers: list[tuple[str, str]]):
    status, hdrs, raw, cookies = asgi_call(app, method="GET", path=path, headers=headers)
    return status, hdrs, (json.loads(raw) if raw else None), cookies


def _read(app, session: str, query: str):
    return _get(
        app,
        f"{ASSUMPTIONS_PATH}?{query}",
        [("Host", f"127.0.0.1:{TEST_PORT}"), ("Cookie", f"krellbot_session={session}")],
    )


def _arm(home: Path, *, stop: dict | None = None, balance: Decimal = Decimal(1000)) -> Path:
    pack = _write_dsl_pack(home)
    body = json.loads(pack.read_text(encoding="utf-8"))
    body["risk"]["max_account_pct"] = 100
    if stop is not None:
        body["risk"]["stop"] = stop
    pack.write_text(json.dumps(body), encoding="utf-8")
    result = PaperService(home=home).arm(
        pack, venue="kraken", mode="paper", paper_balance=balance, correlation_id="assumptions-arm"
    )
    assert result.ok, result
    return pack


def _snapshot(home: Path) -> dict:
    return {
        str(path.relative_to(home)): (path.read_bytes() if path.is_file() else None, path.stat().st_mtime_ns)
        for path in [home, *sorted(home.rglob("*"))]
    }


def test_paper_slippage_copy_equals_the_paper_venue_constant() -> None:
    # application/paper keeps a copy so the read never imports the venue risk path.
    assert paper_app._PAPER_SLIPPAGE_BPS == paper_venue.SLIPPAGE_BPS


def test_paper_taker_fee_table_equals_the_paper_venue_fee_for_every_venue() -> None:
    assert set(paper_app._PAPER_TAKER_BPS) == set(kb_secrets.VENUES)
    for venue in sorted(kb_secrets.VENUES):
        assert paper_app._PAPER_TAKER_BPS[venue] == paper_venue._fee_bps_for(venue), venue


def test_route_serves_each_venues_engine_fee_and_slippage_with_the_research_fee_label(home: Path) -> None:
    app = _build_app(home)
    session = _session(app)
    for venue in sorted(kb_secrets.VENUES):
        status, headers, body, cookies = _read(app, session, f"venue={venue}")
        assert status == 200, (venue, body)
        assert body == {
            "schema_version": "1",
            "venue": venue,
            "pair": None,
            "fee_model": research_app.FEE_MODEL,
            "fee_bps": paper_venue._fee_bps_for(venue),
            "slippage_bps": paper_venue.SLIPPAGE_BPS,
            "timeframe": None,
            "stop": None,
            "starting_cash": None,
            "quote": None,
        }
        assert [v for k, v in headers if k.lower() == "cache-control"] == ["no-store"]
        assert cookies == []
    assert body["fee_model"] == "taker, per side"


def test_route_reports_the_armed_runs_timeframe_stop_and_pretend_balance(home: Path, monkeypatch) -> None:
    _arm(home, balance=Decimal(1000))
    app = _build_app(home)
    session = _session(app)

    def forbidden(*args, **kwargs):
        pytest.fail("the assumptions read constructed a trading service or venue, or reached the network")

    monkeypatch.setattr("krellbot.venues.paper.PaperVenue.__init__", forbidden)
    monkeypatch.setattr("krellbot.application.paper.PaperService.__init__", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)

    before = _snapshot(home)
    for _ in range(2):
        status, _headers, body, cookies = _read(app, session, "venue=kraken&pair=SUIUSD")
        assert status == 200, body
        assert body == {
            "schema_version": "1",
            "venue": "kraken",
            "pair": "SUIUSD",
            "fee_model": "taker, per side",
            "fee_bps": 40,
            "slippage_bps": 5,
            "timeframe": "1h",
            "stop": {"type": "pct", "pct": "5"},
            "starting_cash": "1000",
            "quote": "USD",
        }
        assert cookies == []
    assert _snapshot(home) == before


def test_route_reports_an_atr_stop_rule_as_the_pack_states_it(home: Path) -> None:
    _arm(home, stop={"type": "atr", "len": 14, "mult": 2.5}, balance=Decimal("2500.50"))
    app = _build_app(home)
    status, _h, body, _c = _read(app, _session(app), "venue=kraken&pair=SUIUSD")
    assert status == 200, body
    assert body["stop"] == {"type": "atr", "len": 14, "mult": "2.5"}
    assert body["starting_cash"] == "2500.5"
    assert body["timeframe"] == "1h"


def test_values_the_engine_does_not_give_are_null(home: Path) -> None:
    pack = _arm(home)
    app = _build_app(home)
    session = _session(app)

    # A venue the paper engine does not list has no fee or slippage.
    status, _h, body, _c = _read(app, session, "venue=CANARY")
    assert status == 200, body
    assert body["fee_bps"] is None
    assert body["slippage_bps"] is None
    assert all(body[field] is None for field in ARMED_FIELDS)

    # A pair with no armed record keeps the venue's costs and nothing else.
    status, _h, body, _c = _read(app, session, "venue=kraken&pair=XXBTZUSD")
    assert status == 200, body
    assert (body["fee_bps"], body["slippage_bps"]) == (40, 5)
    assert all(body[field] is None for field in ARMED_FIELDS)

    # An unreadable pack leaves the starting cash but no timeframe or stop.
    pack.write_text("{not json", encoding="utf-8")
    status, _h, body, _c = _read(app, session, "venue=kraken&pair=SUIUSD")
    assert status == 200, body
    assert body["timeframe"] is None
    assert body["stop"] is None
    assert (body["starting_cash"], body["quote"]) == ("1000", "USD")

    # A stop rule the engine cannot read is not described.
    pack.write_text(
        json.dumps({"timeframe": "7m", "risk": {"stop": {"type": "pct", "pct": "CANARY"}}}),
        encoding="utf-8",
    )
    status, _h, body, _c = _read(app, session, "venue=kraken&pair=SUIUSD")
    assert status == 200, body
    assert body["timeframe"] is None
    assert body["stop"] is None


def test_a_record_that_is_not_paper_mode_is_not_described(home: Path) -> None:
    _arm(home)
    path = kb_config.config_path(home)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["armed"][0]["mode"] = "live"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert kb_config.load_config(home).armed[0].mode == "live"

    app = _build_app(home)
    status, _h, body, _c = _read(app, _session(app), "venue=kraken&pair=SUIUSD")
    assert status == 200, body
    assert all(body[field] is None for field in ARMED_FIELDS)


def test_route_requires_a_venue(home: Path) -> None:
    app = _build_app(home)
    session = _session(app)
    for query in ("", "venue=", "pair=SUIUSD"):
        status, _h, body, _c = _read(app, session, query)
        assert status == 400, (query, body)
        assert body == {"detail": "venue required"}


def test_route_keeps_the_session_origin_and_host_gate(home: Path) -> None:
    app = _build_app(home)
    session = _session(app)
    path = f"{ASSUMPTIONS_PATH}?venue=kraken"

    no_session = [("Host", f"127.0.0.1:{TEST_PORT}"), ("Origin", f"http://127.0.0.1:{TEST_PORT}")]
    status, _h, body, _c = _get(app, path, no_session)
    assert status == 403, body

    invented = no_session + [("Cookie", "krellbot_session=CANARY")]
    status, _h, body, _c = _get(app, path, invented)
    assert status == 403, body

    foreign_origin = [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", "http://evil.example"),
        ("Cookie", f"krellbot_session={session}"),
    ]
    status, _h, body, _c = _get(app, path, foreign_origin)
    assert status == 403, body

    foreign_host = [("Host", "evil.example"), ("Cookie", f"krellbot_session={session}")]
    status, _h, body, _c = _get(app, path, foreign_host)
    assert status == 403, body

    status, _h, _raw, _c = asgi_call(
        app,
        method="POST",
        path=path,
        headers=[("Host", f"127.0.0.1:{TEST_PORT}"), ("Cookie", f"krellbot_session={session}")],
    )
    assert status in {403, 405}
