"""Kraken private calls are spaced by `min_interval_ms` in real milliseconds.

The venue's clock returns epoch microseconds: nonces are drawn from it and
must stay strictly increasing on that scale, including across a restart on
one home. The throttle reads the same clock, converts it to milliseconds and
sleeps the rest of the interval in seconds. Everything here is fake: the
clock, the `sleep`, and the transport, which stamps each POST with the fake
time and spends a fixed latency answering it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from itertools import pairwise

import pytest
from fakes.fake_kraken import FakeKrakenTransport

from krellbot.venues.kraken import NONCE_FILENAME, KrakenVenue, RateLimitStop

KEY = "FAKE_KEY_FOR_KRAKEN_SPACING_TESTS"
SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
TRADE_ONLY = {"permissions": ["query-funds", "query-open-trades", "modify-trades", "close-trades"]}

T0_US = 1_700_000_000_000_000  # 2023-11-14T22:13:20Z in epoch microseconds
US_PER_MS = 1_000
US_PER_S = 1_000_000


class FakeClock:
    """Epoch-microsecond wall clock that moves only when told to, or on sleep."""

    def __init__(self, start_us: int = T0_US) -> None:
        self.us = start_us
        self.sleeps: list[float] = []

    def now(self) -> int:
        return self.us

    def advance_ms(self, ms: int) -> None:
        self.us += ms * US_PER_MS

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.us += round(seconds * US_PER_S)


class TimedTransport(FakeKrakenTransport):
    """Fake Kraken that records when each private POST left, in fake time."""

    def __init__(self, clock: FakeClock, *, latency_ms: int = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self._clock = clock
        self._latency_ms = latency_ms
        self.sent_at_us: list[int] = []

    def post(self, url: str, form: dict[str, str], headers: dict[str, str]) -> dict:
        self.sent_at_us.append(self._clock.now())
        self._clock.advance_ms(self._latency_ms)
        return super().post(url, form, headers)


def _venue(transport, clock: FakeClock, home, **kwargs) -> KrakenVenue:
    """A venue with the live default interval (no `min_interval_ms` override)."""
    return KrakenVenue(KEY, SECRET, transport, now_ms=clock.now, home=home, sleep=clock.sleep, **kwargs)


def _gaps_us(stamps: list[int]) -> list[int]:
    return [later - earlier for earlier, later in pairwise(stamps)]


def _nonces(transport: FakeKrakenTransport) -> list[int]:
    return [int(call.form["nonce"]) for call in transport.calls]


@pytest.mark.parametrize(
    ("elapsed_ms", "expected_sleep_s"),
    [(0, 1.0), (1, 0.999), (250, 0.75), (999, 0.001)],
)
def test_private_call_inside_one_second_sleeps_the_remainder_in_seconds(home, elapsed_ms, expected_sleep_s):
    clock = FakeClock()
    transport = TimedTransport(clock, api_key_info=TRADE_ONLY)
    venue = _venue(transport, clock, home)

    venue.check_key()
    clock.advance_ms(elapsed_ms)
    venue.check_key()

    assert clock.sleeps == [pytest.approx(expected_sleep_s)]
    assert _gaps_us(transport.sent_at_us) == [US_PER_S]
    assert len(transport.calls) == 2


@pytest.mark.parametrize("elapsed_ms", [1_000, 1_500, 60_000])
def test_private_calls_one_second_or_more_apart_do_not_sleep(home, elapsed_ms):
    clock = FakeClock()
    transport = TimedTransport(clock, api_key_info=TRADE_ONLY)
    venue = _venue(transport, clock, home)

    venue.check_key()
    clock.advance_ms(elapsed_ms)
    venue.check_key()

    assert clock.sleeps == []
    assert _gaps_us(transport.sent_at_us) == [elapsed_ms * US_PER_MS]


def test_back_to_back_private_reads_reach_the_wire_one_second_apart(home):
    clock = FakeClock()
    transport = TimedTransport(clock, latency_ms=120, balances={"ZUSD": "10"})
    venue = _venue(transport, clock, home)

    venue.snapshot()

    endpoints = [call.url.rsplit("/", 1)[-1] for call in transport.calls]
    assert endpoints == ["Balance", "OpenOrders", "TradesHistory"]
    assert _gaps_us(transport.sent_at_us) == [US_PER_S, US_PER_S]
    assert clock.sleeps == [pytest.approx(0.88), pytest.approx(0.88)]


def test_wall_clock_stepping_back_waits_one_interval_at_most(home):
    clock = FakeClock()
    transport = TimedTransport(clock, api_key_info=TRADE_ONLY)
    venue = _venue(transport, clock, home)

    venue.check_key()
    clock.us -= 3_600 * US_PER_S  # NTP steps the wall clock back an hour
    venue.check_key()

    assert clock.sleeps == [pytest.approx(1.0)]
    assert len(transport.calls) == 2


@pytest.mark.usefixtures("live_capable_release")
def test_throttled_orders_are_each_posted_once(home):
    clock = FakeClock()
    transport = TimedTransport(
        clock,
        latency_ms=120,
        responses=[{"error": [], "result": {"txid": ["TX-A"]}}, {"error": [], "result": {"txid": ["TX-B"]}}],
    )
    venue = _venue(transport, clock, home)

    first = venue.place_entry_with_stop("coid-A", Decimal("0.1"), Decimal(100), pair="XBTUSD")
    second = venue.place_entry_with_stop("coid-B", Decimal("0.1"), Decimal(100), pair="XBTUSD")

    assert (first.id, second.id) == ("TX-A", "TX-B")
    adds = [call.form["cl_ord_id"] for call in transport.calls if call.url.endswith("/AddOrder")]
    assert adds == ["coid-A", "coid-B"]
    assert all(gap >= US_PER_S for gap in _gaps_us(transport.sent_at_us))
    assert clock.sleeps, "back-to-back private calls must wait for the interval"
    nonces = _nonces(transport)
    assert nonces == sorted(set(nonces))


def test_nonces_stay_epoch_microseconds_and_increase_across_a_restart(home):
    clock = FakeClock()
    first_transport = TimedTransport(clock, latency_ms=120, balances={"ZUSD": "10"})
    _venue(first_transport, clock, home).snapshot()

    first = _nonces(first_transport)
    assert first == sorted(set(first))
    assert first[0] == T0_US  # microsecond scale, read from the clock as is
    nonce_path = home / "run" / NONCE_FILENAME
    assert int(nonce_path.read_text(encoding="utf-8").strip()) == first[-1]

    # Restart on the same home with the wall clock behind the persisted
    # nonce: the new process must still go above it.
    restarted = FakeClock(start_us=T0_US)
    second_transport = TimedTransport(restarted, latency_ms=120, balances={"ZUSD": "10"})
    _venue(second_transport, restarted, home).snapshot()

    second = _nonces(second_transport)
    assert second[0] > first[-1]
    assert first + second == sorted(set(first + second))
    assert int(nonce_path.read_text(encoding="utf-8").strip()) == second[-1]


def test_nonce_is_drawn_after_the_wait_so_the_wire_sees_it_increase(home):
    """A second process on the same home sends while this one waits its turn.

    The nonce must be drawn after the wait, not before it, or the waiting
    call would reach Kraken with a nonce below the one just accepted.
    """
    clock = FakeClock()
    transport = TimedTransport(clock, api_key_info=TRADE_ONLY)
    other_process = _venue(transport, clock, home)
    waits: list[float] = []

    def sleep_while_the_other_process_sends(seconds: float) -> None:
        if not waits:
            other_process.check_key()
        waits.append(seconds)
        clock.sleep(seconds)

    venue = KrakenVenue(KEY, SECRET, transport, now_ms=clock.now, home=home, sleep=sleep_while_the_other_process_sends)
    venue.check_key()
    clock.advance_ms(250)
    venue.check_key()

    assert waits == [pytest.approx(0.75)]
    wire = _nonces(transport)
    assert len(wire) == 3
    assert wire == sorted(set(wire))
    assert wire[-1] == transport.sent_at_us[-1]


def test_new_home_never_sends_a_nonce_below_an_earlier_homes(home, tmp_path_factory):
    clock = FakeClock()
    old_transport = TimedTransport(clock, latency_ms=120, balances={"ZUSD": "10"})
    _venue(old_transport, clock, home).snapshot()
    accepted = max(_nonces(old_transport))

    clock.advance_ms(1)
    fresh_home = tmp_path_factory.mktemp("fresh-home")
    new_transport = TimedTransport(clock, balances={"ZUSD": "10"})
    _venue(new_transport, clock, fresh_home).snapshot()

    assert min(_nonces(new_transport)) > accepted


@pytest.mark.usefixtures("live_capable_release")
def test_rate_limit_journal_ts_stays_in_seconds(home):
    clock = FakeClock()
    transport = TimedTransport(clock, latency_ms=120, rate_limit_first_n=3)
    venue = _venue(transport, clock, home)

    with pytest.raises(RateLimitStop):
        venue.place_entry_with_stop("coid-rl", Decimal("0.1"), Decimal(100), pair="XBTUSD")

    assert [s for s in clock.sleeps if s >= 300.0] == [300.0, 600.0]
    assert all(s <= 1.0 for s in clock.sleeps if s < 300.0)
    lines = (home / "journal" / "2023-11.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if '"kind":"rate_limit"' in line]
    assert [r["ts"] for r in records] == [clock.now() // US_PER_S]
