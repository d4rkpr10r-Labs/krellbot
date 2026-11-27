"""Per-bar fills behind a research result, served beside the stored result.

The bar-by-bar trace explains what the rules decided. It does not say what
the backtest filled. ``BarRecord.fill`` is one slot per bar: when a fresh
long is stopped on its own entry candle, the slot keeps only the stop and
the entry leg disappears from the per-bar record (the ledger still pays for
both legs).

``ResearchService`` therefore builds the fills itself, keyed by ``bar_ts``,
including that missing entry leg, and checks every leg against the ledger.
The job manager stores them in a side-car outside ``research-jobs/`` and
``GET /api/v1/jobs/{job_id}/fills`` serves them behind the same session gate
as the result route. The stored ``{legacy_receipt, trace}`` result, the
receipt, ``ResearchResult.detail`` and ``BarRecord`` keep their shapes.

All data here is synthetic and labelled as such; nothing is market data.
"""

from __future__ import annotations

import csv
import json
import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from krellbot.application.research import ResearchRequest, ResearchService
from krellbot.backtest.engine import Backtester
from krellbot.domain.trace import shared_decision_trace
from krellbot.pack.model import Candle
from tests.test_ns06_api import TEST_PORT, asgi_call
from tests.test_ns06_jobs import _bootstrap_session, _build_app, _get_job, _submit_job

# (ts_ms, open, high, low, close); volume is a constant 100 for every bar.
# Bar 2 closes above sma2 (entry, 10% stop -> 90). Bar 3 buys at its open
# (100) and its low (80) breaches the stop on the same candle.
ENTRY_CANDLE_STOP_BARS = (
    (0, "90", "90", "90", "90"),
    (3_600_000, "90", "90", "90", "90"),
    (7_200_000, "90", "100", "90", "100"),
    (10_800_000, "100", "100", "80", "80"),
)
# Entry on bar 2, held through bar 3, exit signal on bar 4 (close 95 crosses
# below sma2), sell at bar 5's open. The stop (90) is never touched.
ENTRY_THEN_EXIT_BARS = (
    (0, "90", "90", "90", "90"),
    (3_600_000, "90", "90", "90", "90"),
    (7_200_000, "90", "100", "90", "100"),
    (10_800_000, "100", "106", "99", "105"),
    (14_400_000, "105", "105", "94", "95"),
    (18_000_000, "96", "97", "93", "94"),
)
VOLUME = "100"

DETAIL_KEYS = {"schema_version", "venue", "pair", "tf", "bar_count", "trade_count", "trace", "data_source"}
RECEIPT_KEYS = {
    "engine_version",
    "pack_sha256",
    "data_manifest_sha256",
    "venue",
    "pair",
    "tf",
    "from",
    "to",
    "fee_bps",
    "slippage_bps",
    "slippage_mult",
    "metrics",
    "equity_curve",
}
LEG_KEYS = {"side", "price", "qty", "fee", "is_stop"}
TEN_THOUSAND = Decimal(10000)


def _pack() -> dict:
    return {
        "schema_version": 1,
        "id": "synthetic-fills",
        "version": "1.0.0",
        "label": "Synthetic fills",
        "author": "krellbot tests",
        "origin": "Synthetic test data for per-bar research fills; not market data.",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 10}},
        "markets": [{"venue": "kraken", "pair": "SUIUSD"}],
    }


def _candles(bars) -> list[Candle]:
    return [
        Candle(
            ts_ms=ts,
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(low),
            close=Decimal(c),
            volume=Decimal(VOLUME),
        )
        for ts, o, h, low, c in bars
    ]


def _write_inputs(directory: Path, bars) -> tuple[Path, Path]:
    pack_path = directory / "synthetic-fills-pack.json"
    pack_path.write_text(json.dumps(_pack()), encoding="utf-8")
    csv_path = directory / "synthetic-fills.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ts_ms", "open", "high", "low", "close", "volume"])
        for ts, o, h, low, c in bars:
            writer.writerow([ts, o, h, low, c, VOLUME])
    return pack_path, csv_path


def _run(home: Path, bars, *, fee_bps: int, slippage_bps: int, slippage_mult: float):
    pack_path, csv_path = _write_inputs(home, bars)
    return ResearchService(home=home).run(
        ResearchRequest(
            pack_path=pack_path,
            dataset_csv=csv_path,
            venue="kraken",
            pair="SUIUSD",
            starting_cash=Decimal(10000),
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            slippage_mult=slippage_mult,
        )
    )


def _slip(slippage_bps: int, slippage_mult: float) -> Decimal:
    return (Decimal(slippage_bps) / TEN_THOUSAND) * Decimal(str(slippage_mult))


def _cash_after_legs(start: Decimal, legs: list[dict]) -> Decimal:
    cash = start
    for leg in legs:
        amount = leg["qty"] * leg["price"]
        cash = cash - (amount + leg["fee"]) if leg["side"] == "buy" else cash + (amount - leg["fee"])
    return cash


# ---------------------------------------------------------------------------
# ResearchService builds per-bar fills, including the dropped entry leg
# ---------------------------------------------------------------------------


def test_entry_candle_stop_fills_keep_the_entry_leg_the_bar_record_drops(home: Path) -> None:
    fee_bps, slippage_bps, slippage_mult = 10, 20, 1.5
    result = _run(home, ENTRY_CANDLE_STOP_BARS, fee_bps=fee_bps, slippage_bps=slippage_bps, slippage_mult=slippage_mult)
    assert result.ok is True, result.refusal

    # The engine's single per-bar slot still reports only the stop.
    records = Backtester(
        _pack(),
        _candles(ENTRY_CANDLE_STOP_BARS),
        starting_cash=Decimal(10000),
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        slippage_mult=slippage_mult,
    ).run()
    assert [r.fill["side"] for r in records if r.fill is not None] == ["sell"]

    # The research fills carry both legs on the entry candle, in order.
    fills = result.fills
    assert isinstance(fills, dict)
    assert list(fills) == [10_800_000]
    buy, stop = fills[10_800_000]
    assert set(buy) == LEG_KEYS and set(stop) == LEG_KEYS

    slip = _slip(slippage_bps, slippage_mult)
    fee_rate = Decimal(fee_bps)
    buy_price = Decimal(100) * (1 + slip)
    qty = (Decimal(10000) / (buy_price * (1 + fee_rate / TEN_THOUSAND))).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )
    assert buy == {
        "side": "buy",
        "price": buy_price,
        "qty": qty,
        "fee": qty * buy_price * fee_rate / TEN_THOUSAND,
        "is_stop": False,
    }
    sell_price = Decimal(90) * (1 - slip)
    assert stop == {
        "side": "sell",
        "price": sell_price,
        "qty": qty,
        "fee": qty * sell_price * fee_rate / TEN_THOUSAND,
        "is_stop": True,
    }
    assert all(isinstance(leg[k], Decimal) for leg in (buy, stop) for k in ("price", "qty", "fee"))

    # Both legs reconcile exactly with the ledger's final cash.
    assert _cash_after_legs(Decimal(10000), [buy, stop]) == records[-1].cash_after

    # The pinned shapes stay as they are.
    assert set(result.legacy_receipt) == RECEIPT_KEYS
    assert set(result.detail) == DETAIL_KEYS
    assert result.detail["trace"] == shared_decision_trace(_pack(), _candles(ENTRY_CANDLE_STOP_BARS))


def test_entry_and_signal_exit_fill_on_their_own_bars(home: Path) -> None:
    result = _run(home, ENTRY_THEN_EXIT_BARS, fee_bps=40, slippage_bps=5, slippage_mult=1.0)
    assert result.ok is True, result.refusal
    trace = result.detail["trace"]
    assert [bar["target"]["reason"] for bar in trace] == ["warmup", "flat", "entry", "flat", "exit", "flat"]

    fills = result.fills
    assert list(fills) == [10_800_000, 18_000_000]
    (buy,) = fills[10_800_000]
    (sell,) = fills[18_000_000]
    assert buy["side"] == "buy" and buy["is_stop"] is False
    assert sell["side"] == "sell" and sell["is_stop"] is False
    assert buy["qty"] == sell["qty"] > 0
    assert buy["price"] == Decimal(100) * (1 + _slip(5, 1.0))
    assert sell["price"] == Decimal(96) * (1 - _slip(5, 1.0))

    records = Backtester(
        _pack(), _candles(ENTRY_THEN_EXIT_BARS), starting_cash=Decimal(10000), fee_bps=40, slippage_bps=5
    ).run()
    assert _cash_after_legs(Decimal(10000), [buy, sell]) == records[-1].cash_after
    assert result.detail["trade_count"] == 1


def test_a_run_without_fills_has_an_empty_mapping_not_invented_legs(home: Path) -> None:
    flat_bars = tuple((i * 3_600_000, "90", "90", "90", "90") for i in range(4))
    result = _run(home, flat_bars, fee_bps=40, slippage_bps=5, slippage_mult=1.0)
    assert result.ok is True, result.refusal
    assert result.fills == {}


# ---------------------------------------------------------------------------
# The job side-car and GET /api/v1/jobs/{job_id}/fills
# ---------------------------------------------------------------------------


def _get(app, path: str, headers: list[tuple[str, str]]):
    status, hdrs, raw, _cookies = asgi_call(app, method="GET", path=path, headers=headers)
    return status, {k.lower(): v for k, v in hdrs}, (json.loads(raw) if raw else None)


def _read_headers(session: str) -> list[tuple[str, str]]:
    return [("Host", f"127.0.0.1:{TEST_PORT}"), ("Cookie", f"krellbot_session={session}")]


def _wait_terminal(app, csrf: str, session: str, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    snap: dict = {}
    while time.monotonic() < deadline:
        _status, snap = _get_job(app, csrf, session, job_id)
        if snap["state"] in {"succeeded", "failed", "cancelled"}:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished: {snap}")


def _submit_entry_candle_stop_job(home: Path, app, csrf: str, session: str) -> str:
    inputs = home / "inputs"
    inputs.mkdir()
    pack_path, csv_path = _write_inputs(inputs, ENTRY_CANDLE_STOP_BARS)
    status, job = _submit_job(
        app,
        csrf,
        session,
        {
            "correlation_id": "fills",
            "pack_path": str(pack_path),
            "dataset_csv": str(csv_path),
            "venue": "kraken",
            "fee_bps": 10,
            "slippage_bps": 20,
            "slippage_mult": 1.5,
        },
    )
    assert status == 200, job
    snap = _wait_terminal(app, csrf, session, job["id"])
    assert snap["state"] == "succeeded", snap
    return job["id"]


def test_fills_route_serves_the_side_car_and_leaves_the_stored_result_alone(home: Path) -> None:
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    job_id = _submit_entry_candle_stop_job(home, app, csrf, session)

    status, headers, body = _get(app, f"/api/v1/jobs/{job_id}/fills", _read_headers(session))
    assert status == 200, body
    assert headers.get("cache-control") == "no-store"
    assert body["schema_version"] == "1"
    assert body["job_id"] == job_id
    assert list(body["fills"]) == ["10800000"]
    buy, stop = body["fills"]["10800000"]
    assert buy["side"] == "buy" and buy["is_stop"] is False
    assert stop["side"] == "sell" and stop["is_stop"] is True
    # Exact Decimal values travel as plain decimal strings, never floats or
    # exponent notation.
    for leg in (buy, stop):
        assert set(leg) == LEG_KEYS
        for key in ("price", "qty", "fee"):
            assert isinstance(leg[key], str) and "E" not in leg[key].upper()
    assert Decimal(buy["qty"]) == Decimal(stop["qty"]) > 0
    assert Decimal(buy["price"]) == Decimal(100) * (1 + _slip(20, 1.5))

    # The stored result is still exactly {legacy_receipt, trace}.
    status, _h, stored = _get(app, f"/api/v1/jobs/{job_id}/result", _read_headers(session))
    assert status == 200
    assert set(stored) == {"legacy_receipt", "trace"}
    assert set(stored["legacy_receipt"]) == RECEIPT_KEYS
    assert stored["trace"] == shared_decision_trace(_pack(), _candles(ENTRY_CANDLE_STOP_BARS))

    # The side-car is not a *.json file under research-jobs/.
    assert sorted(p.name for p in (home / "research-jobs").iterdir()) == [f"{job_id}.json"]
    persisted = json.loads((home / "research-jobs" / f"{job_id}.json").read_text(encoding="utf-8"))
    assert set(persisted) == {"job", "result", "run"}
    assert set(persisted["result"]) == {"legacy_receipt", "trace"}


def test_fills_survive_a_workstation_restart(home: Path) -> None:
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    job_id = _submit_entry_candle_stop_job(home, app, csrf, session)
    _s, _h, before = _get(app, f"/api/v1/jobs/{job_id}/fills", _read_headers(session))

    restarted = _build_app(home)
    _csrf2, session2 = _bootstrap_session(restarted)
    status, _h, after = _get(restarted, f"/api/v1/jobs/{job_id}/fills", _read_headers(session2))
    assert status == 200, after
    assert after == before


def test_fills_route_keeps_the_session_origin_and_host_gates(home: Path) -> None:
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    job_id = _submit_entry_candle_stop_job(home, app, csrf, session)
    path = f"/api/v1/jobs/{job_id}/fills"

    no_cookie = [("Host", f"127.0.0.1:{TEST_PORT}")]
    status, _h, body = _get(app, path, no_cookie)
    assert status == 403 and "fills" not in (body or {})

    invented = [("Host", f"127.0.0.1:{TEST_PORT}"), ("Cookie", "krellbot_session=invented")]
    status, _h, body = _get(app, path, invented)
    assert status == 403 and "fills" not in (body or {})

    foreign_origin = [*_read_headers(session), ("Origin", "https://example.com")]
    status, _h, body = _get(app, path, foreign_origin)
    assert status == 403 and "fills" not in (body or {})

    foreign_host = [("Host", "example.com"), ("Cookie", f"krellbot_session={session}")]
    status, _h, body = _get(app, path, foreign_host)
    assert status == 403 and "fills" not in (body or {})


def test_fills_route_says_unavailable_instead_of_inventing_fills(home: Path) -> None:
    def runner_without_fills(_request):
        return {"ok": True, "result_ref": "ref", "legacy_receipt": {"engine_version": "t"}, "trace": []}

    app = _build_app(home, runner=runner_without_fills)
    csrf, session = _bootstrap_session(app)

    status, _h, body = _get(app, "/api/v1/jobs/unknown-job/fills", _read_headers(session))
    assert status == 404 and body["code"] == "not_found"

    _s, job = _submit_job(app, csrf, session, {"correlation_id": "no-fills"})
    assert _wait_terminal(app, csrf, session, job["id"])["state"] == "succeeded"
    status, _h, body = _get(app, f"/api/v1/jobs/{job['id']}/fills", _read_headers(session))
    assert status == 404
    assert body["code"] == "fills_unavailable"
    assert "fills" not in body


def test_failed_job_has_no_fills(home: Path) -> None:
    def refusing_runner(_request):
        return {"ok": False, "code": "invalid_dataset", "message": "dataset missing"}

    app = _build_app(home, runner=refusing_runner)
    csrf, session = _bootstrap_session(app)
    _s, job = _submit_job(app, csrf, session, {"correlation_id": "refused"})
    assert _wait_terminal(app, csrf, session, job["id"])["state"] == "failed"
    status, _h, body = _get(app, f"/api/v1/jobs/{job['id']}/fills", _read_headers(session))
    assert status == 404 and body == {"code": "result_unavailable"}


@pytest.mark.parametrize("job_id", ["..%2F..%2Fdatasets", "a.b", "%2e%2e"])
def test_fills_route_never_reads_outside_its_side_car_directory(home: Path, job_id: str) -> None:
    app = _build_app(home)
    _csrf, session = _bootstrap_session(app)
    (home / "datasets.json").write_text("{}", encoding="utf-8")
    status, _h, body = _get(app, f"/api/v1/jobs/{job_id}/fills", _read_headers(session))
    assert status == 404
    assert body == {"code": "not_found", "message": "job not found"}
