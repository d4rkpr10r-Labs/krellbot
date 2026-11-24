"""Workstation and release-policy acceptance for every registered spot venue."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from krellbot import cli, config, release_policy, secrets
from krellbot.application.distribution_venues import PAPER_ONLY_VENUES, QUALIFIED_LIVE_VENUES
from krellbot.data.instruments import (
    METADATA_STALE,
    METADATA_SYMBOL_MISMATCH,
    METADATA_UNAVAILABLE,
    SCHEMA_VERSION,
    InstrumentRulesSnapshot,
    InstrumentRulesV1,
)
from tests.test_community_pack_paper_arm import _auth, _bootstrap, _get
from tests.test_ns06_api import _build_app, _post_json

WAVE_MARKETS = {
    "kraken": ("XXBTZUSD", "USD"),
    "coinbase": ("BTC-USD", "USD"),
    "binance": ("BTCUSDT", "USDT"),
    "gate": ("BTC-USDT", "USDT"),
    "pionex": ("BTC-USDT", "USDT"),
    "okx": ("BTC-USDT", "USDT"),
    "bybit": ("BTCUSDT", "USDT"),
    "kucoin": ("BTC-USDT", "USDT"),
    "ourbit": ("BTCUSDT", "USDT"),
    "toobit": ("BTCUSDT", "USDT"),
    "xt": ("BTCUSDT", "USDT"),
    "mexc": ("BTCUSDT", "USDT"),
    "deepcoin": ("BTC-USDT", "USDT"),
    "lbank": ("BTC-USDT", "USDT"),
    "bitget": ("BTCUSDT", "USDT"),
    "htx": ("BTCUSDT", "USDT"),
    "btcc": ("BTCUSDT", "USDT"),
    "cryptocom": ("BTC-USDT", "USDT"),
}


def test_acceptance_matrix_covers_every_registered_spot_venue():
    assert WAVE_MARKETS.keys() == PAPER_ONLY_VENUES | QUALIFIED_LIVE_VENUES == secrets.VENUES


@pytest.fixture
def workstation(home):
    app = _build_app(home)
    app.state.krellbot.keys_transport = _NO_TRANSPORT
    app.state.krellbot.live_transport = _NO_TRANSPORT
    headers = _auth(*_bootstrap(app))

    def request(path, payload=None):
        if payload is None:
            status, body = _get(app, path, headers=headers)
        else:
            status, _, body, _ = _post_json(app, path, payload, headers=headers)
        return status, json.loads(body)

    return request


def _validate_pack(workstation, venue, pair):
    pack = {
        "schema_version": 1,
        "id": "btc-wave",
        "version": "1.0.0",
        "label": "BTC paper wave",
        "author": "Krellbot tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": venue, "pair": pair}],
    }
    status, draft = workstation("/api/v1/strategies/drafts", {"pack": pack})
    assert status == 200, draft
    revision = draft["revision_id"]
    status, validated = workstation(f"/api/v1/strategies/drafts/{revision}/validate", {})
    assert status == 200, validated
    assert validated["state"] == "validated"
    return revision


def _command(workstation, name, venue, pair, revision):
    payload = {"venue": venue, "pair": pair, "revision_id": revision}
    if name == "live.arm":
        payload["confirmation"] = "LIVE"
    else:
        payload["paper_balance"] = "1000"
    return workstation(
        "/api/v1/commands",
        {
            "schema_version": "1",
            "command": name,
            "correlation_id": name,
            "expected_revision": None,
            "payload": payload,
        },
    )


def _arm(workstation, venue, pair, revision):
    status, result = _command(workstation, "paper.arm", venue, pair, revision)
    assert status == 200, result
    assert result["ok"] and result["code"] == "armed"
    status, armed = workstation("/api/v1/paper/armed")
    assert status == 200, armed
    [record] = armed["records"]
    assert (record["venue"], record["pair"], record["mode"]) == (venue, pair, "paper")


def _read_position_and_run(workstation):
    status, position = workstation("/api/v1/paper/position")
    assert status == 200, position
    status, history = workstation("/api/v1/paper/history")
    assert status == 200, history
    [run] = history["runs"]
    return position, run


@pytest.fixture
def offline_tick(home, fresh_keyring):
    csv_path = home / "candles.csv"
    csv_path.write_text(
        "ts_ms,open,high,low,close,volume\n0,11,12,10,11,100\n3600000,10,11,9,10,100\n7200000,13,14,12,13,100\n",
        encoding="utf-8",
    )

    def tick(venue, *, offline=True):
        args = ["--venue", venue]
        if offline:
            args.extend(["--offline-candles", str(csv_path)])
        return cli.cmd_tick(args, fetch=_unexpected_access, transport=_NO_TRANSPORT)

    return tick


def _unexpected_access(*args, **kwargs):
    raise AssertionError("offline acceptance must not access market data, credentials, or a transport")


class NoTransport:
    def __getattr__(self, name):
        raise AssertionError(f"offline acceptance must not access transport.{name}")


_NO_TRANSPORT = NoTransport()


@pytest.fixture(params=WAVE_MARKETS)
def market(request):
    venue = request.param
    pair, quote = WAVE_MARKETS[venue]
    return venue, pair, quote


@pytest.fixture
def rules(market):
    venue, pair, quote = market
    return InstrumentRulesV1(
        schema_version=SCHEMA_VERSION,
        venue=venue,
        canonical_pair=pair,
        venue_symbol=pair,
        base_asset="BTC",
        quote_asset=quote,
        min_quantity="0.000001",
        min_notional="1",
        quantity_increment="0.000001",
        price_increment="0.01",
        source={
            "endpoint": "fixture://btc-wave-rules",
            "retrieved_at": "2020-01-01T00:00:00Z",
            "source_version": "btc-wave",
            "payload_sha256": "0" * 64,
        },
        usable_until="2099-01-01T00:00:00Z",
    )


def test_paper_btc_fill_is_visible_in_workstation(home, workstation, offline_tick, market, rules):
    venue, pair, quote = market
    revision = _validate_pack(workstation, venue, pair)
    _arm(workstation, venue, pair, revision)
    InstrumentRulesSnapshot(home).write({venue: {pair: rules}})

    assert offline_tick(venue) == 0

    position, run = _read_position_and_run(workstation)
    state = json.loads((home / "run" / f"paper-{venue}.json").read_text(encoding="utf-8"))
    [fill] = state["recent_fills"]
    assert fill["pair"] == pair
    assert fill["side"] == "buy"
    assert Decimal(fill["qty"]) > 0
    [stop] = state["open_orders"]
    assert (stop["pair"], stop["side"], stop["qty"]) == (pair, "sell", fill["qty"])
    assert Decimal(position["free_cash"]) == Decimal(state["balances"][quote])
    assert 0 < Decimal(position["free_cash"]) < 1000
    assert position["holdings"] == [{"symbol": "BTC", "qty": fill["qty"]}]
    assert (run["venue"], run["pair"], run["mode"], run["status"]) == (venue, pair, "paper", "active")
    assert position["as_of_run"] == f"{venue}:btc-wave:{pair}"
    assert run["last_refusal_code"] is None
    assert run["recent_fills"] == [{"coid": fill["coid"], "side": "buy", "ts_ms": fill["ts_ms"]}]


@pytest.mark.parametrize("refusal", [METADATA_STALE, METADATA_SYMBOL_MISMATCH, METADATA_UNAVAILABLE])
def test_paper_tick_without_valid_rules_does_not_spend_cash(home, workstation, offline_tick, market, rules, refusal):
    venue, pair, _ = market
    if refusal == METADATA_UNAVAILABLE:
        pair = pair.replace("BTC", "DOGE") if venue != "kraken" else "DOGEUSD"
    elif refusal == METADATA_STALE:
        rules = replace(rules, usable_until="2020-01-01T00:00:00Z")
    else:
        rules = replace(rules, canonical_pair="OTHER")
    revision = _validate_pack(workstation, venue, pair)
    _arm(workstation, venue, pair, revision)
    InstrumentRulesSnapshot(home).write({venue: {market[1]: rules}})

    assert offline_tick(venue) == 0

    position, run = _read_position_and_run(workstation)
    assert Decimal(position["free_cash"]) == 1000
    assert position["holdings"] == []
    assert run["pair"] == pair
    assert run["last_refusal_code"] == refusal
    assert run["recent_fills"] == []


def test_release_blocks_live_arm_and_saved_live_ticks(home, workstation, offline_tick, market, monkeypatch, capsys):
    venue, pair, _ = market
    revision = _validate_pack(workstation, venue, pair)
    monkeypatch.setenv("KRELLBOT_ENABLE_LIVE", "1")
    with monkeypatch.context() as patch:
        patch.setattr(secrets, "get", _unexpected_access)
        status, refused = _command(workstation, "live.arm", venue, pair, revision)
    assert status == 403, refused
    assert refused["code"] == release_policy.CODE_PAPER_ONLY_RELEASE
    assert config.load_config(home).armed == []

    _arm(workstation, venue, pair, revision)
    saved = config.load_config(home)
    saved.armed = [replace(saved.armed[0], mode="live", starting_cash=None)]
    config.save_config(home, saved)

    monkeypatch.setattr(secrets, "get", _unexpected_access)
    for offline in (False, True):
        assert offline_tick(venue, offline=offline) == 1
        assert "paper-only" in capsys.readouterr().out
    assert config.load_config(home).armed[0].mode == "live"
    assert not (home / "run" / f"paper-{venue}.json").exists()
