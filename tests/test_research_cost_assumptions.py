"""Research cost assumptions and a stressed-cost rerun.

A research run states the costs it charged: the fee model ('taker, per
side'), ``fee_bps``, ``slippage_bps``, the Decimal total of every fill fee,
the return of a zero-cost rerun and the return under twice the fee and
slippage. The block is served from a side-car at
``GET /api/v1/jobs/{job_id}/costs``; the 13-key receipt, the research
detail keys and the stored ``{legacy_receipt, trace}`` result stay as they
are.

Every scenario below is explicitly synthetic local data, never market data.
Expected values are computed by hand from literal prices with Decimal
arithmetic that mirrors the engine's locked rules (buy at
``open * (1 + slip)``, sell at ``open * (1 - slip)``, a stop at
``min(stop, open) * (1 - slip)``, a fee of ``notional * fee_bps / 10000`` on
every fill, entry quantity rounded down to 8 decimals).
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from krellbot.api.jobs import canonical_receipt_bytes
from krellbot.application import research as research_module
from krellbot.application.research import ResearchRequest, ResearchService
from krellbot.backtest.engine import Backtester
from krellbot.pack.model import Candle
from tests.test_ns06_api import TEST_PORT, asgi_call
from tests.test_ns06_jobs import _bootstrap_session, _build_app, _get_job, _submit_job

QTY = Decimal("0.00000001")
VOLUME = "100"

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
METRIC_KEYS = {
    "total_return_pct",
    "cagr_pct",
    "max_drawdown_pct",
    "return_to_dd",
    "per_year",
    "trade_count",
    "exposure_pct",
    "buy_and_hold",
}
DETAIL_KEYS = {"schema_version", "venue", "pair", "tf", "bar_count", "trade_count", "trace", "data_source"}

# Entry signal on bar 2 (close 100 crosses above sma2 95, 10% stop -> 90).
# Bar 3 buys at its open of 100 and its low of 80 breaches the stop on the
# same candle, so the stop sells at min(90, 100) = 90 before the bar closes.
ENTRY_CANDLE_STOP_BARS = (
    (0, "90", "90", "90", "90"),
    (3_600_000, "90", "90", "90", "90"),
    (7_200_000, "90", "100", "90", "100"),
    (10_800_000, "100", "100", "80", "80"),
)

# One ordinary round trip, then an entry-candle stop:
#   bar 2 entry signal (stop 90); bar 3 buys at 100, low 95 holds the stop;
#   bar 4 close 100 crosses below sma2 105 -> exit signal; bar 5 sells at 100;
#   bar 7 close 120 crosses above sma2 110 -> entry signal (stop 108);
#   bar 8 buys at 120 and its low 100 breaches 108 -> stop sells at 108.
ROUND_TRIP_THEN_ENTRY_STOP_BARS = (
    (0, "90", "90", "90", "90"),
    (3_600_000, "90", "90", "90", "90"),
    (7_200_000, "90", "100", "90", "100"),
    (10_800_000, "100", "110", "95", "110"),
    (14_400_000, "110", "110", "100", "100"),
    (18_000_000, "100", "100", "100", "100"),
    (21_600_000, "100", "100", "100", "100"),
    (25_200_000, "100", "120", "100", "120"),
    (28_800_000, "120", "120", "100", "100"),
)


def _pack() -> dict:
    return {
        "schema_version": 1,
        "id": "synthetic-cost-assumptions",
        "version": "1.0.0",
        "label": "Synthetic cost assumptions",
        "author": "krellbot tests",
        "origin": "Synthetic regression data for research cost assumptions; not market data.",
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
    pack_path = directory / "synthetic-cost-pack.json"
    pack_path.write_text(json.dumps(_pack()), encoding="utf-8")
    csv_path = directory / "synthetic-cost-candles.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts_ms", "open", "high", "low", "close", "volume"])
        for ts, o, h, low, c in bars:
            writer.writerow([ts, o, h, low, c, VOLUME])
    return pack_path, csv_path


def _request(tmp_path: Path, bars, *, starting_cash: Decimal, fee_bps: int, slippage_bps: int) -> ResearchRequest:
    pack_path, csv_path = _write_inputs(tmp_path, bars)
    return ResearchRequest(
        pack_path=pack_path,
        dataset_csv=csv_path,
        venue="kraken",
        starting_cash=starting_cash,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )


def _run(tmp_path: Path, bars, *, starting_cash: Decimal, fee_bps: int, slippage_bps: int):
    request = _request(tmp_path, bars, starting_cash=starting_cash, fee_bps=fee_bps, slippage_bps=slippage_bps)
    return ResearchService(home=tmp_path).run_with_cost_reruns(request)


def _pct(final_cash: Decimal, starting_cash: Decimal) -> Decimal:
    return (final_cash / starting_cash - 1) * 100


def test_total_fees_include_the_entry_leg_an_entry_candle_stop_drops(tmp_path, monkeypatch):
    monkeypatch.setenv("KRELLBOT_HOME", str(tmp_path))
    cash0 = Decimal("10030.02")

    # Nominal run: 10 bps fee, 20 bps slippage. 10030.02 buys exactly 100.
    #   buy  100 @ 100 * 1.002 = 100.2 -> notional 10020,  fee 10.02
    #   stop 100 @  90 * 0.998 =  89.82 -> notional 8982,  fee 8.982
    #   cash 10030.02 - 10030.02 + (8982 - 8.982) = 8973.018
    buy_fee = Decimal(100) * Decimal("100.2") * Decimal("0.001")
    stop_fee = Decimal(100) * Decimal("89.82") * Decimal("0.001")
    assert buy_fee == Decimal("10.02") and stop_fee == Decimal("8.982")
    total_fees = Decimal("19.002")
    assert buy_fee + stop_fee == total_fees

    # Zero-cost rerun: 10030.02 / 100 = 100.3002 bought at 100, stopped at 90.
    gross_cash = Decimal("10030.02") - Decimal("100.3002") * 100 + Decimal("100.3002") * 90
    assert gross_cash == Decimal("9027.018")
    assert _pct(gross_cash, cash0) == Decimal(-10)

    # Stressed rerun: 2x -> 20 bps fee (0.002), 40 bps slippage (0.004).
    #   buy price 100.4, qty floor(10030.02 / (100.4 * 1.002), 8) = 99.70119521
    #   stop price 90 * 0.996 = 89.64
    stressed_qty = (cash0 / (Decimal("100.4") * Decimal("1.002"))).quantize(QTY, rounding=ROUND_DOWN)
    assert stressed_qty == Decimal("99.70119521")
    stressed_buy = stressed_qty * Decimal("100.4")
    stressed_sell = stressed_qty * Decimal("89.64")
    stressed_cash = cash0 - stressed_buy * Decimal("1.002") + (stressed_sell - stressed_sell * Decimal("0.002"))
    assert stressed_cash == Decimal("8919.34070926498320")

    result = _run(
        tmp_path,
        ENTRY_CANDLE_STOP_BARS,
        starting_cash=cash0,
        fee_bps=10,
        slippage_bps=20,
    )
    assert result.ok is True, result.refusal
    costs = result.costs
    assert isinstance(costs, dict)
    assert costs["schema_version"] == "1"
    assert costs["fee_model"] == "taker, per side"
    assert costs["fee_bps"] == 10
    assert costs["slippage_bps"] == 20
    assert costs["slippage_mult"] == 1.0
    assert Decimal(costs["starting_cash"]) == cash0
    assert costs["fill_count"] == 2
    assert Decimal(costs["total_fees"]) == total_fees
    assert Decimal(costs["net_return_pct"]) == _pct(Decimal("8973.018"), cash0)
    assert Decimal(costs["gross_return_pct"]) == Decimal(-10)
    assert costs["stressed"]["factor"] == 2
    assert costs["stressed"]["fee_bps"] == 20
    assert costs["stressed"]["slippage_bps"] == 40
    assert costs["stressed"]["slippage_mult"] == 1.0
    assert Decimal(costs["stressed"]["return_pct"]) == _pct(stressed_cash, cash0)
    # Decimal text, never exponent notation the browser cannot read as a number.
    for text in (costs["total_fees"], costs["net_return_pct"], costs["gross_return_pct"]):
        assert "E" not in text and "e" not in text

    # The per-bar fill slot keeps only the terminal stop, so summing it
    # alone undercounts by exactly the dropped entry leg.
    records = Backtester(
        _pack(), _candles(ENTRY_CANDLE_STOP_BARS), starting_cash=cash0, fee_bps=10, slippage_bps=20
    ).run()
    slot_fees = sum((r.fill["fee"] for r in records if r.fill is not None), Decimal(0))
    assert slot_fees == stop_fee
    assert total_fees - slot_fees == buy_fee

    # The total is summed from the run's ledger-reconciled fills, which name
    # the entry leg on the bar that filled it.
    legs = [(bar_ts, leg) for bar_ts, bar_legs in result.fills.items() for leg in bar_legs]
    assert [(bar_ts, leg["side"], leg["is_stop"]) for bar_ts, leg in legs] == [
        (10_800_000, "buy", False),
        (10_800_000, "sell", True),
    ]
    assert legs[0][1]["price"] == Decimal("100.2")
    assert legs[0][1]["qty"] == Decimal(100)
    assert legs[0][1]["fee"] == buy_fee
    assert legs[1][1]["price"] == Decimal("89.82")
    assert legs[1][1]["fee"] == stop_fee


def test_total_fees_sum_every_fill_across_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("KRELLBOT_HOME", str(tmp_path))
    cash0 = Decimal(10010)

    # 10 bps fee, no slippage.
    #   bar 3 buy  100 @ 100 (10010 / 100.1 = 100) fee 10      cash 0
    #   bar 5 sell 100 @ 100                     fee 10      cash 9990
    #   bar 8 buy  floor(9990 / 120.12, 8) = 83.16683316 @ 120
    #   bar 8 stop 83.16683316 @ 108 (min(108, 120))
    qty2 = (Decimal(9990) / Decimal("120.12")).quantize(QTY, rounding=ROUND_DOWN)
    assert qty2 == Decimal("83.16683316")
    fees = [
        Decimal(10),
        Decimal(10),
        qty2 * Decimal(120) * Decimal("0.001"),
        qty2 * Decimal(108) * Decimal("0.001"),
    ]
    assert fees[2] == Decimal("9.98001997920")
    assert fees[3] == Decimal("8.98201798128")
    total_fees = Decimal("38.96203796048")
    assert sum(fees, Decimal(0)) == total_fees
    final_cash = Decimal(9990) - qty2 * Decimal(120) * Decimal("1.001") + qty2 * Decimal(108) * Decimal("0.999")
    assert final_cash == Decimal("8973.03596411952")

    # Zero-cost rerun: 100.1 @ 100 out @ 100, then floor(10010 / 120, 8) @ 120 -> 108.
    gross_qty2 = (Decimal(10010) / Decimal(120)).quantize(QTY, rounding=ROUND_DOWN)
    assert gross_qty2 == Decimal("83.41666666")
    gross_cash = Decimal(10010) - gross_qty2 * 120 + gross_qty2 * 108
    assert gross_cash == Decimal("9009.00000008")

    # Stressed rerun: 20 bps fee, 2 x 0 = 0 bps slippage.
    s_qty1 = (cash0 / Decimal("100.2")).quantize(QTY, rounding=ROUND_DOWN)
    assert s_qty1 == Decimal("99.90019960")
    s_cash = cash0 - s_qty1 * 100 * Decimal("1.002") + s_qty1 * 100 * Decimal("0.998")
    s_qty2 = (s_cash / Decimal("120.24")).quantize(QTY, rounding=ROUND_DOWN)
    assert s_qty2 == Decimal("82.91783034")
    s_cash = s_cash - s_qty2 * 120 * Decimal("1.002") + s_qty2 * 108 * Decimal("0.998")
    assert s_cash == Decimal("8937.21542544496")

    result = _run(
        tmp_path,
        ROUND_TRIP_THEN_ENTRY_STOP_BARS,
        starting_cash=cash0,
        fee_bps=10,
        slippage_bps=0,
    )
    assert result.ok is True, result.refusal
    assert result.legacy_receipt["metrics"]["trade_count"] == 2
    costs = result.costs
    assert costs["fill_count"] == 4
    assert Decimal(costs["total_fees"]) == total_fees
    assert Decimal(costs["net_return_pct"]) == _pct(final_cash, cash0)
    assert costs["net_return_pct"] is not None
    assert result.legacy_receipt["metrics"]["total_return_pct"] == pytest.approx(
        float(_pct(final_cash, cash0)), abs=1e-9
    )
    assert Decimal(costs["gross_return_pct"]) == _pct(gross_cash, cash0)
    assert costs["stressed"]["fee_bps"] == 20
    assert costs["stressed"]["slippage_bps"] == 0
    assert Decimal(costs["stressed"]["return_pct"]) == _pct(s_cash, cash0)

    # The total is summed over every leg of the run's fills, in bar order.
    legs = [(bar_ts, leg) for bar_ts, bar_legs in result.fills.items() for leg in bar_legs]
    assert [(bar_ts, leg["side"], leg["is_stop"]) for bar_ts, leg in legs] == [
        (10_800_000, "buy", False),
        (18_000_000, "sell", False),
        (28_800_000, "buy", False),
        (28_800_000, "sell", True),
    ]
    assert [leg["fee"] for _bar_ts, leg in legs] == fees


def test_costs_leave_receipt_and_detail_shapes_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("KRELLBOT_HOME", str(tmp_path))
    request = _request(
        tmp_path,
        ENTRY_CANDLE_STOP_BARS,
        starting_cash=Decimal("10030.02"),
        fee_bps=10,
        slippage_bps=20,
    )
    service = ResearchService(home=tmp_path)
    nominal = service.run(request)
    result = service.run_with_cost_reruns(request)
    assert result.ok is True
    # The reruns never replace the nominal receipt or detail.
    assert result.legacy_receipt == nominal.legacy_receipt
    assert result.detail == nominal.detail
    assert set(result.legacy_receipt) == RECEIPT_KEYS
    assert set(result.legacy_receipt["metrics"]) == METRIC_KEYS
    assert set(result.detail) == DETAIL_KEYS
    assert "costs" not in result.legacy_receipt
    assert "costs" not in result.detail
    # The receipt describes the nominal run, not a rerun.
    assert result.legacy_receipt["fee_bps"] == 10
    assert result.legacy_receipt["slippage_bps"] == 20


def test_plain_run_reports_nominal_costs_and_backtests_once(tmp_path, monkeypatch):
    monkeypatch.setenv("KRELLBOT_HOME", str(tmp_path))
    built: list[int] = []
    real = research_module.Backtester

    class _Counting(real):
        def __init__(self, *args, **kwargs):
            built.append(kwargs["fee_bps"])
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(research_module, "Backtester", _Counting)
    request = _request(
        tmp_path,
        ENTRY_CANDLE_STOP_BARS,
        starting_cash=Decimal("10030.02"),
        fee_bps=10,
        slippage_bps=20,
    )
    service = ResearchService(home=tmp_path)

    result = service.run(request)
    assert built == [10]
    assert Decimal(result.costs["total_fees"]) == Decimal("19.002")
    assert "gross_return_pct" not in result.costs
    assert "stressed" not in result.costs

    built.clear()
    full = service.run_with_cost_reruns(request)
    # One ordinary run each: nominal, zero-cost, then 2x fee and slippage.
    assert built == [10, 0, 20]
    assert full.costs["total_fees"] == result.costs["total_fees"]


def test_refused_research_run_has_no_costs(tmp_path, monkeypatch):
    monkeypatch.setenv("KRELLBOT_HOME", str(tmp_path))
    result = ResearchService(home=tmp_path).run_with_cost_reruns(
        ResearchRequest(pack_path=tmp_path / "missing.json", dataset_csv=tmp_path / "missing.csv", venue="kraken")
    )
    assert result.ok is False
    assert result.costs is None


# ---------------------------------------------------------------------------
# Side-car and route
# ---------------------------------------------------------------------------


def _read_headers(csrf: str, session: str) -> list[tuple[str, str]]:
    return [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
        ("X-Krellbot-CSRF", csrf),
        ("Cookie", f"krellbot_session={session}"),
    ]


def _get(app, path: str, headers: list[tuple[str, str]]):
    status, _hdrs, body, _cookies = asgi_call(app, method="GET", path=path, headers=headers)
    return status, (json.loads(body) if body else None)


def _wait_terminal(app, csrf, session, job_id, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    snap: dict | None = None
    while time.monotonic() < deadline:
        _s, snap = _get_job(app, csrf, session, job_id)
        if snap["state"] in {"succeeded", "failed", "cancelled"}:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished: {snap}")


def test_costs_route_serves_the_side_car_and_keeps_the_stored_result(home: Path) -> None:
    pack_path, csv_path = _write_inputs(home, ENTRY_CANDLE_STOP_BARS)
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    status, job = _submit_job(
        app,
        csrf,
        session,
        {
            "kind": "research.backtest",
            "correlation_id": "costs",
            "pack_path": str(pack_path),
            "dataset_csv": str(csv_path),
            "venue": "kraken",
            "starting_cash": "10030.02",
            "fee_bps": 10,
            "slippage_bps": 20,
        },
    )
    assert status == 200, job
    job_id = job["id"]
    final = _wait_terminal(app, csrf, session, job_id)
    assert final["state"] == "succeeded", final

    status, body = _get(app, f"/api/v1/jobs/{job_id}/costs", _read_headers(csrf, session))
    assert status == 200, body
    assert body["fee_model"] == "taker, per side"
    assert body["fee_bps"] == 10
    assert body["slippage_bps"] == 20
    assert Decimal(body["total_fees"]) == Decimal("19.002")
    assert Decimal(body["gross_return_pct"]) == Decimal(-10)
    assert body["stressed"]["fee_bps"] == 20
    assert body["stressed"]["slippage_bps"] == 40

    # The fee total is the sum of the same job's served fills, entry leg included.
    status, fills = _get(app, f"/api/v1/jobs/{job_id}/fills", _read_headers(csrf, session))
    assert status == 200, fills
    served_fees = [Decimal(leg["fee"]) for legs in fills["fills"].values() for leg in legs]
    assert len(served_fees) == body["fill_count"] == 2
    assert sum(served_fees, Decimal(0)) == Decimal(body["total_fees"])

    status, stored = _get(app, f"/api/v1/jobs/{job_id}/result", _read_headers(csrf, session))
    assert status == 200
    assert set(stored) == {"legacy_receipt", "trace"}
    assert set(stored["legacy_receipt"]) == RECEIPT_KEYS
    assert set(stored["legacy_receipt"]["metrics"]) == METRIC_KEYS
    # The side-car names the receipt it describes.
    assert (
        body["receipt_canonical_sha256"]
        == hashlib.sha256(canonical_receipt_bytes(stored["legacy_receipt"])).hexdigest()
    )

    # The side-car lives outside research-jobs/; only the job record is JSON there.
    assert (home / "research-costs" / f"{job_id}.json").is_file()
    assert sorted(p.name for p in (home / "research-jobs").rglob("*.json")) == [f"{job_id}.json"]
    job_record = json.loads((home / "research-jobs" / f"{job_id}.json").read_text(encoding="utf-8"))
    assert set(job_record) == {"job", "result"}
    assert set(job_record["result"]) == {"legacy_receipt", "trace"}

    # A restarted workstation restores the job and still serves its costs.
    restarted = _build_app(home)
    csrf2, session2 = _bootstrap_session(restarted)
    status, again = _get(restarted, f"/api/v1/jobs/{job_id}/costs", _read_headers(csrf2, session2))
    assert status == 200
    assert again == body


def test_costs_route_keeps_the_session_and_origin_gate(home: Path) -> None:
    app = _build_app(home, runner=lambda _request: {"ok": True, "result_ref": "r", "legacy_receipt": {}, "trace": []})
    csrf, session = _bootstrap_session(app)
    _s, job = _submit_job(app, csrf, session, {"correlation_id": "gate"})
    job_id = job["id"]
    _wait_terminal(app, csrf, session, job_id)
    path = f"/api/v1/jobs/{job_id}/costs"

    no_session = [("Host", f"127.0.0.1:{TEST_PORT}"), ("Origin", f"http://127.0.0.1:{TEST_PORT}")]
    status, body = _get(app, path, no_session)
    assert status == 403, body

    invented = no_session + [("Cookie", "krellbot_session=invented")]
    status, body = _get(app, path, invented)
    assert status == 403, body

    foreign_origin = [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", "http://evil.example"),
        ("Cookie", f"krellbot_session={session}"),
    ]
    status, body = _get(app, path, foreign_origin)
    assert status == 403, body

    foreign_host = [
        ("Host", "evil.example"),
        ("Cookie", f"krellbot_session={session}"),
    ]
    status, body = _get(app, path, foreign_host)
    assert status == 403, body


def test_costs_route_is_unavailable_without_a_side_car(home: Path) -> None:
    # An injected runner (or a job from before costs were recorded) has no side-car.
    app = _build_app(home, runner=lambda _request: {"ok": True, "result_ref": "r", "legacy_receipt": {}, "trace": []})
    csrf, session = _bootstrap_session(app)
    headers = _read_headers(csrf, session)

    status, body = _get(app, "/api/v1/jobs/no-such-job/costs", headers)
    assert status == 404
    assert body["code"] == "not_found"

    _s, job = _submit_job(app, csrf, session, {"correlation_id": "no-costs"})
    _wait_terminal(app, csrf, session, job["id"])
    status, body = _get(app, f"/api/v1/jobs/{job['id']}/costs", headers)
    assert status == 404
    assert body == {"code": "costs_unavailable"}


def test_failed_research_job_has_no_costs(home: Path) -> None:
    app = _build_app(home)
    csrf, session = _bootstrap_session(app)
    _s, job = _submit_job(
        app,
        csrf,
        session,
        {"correlation_id": "refused", "pack_path": str(home / "missing.json"), "dataset_csv": str(home / "x.csv")},
    )
    final = _wait_terminal(app, csrf, session, job["id"])
    assert final["state"] == "failed"
    status, body = _get(app, f"/api/v1/jobs/{job['id']}/costs", _read_headers(csrf, session))
    assert status == 404
    assert body == {"code": "costs_unavailable"}
    assert not (home / "research-costs").exists()
