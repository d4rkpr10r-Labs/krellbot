"""Run engine: arm, disarm, tick, status, journal.

`tick` is the only function that talks to a venue. It:
    1. Acquires `$KRELLBOT_HOME/run/<venue>.lock`. If held, exit 0 silently
       with `another tick running` on stdout.
    2. Loads the license cache + armed packs config.
    3. Snapshots the venue.
    4. For each armed pack on the venue: ensure a resting stop if owned > 0.
       If the stop-place fails, market-exit the owned qty in the same tick.
    5. Acts on the latest closed bar only. Missed bars are coalesced by
       feeding every candle up to and including the latest into evaluate.run
       so indicator state includes them.
    6. Entry / exit / raise_stop per the engine rules.
    7. Journal one tick record per pack per bar, including refusals.
    8. If entries are blocked by the license gate, print the lapsed message.

`arm_pack` records a pack into config. It refuses a second pack on the same
venue+pair, refuses if the cap cannot meet the pair's costmin (paper), and
for live mode requires ``skip_confirm=True`` (web ``live.arm`` after an
operator grant that already required typed LIVE). The CLI no longer prompts
``Type LIVE to arm:`` on stdin — that path is refused with an honest
workstation-grant message (M13.5).

Money is `Decimal`. No HTTP. No secret in any log/journal line.
"""

from __future__ import annotations

import errno
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from krellbot import catalog as kb_catalog
from krellbot import config as kb_config
from krellbot import journal as kb_journal
from krellbot import license as kb_license
from krellbot import paths as kb_paths
from krellbot import secrets as kb_secrets
from krellbot.application import live_gate
from krellbot.data.instruments import InstrumentMetadataError
from krellbot.domain import trace as kb_trace
from krellbot.execution import obligations as kb_obligations
from krellbot.pack import evaluate as kb_evaluate
from krellbot.pack import lint as kb_pack_lint
from krellbot.pack.model import Candle as Candle
from krellbot.run.reconcile import OwnershipIdentity, project_ownership, stop_fill_history
from krellbot.storage.database import OperationalStore, StoreError, StoreFull
from krellbot.storage.outbox import (
    ModeError,
    Outbox,
    audit_late_fill,
    audit_needs_reconcile,
    audit_partial_fill,
    audit_unknown_ack,
    audited_coids,
    dispatched_coids,
)
from krellbot.venues.base import OrderOutcome, OrderRef, PairRules, ProtectiveStopRejectedError, VenueRejectedError
from krellbot.venues.paper import SLIPPAGE_BPS

from .lock import TickLock

# Tick ``intent_refused`` code when journal history for an arm cannot be attributed.
OWNERSHIP_AMBIGUOUS = "ownership_ambiguous"
# Journal ``reason`` for a record that accounts a reconciled intent's outcome.
RECONCILED_REASON = "reconciled"
# Obligation kind of an exit hold: a sell on an arm's pair whose order the venue
# could not establish (``Fill.order_unknown``), so it may be that arm's stop.
# It holds the arm's exits and stop repair. No venue order carries its coid, so
# only an operator resolve settles it; "executed N" journals ``stop_qty`` N.
UNLINKED_SELL_KIND = "unlinked_sell"
_QTY_FIELD_FOR_INTENT = {"entry": "entry_qty", "exit": "exit_qty", "stop": "stop_qty", UNLINKED_SELL_KIND: "stop_qty"}


# ---- coid ----------------------------------------------------------------


def coid_for(
    *,
    pack_id: str,
    pack_version: str,
    venue: str,
    pair: str,
    bar_ts: int,
    intent: str,
) -> str:
    """First 18 hex chars of sha256 over the canonical intent key."""
    raw = f"{pack_id}|{pack_version}|{venue}|{pair}|{bar_ts}|{intent}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]


# ---- arm -----------------------------------------------------------------


def arm_pack(
    pack_path: Path | str,
    *,
    venue: str,
    mode: str,
    paper_balance: Decimal | None = None,
    requires_license: bool | None = None,
    confirm_fn: Callable[[], str] | None = None,
    key_check: Callable[[str], Any] | None = None,
    home: Path | None = None,
    skip_confirm: bool = False,
) -> int:
    """Arm a pack on a venue. Returns 0 on success, 1 on refusal, 2 on bad input.

    ``key_check`` is injected so tests don't touch the network. The CLI wires
    ``cli_keys._probe`` for live attempts that still run key diagnostics.

    ``skip_confirm`` is required for a successful live arm: the web
    ``live.arm`` path sets it after the operator typed LIVE to grant
    authorization. Without it, live arm refuses with a workstation-grant
    message instead of blocking on ``Type LIVE to arm:`` stdin.
    ``confirm_fn`` is retained for call-site compatibility and is never
    invoked for live confirmation.
    """
    from krellbot.venues.base import WithdrawCapableError

    home = Path(home) if home is not None else kb_paths.home()
    pack_path = Path(pack_path)
    if not pack_path.exists():
        # Do not interpolate pack_path: CodeQL treats path args reachable
        # from the web live.arm entry as sensitive for clear-text logging.
        print("no such pack", flush=True)
        return 2
    try:
        pack_data = json.loads(pack_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("invalid JSON", flush=True)
        return 2
    if not isinstance(pack_data, dict):
        print("pack must be a JSON object", flush=True)
        return 2
    if kb_pack_lint.is_legacy(pack_data):
        print("legacy: not runnable", flush=True)
        return 2
    errors = kb_pack_lint.check(pack_data)
    if errors:
        for err in errors:
            print(f"{err['field']}: {err['message']}", flush=True)
        return 2

    if venue not in kb_secrets.VENUES:
        print(f"unknown venue: {venue}", flush=True)
        return 2
    if mode not in {"paper", "live"}:
        print(f"unknown mode: {mode}", flush=True)
        return 2

    markets = pack_data.get("markets") or []
    market = next((m for m in markets if m.get("venue") == venue), None)
    if market is None:
        print(f"pack has no market for {venue}", flush=True)
        return 2
    pair = str(market.get("pair", ""))

    pack_id = str(pack_data.get("id", ""))
    pack_version = str(pack_data.get("version", ""))
    cap = Decimal(str(pack_data["risk"]["max_account_pct"]))
    stop_block = pack_data.get("risk", {}).get("stop", {}) or {}
    # Compute the stop. The engine stores a Decimal; the broker will quantize.
    if stop_block.get("type") == "pct":
        stop = Decimal(str(stop_block["pct"]))
    elif stop_block.get("type") == "atr":
        stop = Decimal(str(stop_block.get("mult", "1")))
    else:
        stop = Decimal(0)

    config = kb_config.load_config(home)
    if kb_config.find_armed(config, venue, pair) is not None:
        print(f"already armed: {venue} {pair}", flush=True)
        return 1

    if mode == "live":
        gate_result = live_gate.check_live_send(home, venue=venue, pair=pair)
        if not gate_result.ok:
            if gate_result.code == live_gate.CODE_PAPER_ONLY_RELEASE:
                print(f"live refused: {gate_result.message}", flush=True)
            elif gate_result.code == live_gate.CODE_LIVE_DISABLED:
                print("live is off; set KRELLBOT_ENABLE_LIVE=1 to arm a live pack", flush=True)
            else:
                print(f"live refused: {gate_result.code}", flush=True)
            return 1
        # Must have a stored key for this venue.
        try:
            kb_secrets.get(venue)
        except FileNotFoundError:
            print(f"no key stored for {venue}", flush=True)
            return 1
        except (ValueError, PermissionError) as exc:
            print(f"could not read key for {venue}: {type(exc).__name__}", flush=True)
            return 1
        # Permissions must be trade-yes, withdraw-no.
        if key_check is None:
            print("live arm requires a key_check callable", flush=True)
            return 1
        try:
            perms = key_check(venue)
        except WithdrawCapableError:
            print(f"{venue}: key can withdraw; trade-only keys refused", flush=True)
            return 1
        except (OSError, RuntimeError, ValueError, TypeError):
            print(f"{venue}: key check failed", flush=True)
            return 1
        # Check `can_withdraw` BEFORE `can_trade` so a confirmed-withdraw
        # key reports "withdraw is on" (the actual cause) instead of
        # collapsing to the generic "trade is off" — without changing
        # the engine's refusal contract: every non-`trade_only` outcome
        # still returns 1.
        if getattr(perms, "can_withdraw", False):
            print(f"{venue}: withdraw is on; trade-only keys refused", flush=True)
            return 1
        if perms is None or not getattr(perms, "can_trade", False):
            print(f"{venue}: trade is off", flush=True)
            return 1
        if stop <= Decimal(0):
            print("live arm requires a stop", flush=True)
            return 1
        if cap <= Decimal(0):
            print("live arm requires a cap > 0", flush=True)
            return 1
        # Live arm confirmation lives on the workstation grant (typed LIVE).
        # Do not prompt CLI stdin ``Type LIVE to arm:`` (M13.5). Web
        # ``live.arm`` passes skip_confirm=True after the grant check.
        _ = confirm_fn  # retained for API compatibility; never used for LIVE
        if not skip_confirm:
            print(
                "live arm uses the workstation grant; use live.arm in the web "
                "workstation (CLI does not prompt Type LIVE)",
                flush=True,
            )
            return 1
        if not config.live_first_armed:
            config.live_first_armed = True

    if requires_license is None:
        requires_license = kb_catalog.requires_license_for(pack_path, home)

    if mode == "paper":
        if paper_balance is None:
            print("paper arm requires --paper-balance", flush=True)
            return 2
        if paper_balance <= Decimal(0):
            print("paper-balance must be positive", flush=True)
            return 2
        # Cap must be able to meet costmin: rough check uses a floor of
        # $1 per unit. We don't know the price without a candle feed, so we
        # require paper_balance * cap / 100 >= 0.5 USD (costmin floor).
        cash_for_cap = paper_balance * cap / Decimal(100)
        if cash_for_cap < Decimal("0.5"):
            print("cap cannot meet the pair minimum", flush=True)
            return 1

    kb_paths.ensure_layout()
    sha = hashlib.sha256(pack_path.read_bytes()).hexdigest()
    armed = kb_config.ArmedPack(
        pack_path=str(pack_path),
        pack_sha256=sha,
        pack_id=pack_id,
        pack_version=pack_version,
        venue=venue,
        pair=pair,
        cap=cap,
        stop=stop,
        mode=mode,
        starting_cash=paper_balance if mode == "paper" else None,
        requires_license=bool(requires_license),
        armed_at_ts=int(_now_seconds()),
    )
    config.armed.append(armed)
    kb_config.save_config(home, config)
    print(f"armed {pack_id} {pack_version} on {venue} {pair}", flush=True)
    return 0


def disarm_pack(
    *,
    venue: str,
    pair: str,
    home: Path | None = None,
) -> int:
    """Remove an armed record. Returns 0 on success, 1 if not armed."""
    home = Path(home) if home is not None else kb_paths.home()
    config = kb_config.load_config(home)
    found = kb_config.find_armed(config, venue, pair)
    if found is None:
        print(f"not armed: {venue} {pair}", flush=True)
        return 1
    config.armed = [a for a in config.armed if not (a.venue == venue and a.pair == pair)]
    kb_config.save_config(home, config)
    print(f"disarmed {venue} {pair}", flush=True)
    return 0


def set_stop(
    *,
    venue: str,
    pair: str,
    new_stop: Decimal,
    home: Path | None = None,
) -> int:
    """Raise (only) the stop for an armed pack."""
    home = Path(home) if home is not None else kb_paths.home()
    config = kb_config.load_config(home)
    armed = kb_config.find_armed(config, venue, pair)
    if armed is None:
        print(f"not armed: {venue} {pair}", flush=True)
        return 1
    if new_stop <= armed.stop:
        print("new stop must be strictly above the current stop", flush=True)
        return 1
    armed.stop = new_stop
    kb_config.save_config(home, config)
    print(f"stop set: {venue} {pair} -> {new_stop}", flush=True)
    return 0


# ---- paper send + fault-fill audit ---------------------------------------


def _paper_send_via_outbox(
    *,
    home: Path,
    mode: str,
    coid: str,
    body: str,
    send: Callable[[str, str], None],
    journal_sink: Callable[[dict], Path] | None = None,
) -> str:
    """Commit-then-send a paper order through the Outbox.

    Refuses for non-paper mode by raising ``ModeError``; live arms bypass
    the Outbox entirely and write no ``paper-<venue>.json``. The Outbox
    is the boundary that prevents double-sends across restarts: a second
    call for an already-sent coid returns ``"already_sent"`` without
    invoking ``send``; a committed-but-not-sent coid returns
    ``"needs_reconcile"`` and we record the audit so the next tick can
    reconcile from the venue's snapshot.
    """
    if mode != "paper":
        raise ModeError(f"paper-send invoked in mode={mode!r}; only paper arms route through the outbox")
    store = OperationalStore(home / "ops.sqlite")
    state = Outbox(store).dispatch(coid, body, send, mode=mode, venue=None)
    if state == "needs_reconcile":
        audit_needs_reconcile(store, coid=coid)
        if journal_sink is not None:
            try:
                journal_sink(
                    {
                        "kind": "needs_reconcile",
                        "coid": coid,
                        "body": body,
                    }
                )
            except (ValueError, TypeError):
                pass
    return state


def _paper_dispatch_place(
    *,
    home: Path,
    mode: str,
    coid: str,
    body: str,
    venue_obj: Any,
) -> tuple[str, OrderRef | None]:
    """Thin wrapper that forwards ``_dispatch_place`` with ``mode="paper"``."""
    return _dispatch_place(
        home=home,
        mode="paper",
        venue=None,
        coid=coid,
        body=body,
        venue_obj=venue_obj,
    )


def _dispatch_place(
    *,
    home: Path,
    mode: str,
    venue: str | None,
    coid: str,
    body: str,
    venue_obj: Any,
    identity: dict | None = None,
) -> tuple[str, OrderRef | None]:
    """Commit-before-send dispatch for every ``place_*`` call.

    Both paper and live arms route through here. Paper takes the same
    Outbox path as before; live goes through the same Outbox so a
    coid is durable in ``<home>/ops.sqlite`` BEFORE the venue call.
    On ``needs_reconcile`` the helper records the audit and returns
    ``"needs_reconcile"`` without invoking ``venue_obj``. ``StoreError``
    is propagated; the caller is responsible for refusing before any
    venue call.
    """
    captured: list[OrderRef | None] = [None]

    def _send(c: str, b: str) -> None:
        payload = json.loads(b)
        kind = str(payload.get("kind", ""))
        if kind == "entry":
            captured[0] = venue_obj.place_entry_with_stop(
                c,
                Decimal(str(payload["qty"])),
                Decimal(str(payload["stop"])),
                pair=str(payload["pair"]),
            )
        elif kind == "exit":
            captured[0] = venue_obj.place_exit(c, Decimal(str(payload["qty"])), pair=str(payload["pair"]))
        elif kind == "stop":
            venue_obj.place_stop(
                c,
                Decimal(str(payload["qty"])),
                Decimal(str(payload["stop"])),
                pair=str(payload["pair"]),
            )

    if identity:
        # Owner context travels in the committed intent itself, durable before
        # any send, so a later process can attribute the outcome.
        body = json.dumps({**json.loads(body), **identity}, separators=(",", ":"), sort_keys=True)
    store = OperationalStore(home / "ops.sqlite")
    try:
        state = Outbox(store).dispatch(coid, body, _send, mode=mode, venue=venue)
    except ProtectiveStopRejectedError:
        audit_needs_reconcile(store, coid=coid)
        raise
    if state == "needs_reconcile":
        audit_needs_reconcile(store, coid=coid)
    return state, captured[0]


def _audit_paper_fault_fills(*, home: Path, venue_obj: Any) -> None:
    """Classify venue fills for paper mode and write late_fill / unknown_ack audits.

    A fill whose coid is in the Outbox's dispatched set but matches no
    currently-open order is a late fill (the position was already exited
    or the stop was cancelled). A fill whose coid is not in the dispatched
    set at all is an unknown ack — the venue reported a coid we never
    generated; we audit it but do not resubmit.
    """
    try:
        snap = venue_obj.snapshot()
    except (InstrumentMetadataError, RuntimeError, ValueError, OSError):
        # Metadata refusals and transient venue errors must not crash the
        # tick; the audit is best-effort and the next tick can re-run it.
        return
    store = OperationalStore(home / "ops.sqlite")
    dispatched = dispatched_coids(store)
    already = audited_coids(store, kinds=("late_fill", "unknown_ack"))
    fills = list(getattr(snap, "recent_fills", []) or [])
    open_coids = {getattr(o, "coid", None) for o in (getattr(snap, "open_orders", []) or [])}
    for fill in fills:
        coid = getattr(fill, "coid", None)
        if not coid or coid in already:
            continue
        qty = str(getattr(fill, "qty", "0"))
        price = str(getattr(fill, "price", "0"))
        ts_ms = int(getattr(fill, "ts_ms", 0))
        if coid not in dispatched:
            audit_unknown_ack(store, coid=coid, qty=qty, price=price, ts_ms=ts_ms)
        elif coid not in open_coids:
            audit_late_fill(store, coid=coid, qty=qty, price=price, ts_ms=ts_ms)


# ---- tick ----------------------------------------------------------------


def tick(
    *,
    venue: str,
    venue_obj: Any,
    reader: Callable[[str, str], list],
    journal_sink: Callable[[dict], Path] | None = None,
    clock: Callable[[], float] | None = None,
    home: Path | None = None,
    transport: Any | None = None,
    on_pack_completed: Callable[[str, str], None] | None = None,
) -> int:
    """Run one tick. Returns 0 on success, 1 on refusal.

    `venue_obj` is duck-typed: it must satisfy the Venue protocol surface.
    `journal_sink` defaults to `journal.append` (which requires the required
    keys). Tests inject a recording sink. `transport` defaults to a no-op
    so the CLI does not need to wire one; tests inject a spy to assert that
    telemetry calls happen (or do not happen) when expected.

    Refusal order (the brief):
      1. live gate (A1) — refuses BEFORE any ``venue_obj`` attribute.
      2. store open (C3) — refuses BEFORE any ``venue_obj`` attribute.
      3. per-venue send (C4) — catches ``StoreError`` before generic
         ``RuntimeError``, so a commit failure refuses without a place.
    """
    home = Path(home) if home is not None else kb_paths.home()
    journal = journal_sink if journal_sink is not None else kb_journal.append
    now = clock() if clock is not None else _now_seconds()

    lock = TickLock(home, venue)
    if not lock.acquire():
        print("another tick running", flush=True)
        return 0

    try:
        kb_paths.ensure_layout()
        config = kb_config.load_config(home)
        armed_for_venue = [a for a in config.armed if a.venue == venue]
        if not armed_for_venue:
            print(f"no armed packs for {venue}", flush=True)
            return 0

        # A1: live gate comes BEFORE any venue_obj attribute access.
        # Kill alone still hard-refuses (rc=1). Kill + entries_paused is the
        # restore fence: nothing sends, tick succeeds (rc=0).
        fenced_live = False
        for live_armed in armed_for_venue:
            if live_armed.mode != "live":
                continue
            gate_result = live_gate.check_live_send(home, venue=venue, pair=live_armed.pair)
            if gate_result.ok:
                continue
            if gate_result.code == live_gate.CODE_KILL_SWITCH and live_armed.entries_paused:
                _record_tick(
                    journal,
                    now=int(now),
                    venue=venue,
                    pack=live_armed.pack_id,
                    bar_ts=0,
                    detail={
                        "reason": "live_fenced",
                        "mode": live_armed.mode,
                        "code": gate_result.code,
                        "pair": live_armed.pair,
                    },
                )
                fenced_live = True
                continue
            print(f"live refused: {gate_result.code}", flush=True)
            _record_tick(
                journal,
                now=int(now),
                venue=venue,
                pack=live_armed.pack_id,
                bar_ts=0,
                detail={
                    "reason": "live_refused",
                    "mode": live_armed.mode,
                    "code": gate_result.code,
                    "pair": live_armed.pair,
                },
            )
            return 1
        if fenced_live:
            return 0

        # C3: store open BEFORE any venue call.
        try:
            OperationalStore(home / "ops.sqlite").connect()
        except StoreError as exc:
            print(f"store refused: {exc.code}", flush=True)
            for live_armed in armed_for_venue:
                _record_tick(
                    journal,
                    now=int(now),
                    venue=venue,
                    pack=live_armed.pack_id,
                    bar_ts=0,
                    detail={
                        "reason": "store_refused",
                        "mode": live_armed.mode,
                        "code": exc.code,
                        "intent_refused": exc.code,
                        "pair": live_armed.pair,
                    },
                )
            return 1

        cache = kb_license.read_cache(home)

        snap = venue_obj.snapshot()
        # Settle durable intents from venue evidence before any new decision,
        # independent of signals, bars or which packs are armed now.
        _resolve_obligations(home, venue=venue, venue_obj=venue_obj, snap=snap, journal=journal, now=now)
        snap = venue_obj.snapshot()
        obligations_view = kb_obligations.read_obligations(home)
        snap_balances = _snapshot_balances(snap)
        snap_orders = _snapshot_orders(snap)
        fills_before = {f.coid for f in (snap.recent_fills or [])}
        fill_context: dict[str, dict] = {}
        warned = False
        # C4: per-pack refusal on StoreError during a venue send.
        pack_intent_refused: dict[str, str] = {}
        any_refused = False

        for armed in armed_for_venue:
            pack = armed_pack_dict(home, armed)
            blocked = armed.requires_license and not kb_license.entries_allowed(cache, now=int(now))
            if blocked and not warned:
                print("License lapsed or unreachable: entries off, exits on", flush=True)
                warned = True

            candles = list(reader(venue, armed.pair) or [])
            latest = candles[-1] if candles else None
            bar_ts = int(latest.ts_ms) if latest is not None else 0
            if candles and _already_journaled(home, armed.pack_id, bar_ts, venue=venue, pair=armed.pair):
                if on_pack_completed is not None:
                    on_pack_completed(armed.pack_id, armed.pair)
                continue

            base, _quote = _base_quote(armed.pair)
            venue_base = snap_balances.get(base, Decimal(0))
            identity = _ownership_identity(armed, venue)
            ownership = project_ownership(home, identity)
            # A resting stop of this strategy that filled (possibly inside this
            # tick's own snapshot) is a decrease the journal has not recorded
            # yet. It is recorded below with its fill ids, exactly once.
            stop_fill_ids, stop_fill_qty = _stop_fills_for(home, identity, snap.recent_fills or [])
            journal_qty = max(ownership.attributable_qty - stop_fill_qty, Decimal(0))
            # A sell on the pair that the venue could not link to an order may be
            # this arm's stop. It is never guessed at: while the arm holds
            # something, the sell holds its exits and stop repair until an
            # operator resolves it from Operations.
            hold_refused = False
            if journal_qty > 0:
                try:
                    if _hold_unlinked_sells(home, armed, identity, snap.recent_fills or [], bar_ts=bar_ts):
                        obligations_view = kb_obligations.read_obligations(home)
                except StoreError as exc:
                    pack_intent_refused[armed.pack_id] = exc.code
                    any_refused = True
                    hold_refused = True
                    print(f"intent refused: {exc.code}", flush=True)
            exits_held_by = _exit_holds(obligations_view, identity)
            exits_held = hold_refused or bool(exits_held_by)
            if exits_held_by:
                print("exits held: a sell on the pair is not linked to an order; resolve it in Operations", flush=True)
            if ownership.ambiguous:
                # History that may belong to this arm cannot be attributed, so
                # the strategy cannot be shown to be flat: hold new entries.
                # Exits and stop maintenance act only on what the recorded
                # history supports. A requested open-order quantity is not
                # proof of executed inventory, so it cannot raise that bound;
                # resting orders are left as they are.
                pack_intent_refused.setdefault(armed.pack_id, OWNERSHIP_AMBIGUOUS)
                owned = min(venue_base, journal_qty)
            else:
                recognized = _recognized_open_qty(armed, venue, candles, snap_orders)
                owned = min(venue_base, max(journal_qty, recognized))
            resting = next(
                (
                    o.stop_price
                    for o in snap_orders
                    if getattr(o, "pair", None) == armed.pair and getattr(o, "stop_price", None) is not None
                ),
                None,
            )

            # Intents the venue's documented response refused, as (coid, code).
            venue_refusals: list[tuple[str, str]] = []
            ensure_stop_failed = False
            ensure_stop_entry = ""
            if owned > Decimal(0) and resting is None and latest is not None and not exits_held:
                repair = _stop_price(pack, Decimal(str(latest.close)))
                stop_coid = coid_for(
                    pack_id=armed.pack_id,
                    pack_version=armed.pack_version,
                    venue=venue,
                    pair=armed.pair,
                    bar_ts=bar_ts,
                    intent="stop",
                )
                stop_body = json.dumps(
                    {"kind": "stop", "qty": str(owned), "stop": str(repair), "pair": armed.pair},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                try:
                    stop_state, _stop_ref = _dispatch_place(
                        home=home,
                        mode=armed.mode,
                        venue=venue,
                        identity=_intent_identity(armed, bar_ts),
                        coid=stop_coid,
                        body=stop_body,
                        venue_obj=venue_obj,
                    )
                    if stop_state == "sent":
                        ensure_stop_entry = stop_coid
                    if ensure_stop_entry:
                        fill_context[ensure_stop_entry] = {
                            "pack_id": armed.pack_id,
                            "pack_version": armed.pack_version,
                            "bar_ts": bar_ts,
                            "modeled_px": str(repair),
                            "fee_bps": _fee_bps_for_venue(venue),
                            "kind": "stop",
                        }
                except StoreError as exc:
                    pack_intent_refused[armed.pack_id] = exc.code
                    any_refused = True
                    print(f"intent refused: {exc.code}", flush=True)
                    ensure_stop_failed = True
                except (AttributeError, RuntimeError, ValueError) as exc:
                    if isinstance(exc, VenueRejectedError):
                        pack_intent_refused[armed.pack_id] = _venue_refused(exc, stop_coid, venue_refusals)
                    ensure_stop_failed = True
                    print("ensure_stop failed: stop", flush=True)
                    exit_coid = coid_for(
                        pack_id=armed.pack_id,
                        pack_version=armed.pack_version,
                        venue=venue,
                        pair=armed.pair,
                        bar_ts=bar_ts,
                        intent="exit",
                    )
                    exit_body = json.dumps(
                        {"kind": "exit", "qty": str(owned), "pair": armed.pair},
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    try:
                        _dispatch_place(
                            home=home,
                            mode=armed.mode,
                            venue=venue,
                            identity=_intent_identity(armed, bar_ts),
                            coid=exit_coid,
                            body=exit_body,
                            venue_obj=venue_obj,
                        )
                        fill_context[exit_coid] = {
                            "pack_id": armed.pack_id,
                            "pack_version": armed.pack_version,
                            "bar_ts": bar_ts,
                            "modeled_px": str(Decimal(str(latest.close))),
                            "fee_bps": _fee_bps_for_venue(venue),
                            "kind": "exit",
                        }
                    except StoreError as exc:
                        pack_intent_refused[armed.pack_id] = exc.code
                        any_refused = True
                        print(f"intent refused: {exc.code}", flush=True)
                    except VenueRejectedError as exc:
                        pack_intent_refused[armed.pack_id] = _venue_refused(exc, exit_coid, venue_refusals)
                    except (AttributeError, RuntimeError, ValueError):
                        pass
                    owned = Decimal(0)

            if not candles:
                _record_tick(
                    journal,
                    now=int(now),
                    venue=venue,
                    pack=armed.pack_id,
                    bar_ts=0,
                    detail={
                        "reason": "no_candles",
                        "mode": armed.mode,
                        "pair": armed.pair,
                        "ensure_stop_failed": ensure_stop_failed,
                    },
                )
                if on_pack_completed is not None:
                    on_pack_completed(armed.pack_id, armed.pair)
                continue

            target = kb_evaluate.run(pack, candles)
            trace_extra: dict = {}
            if armed.mode == "paper":
                paper_trace = kb_trace.paper_decision_trace(pack, candles)
                trace_extra["decision_trace_bars"] = len(paper_trace)
                if paper_trace:
                    last_reason = paper_trace[-1]["target"]["reason"]
                    if isinstance(last_reason, str):
                        trace_extra["decision_reason"] = last_reason
            entry_qty = Decimal(0)
            exit_qty = Decimal(0)
            entry_coid_for_signal = ""
            kill_engaged = live_gate.kill_state(home).engaged
            entries_suppressed = armed.entries_paused or kill_engaged
            # Unresolved intents on this venue and mode (or of unknown scope),
            # or an unreadable store, hold new entries. Exits and protection
            # are not gated here.
            reconcile_blocked = obligations_view.blocks_entries(venue=venue, mode=armed.mode)
            metadata_refusal: str | None = None
            rules: PairRules | None = None
            try:
                rules = venue_obj.rules(armed.pair)
            except InstrumentMetadataError as exc:
                metadata_refusal = exc.code
            if (
                not blocked
                and not entries_suppressed
                and not reconcile_blocked
                and not ownership.ambiguous
                and target.long
                and owned == Decimal(0)
                and target.stop_price is not None
                and rules is not None
            ):
                entry_coid_for_signal = coid_for(
                    pack_id=armed.pack_id,
                    pack_version=armed.pack_version,
                    venue=venue,
                    pair=armed.pair,
                    bar_ts=bar_ts,
                    intent="entry",
                )
                if venue_obj.order_by_coid(entry_coid_for_signal) is None:
                    assert rules is not None
                    close_price = Decimal(str(candles[-1].close))
                    # Live packs with a recorded reservation size against that
                    # reserved cash, never the venue account balance. Paper and
                    # unreserved live arms keep the balance * cap rule.
                    if armed.mode == "live" and armed.starting_cash is not None:
                        cash_for_cap = Decimal(str(armed.starting_cash)) * armed.cap / Decimal(100)
                    else:
                        cash = _cash_for(venue_obj, armed.pair)
                        cash_for_cap = cash * armed.cap / Decimal(100)
                    if cash_for_cap >= rules.costmin and close_price > 0:
                        qty = _fundable_entry_qty(
                            cash_for_cap,
                            close_price=close_price,
                            rules=rules,
                            fee_bps=_fee_bps_for_venue(venue),
                        )
                        if qty >= rules.ordermin and qty * close_price >= rules.costmin:
                            entry_body = json.dumps(
                                {
                                    "kind": "entry",
                                    "qty": str(qty),
                                    "stop": str(target.stop_price),
                                    "pair": armed.pair,
                                },
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                            try:
                                entry_state, entry_ref = _dispatch_place(
                                    home=home,
                                    mode=armed.mode,
                                    venue=venue,
                                    identity=_intent_identity(armed, bar_ts),
                                    coid=entry_coid_for_signal,
                                    body=entry_body,
                                    venue_obj=venue_obj,
                                )
                                if entry_state == "sent":
                                    ref_filled = getattr(entry_ref, "filled_qty", None)
                                    if armed.mode == "live" and not _fill_evidence(ref_filled):
                                        # A live acknowledgement without an executed
                                        # quantity credits nothing. The entry becomes an
                                        # obligation that holds entries on this venue and
                                        # mode until order_outcome reports what executed;
                                        # _resolve_obligations journals that once by
                                        # resolved_coid. The coid is never resent.
                                        audit_needs_reconcile(
                                            OperationalStore(home / "ops.sqlite"), coid=entry_coid_for_signal
                                        )
                                        obligations_view = kb_obligations.read_obligations(home)
                                    else:
                                        filled = ref_filled if isinstance(ref_filled, Decimal) else qty
                                        if filled < qty:
                                            owned = filled
                                            entry_qty = filled
                                            audit_partial_fill(
                                                OperationalStore(home / "ops.sqlite"),
                                                coid=entry_coid_for_signal,
                                                requested=str(qty),
                                                filled=str(filled),
                                            )
                                        else:
                                            entry_qty = qty
                                            owned = qty
                                    fill_context[entry_coid_for_signal] = {
                                        "pack_id": armed.pack_id,
                                        "pack_version": armed.pack_version,
                                        "bar_ts": bar_ts,
                                        "modeled_px": str(close_price),
                                        "fee_bps": _fee_bps_for_venue(venue),
                                        "kind": "entry",
                                    }
                                elif entry_state == "already_sent":
                                    snap_dedup = venue_obj.snapshot()
                                    dedup_fill = next(
                                        (f for f in (snap_dedup.recent_fills or []) if f.coid == entry_coid_for_signal),
                                        None,
                                    )
                                    # A prior process sent this coid. Credit only a fill the
                                    # venue reports; never invent the requested quantity.
                                    if dedup_fill is not None:
                                        owned = Decimal(str(dedup_fill.qty))
                                        entry_qty = Decimal(str(dedup_fill.qty))
                            except StoreError as exc:
                                pack_intent_refused[armed.pack_id] = exc.code
                                any_refused = True
                                print(f"intent refused: {exc.code}", flush=True)
                                entry_qty = Decimal(0)
                            except ProtectiveStopRejectedError:
                                ensure_stop_failed = True
                                print("entry protective stop rejected; reconciliation required", flush=True)
                            except VenueRejectedError as exc:
                                pack_intent_refused[armed.pack_id] = _venue_refused(
                                    exc, entry_coid_for_signal, venue_refusals
                                )
                            except (RuntimeError, ValueError):
                                entry_qty = Decimal(0)
            elif target.reason == "exit" and owned > Decimal(0) and not exits_held:
                try:
                    venue_obj.cancel_stops(armed.pair)
                except (RuntimeError, ValueError):
                    pass
                exit_coid = coid_for(
                    pack_id=armed.pack_id,
                    pack_version=armed.pack_version,
                    venue=venue,
                    pair=armed.pair,
                    bar_ts=bar_ts,
                    intent="exit",
                )
                close_price = Decimal(str(candles[-1].close))
                exit_body = json.dumps(
                    {"kind": "exit", "qty": str(owned), "pair": armed.pair},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                try:
                    exit_state, _exit_ref = _dispatch_place(
                        home=home,
                        mode=armed.mode,
                        venue=venue,
                        identity=_intent_identity(armed, bar_ts),
                        coid=exit_coid,
                        body=exit_body,
                        venue_obj=venue_obj,
                    )
                    if exit_state == "sent":
                        exit_qty = owned
                        owned = Decimal(0)
                        fill_context[exit_coid] = {
                            "pack_id": armed.pack_id,
                            "pack_version": armed.pack_version,
                            "bar_ts": bar_ts,
                            "modeled_px": str(close_price),
                            "fee_bps": _fee_bps_for_venue(venue),
                            "kind": "exit",
                        }
                except StoreError as exc:
                    pack_intent_refused[armed.pack_id] = exc.code
                    any_refused = True
                    print(f"intent refused: {exc.code}", flush=True)
                except VenueRejectedError as exc:
                    pack_intent_refused[armed.pack_id] = _venue_refused(exc, exit_coid, venue_refusals)
                except (RuntimeError, ValueError):
                    pass
            elif target.long and owned > Decimal(0) and target.stop_price is not None:
                cur_stop = resting
                if cur_stop is None or target.stop_price > cur_stop:
                    try:
                        venue_obj.raise_stop(armed.pair, target.stop_price)
                    except (RuntimeError, ValueError):
                        pass

            armed.owned_qty = owned
            _record_tick(
                journal,
                now=int(now),
                venue=venue,
                pack=armed.pack_id,
                bar_ts=bar_ts,
                detail={
                    "pair": armed.pair,
                    "mode": armed.mode,
                    "reason": target.reason,
                    "entry_qty": str(entry_qty),
                    "exit_qty": str(exit_qty),
                    "stop_qty": str(stop_fill_qty),
                    **({"stop_fill_ids": stop_fill_ids} if stop_fill_ids else {}),
                    **({"exits_held_by": exits_held_by} if exits_held_by else {}),
                    "owned_qty_after": str(owned),
                    "entries_blocked": blocked,
                    "entries_paused": entries_suppressed,
                    "kill_switch": kill_engaged,
                    "ensure_stop_failed": ensure_stop_failed,
                    "entry_coid": ensure_stop_entry or entry_coid_for_signal,
                    "no_double_order": bool(ensure_stop_entry),
                    "metadata_refusal": metadata_refusal,
                    "intent_refused": pack_intent_refused.get(armed.pack_id),
                    **trace_extra,
                },
            )
            # Journal first, then retire: an interruption in between keeps the
            # hold instead of losing the refusal code. Nothing is resent.
            for refused_coid, code in venue_refusals:
                try:
                    kb_obligations.record_rejection(OperationalStore(home / "ops.sqlite"), coid=refused_coid, code=code)
                except StoreError as exc:
                    any_refused = True  # the intent stays unresolved and keeps holding entries
                    print(f"intent refused: {exc.code}", flush=True)
            if on_pack_completed is not None:
                on_pack_completed(armed.pack_id, armed.pair)

        config = kb_config.load_config(home)
        for armed in config.armed:
            if armed.venue != venue:
                continue
            held = next((a for a in armed_for_venue if a.pack_id == armed.pack_id and a.pair == armed.pair), None)
            if held is not None:
                armed.owned_qty = held.owned_qty
            if armed.pending_version and armed.owned_qty == Decimal(0):
                armed.pack_version = armed.pending_version
                armed.pending_version = None
        try:
            kb_config.save_config(home, config)
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ENOSPC:
                for _refused in armed_for_venue:
                    pack_intent_refused[_refused.pack_id] = StoreFull.code
                any_refused = True
                print(f"intent refused: {StoreFull.code}", flush=True)
            else:
                raise

        if any(a.mode == "paper" for a in armed_for_venue):
            _audit_paper_fault_fills(home=home, venue_obj=venue_obj)

        _emit_telemetry(
            venue=venue,
            venue_obj=venue_obj,
            fills_before=fills_before,
            fill_context=fill_context,
            home=home,
            transport=transport,
        )

        if any_refused:
            return 1
        return 0
    finally:
        lock.release()


# ---- status / journal tail ------------------------------------------------


def status(*, venue: str | None = None, home: Path | None = None) -> int:
    """Print armed packs. Returns 0."""
    home = Path(home) if home is not None else kb_paths.home()
    config = kb_config.load_config(home)
    armed = config.armed
    if venue is not None:
        armed = [a for a in armed if a.venue == venue]
    if not armed:
        print("no armed packs", flush=True)
        return 0
    for a in armed:
        print(
            f"{a.pack_id} {a.pack_version} {a.venue} {a.pair} cap={a.cap} stop={a.stop} mode={a.mode} requires_license={a.requires_license}",
            flush=True,
        )
    return 0


def journal_tail(n: int = 5, *, home: Path | None = None) -> int:
    """Print the last `n` journal records across all months."""
    home = Path(home) if home is not None else kb_paths.home()
    journal_dir = Path(home) / "journal"
    if not journal_dir.is_dir():
        print("no journal", flush=True)
        return 0
    lines: list[str] = []
    for path in sorted(journal_dir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.strip():
                lines.append(line)
    tail = lines[-n:]
    for line in tail:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        print(json.dumps(rec, sort_keys=True), flush=True)
    if not tail:
        print("no journal records", flush=True)
    return 0


# ---- internals ------------------------------------------------------------


def reconcile_intents(*, venue: str, venue_obj: Any, home: Path | None = None, clock=None) -> str:
    """Settle unresolved intents for ``venue`` from venue evidence, now.

    Takes the same per-venue lock as ``tick`` so an explicit request and a
    scheduled tick cannot account the same outcome twice. Returns ``"busy"``
    when a tick holds the lock, otherwise ``"done"``. It never sends orders.
    """
    home = Path(home) if home is not None else kb_paths.home()
    lock = TickLock(home, venue)
    if not lock.acquire():
        return "busy"
    try:
        now = clock() if clock is not None else _now_seconds()
        _resolve_obligations(
            home, venue=venue, venue_obj=venue_obj, snap=venue_obj.snapshot(), journal=kb_journal.append, now=now
        )
        return "done"
    finally:
        lock.release()


OPERATOR_EXECUTED = "executed"
OPERATOR_NOT_EXECUTED = "not_executed"
# Venue observations that are an outcome. ``open`` and ``partial`` are not:
# the order may still be working, so only a typed assertion settles it.
_SETTLED_OBSERVATIONS = frozenset({"filled", "absent"})
# Lock for an intent committed without a venue: no tick accounts those, but two
# operator resolves of the same one must still not interleave.
_UNSCOPED_INTENT_LOCK = "unscoped-intents"
_OPERATOR_RESOLVE_MESSAGES = {
    "order_resolved": "Order resolved. Nothing was sent to the exchange.",
    "order_already_resolved": "This order was already resolved. Nothing changed.",
    "bad_input": "The resolve request is not well formed.",
    "obligations_unavailable": "Unresolved orders cannot be read right now. Nothing changed.",
    "order_not_found": "No unresolved order has this client order ID.",
    "tick_running": "A tick for this venue is running. Try again in a moment.",
    "no_venue_evidence": "No venue lookup is recorded for this order. Say what happened and type its client order ID.",
    "order_still_working": (
        "The last venue lookup shows this order still working. Cancel it at the exchange and wait for the next "
        "lookup, or say what happened and type its client order ID."
    ),
    "confirmation_required": "Type the client order ID to confirm what you assert.",
    "confirmation_mismatch": "The typed client order ID does not match this order.",
    "executed_qty_invalid": "Executed quantity must be a non-negative decimal number.",
    "executed_qty_above_requested": "Executed quantity cannot exceed the quantity the order requested.",
    "requested_qty_unknown": "The order's requested quantity cannot be read, so an executed quantity cannot be checked.",
    "executed_below_accounted": "The journal already records more executed quantity for this order than asserted.",
}


@dataclass(frozen=True)
class OperatorResolveResult:
    """What one operator resolve did. ``code`` is stable; see ``_OPERATOR_RESOLVE_MESSAGES``."""

    code: str
    ok: bool = False
    effect: str = "refused"
    outcome: str | None = None
    filled_qty: Decimal | None = None
    evidence: str | None = None
    attributed: bool = False

    @property
    def message(self) -> str:
        return _OPERATOR_RESOLVE_MESSAGES.get(self.code, self.code)


def _operator_qty(raw: object) -> Decimal | None:
    """An operator-typed executed quantity: a finite, non-negative decimal string."""
    if not isinstance(raw, str):
        return None  # a JSON number is not an exact Decimal
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return value if value != 0 else Decimal(0)


def resolve_order_by_operator(
    *,
    coid: str,
    outcome: str | None = None,
    executed_qty: object = None,
    confirmation: object = None,
    home: Path | None = None,
    clock=None,
) -> OperatorResolveResult:
    """Retire one unresolved intent on the operator's word. Never sends an order.

    Without ``outcome`` the intent's latest recorded venue observation settles
    it, and no confirmation is needed, but only when that observation is an
    outcome (``filled`` or ``absent``); an ``open`` or ``partial`` order may
    still fill and is refused as ``order_still_working``. With ``outcome``
    (``not_executed``, or ``executed`` with ``executed_qty``) the operator
    asserts what happened without venue evidence and must type ``coid`` back
    as ``confirmation``.

    Runs under the intent venue's tick lock (``tick_running`` while a tick
    holds it). Durable writes follow ``_resolve_obligations``: evidence (the
    assertion row; a venue observation is already recorded), then the journal
    record for executed quantity not yet accounted under ``resolved_coid``,
    then the ``intent_resolved`` row. Quantity is attributed only to the owner
    recorded in the intent; without one it stays external and is not
    journaled. A resolved intent is left as it is (``order_already_resolved``).
    """
    if not isinstance(coid, str) or not coid:
        return OperatorResolveResult(code="bad_input")
    asserted: Decimal | None = None
    if outcome is None:
        if executed_qty is not None or confirmation is not None:
            return OperatorResolveResult(code="bad_input")
    elif outcome == OPERATOR_EXECUTED:
        asserted = _operator_qty(executed_qty)
        if asserted is None:
            return OperatorResolveResult(code="executed_qty_invalid")
    elif outcome == OPERATOR_NOT_EXECUTED:
        if executed_qty is not None:
            return OperatorResolveResult(code="bad_input")
        asserted = Decimal(0)
    else:
        return OperatorResolveResult(code="bad_input")
    if outcome is not None:
        if confirmation is None or confirmation == "":
            return OperatorResolveResult(code="confirmation_required")
        if confirmation != coid:
            return OperatorResolveResult(code="confirmation_mismatch")

    home = Path(home) if home is not None else kb_paths.home()
    intent = _unresolved_intent(home, coid)
    if not isinstance(intent, kb_obligations.Obligation):
        return intent
    lock = TickLock(home, intent.venue or _UNSCOPED_INTENT_LOCK)
    if not lock.acquire():
        return OperatorResolveResult(code="tick_running")
    try:
        # Read again under the lock: a tick may have settled the intent meanwhile.
        intent = _unresolved_intent(home, coid)
        if not isinstance(intent, kb_obligations.Obligation):
            return intent
        now = clock() if clock is not None else _now_seconds()
        return _settle_by_operator(home, intent, outcome=outcome, asserted=asserted, now=now)
    finally:
        lock.release()


def _unresolved_intent(home: Path, coid: str) -> kb_obligations.Obligation | OperatorResolveResult:
    view = kb_obligations.read_obligations(home)
    if not view.available:
        return OperatorResolveResult(code="obligations_unavailable")
    intent = next((o for o in view.unresolved if o.coid == coid), None)
    if intent is not None:
        return intent
    try:
        resolution = kb_obligations.read_resolution(home, coid)
    except kb_obligations.ObligationsUnavailable:
        return OperatorResolveResult(code="obligations_unavailable")
    if resolution is None:
        return OperatorResolveResult(code="order_not_found")
    try:
        filled: Decimal | None = Decimal(str(resolution.get("filled_qty")))
    except (InvalidOperation, ValueError):
        filled = None
    return OperatorResolveResult(
        code="order_already_resolved",
        ok=True,
        effect="unchanged",
        outcome=resolution.get("outcome") if isinstance(resolution.get("outcome"), str) else None,
        filled_qty=filled if filled is not None and filled.is_finite() else None,
        evidence=resolution.get("evidence") if isinstance(resolution.get("evidence"), str) else None,
    )


def _settle_by_operator(
    home: Path, intent, *, outcome: str | None, asserted: Decimal | None, now: float
) -> OperatorResolveResult:
    """Account and retire ``intent`` while its venue's tick lock is held."""
    field = _QTY_FIELD_FOR_INTENT.get(intent.kind or "")
    attributed = field is not None and None not in (intent.venue, intent.pack_id, intent.mode)
    accounted = _accounted_for(home, intent.coid, field)[0] if attributed else Decimal(0)
    store = OperationalStore(home / "ops.sqlite")
    if outcome is None:
        if intent.last_observation is None or intent.observed_status is None or intent.observed_qty is None:
            return OperatorResolveResult(code="no_venue_evidence")
        if intent.observed_status not in _SETTLED_OBSERVATIONS:
            # An open or partly executed order may still fill: its lookup is not
            # an outcome, so settling on it would lift the entry hold too early.
            return OperatorResolveResult(code="order_still_working")
        status = intent.observed_status
        # An observation row is written once per change of kind; later partial
        # lookups may have journaled more than the row shows.
        filled = max(intent.observed_qty, accounted)
        evidence = "operator_accepted_venue_observation"
    else:
        filled = asserted if asserted is not None else Decimal(0)
        if outcome == OPERATOR_EXECUTED:
            if intent.qty is None:
                return OperatorResolveResult(code="requested_qty_unknown")
            if filled > intent.qty:
                return OperatorResolveResult(code="executed_qty_above_requested")
        if filled < accounted:
            return OperatorResolveResult(code="executed_below_accounted")
        status = "filled" if filled > 0 else "absent"
        evidence = "operator_asserted"
        kb_obligations.record_assertion(store, coid=intent.coid, outcome=outcome, filled_qty=filled, ts=int(now))
    if attributed and filled > accounted:
        _account_intent(
            home,
            intent,
            OrderOutcome(status=status, filled_qty=filled, authoritative=True),
            venue=intent.venue,
            journal=kb_journal.append,
            now=now,
        )
    kb_obligations.record_resolution(store, coid=intent.coid, outcome=status, filled_qty=filled, evidence=evidence)
    return OperatorResolveResult(
        code="order_resolved",
        ok=True,
        effect="changed",
        outcome=status,
        filled_qty=filled,
        evidence=evidence,
        attributed=attributed,
    )


def _resolve_obligations(home: Path, *, venue: str, venue_obj: Any, snap: Any, journal, now: float) -> None:
    """Record evidence for each unresolved intent on ``venue`` and retire what it settles.

    Order of durable writes per intent: observation row, then journal
    accounting (idempotent by ``resolved_coid``), then the resolution row.
    An interruption anywhere leaves a state the next call converges from
    without double counting. Nothing is resubmitted.
    """
    view = kb_obligations.read_obligations(home)
    if not view.available:
        return
    lookup = getattr(venue_obj, "order_outcome", None)
    store = OperationalStore(home / "ops.sqlite")
    for intent in view.unresolved:
        if intent.venue != venue or intent.pair is None:
            continue
        if intent.kind == UNLINKED_SELL_KIND:
            # No venue order carries an exit hold's coid, so no lookup is made:
            # only an operator resolve settles it. It stays audited, once.
            if not intent.marked:
                audit_needs_reconcile(store, coid=intent.coid)
            continue
        outcome: OrderOutcome | None = None
        if lookup is not None:
            try:
                outcome = lookup(intent.coid, pair=intent.pair)
            except (InstrumentMetadataError, RuntimeError, ValueError, OSError):
                outcome = None
        if not isinstance(outcome, OrderOutcome) or outcome.status == "unknown":
            # The outcome cannot be established: keep the intent unresolved and
            # make sure it is audited for reconcile (critical alert), once.
            if not intent.marked:
                audit_needs_reconcile(store, coid=intent.coid)
            continue
        kb_obligations.record_observation(
            store,
            coid=intent.coid,
            status=outcome.status,
            filled_qty=outcome.filled_qty,
            previous=intent.last_observation,
        )
        needs_accounting = outcome.status in ("filled", "partial") or (
            outcome.status == "open" and intent.kind == "stop"
        )
        if needs_accounting and not _account_intent(home, intent, outcome, venue=venue, journal=journal, now=now):
            continue  # no durable owner context: never assign it to whoever is armed now
        if outcome.status == "absent" and outcome.authoritative:
            kb_obligations.record_resolution(
                store, coid=intent.coid, outcome="absent", filled_qty=Decimal(0), evidence="venue_record_complete"
            )
        elif outcome.status == "open" and intent.kind == "stop":
            kb_obligations.record_resolution(
                store, coid=intent.coid, outcome="open", filled_qty=Decimal(0), evidence="protective_stop_resting"
            )
        elif outcome.status == "filled" and outcome.authoritative:
            if intent.kind == "entry" and not _protected_or_flat(home, intent, venue=venue, snap=snap):
                continue  # executed but unprotected: the arm's ensure-stop path must act first
            kb_obligations.record_resolution(
                store, coid=intent.coid, outcome="filled", filled_qty=outcome.filled_qty, evidence="venue_fill"
            )


def _account_intent(home: Path, intent, outcome: OrderOutcome, *, venue: str, journal, now: float) -> bool:
    """Journal the executed quantity of ``intent`` once; False without owner context."""
    field = _QTY_FIELD_FOR_INTENT.get(intent.kind or "")
    if field is None or intent.pack_id is None or intent.mode is None:
        return False
    already, already_ids = _accounted_for(home, intent.coid, field)
    has_record = already > 0 or bool(already_ids) or _has_resolved_record(home, intent.coid)
    delta = outcome.filled_qty - already
    if delta <= 0 and has_record:
        return True
    detail: dict = {
        "pair": intent.pair,
        "mode": intent.mode,
        "reason": RECONCILED_REASON,
        "resolved_coid": intent.coid,
        "entry_qty": "0",
        "exit_qty": "0",
        "stop_qty": "0",
    }
    if delta > 0:
        detail[field] = str(delta)
    if intent.kind in ("entry", "stop"):
        # Entry and repaired-stop coids own their protective stop's later fill.
        detail["entry_coid"] = intent.coid
    if intent.kind == "stop":
        new_ids = [i for i in outcome.fill_ids if i and i not in already_ids]
        if new_ids:
            detail["stop_fill_ids"] = new_ids
    journal(
        {
            "ts": int(now),
            "kind": "tick",
            "venue": venue,
            "pack": intent.pack_id,
            "bar_ts": int(intent.bar_ts or 0),
            "detail": detail,
        }
    )
    return True


def _accounted_for(home: Path, coid: str, field: str) -> tuple[Decimal, set[str]]:
    """``field`` quantity and stop fill ids already journaled under ``resolved_coid``."""
    already = Decimal(0)
    already_ids: set[str] = set()
    for rec in kb_journal.read_records_lenient(home):
        detail = rec.get("detail") if isinstance(rec, dict) else None
        if rec.get("kind") != "tick" or not isinstance(detail, dict) or detail.get("resolved_coid") != coid:
            continue
        try:
            already += Decimal(str(detail.get(field) or "0"))
        except (ArithmeticError, ValueError):
            pass
        ids = detail.get("stop_fill_ids")
        if isinstance(ids, list):
            already_ids.update(i for i in ids if isinstance(i, str))
    return already, already_ids


def _has_resolved_record(home: Path, coid: str) -> bool:
    return any(
        rec.get("kind") == "tick" and isinstance(rec.get("detail"), dict) and rec["detail"].get("resolved_coid") == coid
        for rec in kb_journal.read_records_lenient(home)
    )


def _protected_or_flat(home: Path, intent, *, venue: str, snap: Any) -> bool:
    """True when a stop rests on the pair or the intent's identity owns nothing."""
    if any(
        getattr(o, "pair", None) == intent.pair and getattr(o, "stop_price", None) is not None
        for o in (getattr(snap, "open_orders", None) or [])
    ):
        return True
    identity = OwnershipIdentity(pack_id=intent.pack_id, venue=venue, pair=intent.pair, mode=intent.mode)
    return project_ownership(home, identity).attributable_qty == 0


def _fill_evidence(filled: Any) -> bool:
    """True when a venue acknowledgement reports a positive, finite Decimal fill."""
    return isinstance(filled, Decimal) and filled.is_finite() and filled > 0


def _venue_refused(exc: VenueRejectedError, coid: str, refusals: list[tuple[str, str]]) -> str:
    """Note that the venue's documented response refused ``coid``; return the code to journal.

    The intent is resolved as not executed after the tick record carrying the
    code is journaled. Only the classified code is kept, never venue text.
    """
    refusals.append((coid, exc.code))
    print(f"venue refused: {exc.code}", flush=True)
    return exc.code


def _intent_identity(armed: kb_config.ArmedPack, bar_ts: int) -> dict:
    """Durable owner context stored with an Outbox intent before it is sent."""
    return {"pack_id": armed.pack_id, "pack_version": armed.pack_version, "bar_ts": int(bar_ts)}


def _ownership_identity(armed: kb_config.ArmedPack, venue: str) -> OwnershipIdentity:
    return OwnershipIdentity(pack_id=armed.pack_id, venue=venue, pair=armed.pair, mode=armed.mode)


def _stop_fills_for(home: Path, identity: OwnershipIdentity, fills: list) -> tuple[list[str], Decimal]:
    """Stop fills of this identity's own orders that the journal has not accounted.

    A fill counts only when the venue reports which order it executed
    (``order_coid``) and that order is one this identity journaled. Fills
    without that link are never attributed. A fill once held as unlinkable
    (``_hold_unlinked_sells``) is accounted only through the operator's
    resolve of that hold, so a later link never counts it a second time.
    """
    order_coids, accounted = stop_fill_history(home, identity)
    held: set[str] | None = None
    ids: list[str] = []
    qty = Decimal(0)
    for fill in fills:
        order_coid = getattr(fill, "order_coid", None)
        fill_id = getattr(fill, "id", "")
        if getattr(fill, "side", "") != "sell" or getattr(fill, "pair", None) != identity.pair:
            continue
        if not order_coid or order_coid not in order_coids or not fill_id or fill_id in accounted or fill_id in ids:
            continue
        if held is None:
            held = _committed_coids(home)
        if _unlinked_sell_coid(identity, fill_id) in held:
            continue
        ids.append(fill_id)
        qty += Decimal(str(fill.qty))
    return ids, qty


class _HoldNotSent(Exception):
    """Raised by an exit hold's send: the Outbox commits the hold, and nothing is sent."""


def _never_send(_coid: str, _body: str) -> None:
    raise _HoldNotSent


def _unlinked_sell_coid(identity: OwnershipIdentity, fill_id: str) -> str:
    """The exit hold's coid: one per identity and venue fill id, stable across restarts."""
    raw = f"{identity.pack_id}|{identity.venue}|{identity.pair}|{identity.mode}|{fill_id}|{UNLINKED_SELL_KIND}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]


def _committed_coids(home: Path) -> set[str]:
    """Every coid the Outbox holds. Empty when the ledger cannot be read.

    Empty only lets a linked stop fill be credited, which can lower recorded
    ownership but never raise it, so nothing external is ever sold for it.
    """
    path = Path(home) / "ops.sqlite"
    if not path.exists():
        return set()
    try:
        return dispatched_coids(OperationalStore(path))
    except StoreError:
        return set()


def _hold_unlinked_sells(
    home: Path, armed: kb_config.ArmedPack, identity: OwnershipIdentity, fills: list, *, bar_ts: int
) -> bool:
    """Commit an exit hold for each unlinkable sell on the pair; True when one is new.

    A sell counts when the venue reported its order as unknown
    (``Fill.order_unknown``) and it is not already accounted as this
    identity's stop fill. The hold is an Outbox intent of kind
    ``UNLINKED_SELL_KIND``, committed and never sent: its venue and mode scope
    it (so KQ-02's per-venue-and-mode entry hold applies unchanged), and its
    body names the owner and the sell's quantity, which bounds what an
    operator may assert executed. A ``needs_reconcile`` marker lists it in
    Operations and raises the critical alert once. Its coid comes from the
    identity and the fill id, so repeated snapshots and restarts find the same
    hold, and a resolved hold is never committed again. ``StoreError``
    propagates: the caller then acts as held.
    """
    unknown = [
        f
        for f in fills
        if getattr(f, "order_unknown", False)
        and getattr(f, "side", "") == "sell"
        and getattr(f, "pair", None) == identity.pair
        and getattr(f, "id", "")
    ]
    if not unknown:
        return False
    _order_coids, accounted = stop_fill_history(home, identity)
    store = OperationalStore(home / "ops.sqlite")
    outbox = Outbox(store)
    created = False
    for fill in unknown:
        if fill.id in accounted:
            continue
        coid = _unlinked_sell_coid(identity, fill.id)
        body = json.dumps(
            {
                "kind": UNLINKED_SELL_KIND,
                "pair": identity.pair,
                "qty": str(fill.qty),
                "fill_id": fill.id,
                **_intent_identity(armed, bar_ts),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            # A hold already committed (resolved or not) returns without a send.
            outbox.dispatch(coid, body, _never_send, mode=identity.mode, venue=identity.venue)
        except _HoldNotSent:
            audit_needs_reconcile(store, coid=coid)
            created = True
    return created


def _exit_holds(view: kb_obligations.ObligationsView, identity: OwnershipIdentity) -> list[str]:
    """Coids of the unresolved exit holds of ``identity``."""
    return [
        o.coid
        for o in view.unresolved
        if o.kind == UNLINKED_SELL_KIND
        and o.venue == identity.venue
        and o.mode == identity.mode
        and o.pair == identity.pair
        and o.pack_id == identity.pack_id
    ]


def _recognized_open_qty(armed: kb_config.ArmedPack, venue: str, candles: list, orders: list) -> Decimal:
    known = {
        coid_for(
            pack_id=armed.pack_id,
            pack_version=armed.pack_version,
            venue=venue,
            pair=armed.pair,
            bar_ts=int(candle.ts_ms),
            intent=intent,
        )
        for candle in candles
        for intent in ("entry", "stop")
    }
    total = Decimal(0)
    for order in orders:
        coid = getattr(order, "coid", None)
        pair = getattr(order, "pair", None)
        qty = getattr(order, "qty", None)
        if coid in known and pair == armed.pair and qty is not None:
            total += Decimal(str(qty))
    return total


def _stop_price(pack: dict, last_close: Decimal) -> Decimal:
    stop = (pack.get("risk") or {}).get("stop") or {}
    if stop.get("type") == "pct":
        return last_close * (Decimal(1) - Decimal(str(stop["pct"])) / Decimal(100))
    if last_close > 0:
        return last_close / Decimal(2)
    return Decimal(0)


def _fee_bps_for_venue(venue: str) -> int:
    """Taker fee in bps per venue. Matches PaperVenue constants."""
    if venue == "kraken":
        return 40
    if venue == "coinbase":
        return 120
    if venue == "binance":
        return 10
    if venue == "gate":
        return 20
    if venue == "pionex":
        return 5
    if venue in {"okx", "bybit", "kucoin"}:
        return 10
    if venue in {"ourbit", "toobit"}:
        return 0
    if venue == "xt":
        return 20
    if venue == "mexc":
        return 5
    if venue in {"deepcoin", "lbank", "bitget"}:
        return 10
    if venue == "htx":
        return 20
    if venue == "btcc":
        return 30
    if venue == "cryptocom":
        return 50
    return 0


def _fundable_entry_qty(
    budget: Decimal,
    *,
    close_price: Decimal,
    rules: PairRules,
    fee_bps: int,
) -> Decimal:
    """Size a paper entry so notional plus fee fit the budget.

    Uses the same buy slippage and fee quantization as ``PaperVenue`` so a
    full-cap order does not request more quote cash than the venue will debit.
    """
    if budget <= 0 or close_price <= 0:
        return Decimal(0)
    slip = Decimal(SLIPPAGE_BPS) / Decimal(10_000)
    buy_price = (close_price * (Decimal(1) + slip)).quantize(
        Decimal(1).scaleb(-rules.price_decimals), rounding=ROUND_DOWN
    )
    if buy_price <= 0:
        return Decimal(0)
    fee_mult = Decimal(1) + Decimal(fee_bps) / Decimal(10_000)
    lot = Decimal(1).scaleb(-rules.lot_decimals)
    qty = (budget / (buy_price * fee_mult)).quantize(lot, rounding=ROUND_DOWN)
    while qty > 0:
        notional = qty * buy_price
        fee = (notional * Decimal(fee_bps) / Decimal(10_000)).quantize(
            Decimal(1).scaleb(-rules.price_decimals), rounding=ROUND_DOWN
        )
        if notional + fee <= budget:
            return qty
        qty -= lot
    return Decimal(0)


def _emit_telemetry(
    *,
    venue: str,
    venue_obj: Any,
    fills_before: set[str],
    fill_context: dict[str, dict],
    home: Path,
    transport: Any | None,
) -> None:
    """Send telemetry for paper fills that happened during this tick.

    A disabled install returns from `maybe_send` without touching the
    transport, so the test for the off-by-default path needs no extra
    guard. `transport` defaults to a no-op; tests inject a spy. When the
    caller passes None (the CLI default), no POST is made.
    """
    from krellbot import telemetry as kb_telemetry

    transport = kb_telemetry.resolve_transport(home, transport)
    if transport is None:
        return
    snap_after = venue_obj.snapshot()
    fills_after = list(getattr(snap_after, "recent_fills", []) or [])
    for fill in fills_after:
        coid = getattr(fill, "coid", None)
        if not coid or coid in fills_before:
            continue
        ctx = fill_context.get(coid)
        if ctx is None:
            continue
        qty = Decimal(str(getattr(fill, "qty", "0")))
        price = Decimal(str(getattr(fill, "price", "0")))
        notional = qty * price
        payload = {
            "pack_id": str(ctx["pack_id"]),
            "pack_version": str(ctx["pack_version"]),
            "venue": venue,
            "pair": str(getattr(fill, "pair", "")),
            "side": str(getattr(fill, "side", "")),
            "bar_ts": int(ctx["bar_ts"]),
            "modeled_px": str(ctx["modeled_px"]),
            "fill_px": str(price),
            "qty_bucket": kb_telemetry.qty_bucket(notional),
            "fee_bps": int(ctx["fee_bps"]),
        }
        kb_telemetry.maybe_send(home, payload, transport=transport)


def _now_seconds() -> float:
    import time

    return time.time()


def _base_quote(pair: str) -> tuple[str, str]:
    """Split a venue pair into ``(base, quote)``.

    Matches ``venues.paper._base_quote`` / ``application.paper.base_quote``:
    Coinbase ``BASE-QUOTE`` product ids (wave paper arm ``BTC-USD``),
    Binance-class compact symbols that strip ``USDT``/``USDC`` before
    ``USD`` (wave paper arm ``BTCUSDT``), and Kraken-style compact pairs
    that strip a trailing ``USD``.
    """
    if not pair:
        return ("", "")
    if "-" in pair:
        base, _, quote = pair.partition("-")
        if base and quote and "-" not in quote:
            return base, quote
    for quote in ("USDT", "USDC", "USD"):
        if pair.endswith(quote) and len(pair) > len(quote):
            return pair[: -len(quote)], quote
    if len(pair) >= 6:
        return pair[:-3], pair[-3:]
    return pair, ""


def _snapshot_balances(snap: Any) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    if isinstance(snap, dict):
        for k, v in snap.get("balances", {}).items():
            out[k] = Decimal(str(v))
    else:
        for b in getattr(snap, "balances", []) or []:
            out[str(b.asset)] = Decimal(str(b.free))
    return out


def _snapshot_orders(snap: Any) -> list:
    if isinstance(snap, dict):
        return list(snap.get("open_orders", []))
    return list(getattr(snap, "open_orders", []) or [])


def _cash_for(venue_obj: Any, pair: str) -> Decimal:
    _base, quote = _base_quote(pair)
    snap = venue_obj.snapshot()
    if isinstance(snap, dict):
        return Decimal(str(snap.get("balances", {}).get(quote, Decimal(0))))
    for b in snap.balances:
        if b.asset == quote:
            return Decimal(str(b.free))
    return Decimal(0)


def _record_tick(
    journal: Callable[[dict], Path],
    *,
    now: int,
    venue: str,
    pack: str,
    bar_ts: int,
    detail: dict,
) -> None:
    try:
        journal(
            {
                "ts": int(now),
                "kind": "tick",
                "venue": venue,
                "pack": pack,
                "bar_ts": int(bar_ts),
                "detail": detail,
            }
        )
    except ValueError:
        # Test sinks may not validate required keys; that's their problem.
        pass


def _already_journaled(home: Path, pack_id: str, bar_ts: int, *, venue: str, pair: str) -> bool:
    journal_dir = Path(home) / "journal"
    if not journal_dir.is_dir():
        return False
    for path in sorted(journal_dir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "tick" and rec.get("pack") == pack_id and int(rec.get("bar_ts", 0)) == int(bar_ts):
                recorded_venue = rec.get("venue")
                detail = rec.get("detail")
                if isinstance(detail, dict) and detail.get("reason") == RECONCILED_REASON:
                    continue
                recorded_pair = detail.get("pair") if isinstance(detail, dict) else None
                # Ambiguous legacy dimensions remain conservative wildcards.
                if isinstance(recorded_venue, str) and recorded_venue.strip() and recorded_venue != venue:
                    continue
                if isinstance(recorded_pair, str) and recorded_pair.strip() and recorded_pair != pair:
                    continue
                return True
    return False


def armed_pack_dict(home: Path, armed: kb_config.ArmedPack) -> dict:
    """Read the pack JSON bytes for the engine to evaluate."""
    path = Path(armed.pack_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
