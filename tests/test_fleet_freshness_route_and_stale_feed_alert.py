"""Candle age for every armed feed, and a ``feed_stale`` alert.

``GET /api/v1/paper/health`` judges one arm's candle by the 2x-timeframe
rule: a tick read a fresh candle when that candle was at most two timeframes
old at the tick. No other armed feed had that evidence on the wire, and a
stale feed raised no alert.

Pins:

* ``GET /api/v1/paper/fleet/freshness`` uses the paper read gate and serves
  one closed row per armed ``(venue, pack_id, pair)`` with
  ``last_bar_ts_ms``, ``candle_age_s`` and ``candle_stale``, read from the
  current arm's latest tick.
* The route and ``/api/v1/paper/health`` use one rule, so they agree.
* Missing evidence (no tick of this arm, a tick that read no candle, an
  unreadable timeframe) is ``null``, never a guessed freshness.
* ``/api/v1/paper/fleet`` keeps its closed 9-key row.
* ``alerts.collect`` raises one warning ``feed_stale`` alert per stale feed,
  dated by the tick that first read a stale bar. It notifies once per
  occurrence, a fresh candle ends the occurrence, and an ack holds.

Every test uses an isolated home and ``FakeNotifier``: no venue, desktop bus
or SMTP host is touched.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot import config as kb_config
from krellbot import notifications as kb_notifications
from krellbot.application import alerts
from krellbot.application.paper import PaperService
from tests.test_ns06_api import TEST_PORT, _bootstrap_and_get_csrf, _build_app, asgi_call

FRESHNESS = "/api/v1/paper/fleet/freshness"
HOUR = 3600
# Every arm in this file starts here; ticks at or before it belong to an
# earlier arm.
ARMED_AT = 1_790_000_000
FEED_ROW_KEYS = {"venue", "pack_id", "pair", "last_bar_ts_ms", "candle_age_s", "candle_stale"}
FLEET_ROW_KEYS = {
    "venue",
    "pair",
    "mode",
    "pack_id",
    "entries_paused",
    "last_tick_ts_ms",
    "last_tick_result",
    "last_refusal_code",
    "detail_href",
}


# ---- fixtures on disk ---------------------------------------------------------


def _arm(
    home: Path,
    *,
    pack_id: str = "trend-follow",
    venue: str = "kraken",
    pair: str = "SUIUSD",
    timeframe: str = "1h",
) -> None:
    packs = home / "packs"
    packs.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 1,
        "id": pack_id,
        "version": "1.0.0",
        "label": pack_id,
        "author": "krellbot feed freshness tests",
        "timeframe": timeframe,
        "indicators": {"sma20": {"fn": "sma", "src": "close", "len": 20}},
        "entry": ["close", ">", "sma20"],
        "exit": ["close", "<", "sma20"],
        "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": venue, "pair": pair}],
    }
    pack_path = packs / f"{pack_id}.json"
    pack_path.write_text(json.dumps(body), encoding="utf-8")
    result = PaperService(home=home).arm(
        pack_path,
        venue=venue,
        mode="paper",
        paper_balance=Decimal(1000),
        correlation_id=f"seed-feed-freshness-{venue}",
    )
    assert result.ok, (result.code, result.message)
    saved = kb_config.load_config(home)
    for record in saved.armed:
        record.armed_at_ts = ARMED_AT
    kb_config.save_config(home, saved)


def _tick(
    home: Path,
    *,
    ts: int,
    candle_age_s: int | None,
    venue: str = "kraken",
    pack: str = "trend-follow",
    pair: str = "SUIUSD",
    reason: str = "flat",
) -> None:
    """Append one tick; ``candle_age_s=None`` is a tick that read no candle."""

    bar_ts = 0 if candle_age_s is None else (ts - candle_age_s) * 1000
    record = {
        "ts": ts,
        "kind": "tick",
        "venue": venue,
        "pack": pack,
        "bar_ts": bar_ts,
        "detail": {"pair": pair, "reason": reason},
    }
    journal = home / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    with (journal / "2026-09.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _session_app(home: Path, *, now: int | None = None):
    token = f"boot-feed-freshness-{home.name}"
    app = _build_app(home, bootstrap_token=token)
    if now is not None:
        app.state.krellbot.clock = lambda: now
    assert _bootstrap_and_get_csrf(app, token)
    session = next(iter(app.state.krellbot.sessions.keys()))
    return app, session


def _get(app, path: str, *, session: str | None, origin: str | None = None, host: str | None = None):
    headers = [("Host", host or f"127.0.0.1:{TEST_PORT}")]
    if origin is not None:
        headers.append(("Origin", origin))
    if session is not None:
        headers.append(("Cookie", f"krellbot_session={session}"))
    status, flat, body, _cookies = asgi_call(app, method="GET", path=path, headers=headers)
    return status, dict(flat), json.loads(body)


def _feeds(home: Path) -> dict[tuple[str, str], dict]:
    app, session = _session_app(home)
    status, headers, body = _get(app, FRESHNESS, session=session)
    assert status == 200, body
    assert headers.get("cache-control") == "no-store", headers
    assert body["schema_version"] == "1", body
    rows = body["feeds"]
    assert all(set(row) == FEED_ROW_KEYS for row in rows), rows
    return {(row["venue"], row["pair"]): row for row in rows}


def _feed_stale(home: Path, *, now: int) -> list[alerts.Alert]:
    return [a for a in alerts.collect(home, now=now) if a.kind == "feed_stale"]


def _notify(home: Path, fake: kb_notifications.FakeNotifier, *, now: int) -> None:
    kb_notifications.maybe_notify(home, alerts.collect(home, now=now), notifier=fake, now=now)


def _sent_feed_stale(fake: kb_notifications.FakeNotifier) -> list[dict[str, str]]:
    return [row for row in fake.sent if row["title"].endswith(": feed_stale")]


# ---- the route ----------------------------------------------------------------


def test_freshness_route_uses_the_paper_read_gate(home: Path) -> None:
    _arm(home)
    app, session = _session_app(home)
    loopback = f"http://127.0.0.1:{TEST_PORT}"

    refused = [
        _get(app, FRESHNESS, session=None, origin=loopback),
        _get(app, FRESHNESS, session="invented-session", origin=loopback),
    ]
    for status, _headers, body in refused:
        assert status == 403, body
        assert body == {"detail": "session required"}, body

    status, _headers, body = _get(app, FRESHNESS, session=session, origin="http://evil.example.com")
    assert status == 403, body
    assert "feeds" not in body, body
    status, _headers, body = _get(app, FRESHNESS, session=session, origin=loopback, host="evil.example.com")
    assert status == 403, body
    assert "feeds" not in body, body

    # A same-origin GET carries no Origin header, like every paper read route.
    status, _headers, body = _get(app, FRESHNESS, session=session)
    assert status == 200, body


def test_freshness_lists_no_feed_when_nothing_is_armed(home: Path) -> None:
    app, session = _session_app(home)
    status, _headers, body = _get(app, FRESHNESS, session=session)
    assert status == 200, body
    assert body == {"schema_version": "1", "feeds": []}


def test_freshness_serves_one_closed_row_per_armed_feed(home: Path) -> None:
    _arm(home)
    _arm(home, pack_id="mean-revert", venue="coinbase", pair="BTC-USD")
    checked_at = ARMED_AT + HOUR + 60
    _tick(home, ts=checked_at, candle_age_s=HOUR + 60)
    _tick(home, ts=checked_at, candle_age_s=3 * HOUR, venue="coinbase", pack="mean-revert", pair="BTC-USD")

    feeds = _feeds(home)

    assert set(feeds) == {("kraken", "SUIUSD"), ("coinbase", "BTC-USD")}, feeds
    assert feeds[("kraken", "SUIUSD")] == {
        "venue": "kraken",
        "pack_id": "trend-follow",
        "pair": "SUIUSD",
        "last_bar_ts_ms": (checked_at - HOUR - 60) * 1000,
        "candle_age_s": HOUR + 60,
        "candle_stale": False,
    }
    assert feeds[("coinbase", "BTC-USD")] == {
        "venue": "coinbase",
        "pack_id": "mean-revert",
        "pair": "BTC-USD",
        "last_bar_ts_ms": (checked_at - 3 * HOUR) * 1000,
        "candle_age_s": 3 * HOUR,
        "candle_stale": True,
    }


def test_freshness_reads_the_latest_tick_of_the_current_arm(home: Path) -> None:
    _arm(home)
    # A stale read from an earlier arm, then a stale and a fresh read of this one.
    _tick(home, ts=ARMED_AT, candle_age_s=9 * HOUR)
    _tick(home, ts=ARMED_AT + HOUR, candle_age_s=5 * HOUR)
    _tick(home, ts=ARMED_AT + 2 * HOUR, candle_age_s=HOUR)

    row = _feeds(home)[("kraken", "SUIUSD")]

    assert row["candle_age_s"] == HOUR, row
    assert row["candle_stale"] is False, row


@pytest.mark.parametrize(
    ("timeframe", "age", "stale"),
    [
        ("1h", 2 * HOUR, False),
        ("1h", 2 * HOUR + 1, True),
        ("4h", 8 * HOUR, False),
        ("4h", 8 * HOUR + 1, True),
        ("1d", 48 * HOUR, False),
        ("1d", 48 * HOUR + 1, True),
    ],
)
def test_a_candle_older_than_two_timeframes_is_stale(home: Path, timeframe: str, age: int, stale: bool) -> None:
    _arm(home, timeframe=timeframe)
    _tick(home, ts=ARMED_AT + HOUR, candle_age_s=age)

    row = _feeds(home)[("kraken", "SUIUSD")]

    assert row["candle_age_s"] == age, row
    assert row["candle_stale"] is stale, row


@pytest.mark.parametrize(("age", "health_state"), [(HOUR + 60, "checked"), (3 * HOUR, "stale_candles")])
def test_freshness_agrees_with_paper_health(home: Path, age: int, health_state: str) -> None:
    _arm(home)
    checked_at = ARMED_AT + HOUR
    _tick(home, ts=checked_at, candle_age_s=age)
    app, session = _session_app(home, now=checked_at + 1)

    _status, _headers, health = _get(app, "/api/v1/paper/health?venue=kraken&pair=SUIUSD", session=session)
    _status, _headers, body = _get(app, FRESHNESS, session=session)

    assert health["state"] == health_state, health
    assert body["feeds"][0]["candle_stale"] is (health_state == "stale_candles"), body


@pytest.mark.parametrize("case", ["no_tick", "tick_before_this_arm", "latest_tick_read_no_candle", "another_feed"])
def test_missing_candle_evidence_is_null_never_guessed(home: Path, case: str) -> None:
    _arm(home)
    if case == "tick_before_this_arm":
        _tick(home, ts=ARMED_AT, candle_age_s=HOUR)
    elif case == "latest_tick_read_no_candle":
        _tick(home, ts=ARMED_AT + HOUR, candle_age_s=HOUR)
        _tick(home, ts=ARMED_AT + 2 * HOUR, candle_age_s=None, reason="fetch_error")
    elif case == "another_feed":
        _tick(home, ts=ARMED_AT + HOUR, candle_age_s=HOUR, pair="BTCUSD")
        _tick(home, ts=ARMED_AT + HOUR, candle_age_s=HOUR, pack="other-pack")

    row = _feeds(home)[("kraken", "SUIUSD")]

    assert row["last_bar_ts_ms"] is None, row
    assert row["candle_age_s"] is None, row
    assert row["candle_stale"] is None, row


def test_unreadable_timeframe_reports_age_without_a_freshness_verdict(home: Path) -> None:
    _arm(home)
    pack_path = Path(kb_config.load_config(home).armed[0].pack_path)
    pack_path.write_text("{not json", encoding="utf-8")
    _tick(home, ts=ARMED_AT + HOUR, candle_age_s=5 * HOUR)

    row = _feeds(home)[("kraken", "SUIUSD")]

    assert row["candle_age_s"] == 5 * HOUR, row
    assert row["candle_stale"] is None, row
    assert _feed_stale(home, now=ARMED_AT + HOUR + 1) == []


def test_fleet_row_stays_the_closed_nine_keys(home: Path) -> None:
    _arm(home)
    _tick(home, ts=ARMED_AT + HOUR, candle_age_s=3 * HOUR)
    app, session = _session_app(home)

    status, _headers, body = _get(app, "/api/v1/paper/fleet", session=session)

    assert status == 200, body
    assert [set(row) for row in body["deployments"]] == [FLEET_ROW_KEYS], body


# ---- the feed_stale alert -----------------------------------------------------


def test_stale_feed_raises_a_warning_dated_by_its_first_stale_bar(home: Path) -> None:
    _arm(home)
    _tick(home, ts=ARMED_AT + HOUR, candle_age_s=HOUR)
    first_stale = ARMED_AT + 2 * HOUR
    _tick(home, ts=first_stale, candle_age_s=3 * HOUR)
    _tick(home, ts=first_stale + HOUR, candle_age_s=None, reason="fetch_error")
    _tick(home, ts=first_stale + 2 * HOUR, candle_age_s=5 * HOUR)

    for now in (first_stale + 2 * HOUR + 1, first_stale + 9 * HOUR):
        found = _feed_stale(home, now=now)
        assert len(found) == 1, found
        alert = found[0]
        assert alert.severity == alerts.SEVERITY_WARNING, alert
        assert alert.code == "candle_stale", alert
        assert (alert.venue, alert.pair) == ("kraken", "SUIUSD"), alert
        assert alert.first_ts == first_stale, alert
        assert alert.last_ts == first_stale + 2 * HOUR, alert
        assert alert.count == 2, alert
        assert alert.acknowledged is False, alert


def test_no_feed_stale_alert_without_a_stale_candle_of_this_arm(home: Path) -> None:
    _arm(home)
    now = ARMED_AT + 5 * HOUR
    assert _feed_stale(home, now=now) == []

    _tick(home, ts=ARMED_AT, candle_age_s=9 * HOUR)  # an earlier arm's read
    _tick(home, ts=ARMED_AT + HOUR, candle_age_s=None, reason="fetch_error")
    assert _feed_stale(home, now=now) == []

    _tick(home, ts=ARMED_AT + 2 * HOUR, candle_age_s=3 * HOUR)
    _tick(home, ts=ARMED_AT + 3 * HOUR, candle_age_s=HOUR)  # a fresh read ends it
    assert _feed_stale(home, now=now) == []


def test_stale_feed_notifies_once_per_occurrence(home: Path) -> None:
    _arm(home)
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()

    first_stale = ARMED_AT + HOUR
    _tick(home, ts=first_stale, candle_age_s=3 * HOUR)
    for step in range(4):
        _notify(home, fake, now=first_stale + 5 * step)
    _tick(home, ts=first_stale + HOUR, candle_age_s=None, reason="fetch_error")
    _tick(home, ts=first_stale + 2 * HOUR, candle_age_s=4 * HOUR)
    for step in range(4):
        _notify(home, fake, now=first_stale + 2 * HOUR + 5 * step)
    assert len(_sent_feed_stale(fake)) == 1, fake.sent

    # A fresh candle ends the occurrence; the next stale read is a new one.
    _tick(home, ts=first_stale + 3 * HOUR, candle_age_s=HOUR)
    _notify(home, fake, now=first_stale + 3 * HOUR + 5)
    _tick(home, ts=first_stale + 4 * HOUR, candle_age_s=3 * HOUR)
    for step in range(3):
        _notify(home, fake, now=first_stale + 4 * HOUR + 5 * step)
    assert len(_sent_feed_stale(fake)) == 2, fake.sent


def test_feed_stale_ack_holds_until_a_fresh_candle(home: Path) -> None:
    _arm(home)
    first_stale = ARMED_AT + HOUR
    _tick(home, ts=first_stale, candle_age_s=3 * HOUR)
    stale = _feed_stale(home, now=first_stale + 10)[0]
    alerts.acknowledge(home, stale.id, now=first_stale + 10)

    _tick(home, ts=first_stale + HOUR, candle_age_s=4 * HOUR)
    later = _feed_stale(home, now=first_stale + HOUR + 10)
    assert [a.acknowledged for a in later] == [True], later

    kb_notifications.update_settings(home, {"desktop_enabled": True})
    fake = kb_notifications.FakeNotifier()
    _notify(home, fake, now=first_stale + HOUR + 20)
    assert _sent_feed_stale(fake) == []

    _tick(home, ts=first_stale + 2 * HOUR, candle_age_s=HOUR)
    _tick(home, ts=first_stale + 3 * HOUR, candle_age_s=3 * HOUR)
    recurred = _feed_stale(home, now=first_stale + 3 * HOUR + 10)
    assert [(a.first_ts, a.acknowledged) for a in recurred] == [(first_stale + 3 * HOUR, False)], recurred


def test_reading_feed_freshness_writes_nothing(home: Path) -> None:
    _arm(home)
    _tick(home, ts=ARMED_AT + HOUR, candle_age_s=3 * HOUR)
    app, session = _session_app(home)

    def files() -> list[tuple[Path, int]]:
        return sorted((p.relative_to(home), p.stat().st_mtime_ns) for p in home.rglob("*") if p.is_file())

    before = files()
    assert len(_feed_stale(home, now=ARMED_AT + HOUR + 1)) == 1
    status, _headers, body = _get(app, FRESHNESS, session=session)
    assert status == 200, body
    assert body["feeds"][0]["candle_stale"] is True, body
    assert files() == before
