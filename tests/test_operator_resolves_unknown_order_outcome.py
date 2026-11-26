"""An operator resolves an unknown order outcome from Operations; nothing is resent.

``operations.resolve_order`` (``POST /api/v1/commands``) retires one
unresolved intent:

* With a recorded venue observation (a ``reconcile_*`` row) it settles from
  that evidence, and no typed confirmation is needed.
* Without one the operator asserts 'did not execute' or 'executed N' (a
  finite, non-negative Decimal no larger than the requested quantity) and
  types the client order id back exactly. Anything else is refused with a
  stable code and writes nothing.

Executed quantity is journaled once per coid under ``resolved_coid``, under
the venue's tick lock. Durable writes follow ``run._resolve_obligations``:
evidence, then the journal record, then the ``intent_resolved`` row, so an
interruption never retires an obligation without its accounting. No order is
sent or re-sent: the fake transports record no call during a resolve.

Live paths run on fake transports with the release gate patched in-process
(``live_capable_release``); ``PAPER_ONLY_RELEASE`` stays True in source.
"""

# The imported ``live`` fixture is requested by parameter name.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fakes.fake_binance import FakeBinanceTransport

from krellbot import journal as kb_journal
from krellbot import release_policy, run
from krellbot.application import alerts as kb_alerts
from krellbot.application import operations as kb_operations
from krellbot.execution import obligations as kb_obligations
from krellbot.execution.obligations import read_obligations
from krellbot.run.lock import TickLock
from krellbot.run.reconcile import OwnershipIdentity, project_ownership
from krellbot.storage.database import OperationalStore
from krellbot.venues.base import ProtectiveStopRejectedError
from krellbot.venues.binance import BinanceVenue
from tests.test_live_entry_ack_without_fill_is_reconciled import (
    ENTRY_BARS,
    ENTRY_COID,
    IDENTITY,
    _add_orders,
    _enter,
    _entry_row,
    _reconciled_entry_qty,
    _resting_stop,
    _tick,
    _transport,
    live,  # noqa: F401  (the fixture: a live Kraken arm with a grant)
)
from tests.test_unresolved_intents_are_recovered_from_evidence import _arm, _crash

TEST_PORT = 8080
BOOT_TOKEN = "boot-operator-resolve"
COMMAND = "operations.resolve_order"
CANARY = "CANARY-not-the-client-order-id"

live_release = pytest.mark.usefixtures("live_capable_release")


# ---- a minimal ASGI client ----------------------------------------------------


def _call(app, *, method: str, path: str, body: bytes = b"", headers: list[tuple[str, str]]):
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
        "query_string": b"",
        "server": ("127.0.0.1", 0),
        "client": ("127.0.0.1", 12345),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    flat = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in captured["headers"]]
    return captured["status"], flat, captured["body"]


def _base_headers(body: bytes) -> list[tuple[str, str]]:
    return [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]


def _app(home: Path):
    from krellbot.api.app import create_app

    return create_app(home=home, port=TEST_PORT, bootstrap_token=BOOT_TOKEN)


def _session(app) -> dict[str, str]:
    body = json.dumps({"token": BOOT_TOKEN}).encode("utf-8")
    status, headers, raw = _call(
        app, method="POST", path="/api/v1/session/bootstrap", body=body, headers=_base_headers(body)
    )
    assert status == 200, raw
    cookie = next(v for k, v in headers if k == "set-cookie" and v.startswith("krellbot_session="))
    return {"cookie": cookie.split(";", 1)[0], "csrf": json.loads(raw)["csrf_token"]}


def _post_command(app, session: dict[str, str] | None, payload: dict, **overrides: str) -> tuple[int, dict]:
    body = json.dumps({"schema_version": "1", "command": COMMAND, "payload": payload}).encode("utf-8")
    headers = dict(_base_headers(body))
    if session is not None:
        headers["Cookie"] = session["cookie"]
        headers["X-Krellbot-CSRF"] = session["csrf"]
    for name, value in overrides.items():
        headers[name.replace("_", "-")] = value
    status, _headers, raw = _call(
        app, method="POST", path="/api/v1/commands", body=body, headers=[(k, v) for k, v in headers.items() if v]
    )
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"raw": raw.decode("utf-8", "replace")}


def _resolve(home: Path, **payload) -> dict:
    app = _app(home)
    status, body = _post_command(app, _session(app), payload)
    assert status == 200, body
    return body


# ---- durable state ----------------------------------------------------------------


def _ledger(home: Path) -> list[tuple[str, dict]]:
    return [(kind, json.loads(payload)) for _id, kind, payload in OperationalStore(home / "ops.sqlite").read_ledger()]


def _resolutions(home: Path) -> list[dict]:
    return [p for kind, p in _ledger(home) if kind == kb_obligations.RESOLVED_KIND]


def _journal(home: Path) -> list[dict]:
    return list(kb_journal.read_records_lenient(home))


def _requested(home: Path, coid: str) -> Decimal:
    [outbox] = [p for kind, p in _ledger(home) if kind == "outbox" and p["coid"] == coid]
    return Decimal(json.loads(outbox["body"])["qty"])


def _held(home: Path, *, venue: str = "kraken", mode: str = "live") -> bool:
    return read_obligations(home).blocks_entries(venue=venue, mode=mode)


def _writes(home: Path) -> tuple[list, list]:
    return _ledger(home), _journal(home)


# ---- no venue evidence: the operator asserts and types the coid back --------------


@live_release
def test_operator_asserted_execution_is_journaled_once_and_nothing_is_resent(live):
    transport = _transport()
    _enter(live, transport)  # Kraken acknowledged the entry; it cannot show what executed
    _tick(live, transport, ENTRY_BARS)
    assert kb_operations.list_unresolved_orders(live, now=0)[0]["last_lookup_result"] == "none"
    executed = (_requested(live, ENTRY_COID) / 2).quantize(Decimal("0.00001"))
    calls_before, gets_before = len(transport.calls), len(transport.gets)

    body = _resolve(
        live, client_order_id=ENTRY_COID, outcome="executed", executed_qty=str(executed), confirmation=ENTRY_COID
    )

    assert (body["ok"], body["code"], body["effect"]) == (True, "order_resolved", "changed")
    assert (body["outcome"], Decimal(body["filled_qty"]), body["evidence"]) == ("filled", executed, "operator_asserted")
    assert transport.calls[calls_before:] == [] and transport.gets[gets_before:] == []  # nothing sent or looked up
    assert _reconciled_entry_qty(live) == [executed]
    assert project_ownership(live, IDENTITY).attributable_qty == executed
    [resolution] = _resolutions(live)
    assert (resolution["coid"], resolution["outcome"], Decimal(resolution["filled_qty"])) == (
        ENTRY_COID,
        "filled",
        executed,
    )
    assert not _held(live)
    assert kb_operations.list_unresolved_orders(live, now=0) == []
    assert not [a for a in kb_alerts.collect(live, now=0) if a.kind == "needs_reconcile"]

    # Submitting the same resolution again changes nothing.
    again = _resolve(
        live, client_order_id=ENTRY_COID, outcome="executed", executed_qty=str(executed), confirmation=ENTRY_COID
    )
    assert (again["ok"], again["code"], again["effect"]) == (True, "order_already_resolved", "unchanged")
    _tick(live, transport, ENTRY_BARS)
    assert _reconciled_entry_qty(live) == [executed]
    assert len(_resolutions(live)) == 1
    assert [a.form["cl_ord_id"] for a in _add_orders(transport)] == [ENTRY_COID]  # sent once, never again


@live_release
@pytest.mark.parametrize(
    ("confirmation", "code"),
    [
        (None, "confirmation_required"),
        ("", "confirmation_required"),
        (CANARY, "confirmation_mismatch"),
        (f" {ENTRY_COID}", "confirmation_mismatch"),
        (ENTRY_COID[:-1], "confirmation_mismatch"),
    ],
    ids=["missing", "empty", "other-text", "leading-space", "truncated"],
)
@pytest.mark.parametrize("outcome", ["executed", "not_executed"])
def test_assertion_without_evidence_needs_the_client_order_id_typed_back(live, confirmation, code, outcome):
    transport = _transport()
    _enter(live, transport)
    before = _writes(live)
    payload = {"client_order_id": ENTRY_COID, "outcome": outcome}
    if outcome == "executed":
        payload["executed_qty"] = "0.5"
    if confirmation is not None:
        payload["confirmation"] = confirmation

    body = _resolve(live, **payload)

    assert (body["ok"], body["code"], body["effect"]) == (False, code, "refused")
    assert _writes(live) == before
    assert _held(live)


@live_release
def test_resolve_without_an_assertion_needs_recorded_venue_evidence(live):
    transport = _transport()
    _enter(live, transport)
    before = _writes(live)

    body = _resolve(live, client_order_id=ENTRY_COID)

    assert (body["ok"], body["code"], body["effect"]) == (False, "no_venue_evidence", "refused")
    assert _writes(live) == before
    assert _held(live)


@live_release
@pytest.mark.parametrize(
    ("executed_qty", "code"),
    [
        ("NaN", "executed_qty_invalid"),
        ("Infinity", "executed_qty_invalid"),
        ("-0.5", "executed_qty_invalid"),
        ("CANARY", "executed_qty_invalid"),
        ("", "executed_qty_invalid"),
        (None, "executed_qty_invalid"),
        (0.5, "executed_qty_invalid"),  # a JSON float is not an exact Decimal
        ("above", "executed_qty_above_requested"),
    ],
    ids=["nan", "infinity", "negative", "text", "empty", "missing", "json-float", "above-requested"],
)
def test_executed_quantity_must_be_a_finite_non_negative_decimal_within_the_request(live, executed_qty, code):
    transport = _transport()
    _enter(live, transport)
    if executed_qty == "above":
        executed_qty = str(_requested(live, ENTRY_COID) + Decimal("0.00001"))
    before = _writes(live)
    payload = {"client_order_id": ENTRY_COID, "outcome": "executed", "confirmation": ENTRY_COID}
    if executed_qty is not None:
        payload["executed_qty"] = executed_qty

    body = _resolve(live, **payload)

    assert (body["ok"], body["code"], body["effect"]) == (False, code, "refused")
    assert _writes(live) == before
    assert _held(live)


@live_release
def test_did_not_execute_assertion_retires_the_order_without_accounting(live):
    transport = _transport()
    _enter(live, transport)
    calls_before = len(transport.calls)

    body = _resolve(live, client_order_id=ENTRY_COID, outcome="not_executed", confirmation=ENTRY_COID)

    assert (body["ok"], body["code"], body["outcome"], Decimal(body["filled_qty"])) == (
        True,
        "order_resolved",
        "absent",
        Decimal(0),
    )
    assert transport.calls[calls_before:] == []
    assert _reconciled_entry_qty(live) == []
    assert project_ownership(live, IDENTITY).attributable_qty == 0
    [resolution] = _resolutions(live)
    assert (resolution["outcome"], resolution["evidence"]) == ("absent", "operator_asserted")
    assert not _held(live)


# ---- recorded venue evidence: no typed confirmation, but only for an outcome -------


def _observations(home: Path) -> list[tuple[str, str]]:
    return [
        (k, p.get("status")) for k, p in _ledger(home) if p.get("coid") == ENTRY_COID and k.startswith("reconcile_")
    ]


def _working_evidence(home: Path, transport, *, executed: bool) -> Decimal:
    """Kraken shows the entry still on the book, partly executed or not; return what executed."""
    requested = _requested(home, ENTRY_COID)
    first = (requested / 4).quantize(Decimal("0.00001")) if executed else Decimal(0)
    transport.set_order_books(open_rows=_entry_row(vol=requested, vol_exec=first, status="open"))
    transport.balances["SUI"] = str(first)
    _tick(home, transport, ENTRY_BARS)
    assert _reconciled_entry_qty(home) == ([first] if executed else [])
    assert _observations(home) == ([("reconcile_filled", "partial")] if executed else [("reconcile_open", "open")])
    assert _held(home)
    return first


def _partial_evidence(home: Path, transport) -> Decimal:
    return _working_evidence(home, transport, executed=True)


def _filled_unprotected_evidence(home: Path, transport) -> Decimal:
    """Kraken shows the entry closed and executed, with no stop resting: the tick holds it."""
    requested = _requested(home, ENTRY_COID)
    transport.set_order_books(closed_rows=_entry_row(vol=requested, vol_exec=requested, status="closed"))
    transport.balances["SUI"] = str(requested)
    _tick(home, transport, ENTRY_BARS)
    assert _reconciled_entry_qty(home) == [requested]
    assert _observations(home) == [("reconcile_filled", "filled")]
    assert _held(home)
    return requested


@live_release
def test_recorded_venue_observation_resolves_without_typed_confirmation(live):
    transport = _transport()
    _enter(live, transport)
    executed = _filled_unprotected_evidence(live, transport)
    assert kb_operations.list_unresolved_orders(live, now=0)[0]["last_lookup_result"] == "filled"
    calls_before = len(transport.calls)

    body = _resolve(live, client_order_id=ENTRY_COID)

    assert (body["ok"], body["code"], body["effect"]) == (True, "order_resolved", "changed")
    assert (body["outcome"], Decimal(body["filled_qty"]), body["evidence"]) == (
        "filled",
        executed,
        "operator_accepted_venue_observation",
    )
    assert transport.calls[calls_before:] == []
    assert _reconciled_entry_qty(live) == [executed]  # already accounted: journaled once, not again
    assert project_ownership(live, IDENTITY).attributable_qty == executed
    assert not _held(live)
    assert len(_add_orders(transport)) == 1


@live_release
@pytest.mark.parametrize("executed", [False, True], ids=["open", "partial"])
def test_order_still_working_is_not_settled_from_its_lookup(live, executed):
    transport = _transport()
    _enter(live, transport)
    _working_evidence(live, transport, executed=executed)
    before = _writes(live)
    calls_before = len(transport.calls)

    body = _resolve(live, client_order_id=ENTRY_COID)

    assert (body["ok"], body["code"], body["effect"]) == (False, "order_still_working", "refused")
    assert "still working" in body["message"]
    assert _writes(live) == before
    assert _held(live)  # the entry hold on kraken/live stays
    assert transport.calls[calls_before:] == []


@live_release
@pytest.mark.parametrize("executed", [False, True], ids=["open", "partial"])
def test_typed_assertion_settles_an_order_still_working(live, executed):
    transport = _transport()
    _enter(live, transport)
    first = _working_evidence(live, transport, executed=executed)
    asserted = (_requested(live, ENTRY_COID) / 2).quantize(Decimal("0.00001")) if executed else Decimal(0)
    payload = {"client_order_id": ENTRY_COID, "confirmation": ENTRY_COID}
    payload.update({"outcome": "executed", "executed_qty": str(asserted)} if executed else {"outcome": "not_executed"})
    assert _resolve(live, **{**payload, "confirmation": CANARY})["code"] == "confirmation_mismatch"
    calls_before = len(transport.calls)

    body = _resolve(live, **payload)

    assert (body["ok"], body["code"], body["evidence"]) == (True, "order_resolved", "operator_asserted")
    assert (body["outcome"], Decimal(body["filled_qty"])) == (("filled", asserted) if executed else ("absent", 0))
    assert transport.calls[calls_before:] == []
    assert sum(_reconciled_entry_qty(live), Decimal(0)) == asserted
    assert _reconciled_entry_qty(live) == ([first, asserted - first] if executed else [])
    assert project_ownership(live, IDENTITY).attributable_qty == asserted
    assert not _held(live)
    assert len(_add_orders(transport)) == 1


@live_release
def test_assertion_after_partial_accounting_journals_only_the_remainder(live):
    transport = _transport()
    _enter(live, transport)
    first = _partial_evidence(live, transport)
    final = (_requested(live, ENTRY_COID) / 2).quantize(Decimal("0.00001"))
    before = _writes(live)

    below = _resolve(
        live, client_order_id=ENTRY_COID, outcome="executed", executed_qty=str(first / 2), confirmation=ENTRY_COID
    )
    assert (below["ok"], below["code"]) == (False, "executed_below_accounted")
    assert _writes(live) == before

    body = _resolve(
        live, client_order_id=ENTRY_COID, outcome="executed", executed_qty=str(final), confirmation=ENTRY_COID
    )

    assert (body["ok"], body["code"], Decimal(body["filled_qty"])) == (True, "order_resolved", final)
    assert _reconciled_entry_qty(live) == [first, final - first]
    assert project_ownership(live, IDENTITY).attributable_qty == final
    assert not _held(live)


# ---- the tick lock and the order of durable writes --------------------------------


@live_release
def test_resolve_waits_for_a_running_tick_and_a_later_tick_does_not_account_again(live):
    transport = _transport()
    _enter(live, transport)
    executed = _requested(live, ENTRY_COID)
    payload = {
        "client_order_id": ENTRY_COID,
        "outcome": "executed",
        "executed_qty": str(executed),
        "confirmation": ENTRY_COID,
    }
    lock = TickLock(live, "kraken")
    assert lock.acquire()
    try:
        before = _writes(live)
        busy = _resolve(live, **payload)
        assert (busy["ok"], busy["code"], busy["effect"]) == (False, "tick_running", "refused")
        assert _writes(live) == before
    finally:
        lock.release()

    assert _resolve(live, **payload)["code"] == "order_resolved"
    # The venue later shows the same execution: the tick never accounts it twice.
    transport.set_order_books(
        open_rows=_resting_stop(executed),
        closed_rows=_entry_row(vol=executed, vol_exec=executed, status="closed"),
    )
    transport.balances["SUI"] = str(executed)
    for _ in range(2):
        _tick(live, transport, ENTRY_BARS)

    assert _reconciled_entry_qty(live) == [executed]
    assert len(_resolutions(live)) == 1
    assert len(_add_orders(transport)) == 1


@live_release
def test_resolve_writes_evidence_then_journal_then_resolution(live, monkeypatch):
    transport = _transport()
    _enter(live, transport)
    order: list[str] = []
    real_assert, real_append, real_resolve = (
        kb_obligations.record_assertion,
        kb_journal.append,
        kb_obligations.record_resolution,
    )

    def _spy(name, real):
        def wrapped(*args, **kwargs):
            order.append(name)
            return real(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(kb_obligations, "record_assertion", _spy("evidence", real_assert))
    monkeypatch.setattr(kb_journal, "append", _spy("journal", real_append))
    monkeypatch.setattr(kb_obligations, "record_resolution", _spy("resolution", real_resolve))

    body = _resolve(live, client_order_id=ENTRY_COID, outcome="executed", executed_qty="0.5", confirmation=ENTRY_COID)

    assert body["code"] == "order_resolved"
    assert order == ["evidence", "journal", "resolution"]
    [assertion] = [p for k, p in _ledger(live) if k == kb_obligations.OPERATOR_ASSERTION_KIND]
    assert (assertion["coid"], assertion["outcome"], Decimal(assertion["filled_qty"])) == (
        ENTRY_COID,
        "executed",
        Decimal("0.5"),
    )


@live_release
@pytest.mark.parametrize("crash_at", ["journal", "resolution"])
def test_interrupted_resolve_never_retires_without_accounting_and_converges_once(live, monkeypatch, crash_at):
    transport = _transport()
    _enter(live, transport)
    executed = Decimal("0.5")
    target = (kb_journal, "append") if crash_at == "journal" else (kb_obligations, "record_resolution")
    real = getattr(*target)

    def die(*args, **kwargs):
        raise RuntimeError(f"interrupted at the {crash_at} write")

    def resolve():
        return run.resolve_order_by_operator(
            home=live,
            coid=ENTRY_COID,
            outcome="executed",
            executed_qty=str(executed),
            confirmation=ENTRY_COID,
            clock=lambda: 1_700_000_000,
        )

    monkeypatch.setattr(*target, die)
    with pytest.raises(RuntimeError):
        resolve()
    assert _resolutions(live) == []
    assert _held(live)  # the obligation is not retired without durable accounting
    assert _reconciled_entry_qty(live) == ([] if crash_at == "journal" else [executed])
    probe = TickLock(live, "kraken")
    assert probe.acquire()  # the lock was released on the way out
    probe.release()

    monkeypatch.setattr(*target, real)
    assert resolve().code == "order_resolved"
    assert resolve().code == "order_already_resolved"

    assert _reconciled_entry_qty(live) == [executed]
    assert len(_resolutions(live)) == 1
    assert not _held(live)
    assert len(_add_orders(transport)) == 1


# ---- the Binance stop-rejection marker (#627) ------------------------------------

BINANCE_PAIR = "BTCUSDT"
BINANCE_PACK = "binance-resolve"
BINANCE_COID = "binance-entry-coid-0001"
BINANCE_IDENTITY = OwnershipIdentity(pack_id=BINANCE_PACK, venue="binance", pair=BINANCE_PAIR, mode="live")


@live_release
def test_binance_stop_rejection_marker_is_cleared_by_an_operator_resolve(home):
    transport = FakeBinanceTransport(
        post_responses=[
            {"orderId": 11, "status": "FILLED", "executedQty": "0.01000"},
            {"code": -2010, "msg": "Account has insufficient balance for requested action."},
        ]
    )
    venue = BinanceVenue("test-key", "test-secret", transport, now_fn=lambda: 1_700_000_000_000)
    body = json.dumps(
        {"kind": "entry", "qty": "0.01", "stop": "90000", "pair": BINANCE_PAIR}, separators=(",", ":"), sort_keys=True
    )
    with pytest.raises(ProtectiveStopRejectedError):
        run._dispatch_place(
            home=home,
            mode="live",
            venue="binance",
            coid=BINANCE_COID,
            body=body,
            venue_obj=venue,
            identity={"pack_id": BINANCE_PACK, "pack_version": "1.0.0", "bar_ts": 7_200_000},
        )
    assert [p["coid"] for k, p in _ledger(home) if k == "needs_reconcile"] == [BINANCE_COID]
    assert _held(home, venue="binance")
    calls_before = len(transport.calls)

    result = _resolve(
        home, client_order_id=BINANCE_COID, outcome="executed", executed_qty="0.01", confirmation=BINANCE_COID
    )

    assert (result["ok"], result["code"]) == (True, "order_resolved")
    assert transport.calls[calls_before:] == []
    reconciled = [
        r["detail"]
        for r in _journal(home)
        if r.get("kind") == "tick" and (r.get("detail") or {}).get("resolved_coid") == BINANCE_COID
    ]
    assert [Decimal(d["entry_qty"]) for d in reconciled] == [Decimal("0.01")]
    assert project_ownership(home, BINANCE_IDENTITY).attributable_qty == Decimal("0.01")
    assert not _held(home, venue="binance")
    assert kb_operations.list_unresolved_orders(home, now=0) == []
    assert not [a for a in kb_alerts.collect(home, now=0) if a.kind == "needs_reconcile"]


# ---- the shipped paper build and the request gate ---------------------------------


def test_paper_obligation_is_resolved_in_the_paper_only_release(home):
    assert release_policy.PAPER_ONLY_RELEASE is True
    _arm(home)
    coid = _crash(home=home)  # committed, never reached the venue, nothing looked up
    assert _held(home, mode="paper")

    body = _resolve(home, client_order_id=coid, outcome="not_executed", confirmation=coid)

    assert (body["ok"], body["code"], body["outcome"]) == (True, "order_resolved", "absent")
    assert not _held(home, mode="paper")
    outbox = [p for k, p in _ledger(home) if k == "outbox" and p["coid"] == coid]
    assert [p["sent"] for p in outbox] == [False]  # retired, never resent


def _detail_free_lookup(home: Path, coid: str, kind: str) -> None:
    store = OperationalStore(home / "ops.sqlite")
    with store.transaction() as conn:  # an older lookup row: no status, no quantity
        conn.execute("INSERT INTO ledger (kind, payload) VALUES (?, ?)", (kind, json.dumps({"coid": coid})))


def test_lookup_row_without_detail_still_settles_an_absent_order(home):
    _arm(home)
    coid = _crash(home=home)
    _detail_free_lookup(home, coid, "reconcile_absent")
    assert kb_operations.list_unresolved_orders(home, now=0)[0]["last_lookup_result"] == "absent"

    body = _resolve(home, client_order_id=coid)

    assert (body["ok"], body["code"], body["outcome"], Decimal(body["filled_qty"])) == (
        True,
        "order_resolved",
        "absent",
        Decimal(0),
    )
    assert body["evidence"] == "operator_accepted_venue_observation"
    assert not _held(home, mode="paper")


def test_lookup_row_without_detail_showing_open_is_still_working(home):
    _arm(home)
    coid = _crash(home=home)
    _detail_free_lookup(home, coid, "reconcile_open")
    assert kb_operations.list_unresolved_orders(home, now=0)[0]["last_lookup_result"] == "open"
    before = _writes(home)

    body = _resolve(home, client_order_id=coid)

    assert (body["ok"], body["code"], body["effect"]) == (False, "order_still_working", "refused")
    assert _writes(home) == before
    assert _held(home, mode="paper")
    assert _resolve(home, client_order_id=coid, outcome="not_executed", confirmation=coid)["code"] == "order_resolved"
    assert not _held(home, mode="paper")


@pytest.mark.parametrize(
    "payload",
    [
        {"client_order_id": 7},
        {"outcome": "not_executed"},
        {"client_order_id": "", "outcome": "not_executed", "confirmation": ""},
        {"client_order_id": "COID", "outcome": "CANARY", "confirmation": "COID"},
        {"client_order_id": "COID", "outcome": ["executed"], "confirmation": "COID"},
        {"client_order_id": "COID", "outcome": "not_executed", "executed_qty": "1", "confirmation": "COID"},
        {"client_order_id": "COID", "confirmation": "COID"},
    ],
    ids=[
        "coid-not-text",
        "coid-missing",
        "coid-empty",
        "unknown-outcome",
        "outcome-not-text",
        "quantity-with-did-not-execute",
        "confirmation-without-assertion",
    ],
)
def test_malformed_resolve_request_is_refused_as_bad_input(home, payload):
    _arm(home)
    coid = _crash(home=home)
    payload = {k: (coid if v == "COID" else v) for k, v in payload.items()}
    before = _writes(home)

    body = _resolve(home, **payload)

    assert (body["ok"], body["code"], body["effect"]) == (False, "bad_input", "refused")
    assert _writes(home) == before
    assert _held(home, mode="paper")


def test_unknown_client_order_id_is_refused(home):
    _arm(home)
    _crash(home=home)
    before = _writes(home)

    body = _resolve(home, client_order_id=CANARY, outcome="not_executed", confirmation=CANARY)

    assert (body["ok"], body["code"], body["effect"]) == (False, "order_not_found", "refused")
    assert _writes(home) == before


def test_unreadable_store_refuses_instead_of_guessing(home):
    (home / "ops.sqlite").write_bytes(b"this is not a sqlite database" * 64)

    body = _resolve(home, client_order_id=CANARY, outcome="not_executed", confirmation=CANARY)

    assert (body["ok"], body["code"], body["effect"]) == (False, "obligations_unavailable", "refused")
    assert _journal(home) == []


@pytest.mark.parametrize(
    "gate",
    ["no-session", "no-csrf", "wrong-csrf", "foreign-origin", "foreign-host"],
)
def test_resolve_command_keeps_the_session_csrf_origin_and_host_gate(home, gate):
    _arm(home)
    coid = _crash(home=home)
    app = _app(home)
    session = _session(app)
    before = _writes(home)
    overrides: dict[str, str] = {}
    if gate == "no-session":
        session = None
    elif gate == "no-csrf":
        overrides["X_Krellbot_CSRF"] = ""
    elif gate == "wrong-csrf":
        overrides["X_Krellbot_CSRF"] = CANARY
    elif gate == "foreign-origin":
        overrides["Origin"] = "http://evil.example"
    else:
        overrides["Host"] = "evil.example:8080"

    status, _body = _post_command(
        app, session, {"client_order_id": coid, "outcome": "not_executed", "confirmation": coid}, **overrides
    )

    assert status == 403
    assert _writes(home) == before
    assert _held(home, mode="paper")
