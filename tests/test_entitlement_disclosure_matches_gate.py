"""The Settings entitlement disclosure says only what the code enforces.

``GET /api/v1/entitlements`` serves the 'Trust & billing' copy as closed
keys. Every key is proven here against the code it describes: the tick gate
(``license.entries_allowed`` on armed records whose ``requires_license`` came
from ``catalog.requires_license_for``), the paid-install SKU check, and the
export and diagnostics routes. A copy key without a behavioural check below
fails ``test_every_copy_key_has_a_behavioural_check``.

Fake venue, isolated ``KRELLBOT_HOME``, no network, no keys.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from tests.test_community_pack_paper_arm import _auth, _bootstrap, _get
from tests.test_ns06_api import TEST_PORT, _build_app, asgi_call

NOW = 1_800_000_000
PAIR = "SUIUSD"

# Closes that make the sma2 cross fire: index 2 is an entry bar (close crosses
# above sma2), index 3 holds (no entry, no exit), index 4 exits (close crosses
# below sma2).
_CLOSES = ("10", "9", "10", "10.5", "9.5")
ENTRY_BARS = 3
HOLD_BARS = 4
EXIT_BARS = 5

# Every cache state the gate distinguishes, with what ``entries_allowed`` says.
CACHE_STATES: dict[str, tuple[dict | None, bool]] = {
    "missing": (None, False),
    "active": ({"status": "active", "period_end": NOW, "grace_until": NOW + 86_400}, True),
    "active_at_grace_date": ({"status": "active", "period_end": NOW, "grace_until": NOW}, True),
    "past_due_in_grace": ({"status": "past_due", "period_end": NOW - 10, "grace_until": NOW + 3_600}, True),
    "past_due_after_grace": ({"status": "past_due", "period_end": NOW - 10, "grace_until": NOW - 1}, False),
    "active_after_grace": ({"status": "active", "period_end": NOW - 10, "grace_until": NOW - 1}, False),
    "lapsed": ({"status": "lapsed", "period_end": NOW - 10, "grace_until": NOW + 3_600}, False),
    "dead": ({"status": "dead", "period_end": NOW - 10, "grace_until": NOW + 3_600}, False),
    "revoked": ({"status": "revoked", "period_end": NOW - 10, "grace_until": NOW + 3_600}, False),
}

_PRICE_OR_TIER = re.compile(
    r"[$€£¥]|\busd\b|\beur\b|per month|/month|a month|monthly|one-time|yearly|annual|price|\btier",
    re.IGNORECASE,
)


# ----------------------------------------------------------------- helpers


def _pack(pack_id: str) -> dict:
    return {
        "schema_version": 1,
        "id": pack_id,
        "version": "1.0.0",
        "label": "SMA cross",
        "author": "krellbot tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 50, "stop": {"type": "pct", "pct": 10}},
        "markets": [{"venue": "kraken", "pair": PAIR}],
    }


def _write_pack(home: Path, where: str, pack_id: str) -> Path:
    """Write a pack under ``packs/`` (own), ``packs/community/`` or ``packs/catalog/``."""
    root = home / "packs" if where == "own" else home / "packs" / where
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{pack_id}.json"
    path.write_text(json.dumps(_pack(pack_id)), encoding="utf-8")
    return path


def _write_license(home: Path, cache: dict | None) -> None:
    from krellbot import license as kb_license

    path = kb_license.cache_path(home)
    if cache is None:
        path.unlink(missing_ok=True)
        return
    kb_license.write_cache(home, **cache)


def _arm(home: Path, path: Path) -> None:
    from krellbot.run import arm_pack

    # ``requires_license`` is left to ``catalog.requires_license_for``, as the
    # workstation's paper arm does.
    rc = arm_pack(path, venue="kraken", mode="paper", paper_balance=Decimal(1000), home=home)
    assert rc == 0


def _armed_requires_license(home: Path, pack_id: str) -> bool:
    from krellbot.config import load_config

    return next(a for a in load_config(home).armed if a.pack_id == pack_id).requires_license


def _candles(n: int) -> list:
    from krellbot.pack.model import Candle

    return [
        Candle(
            ts_ms=i * 3_600_000,
            open=Decimal(c),
            high=Decimal(c),
            low=Decimal(c),
            close=Decimal(c),
            volume=Decimal(100),
        )
        for i, c in enumerate(_CLOSES[:n])
    ]


class _Venue:
    """Paper-shaped fake: an entry fills at once and rests its protective stop."""

    def __init__(self) -> None:
        self.balances: dict[str, Decimal] = {"USD": Decimal(1000)}
        self.orders: list[dict] = []
        self.calls: list[str] = []

    def rules(self, pair: str):
        from krellbot.venues.base import PairRules

        return PairRules(ordermin=Decimal("0.0001"), costmin=Decimal("0.5"), lot_decimals=8, price_decimals=5)

    def snapshot(self):
        from krellbot.venues.base import Balance, OpenOrder, Truth

        return Truth(
            balances=[Balance(asset=k, free=v) for k, v in self.balances.items() if v > 0],
            open_orders=[
                OpenOrder(id=o["coid"], coid=o["coid"], pair=o["pair"], side="sell", qty=o["qty"], stop_price=o["stop"])
                for o in self.orders
            ],
            recent_fills=[],
        )

    def held(self) -> Decimal:
        return self.balances.get("SUI", Decimal(0))

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str):
        from krellbot.venues.base import OrderRef

        self.calls.append("entry")
        self.balances["SUI"] = self.held() + qty
        self.orders.append({"coid": coid, "pair": pair, "qty": qty, "stop": stop})
        return OrderRef(id=coid, coid=coid, pair=pair, side="buy", qty=qty, filled_qty=qty, stop_price=stop)

    def place_exit(self, coid: str, qty: Decimal, *, pair: str):
        from krellbot.venues.base import OrderRef

        self.calls.append("exit")
        self.balances["SUI"] = self.held() - qty
        return OrderRef(id=coid, coid=coid, pair=pair, side="sell", qty=qty, filled_qty=qty)

    def place_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> None:
        self.calls.append("stop")
        self.orders.append({"coid": coid, "pair": pair, "qty": qty, "stop": stop})

    def cancel_stops(self, pair: str) -> None:
        self.orders = [o for o in self.orders if o["pair"] != pair]

    def raise_stop(self, pair: str, new_stop: Decimal) -> None:
        self.calls.append("raise_stop")

    def order_by_coid(self, coid: str):
        return next((o for o in self.orders if o["coid"] == coid), None)


def _tick(home: Path, venue: _Venue, bars: int) -> int:
    from krellbot.run import tick

    candles = _candles(bars)
    return tick(
        venue="kraken",
        venue_obj=venue,
        reader=lambda _venue, _pair: list(candles),
        clock=lambda: NOW,
        home=home,
    )


def _last_tick(home: Path, pack_id: str) -> dict:
    records = []
    for path in sorted((home / "journal").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("kind") == "tick" and record.get("pack") == pack_id:
                records.append(record)
    assert records, "the tick journaled nothing for this pack"
    return records[-1]["detail"]


def _entitlements(home: Path, *, now: int = NOW) -> dict:
    app = _build_app(home)
    app.state.krellbot.clock = lambda: now
    status, body = _get(app, "/api/v1/entitlements", headers=_auth(*_bootstrap(app)))
    assert status == 200, body
    return json.loads(body)


def _rule_text(home: Path, key: str) -> str:
    rules = {rule["key"]: rule["text"] for rule in _entitlements(home)["rules"]}
    return rules[key]


def _forbid_license_reads(monkeypatch) -> None:
    from krellbot import license as kb_license

    def refuse(*_args, **_kwargs):
        raise AssertionError("this path must not read the license")

    for name in ("read_cache", "load", "entries_allowed"):
        monkeypatch.setattr(kb_license, name, refuse)


def _lapse(home: Path) -> None:
    _write_license(home, CACHE_STATES["past_due_after_grace"][0])


def _seed_paid_catalogue(home: Path, pack_id: str) -> None:
    from krellbot import license as kb_license

    kb_license.write_catalogue_cache(
        home, {"catalogue_version": "1", "packs": [{"id": pack_id, "tier": "paid", "version": "1.0.0"}]}
    )


# ------------------------------------------------------- one check per key


def _check_paid_catalogue_entries_need_license(home: Path, monkeypatch) -> None:
    from krellbot import catalog

    path = _write_pack(home, "catalog", "paid-sma")
    assert catalog.requires_license_for(path, home) is True
    _arm(home, path)
    assert _armed_requires_license(home, "paid-sma") is True

    # No verified license on this computer: the gate fails closed.
    _write_license(home, None)
    venue = _Venue()
    assert _tick(home, venue, ENTRY_BARS) == 0
    assert "entry" not in venue.calls
    assert _last_tick(home, "paid-sma")["entries_blocked"] is True

    # The same pack on the same entry bar with an active license enters: the
    # license is what decided it.
    home_active = home / "with-license"
    home_active.mkdir()
    monkeypatch.setenv("KRELLBOT_HOME", str(home_active))
    _write_license(home_active, CACHE_STATES["active"][0])
    _arm(home_active, _write_pack(home_active, "catalog", "paid-sma"))
    venue = _Venue()
    assert _tick(home_active, venue, ENTRY_BARS) == 0
    assert venue.calls[:1] == ["entry"]
    assert _last_tick(home_active, "paid-sma")["entries_blocked"] is False


def _check_grace_keeps_entries(home: Path, monkeypatch) -> None:
    from krellbot import license as kb_license

    def no_refresh(*_args, **_kwargs):
        raise AssertionError("the tick must not refresh the license online")

    # The tick only reads the cache last verified here; it never refreshes.
    monkeypatch.setattr(kb_license, "refresh", no_refresh)
    for name in ("active", "active_at_grace_date", "past_due_in_grace"):
        cache, allowed = CACHE_STATES[name]
        assert allowed is True
        assert kb_license.entries_allowed(cache, now=NOW) is True
        sub = home / name
        sub.mkdir()
        monkeypatch.setenv("KRELLBOT_HOME", str(sub))
        _write_license(sub, cache)
        _arm(sub, _write_pack(sub, "catalog", "paid-sma"))
        venue = _Venue()
        assert _tick(sub, venue, ENTRY_BARS) == 0
        assert "entry" in venue.calls, name


def _check_after_grace_entries_stop(home: Path, monkeypatch) -> None:
    from krellbot import license as kb_license

    for name in ("missing", "past_due_after_grace", "active_after_grace", "lapsed", "dead", "revoked"):
        cache, allowed = CACHE_STATES[name]
        assert allowed is False
        assert kb_license.entries_allowed(cache, now=NOW) is False
        sub = home / name
        sub.mkdir()
        monkeypatch.setenv("KRELLBOT_HOME", str(sub))
        _write_license(sub, cache)
        _arm(sub, _write_pack(sub, "catalog", "paid-sma"))
        venue = _Venue()
        assert _tick(sub, venue, ENTRY_BARS) == 0
        assert venue.calls == [], name
        assert _last_tick(sub, "paid-sma")["entries_blocked"] is True, name


def _enter_then_lapse(home: Path) -> _Venue:
    """A paid catalogue pack enters under an active license, then the license lapses."""
    _write_license(home, CACHE_STATES["active"][0])
    _arm(home, _write_pack(home, "catalog", "paid-sma"))
    venue = _Venue()
    assert _tick(home, venue, ENTRY_BARS) == 0
    assert venue.calls == ["entry"]
    assert venue.held() > 0
    _lapse(home)
    return venue


def _check_exits_and_stops_continue(home: Path, monkeypatch) -> None:
    venue = _enter_then_lapse(home)
    held = venue.held()

    # The resting stop is gone (cancelled at the venue). After the lapse the
    # tick still re-places it for the whole held quantity.
    venue.orders.clear()
    assert _tick(home, venue, HOLD_BARS) == 0
    assert venue.calls == ["entry", "stop"]
    assert venue.orders and venue.orders[0]["qty"] == held
    hold = _last_tick(home, "paid-sma")
    assert hold["entries_blocked"] is True

    # The exit rule fires after the lapse: the position is sold.
    assert _tick(home, venue, EXIT_BARS) == 0
    assert venue.calls[-1] == "exit"
    assert venue.held() == 0
    exit_detail = _last_tick(home, "paid-sma")
    assert exit_detail["entries_blocked"] is True
    assert Decimal(exit_detail["exit_qty"]) == held


def _check_nothing_force_closed(home: Path, monkeypatch) -> None:
    venue = _enter_then_lapse(home)
    held = venue.held()

    # A bar where neither rule fires, after the lapse: nothing is sold.
    assert _tick(home, venue, HOLD_BARS) == 0
    assert "exit" not in venue.calls
    assert venue.held() == held
    detail = _last_tick(home, "paid-sma")
    assert detail["entries_blocked"] is True
    assert Decimal(detail["exit_qty"]) == 0
    assert Decimal(detail["owned_qty_after"]) == held


def _check_own_and_community_packs_never_gated(home: Path, monkeypatch) -> None:
    from krellbot import catalog
    from krellbot.run import disarm_pack

    # Fail-closed state: no license at all.
    _write_license(home, None)
    for where, pack_id in (("own", "my-sma"), ("community", "community-sma")):
        path = _write_pack(home, where, pack_id)
        assert catalog.requires_license_for(path, home) is False
        _arm(home, path)
        assert _armed_requires_license(home, pack_id) is False
        venue = _Venue()
        assert _tick(home, venue, ENTRY_BARS) == 0
        assert venue.calls[:1] == ["entry"], where
        assert _last_tick(home, pack_id)["entries_blocked"] is False
        assert disarm_pack(venue="kraken", pair=PAIR, home=home) == 0


def _check_export_continues(home: Path, monkeypatch) -> None:
    _lapse(home)
    _forbid_license_reads(monkeypatch)
    app = _build_app(home)
    headers = _auth(*_bootstrap(app))
    status, response_headers, body, _ = asgi_call(
        app,
        method="POST",
        path="/api/v1/home/export",
        body=b"",
        headers=[("Host", f"127.0.0.1:{TEST_PORT}"), ("Content-Length", "0"), *headers],
    )
    assert status == 200, body
    assert ("x-krellbot-home-export", "1") in response_headers
    assert body[:2] == b"\x1f\x8b"
    status, body = _get(app, "/api/v1/journal?tail=5", headers=headers)
    assert status == 200, body


def _check_diagnostics_continue(home: Path, monkeypatch) -> None:
    _lapse(home)
    _forbid_license_reads(monkeypatch)
    app = _build_app(home)
    status, body = _get(app, "/api/v1/diagnostics/report", headers=_auth(*_bootstrap(app)))
    assert status == 200, body
    assert set(json.loads(body)) == {"schema_version", "build", "os", "scheduler", "events", "reproduction_steps"}


def _check_paid_install_needs_sku_entitlement(home: Path, monkeypatch) -> None:
    from krellbot.application import checkout, pack_library

    _seed_paid_catalogue(home, "paid-sma")
    # An active license alone does not let a paid entry install.
    _write_license(home, CACHE_STATES["active"][0])
    with pytest.raises(pack_library.InstallRefused) as refused:
        pack_library.install_from_catalogue(home, "paid-sma")
    assert refused.value.code == "activation_required"

    outcome = checkout.start_checkout(home, "paid-sma", transport=checkout.FakePaymentTransport(), now=NOW)
    assert outcome.status == "paid"
    assert checkout.read_entitlements(home) == ["paid-sma"]
    # Without any license, the SKU entitlement is what lets it install.
    _write_license(home, None)
    row = pack_library.install_from_catalogue(home, "paid-sma")
    assert row["pack_id"] == "paid-sma"

    refund = checkout.request_refund(home, "paid-sma", transport=checkout.FakePaymentTransport(), now=NOW)
    assert refund.status == "refunded"
    assert checkout.read_entitlements(home) == []


def _check_records_can_disagree(home: Path, monkeypatch) -> None:
    from krellbot import license as kb_license
    from krellbot.application import checkout

    payments = checkout.FakePaymentTransport()
    _seed_paid_catalogue(home, "paid-sma")

    # Entitled SKU, lapsed license: install says yes, the tick gate says no.
    assert checkout.start_checkout(home, "paid-sma", transport=payments, now=NOW).status == "paid"
    _lapse(home)
    assert checkout.has_entitlement(home, "paid-sma") is True
    assert kb_license.entries_allowed(kb_license.read_cache(home), now=NOW) is False
    body = _entitlements(home)
    assert body["sku_entitlements"] == ["paid-sma"]
    assert body["license"]["paid_catalogue_entries_allowed"] is False
    assert body["records_can_disagree"] is True

    # A refund removes the SKU entitlement and leaves the license cache as it was.
    _write_license(home, CACHE_STATES["active"][0])
    before = kb_license.cache_path(home).read_bytes()
    assert checkout.request_refund(home, "paid-sma", transport=payments, now=NOW).status == "refunded"
    assert kb_license.cache_path(home).read_bytes() == before
    assert checkout.has_entitlement(home, "paid-sma") is False
    assert kb_license.entries_allowed(kb_license.read_cache(home), now=NOW) is True
    body = _entitlements(home)
    assert body["sku_entitlements"] == []
    assert body["license"]["paid_catalogue_entries_allowed"] is True


CHECKS: dict[str, Callable[[Path, pytest.MonkeyPatch], None]] = {
    "paid_catalogue_entries_need_license": _check_paid_catalogue_entries_need_license,
    "grace_keeps_entries": _check_grace_keeps_entries,
    "after_grace_entries_stop": _check_after_grace_entries_stop,
    "exits_and_stops_continue": _check_exits_and_stops_continue,
    "nothing_force_closed": _check_nothing_force_closed,
    "own_and_community_packs_never_gated": _check_own_and_community_packs_never_gated,
    "export_continues": _check_export_continues,
    "diagnostics_continue": _check_diagnostics_continue,
    "paid_install_needs_sku_entitlement": _check_paid_install_needs_sku_entitlement,
    "records_can_disagree": _check_records_can_disagree,
}


# ------------------------------------------------------------------- tests


def test_every_copy_key_has_a_behavioural_check(home):
    body = _entitlements(home)
    keys = [rule["key"] for rule in body["rules"]]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(CHECKS)
    for rule in body["rules"]:
        assert set(rule) == {"key", "group", "text"}
        assert rule["group"] in {"gated", "continues", "never_gated", "records"}
        assert isinstance(rule["text"], str) and rule["text"].strip()
    groups = {rule["key"]: rule["group"] for rule in body["rules"]}
    assert {key for key, group in groups.items() if group == "continues"} == {
        "exits_and_stops_continue",
        "nothing_force_closed",
        "export_continues",
        "diagnostics_continue",
    }
    assert groups["own_and_community_packs_never_gated"] == "never_gated"


@pytest.mark.parametrize("key", sorted(CHECKS))
def test_copy_key_matches_code(key, home, monkeypatch):
    assert _rule_text(home, key)
    CHECKS[key](home, monkeypatch)


@pytest.mark.parametrize("state", sorted(CACHE_STATES))
def test_reported_entry_state_is_the_tick_gate(state, home):
    from krellbot import license as kb_license

    cache, allowed = CACHE_STATES[state]
    _write_license(home, cache)
    reported = _entitlements(home)["license"]
    assert reported["paid_catalogue_entries_allowed"] is allowed
    assert reported["paid_catalogue_entries_allowed"] is kb_license.entries_allowed(
        kb_license.read_cache(home), now=NOW
    )
    assert reported["status"] == ("missing" if cache is None else cache["status"])
    assert reported["grace_until"] == (None if cache is None else cache["grace_until"])

    _arm(home, _write_pack(home, "catalog", "paid-sma"))
    venue = _Venue()
    assert _tick(home, venue, ENTRY_BARS) == 0
    assert ("entry" in venue.calls) is allowed
    assert _last_tick(home, "paid-sma")["entries_blocked"] is (not allowed)


def test_route_reports_local_sku_entitlements(home):
    from krellbot.application import checkout

    checkout.grant_entitlement(home, "pack-b")
    checkout.grant_entitlement(home, "pack-a")
    assert _entitlements(home)["sku_entitlements"] == ["pack-a", "pack-b"]


def test_route_lists_exactly_the_packs_requires_license_for_gates(home):
    from krellbot import catalog

    paths = [
        _write_pack(home, "own", "my-sma"),
        _write_pack(home, "community", "community-sma"),
        _write_pack(home, "catalog", "paid-b"),
        _write_pack(home, "catalog", "paid-a"),
    ]
    expected = sorted(p.stem for p in paths if catalog.requires_license_for(p, home))
    assert expected == ["paid-a", "paid-b"]
    assert _entitlements(home)["license_gated_packs"] == expected


def test_unknown_or_malformed_license_status_is_not_invented(home):
    from krellbot import license as kb_license

    path = kb_license.cache_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "sk_live_secret-ish", "period_end": "x", "grace_until": "y"}))
    reported = _entitlements(home)["license"]
    assert reported == {
        "status": "other",
        "period_end": None,
        "grace_until": None,
        "paid_catalogue_entries_allowed": False,
    }
    path.write_text("not json")
    assert _entitlements(home)["license"]["status"] == "missing"


def test_disclosure_shows_no_price_or_tier(home):
    _write_license(home, CACHE_STATES["active"][0])
    body = _entitlements(home)
    for rule in body["rules"]:
        assert not _PRICE_OR_TIER.search(rule["text"]), rule
    assert set(body) == {
        "schema_version",
        "license",
        "license_gated_packs",
        "sku_entitlements",
        "records_can_disagree",
        "rules",
    }


def test_route_is_session_gated_read_only_and_no_store(home):
    from krellbot import license as kb_license

    _write_license(home, CACHE_STATES["past_due_in_grace"][0])
    before = sorted((p.relative_to(home).as_posix(), p.read_bytes()) for p in home.rglob("*") if p.is_file())
    app = _build_app(home)
    host = [("Host", f"127.0.0.1:{TEST_PORT}")]

    status, _h, _b, _c = asgi_call(app, method="GET", path="/api/v1/entitlements", headers=host)
    assert status == 403

    csrf, session = _bootstrap(app)
    status, _h, _b, _c = asgi_call(
        app,
        method="GET",
        path="/api/v1/entitlements",
        headers=[*host, ("Origin", "http://evil.example"), ("Cookie", f"krellbot_session={session}")],
    )
    assert status == 403

    status, _h, _b, _c = asgi_call(
        app,
        method="GET",
        path="/api/v1/entitlements",
        headers=[("Host", "evil.example"), ("Cookie", f"krellbot_session={session}")],
    )
    assert status == 403

    status, response_headers, body, _ = asgi_call(
        app,
        method="GET",
        path="/api/v1/entitlements",
        headers=[*host, ("Cookie", f"krellbot_session={session}")],
    )
    assert status == 200, body
    assert ("cache-control", "no-store") in response_headers

    status, _h, _b, _c = asgi_call(
        app,
        method="POST",
        path="/api/v1/entitlements",
        body=b"{}",
        headers=[*host, *_auth(csrf, session), ("Content-Type", "application/json"), ("Content-Length", "2")],
    )
    assert status == 405

    after = sorted((p.relative_to(home).as_posix(), p.read_bytes()) for p in home.rglob("*") if p.is_file())
    assert [entry for entry in after if not entry[0].startswith(("journal/", "ops.sqlite"))] == [
        entry for entry in before if not entry[0].startswith(("journal/", "ops.sqlite"))
    ]
    assert kb_license.read_cache(home) == CACHE_STATES["past_due_in_grace"][0]
