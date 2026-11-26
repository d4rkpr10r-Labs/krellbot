"""A missing stop is re-placed with the pack's own stop rule, never a guess.

When a strategy owns inventory and no stop rests for it, the tick re-places
one. A percentage stop is re-placed ``pct`` below the latest close, as before.
Every other stop is re-placed with the rule the entry uses
(``pack/evaluate``): for an ATR stop, the latest close minus ``mult`` times
the ATR computed from the tick's candles. It is never placed below the stop
the arm recorded for the position: the ``stop`` of the position's committed
entry intent in ``ops.sqlite``, which survives restarts.

When the rule gives no price (too few candles for the ATR, a stop rule the
engine cannot read), nothing is placed and nothing is sold. The tick journals
``stop_price_unavailable`` and Operations shows one critical
``stop_price_unavailable`` alert per arm. Repeated ticks extend that one
occurrence: it notifies once, an acknowledgement holds, and it clears on the
first tick that reads a bar without the flag.

Paper venue and fake notifier only. No exchange, key or network is touched.
The candles are fixtures, not market data, and nothing here describes
performance.
"""

from __future__ import annotations

import json
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from krellbot import notifications as kb_notifications
from krellbot import run
from krellbot.application import alerts
from krellbot.pack import indicators
from krellbot.pack.model import Candle
from krellbot.venues.base import PairRules
from krellbot.venues.paper import PaperVenue

H = 3_600_000
PAIR = "SUIUSD"
VENUE = "kraken"
PRICE_STEP = Decimal("0.0001")
RULES = PairRules(ordermin=Decimal("0.0001"), costmin=Decimal("0.5"), lot_decimals=5, price_decimals=4)
STARTER_RANGE_BREAKOUT = Path(run.__file__).resolve().parents[1] / "packs" / "examples" / "starter-range-breakout.json"


def _candle(
    i: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    volume: str = "100",
) -> Candle:
    c = Decimal(close)
    return Candle(
        ts_ms=i * H,
        open=c,
        high=Decimal(high) if high is not None else c + 1,
        low=Decimal(low) if low is not None else c - 1,
        close=c,
        volume=Decimal(volume),
    )


# close crosses above sma2 on bar 2 (entry); later bars hold flat.
ENTRY_BARS = [_candle(0, "10"), _candle(1, "10"), _candle(2, "12")]
# ATR(2) on bar 2 is (2 + 3) / 2 = 2.5, so the entry stop is 12 - 1.5 x 2.5.
RECORDED_STOP = Decimal("8.25")


def _cross_pack(stop: dict) -> dict:
    return {
        "schema_version": 1,
        "id": "repair-rule",
        "version": "1.0.0",
        "label": "Repair rule",
        "author": "krellbot tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 100, "stop": stop},
        "markets": [{"venue": VENUE, "pair": PAIR}],
    }


ATR_STOP = {"type": "atr", "len": 2, "mult": 1.5}


class _Feed:
    """The tick's candle reader, and a clock one hour after the latest bar."""

    def __init__(self, candles: list[Candle]) -> None:
        self.candles = list(candles)

    def read(self, _venue: str, _pair: str) -> list[Candle]:
        return list(self.candles)

    def now(self) -> float:
        return self.candles[-1].ts_ms / 1000 + 3600


def _arm(home: Path, pack: dict) -> Path:
    path = home / "packs" / f"{pack['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pack), encoding="utf-8")
    rc = run.arm_pack(path, venue=VENUE, mode="paper", paper_balance=Decimal(1000), requires_license=False, home=home)
    assert rc == 0
    return path


def _venue(home: Path, feed: _Feed) -> PaperVenue:
    """A paper venue over ``home``'s state file; a new one models a restart."""
    return PaperVenue(
        VENUE,
        rules_provider=lambda _pair: RULES,
        candle_reader=feed.read,
        home=home,
        starting_cash=Decimal(1000),
        clock=feed.now,
    )


def _tick(home: Path, venue: PaperVenue, feed: _Feed) -> int:
    return run.tick(venue=VENUE, venue_obj=venue, reader=feed.read, clock=feed.now, home=home)


def _ticks(home: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted((home / "journal").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("kind") == "tick":
                    out.append(record)
    return out


def _sells(venue: PaperVenue) -> list:
    return [f for f in venue.snapshot().recent_fills if f.side == "sell"]


def _enter_then_lose_stop(home: Path, pack: dict, feed: _Feed) -> PaperVenue:
    """Arm ``pack``, enter on the feed's last bar, then lose the resting stop."""
    _arm(home, pack)
    venue = _venue(home, feed)
    assert _tick(home, venue, feed) == 0
    assert venue.owned_qty(PAIR) > 0
    venue.cancel_stops(PAIR)
    assert venue.resting_stop(PAIR) is None
    return venue


def _quantized(price: Decimal) -> Decimal:
    """The paper venue rests a stop at the pair's price step, rounded down."""
    return price.quantize(PRICE_STEP, rounding=ROUND_DOWN)


def _atr_rule(candles: list[Candle], *, length: int, mult: Decimal) -> Decimal:
    """The ATR stop the entry rule computes on the last of ``candles``."""
    atr = indicators.compute(candles, {"fn": "atr", "len": length})[-1]
    assert atr is not None
    return candles[-1].close - Decimal(str(atr)) * mult


def _stop_alerts(home: Path, now: int) -> list[alerts.Alert]:
    return [a for a in alerts.collect(home, now=now) if a.kind == "stop_price_unavailable"]


# ---- the rule, not half the close ------------------------------------------


def test_missing_atr_stop_is_replaced_with_the_entry_rule_not_half_the_close(home):
    feed = _Feed(ENTRY_BARS)
    venue = _enter_then_lose_stop(home, _cross_pack(ATR_STOP), feed)
    entry = next(r for r in _ticks(home) if r["detail"].get("entry_qty") not in (None, "0"))
    assert entry["detail"]["reason"] == "entry"

    # Bar 3 holds flat (no cross), so nothing but the repair moves the stop.
    feed.candles.append(_candle(3, "13"))
    assert _tick(home, venue, feed) == 0

    # ATR(2) on bar 3 is (2.5 + 2) / 2 = 2.25: 13 - 1.5 x 2.25 = 9.625.
    expected = _atr_rule(feed.candles, length=2, mult=Decimal("1.5"))
    assert expected == Decimal("9.625")
    assert venue.resting_stop(PAIR) == _quantized(expected)
    assert venue.resting_stop(PAIR) != Decimal(13) / 2
    assert _sells(venue) == []
    detail = _ticks(home)[-1]["detail"]
    assert detail["reason"] == "flat"
    assert detail["ensure_stop_failed"] is False
    assert "stop_price_unavailable" not in detail
    assert _stop_alerts(home, now=int(feed.now())) == []


def test_repaired_atr_stop_is_never_below_the_recorded_entry_stop(home):
    feed = _Feed(ENTRY_BARS)
    _enter_then_lose_stop(home, _cross_pack(ATR_STOP), feed)

    # A wide bar 3 lifts ATR(2) to (2.5 + 7) / 2 = 4.75, so the rule gives
    # 12 - 1.5 x 4.75 = 4.875, below the 8.25 recorded with the entry.
    feed.candles.append(_candle(3, "12", high="18", low="11"))
    rule = _atr_rule(feed.candles, length=2, mult=Decimal("1.5"))
    assert rule == Decimal("4.875")
    assert rule < RECORDED_STOP

    # The recorded stop is durable: a fresh venue object (a restart) still
    # finds it in the committed entry intent.
    restarted = _venue(home, feed)
    assert _tick(home, restarted, feed) == 0

    assert restarted.resting_stop(PAIR) == _quantized(RECORDED_STOP)
    assert restarted.resting_stop(PAIR) != Decimal(12) / 2
    assert _sells(restarted) == []
    assert "stop_price_unavailable" not in _ticks(home)[-1]["detail"]


# ---- no computable price: no stop, a journal flag, one critical alert -------


def test_too_few_candles_place_no_stop_and_raise_one_critical_alert(home):
    feed = _Feed(ENTRY_BARS)
    venue = _enter_then_lose_stop(home, _cross_pack(ATR_STOP), feed)
    owned = venue.owned_qty(PAIR)
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    notifier = kb_notifications.FakeNotifier()

    def poll(now: int) -> list[alerts.Alert]:
        collected = alerts.collect(home, now=now)
        kb_notifications.maybe_notify(home, collected, notifier=notifier, now=now)
        return [a for a in collected if a.kind == "stop_price_unavailable"]

    first_ts = None
    for i in (3, 4):
        # The feed returns a single bar: ATR(2) has no value on it.
        feed.candles = [_candle(i, "13")]
        assert _tick(home, venue, feed) == 0

        assert venue.resting_stop(PAIR) is None
        assert venue.owned_qty(PAIR) == owned
        assert _sells(venue) == []
        record = _ticks(home)[-1]
        assert record["bar_ts"] == i * H
        assert record["detail"]["stop_price_unavailable"] is True
        assert record["detail"]["ensure_stop_failed"] is True
        assert record["detail"]["owned_qty_after"] == str(owned)
        if first_ts is None:
            first_ts = record["ts"]

        # Operations polls repeatedly between ticks.
        for _ in range(3):
            raised = poll(int(feed.now()))
        assert len(raised) == 1
        alert = raised[0]
        assert alert.severity == alerts.SEVERITY_CRITICAL
        assert (alert.venue, alert.pair) == (VENUE, PAIR)
        assert alert.first_ts == first_ts
        assert alert.last_ts == record["ts"]
        assert alert.count == i - 2

    # One occurrence across both ticks and every poll: one notification.
    sent = [row for row in notifier.sent if row["title"].endswith(": stop_price_unavailable")]
    assert len(sent) == 1
    assert sent[0]["title"] == "Krellbot critical: stop_price_unavailable"

    # An acknowledgement holds while the same occurrence continues.
    alerts.acknowledge(home, alert.id, now=int(feed.now()))
    feed.candles = [_candle(5, "13")]
    assert _tick(home, venue, feed) == 0
    assert _ticks(home)[-1]["detail"]["stop_price_unavailable"] is True
    (held,) = poll(int(feed.now()))
    assert held.acknowledged is True
    assert held.count == 3
    assert len([row for row in notifier.sent if row["title"].endswith(": stop_price_unavailable")]) == 1

    # With enough candles the rule prices the stop again and the alert clears.
    feed.candles = [*ENTRY_BARS, _candle(3, "13"), _candle(4, "13"), _candle(5, "13"), _candle(6, "13")]
    assert _tick(home, venue, feed) == 0
    expected = max(_atr_rule(feed.candles, length=2, mult=Decimal("1.5")), RECORDED_STOP)
    assert venue.resting_stop(PAIR) == _quantized(expected)
    assert "stop_price_unavailable" not in _ticks(home)[-1]["detail"]
    assert poll(int(feed.now())) == []


@pytest.mark.parametrize(
    "unreadable",
    [
        {"type": "trailing", "pct": 5},
        {"type": "atr", "len": 2},
        {"type": "atr", "len": 2, "mult": "wide"},
        None,
    ],
    ids=["unknown-type", "atr-without-mult", "atr-mult-not-a-number", "no-stop-block"],
)
def test_an_unreadable_stop_rule_places_no_stop(home, unreadable):
    feed = _Feed(ENTRY_BARS)
    pack = _cross_pack(ATR_STOP)
    venue = _enter_then_lose_stop(home, pack, feed)
    owned = venue.owned_qty(PAIR)

    # The armed pack file now carries a stop rule the engine cannot read. The
    # tick reads four flat bars: enough for ATR(2), and no entry fires on any
    # of them, so only the rule itself is missing.
    risk = {"max_account_pct": 100, **({"stop": unreadable} if unreadable is not None else {})}
    (home / "packs" / f"{pack['id']}.json").write_text(json.dumps({**pack, "risk": risk}), encoding="utf-8")
    feed.candles = [_candle(i, "13") for i in range(3, 7)]
    assert indicators.compute(feed.candles, {"fn": "atr", "len": 2})[-1] is not None
    assert _tick(home, venue, feed) == 0

    assert venue.resting_stop(PAIR) is None
    assert venue.owned_qty(PAIR) == owned
    assert _sells(venue) == []
    detail = _ticks(home)[-1]["detail"]
    assert detail["stop_price_unavailable"] is True
    assert detail["ensure_stop_failed"] is True
    (alert,) = _stop_alerts(home, now=int(feed.now()))
    assert alert.severity == alerts.SEVERITY_CRITICAL


# ---- percentage stops are unchanged -----------------------------------------


def test_missing_percentage_stop_is_still_replaced_pct_below_the_close(home):
    feed = _Feed(ENTRY_BARS)
    venue = _enter_then_lose_stop(home, _cross_pack({"type": "pct", "pct": 5}), feed)

    feed.candles.append(_candle(3, "13"))
    assert _tick(home, venue, feed) == 0

    assert venue.resting_stop(PAIR) == _quantized(Decimal(13) * (Decimal(1) - Decimal(5) / Decimal(100)))
    assert venue.resting_stop(PAIR) == Decimal("12.35")
    assert _sells(venue) == []
    assert "stop_price_unavailable" not in _ticks(home)[-1]["detail"]


# ---- the bundled ATR starter on paper ---------------------------------------


def _range_bars() -> list[Candle]:
    """25 quiet bars (close 10, range 9-11), then a close just above the range."""
    quiet = [_candle(i, "10", volume="200") for i in range(25)]
    breakout = _candle(25, "11.2", high="11.3", low="11.0", volume="200")
    return [*quiet, breakout]


def test_bundled_range_breakout_repairs_its_atr_stop_on_paper(home):
    pack = json.loads(STARTER_RANGE_BREAKOUT.read_text(encoding="utf-8"))
    assert pack["id"] == "starter-range-breakout"
    stop = pack["risk"]["stop"]
    assert stop["type"] == "atr"
    mult = Decimal(str(stop["mult"]))

    feed = _Feed(_range_bars())
    venue = _enter_then_lose_stop(home, pack, feed)
    recorded = _atr_rule(feed.candles, length=stop["len"], mult=mult)

    # Bar 26 closes back inside the range on volume above 150: neither the
    # entry nor the exit holds, so only the repair places a stop.
    feed.candles.append(_candle(26, "11.0", high="11.1", low="10.95", volume="200"))
    assert _tick(home, venue, feed) == 0
    assert _ticks(home)[-1]["detail"]["reason"] == "flat"

    rule = _atr_rule(feed.candles, length=stop["len"], mult=mult)
    assert rule > recorded
    assert venue.resting_stop(PAIR) == _quantized(rule)
    assert venue.resting_stop(PAIR) != Decimal("11.0") / 2
    assert _sells(venue) == []
    assert _stop_alerts(home, now=int(feed.now())) == []
