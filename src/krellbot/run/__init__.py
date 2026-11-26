"""Run engine: arm, disarm, tick, status, journal.

`tick` is the only function that talks to a venue. It:
    1. Acquires `$KRELLBOT_HOME/run/<venue>.lock`. If held, exit 0 silently
       with `another tick running` on stdout.
    2. Loads the license cache + armed packs config.
    3. Snapshots the venue.
    4. For each armed pack on the venue: ensure a resting stop if owned > 0
       (the venue's ``place_stop``), priced by the pack's own stop rule
       (``_stop_price``). When that rule gives no price, the stop recorded at
       entry is re-placed (no stop when none is recorded) and the tick
       journals ``stop_price_unavailable``. If the venue refuses the stop or
       has no ``place_stop``, market-exit the owned qty in the same tick,
       journaling only what the venue shows executed.
    5. Acts on the latest closed bar only. Missed bars are coalesced by
       feeding every candle up to and including the latest into evaluate.run
       so indicator state includes them.
    6. Entry / exit / raise_stop per the engine rules.
    7. Journal one tick record per pack per bar, including refusals.
    8. If entries are blocked by the license gate, print the lapsed message.

`close_position` runs the tick's exit on request (``positions.close``, the
pair typed back): under the same lock and without waiting for a bar, it
cancels the arm's stop, sells exactly the strategy-owned quantity, journals
what executed and holds new entries with ``closed_by_operator_at``.

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
# Journal ``reason`` for the record an operator close writes (``close_position``).
CLOSE_REASON = "close"
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
    track: bool = False,
) -> tuple[str, OrderRef | None]:
    """Commit-before-send dispatch for every ``place_*`` call.

    Both paper and live arms route through here. Paper takes the same
    Outbox path as before; live goes through the same Outbox so a
    coid is durable in ``<home>/ops.sqlite`` BEFORE the venue call.
    On ``needs_reconcile`` the helper records the audit and returns
    ``"needs_reconcile"`` without invoking ``venue_obj``. ``StoreError``
    is propagated; the caller is responsible for refusing before any
    venue call.

    ``track`` marks the committed intent for reconcile after its commit and
    before the venue call, for an order whose acknowledgement proves nothing
    about what executed. It is then an obligation until ``order_outcome`` (or
    the operator) settles it, wherever the process stops after the send.
    """
    captured: list[OrderRef | None] = [None]
    store = OperationalStore(home / "ops.sqlite")

    def _send(c: str, b: str) -> None:
        if track:
            audit_needs_reconcile(store, coid=c)
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
            captured[0] = venue_obj.place_stop(
                c,
                Decimal(str(payload["qty"])),
                Decimal(str(payload["stop"])),
                pair=str(payload["pair"]),
            )

    if identity:
        # Owner context travels in the committed intent itself, durable before
        # any send, so a later process can attribute the outcome.
        body = json.dumps({**json.loads(body), **identity}, separators=(",", ":"), sort_keys=True)
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
        # The kill switch alone lets the tick run as paper does: entries are
        # suppressed below while exits, stop repair, raise_stop and reconcile
        # run. A kill-switch refusal here is the restore fence: nothing
        # sends, tick succeeds (rc=0).
        fenced_live = False
        for live_armed in armed_for_venue:
            if live_armed.mode != "live":
                continue
            gate_result = live_gate.check_live_tick(
                home, venue=venue, pair=live_armed.pair, entries_paused=live_armed.entries_paused
            )
            if gate_result.ok:
                continue
            if gate_result.code == live_gate.CODE_KILL_SWITCH:
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
            # An earlier exit of this identity whose outcome is not settled may
            # have sold what the journal still records as owned, so nothing more
            # is sold or re-protected until order_outcome (or the operator) says
            # what it executed.
            exits_pending = _unsettled_exits(obligations_view, identity)
            exits_held = hold_refused or bool(exits_held_by) or bool(exits_pending)
            if exits_held_by:
                print("exits held: a sell on the pair is not linked to an order; resolve it in Operations", flush=True)
            if exits_pending:
                print("exits held: an earlier exit's outcome is not settled yet", flush=True)
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

            target = kb_evaluate.run(pack, candles)
            # Intents the venue's documented response refused, as (coid, code).
            venue_refusals: list[tuple[str, str]] = []
            ensure_stop_failed = False
            # Set when this tick's stop repair found no price to place.
            stop_price_unavailable = False
            ensure_stop_entry = ""
            exit_qty = Decimal(0)
            # Set when the repair below falls back to an exit: nothing else is
            # sent for this pack in this tick.
            repair_exited = False
            # On a bar the pack exits, its own exit below sells what it owns. A
            # stop rested first would only be cancelled again, and one that
            # triggered at once would be a second sale of the same units.
            if (
                owned > Decimal(0)
                and resting is None
                and latest is not None
                and not exits_held
                and target.reason != "exit"
            ):
                stop_refused = False
                if not callable(getattr(venue_obj, "place_stop", None)):
                    # This adapter cannot rest a stop on its own. No stop intent
                    # is committed: it could never be sent, so it would hold
                    # entries for good. The position is protected by exiting it.
                    print("ensure_stop failed: the venue cannot place a stop", flush=True)
                    stop_refused = True
                else:
                    stop_coid = coid_for(
                        pack_id=armed.pack_id,
                        pack_version=armed.pack_version,
                        venue=venue,
                        pair=armed.pair,
                        bar_ts=bar_ts,
                        intent="stop",
                    )
                    try:
                        # The pack's own stop rule over this tick's candles, never
                        # below the stop recorded with the position's entry.
                        repair, rule_priced = _stop_price(pack, candles, home=home, identity=identity)
                        if not rule_priced:
                            # The rule gives no price. Nothing is guessed and
                            # nothing is sold: the stop recorded at entry is
                            # re-placed when there is one, the tick journals the
                            # gap and Operations raises a critical alert.
                            stop_price_unavailable = True
                            print(
                                "ensure_stop: the pack's stop rule gives no price; "
                                + ("re-placing the stop recorded at entry" if repair is not None else "no stop placed"),
                                flush=True,
                            )
                        if repair is None:
                            ensure_stop_failed = True
                        else:
                            stop_body = json.dumps(
                                {"kind": "stop", "qty": str(owned), "stop": str(repair), "pair": armed.pair},
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                            # The stop coid is committed before the send and never
                            # sent again: a stop whose reply is lost stays an
                            # obligation until the venue's record shows it resting.
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
                    except (RuntimeError, ValueError) as exc:
                        if isinstance(exc, VenueRejectedError):
                            # Journaled with the tick, then the stop intent is
                            # resolved as not executed: it holds nothing.
                            pack_intent_refused[armed.pack_id] = _venue_refused(exc, stop_coid, venue_refusals)
                        print("ensure_stop failed: stop", flush=True)
                        stop_refused = True
                if stop_refused:
                    ensure_stop_failed = True
                    repair_exited = True
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
                    # A live exit is an obligation from its commit: its
                    # acknowledgement proves nothing about what executed.
                    exit_tracked = armed.mode != "paper"
                    try:
                        exit_state, exit_ref = _dispatch_place(
                            home=home,
                            mode=armed.mode,
                            venue=venue,
                            identity=_intent_identity(armed, bar_ts),
                            coid=exit_coid,
                            body=exit_body,
                            venue_obj=venue_obj,
                            track=exit_tracked,
                        )
                        if exit_state == "sent":
                            fill_context[exit_coid] = {
                                "pack_id": armed.pack_id,
                                "pack_version": armed.pack_version,
                                "bar_ts": bar_ts,
                                "modeled_px": str(Decimal(str(latest.close))),
                                "fee_bps": _fee_bps_for_venue(venue),
                                "kind": "exit",
                            }
                        if exit_state in ("sent", "already_sent"):
                            sold = _executed_exit_qty(armed.mode, exit_ref, requested=owned)
                            if sold is None:
                                # Nothing proves what executed. The exit stays an
                                # obligation that holds this identity's exits and
                                # repair (and entries on this venue and mode) until
                                # order_outcome settles it, journaled once by
                                # resolved_coid. Ownership stays as recorded. A
                                # tracked exit was marked before its send.
                                if not exit_tracked:
                                    audit_needs_reconcile(OperationalStore(home / "ops.sqlite"), coid=exit_coid)
                            else:
                                exit_qty = sold
                                owned -= sold
                    except StoreError as exc:
                        pack_intent_refused[armed.pack_id] = exc.code
                        any_refused = True
                        print(f"intent refused: {exc.code}", flush=True)
                    except VenueRejectedError as exc:
                        pack_intent_refused[armed.pack_id] = _venue_refused(exc, exit_coid, venue_refusals)
                    except (AttributeError, RuntimeError, ValueError):
                        pass
                    obligations_view = kb_obligations.read_obligations(home)

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

            trace_extra: dict = {}
            if armed.mode == "paper":
                paper_trace = kb_trace.paper_decision_trace(pack, candles)
                trace_extra["decision_trace_bars"] = len(paper_trace)
                if paper_trace:
                    last_reason = paper_trace[-1]["target"]["reason"]
                    if isinstance(last_reason, str):
                        trace_extra["decision_reason"] = last_reason
            entry_qty = Decimal(0)
            entry_coid_for_signal = ""
            kill_engaged = live_gate.kill_state(home).engaged
            # An operator close (``close_position``) holds entries with its own
            # marker, never with ``entries_paused``: on live that flag is the
            # restore fence's mark, which would halt the whole venue's tick.
            entries_suppressed = armed.entries_paused or kill_engaged or armed.closed_by_operator_at is not None
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
                and not repair_exited
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
            elif target.reason == "exit" and owned > Decimal(0) and not exits_held and not repair_exited:
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
                # A live acknowledgement is not an executed quantity: Kraken's
                # reports none and Coinbase's echoes the requested size. So a
                # live exit is an obligation from its commit, before the send,
                # and journals exit_qty 0 here. Ownership stays as recorded,
                # which holds this identity's exits and stop repair (and entries
                # on this venue and mode) until order_outcome reports what
                # executed; _resolve_obligations journals that once by
                # resolved_coid. A residual stays strategy-owned, and the repair
                # above re-protects it on a later bar. The coid is never resent.
                exit_tracked = armed.mode != "paper"
                try:
                    exit_state, _exit_ref = _dispatch_place(
                        home=home,
                        mode=armed.mode,
                        venue=venue,
                        identity=_intent_identity(armed, bar_ts),
                        coid=exit_coid,
                        body=exit_body,
                        venue_obj=venue_obj,
                        track=exit_tracked,
                    )
                    if exit_state == "sent" and not exit_tracked:
                        # The paper venue sells exactly the quantity it is
                        # asked to: its acknowledgement is its whole record.
                        exit_qty = owned
                        owned = Decimal(0)
                    if exit_state == "sent":
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
                if exit_tracked:
                    obligations_view = kb_obligations.read_obligations(home)
            elif target.long and owned > Decimal(0) and target.stop_price is not None and not repair_exited:
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
                    **({"exits_pending": exits_pending} if exits_pending else {}),
                    "owned_qty_after": str(owned),
                    "entries_blocked": blocked,
                    "entries_paused": entries_suppressed,
                    "kill_switch": kill_engaged,
                    "ensure_stop_failed": ensure_stop_failed,
                    **({"stop_price_unavailable": True} if stop_price_unavailable else {}),
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


# ---- operator close ---------------------------------------------------------

_CLOSE_MESSAGES = {
    "position_closed": (
        "Position closed: only this strategy's quantity was sold, after its stop was cancelled. "
        "New buys stay paused until you resume them, or stop the run and arm it again."
    ),
    "close_pending": (
        "Close sent for only this strategy's quantity, after its stop was cancelled. What executed is settled "
        "from the exchange's own record on the next tick. New buys stay off until the run is disarmed and armed again."
    ),
    "venue_rejected": (
        "The exchange refused the close, so nothing was sold. The stop was cancelled first: the next tick "
        "re-places it. New buys stay off."
    ),
    "outcome_unknown": (
        "The close's outcome is not known yet. It is never sent again: the next tick settles it from the "
        "exchange's own record. New buys stay off."
    ),
    "bad_input": "The close request is not well formed.",
    "confirmation_required": "Type the pair to confirm the close.",
    "confirmation_mismatch": "The typed pair does not match this run's pair.",
    "not_armed": "No strategy is armed on this venue and pair.",
    "restore_fence": (
        "This home was restored, and live runs on this venue send nothing until the kill switch is released. "
        "Nothing was sent."
    ),
    "tick_running": "A tick for this venue is running. Try again in a moment.",
    "obligations_unavailable": "Unresolved orders cannot be read right now. Nothing was sent.",
    "obligation_open": (
        "An order on this venue is not settled yet: the next tick settles it from the exchange's record, or "
        "resolve it in Operations. Nothing was sent."
    ),
    "ownership_ambiguous": "The journal cannot show exactly what this strategy owns. Nothing was sent.",
    "nothing_to_close": "This strategy owns nothing on this pair. Nothing was sent.",
    "venue_unavailable": "The venue could not be reached. Nothing was sent.",
    "candles_unavailable": "No closed candle could be read for this pair. Nothing was sent.",
    "stop_cancel_failed": "The resting stop could not be cancelled, so nothing was sold. Check the stop at the venue.",
    "config_busy": "Another command is updating this run's settings. Try again in a moment. Nothing was sent.",
}


@dataclass(frozen=True)
class ClosePositionResult:
    """What one operator close did. ``code`` is stable; see ``_CLOSE_MESSAGES``.

    ``ok`` is true for ``position_closed`` and ``close_pending``. ``effect`` is
    ``changed`` once the stop was cancelled and entries held, ``refused``
    otherwise (a refusal writes nothing). ``sold_qty`` is the quantity
    journaled as executed (paper); a live close journals 0 and is settled
    later. ``gate_message`` carries the live gate's own words for its codes.
    """

    code: str
    ok: bool = False
    effect: str = "refused"
    mode: str | None = None
    client_order_id: str | None = None
    requested_qty: Decimal | None = None
    sold_qty: Decimal | None = None
    gate_message: str | None = None

    @property
    def message(self) -> str:
        return _CLOSE_MESSAGES.get(self.code) or self.gate_message or self.code


def close_position(
    *,
    venue: object,
    pair: object,
    confirmation: object,
    open_venue: Callable[[list[kb_config.ArmedPack]], Any] | None = None,
    home: Path | None = None,
    clock: Callable[[], float] | None = None,
) -> ClosePositionResult:
    """Sell exactly the strategy-owned quantity of the arm on ``venue`` ``pair`` now.

    The operator types ``pair`` back as ``confirmation``. This is the tick's
    exit, run on request under the venue's ``TickLock`` without waiting for a
    bar: it cancels the arm's stop, sells what the strategy owns (computed as
    the tick computes it, in Decimal, never external units), journals what
    executed and holds new entries with ``closed_by_operator_at``.

    Refusals write nothing (no config, journal or ledger change). The order
    of checks: well-formed input, the typed pair, the arm, the live gate (on
    live the restore fence refuses; the kill switch alone does not), the tick
    lock, unreadable or open obligations on the venue and mode, ambiguous
    ownership. Only then is ``open_venue(armed_for_venue)`` called (default:
    ``cli.venue_for_tick``), which returns ``(venue_obj, reader)`` or a
    refusal string; a candle must be readable and the snapshot must show
    nothing unrecorded (an unlinked sell is ambiguous ownership) and an owned
    quantity above zero.

    Then, under the config lock: the marker is saved, ``cancel_stops`` runs
    and a fresh snapshot must show no stop resting on the pair, else the
    marker is reverted and the close refused (``stop_cancel_failed``). What is
    sold never exceeds the owned quantity less any base the venue lost while
    the stop was cancelled. The exit is an Outbox intent of kind ``exit``
    under a fresh coid, committed before the send and never resent, with the
    latest closed bar as its owner bar (the journal record's ``bar_ts``).
    Paper journals what its venue executed. Live marks the intent before the
    send and journals ``exit_qty`` 0; ``_resolve_obligations`` settles it once
    by ``resolved_coid`` (M28.27), and the open obligation refuses a repeat. A
    documented venue refusal is journaled and resolves the intent as not
    executed (M28.28).

    The marker is not ``entries_paused``, so the restore fence never reads it
    and a live close under the kill switch leaves the venue's next tick, its
    exits, stops and reconcile running. Entries come back when the arm is
    disarmed and armed again, or, on paper, with ``paper.resume_entries``.
    """
    if not isinstance(venue, str) or not venue or not isinstance(pair, str) or not pair:
        return ClosePositionResult(code="bad_input")
    if confirmation is not None and not isinstance(confirmation, str):
        return ClosePositionResult(code="bad_input")
    if not confirmation:
        return ClosePositionResult(code="confirmation_required")
    if confirmation != pair:
        return ClosePositionResult(code="confirmation_mismatch")

    home = Path(home) if home is not None else kb_paths.home()
    config = kb_config.load_config(home)
    armed = kb_config.find_armed(config, venue, pair)
    if armed is None:
        return ClosePositionResult(code="not_armed")
    refused = _close_gate(home, venue, armed, config)
    if refused is not None:
        return refused

    lock = TickLock(home, venue)
    if not lock.acquire():
        return ClosePositionResult(code="tick_running", mode=armed.mode)
    try:
        now = clock() if clock is not None else _now_seconds()
        return _close_locked(home, venue, pair, open_venue=open_venue, now=now)
    finally:
        lock.release()


def _close_gate(
    home: Path, venue: str, armed: kb_config.ArmedPack, config: kb_config.Config
) -> ClosePositionResult | None:
    """The live gate for a close; None when it may run (paper always may).

    The restore fence of any live arm on the venue refuses, as it halts that
    venue's whole tick. The kill switch alone does not: ``check_live_tick``
    lets exits run under it. Any other gate refusal of the arm is returned
    with the gate's code (``live_disabled_paper_only_release`` in this build).
    """
    if armed.mode != "live":
        return None
    for live_armed in config.armed:
        if live_armed.venue != venue or live_armed.mode != "live":
            continue
        result = live_gate.check_live_tick(
            home, venue=venue, pair=live_armed.pair, entries_paused=live_armed.entries_paused
        )
        if not result.ok and result.code == live_gate.CODE_KILL_SWITCH:
            return ClosePositionResult(code="restore_fence", mode="live")
    result = live_gate.check_live_tick(home, venue=venue, pair=armed.pair, entries_paused=armed.entries_paused)
    if not result.ok:
        return ClosePositionResult(code=result.code, mode="live", gate_message=result.message)
    return None


def _close_locked(
    home: Path,
    venue: str,
    pair: str,
    *,
    open_venue: Callable[[list[kb_config.ArmedPack]], Any] | None,
    now: float,
) -> ClosePositionResult:
    """Everything after the tick lock; see ``close_position``."""
    config = kb_config.load_config(home)
    armed = kb_config.find_armed(config, venue, pair)
    if armed is None:
        return ClosePositionResult(code="not_armed")
    mode = armed.mode
    identity = _ownership_identity(armed, venue)
    view = kb_obligations.read_obligations(home)
    if not view.available:
        return ClosePositionResult(code="obligations_unavailable", mode=mode)
    if view.blocks_entries(venue=venue, mode=mode):
        return ClosePositionResult(code="obligation_open", mode=mode)
    ownership = project_ownership(home, identity)
    if ownership.ambiguous:
        return ClosePositionResult(code=OWNERSHIP_AMBIGUOUS, mode=mode)

    armed_for_venue = [a for a in config.armed if a.venue == venue]
    opener = open_venue if open_venue is not None else _default_close_venue(home)
    try:
        opened = opener(armed_for_venue)
    except (InstrumentMetadataError, OSError, RuntimeError, ValueError, TypeError, KeyError):
        opened = None
    if not isinstance(opened, tuple) or len(opened) != 2:
        return ClosePositionResult(code="venue_unavailable", mode=mode)
    venue_obj, reader = opened
    try:
        candles = list(reader(venue, pair) or [])
    except (InstrumentMetadataError, OSError, RuntimeError, ValueError, TypeError, KeyError):
        candles = []  # a feed failure (``cli.CandleFetchError`` is a RuntimeError): no candle read
    if not candles:
        return ClosePositionResult(code="candles_unavailable", mode=mode)
    bar_ts = int(candles[-1].ts_ms)
    try:
        snap = venue_obj.snapshot()
    except (InstrumentMetadataError, OSError, RuntimeError, ValueError):
        return ClosePositionResult(code="venue_unavailable", mode=mode)

    base, _quote = _base_quote(pair)
    fills = list(snap.recent_fills or [])
    _ids, stop_fill_qty = _stop_fills_for(home, identity, fills)
    journal_qty = max(ownership.attributable_qty - stop_fill_qty, Decimal(0))
    if journal_qty > 0 and _unrecorded_unlinked_sell(home, identity, fills):
        # A sell the venue cannot link may be this arm's stop: the tick would
        # hold it for an operator resolve, so what is owned is not known.
        return ClosePositionResult(code=OWNERSHIP_AMBIGUOUS, mode=mode)
    venue_base = _snapshot_balances(snap).get(base, Decimal(0))
    recognized = _recognized_open_qty(armed, venue, candles, _snapshot_orders(snap))
    owned = min(venue_base, max(journal_qty, recognized))
    if owned <= 0:
        return ClosePositionResult(code="nothing_to_close", mode=mode)
    external = venue_base - owned

    with kb_config._paper_command_transaction(home) as acquired:
        if not acquired:
            return ClosePositionResult(code="config_busy", mode=mode)
        if not _set_closed_marker(home, armed, int(now)):
            return ClosePositionResult(code="config_busy", mode=mode)
        cancelled = _cancel_stops_for_close(venue_obj, pair, base=base, owned=owned, external=external)
        if isinstance(cancelled, str):
            _clear_closed_marker(home, armed, int(now))  # a refusal writes nothing
            return ClosePositionResult(code=cancelled, mode=mode)
    owned, after = cancelled

    stop_fill_ids, stop_fill_qty = _stop_fills_for(home, identity, list(after.recent_fills or []))
    coid = coid_for(
        pack_id=armed.pack_id,
        pack_version=armed.pack_version,
        venue=venue,
        pair=pair,
        bar_ts=int(now * 1000),
        intent="close",
    )
    body = json.dumps({"kind": "exit", "qty": str(owned), "pair": pair}, separators=(",", ":"), sort_keys=True)
    # A live acknowledgement is not an executed quantity (M28.27): the intent
    # is an obligation from before the send, settled by order_outcome.
    tracked = mode != "paper"
    venue_refusals: list[tuple[str, str]] = []
    intent_refused: str | None = None
    sold = Decimal(0)
    try:
        state, ref = _dispatch_place(
            home=home,
            mode=mode,
            venue=venue,
            identity=_intent_identity(armed, bar_ts),
            coid=coid,
            body=body,
            venue_obj=venue_obj,
            track=tracked,
        )
    except StoreError as exc:
        # The commit, the mark or the sent flag failed: what was sent is not
        # known. A committed intent stays an obligation; nothing is resent.
        code, intent_refused = "outcome_unknown", exc.code
        print(f"intent refused: {exc.code}", flush=True)
    except VenueRejectedError as exc:
        code, intent_refused = "venue_rejected", _venue_refused(exc, coid, venue_refusals)
    except (InstrumentMetadataError, OSError, RuntimeError, ValueError, AttributeError):
        code = "outcome_unknown"  # committed before the send, so it stays an obligation
    else:
        if state != "sent":
            code = "outcome_unknown"  # a coid already committed is never sent again
        elif tracked:
            code = "close_pending"
        else:
            executed = _executed_exit_qty(mode, ref, requested=owned)
            if executed is None:
                audit_needs_reconcile(OperationalStore(home / "ops.sqlite"), coid=coid)
                code = "outcome_unknown"
            else:
                code, sold = "position_closed", executed
    _record_tick(
        kb_journal.append,
        now=int(now),
        venue=venue,
        pack=armed.pack_id,
        bar_ts=bar_ts,
        detail={
            "pair": pair,
            "mode": mode,
            "reason": CLOSE_REASON,
            "close_coid": coid,
            "entry_qty": "0",
            "exit_qty": str(sold),
            "stop_qty": str(stop_fill_qty),
            **({"stop_fill_ids": stop_fill_ids} if stop_fill_ids else {}),
            "owned_qty_after": str(owned - sold),
            "entries_paused": True,
            "kill_switch": live_gate.kill_state(home).engaged,
            "intent_refused": intent_refused,
        },
    )
    # Journal first, then retire the refused intent, as the tick does.
    for refused_coid, refused_code in venue_refusals:
        try:
            kb_obligations.record_rejection(OperationalStore(home / "ops.sqlite"), coid=refused_coid, code=refused_code)
        except StoreError as exc:
            print(f"intent refused: {exc.code}", flush=True)  # it stays unresolved and keeps holding entries
    return ClosePositionResult(
        code=code,
        ok=code in ("position_closed", "close_pending"),
        effect="changed",
        mode=mode,
        client_order_id=coid,
        requested_qty=owned,
        sold_qty=sold if code == "position_closed" else None,
    )


def _default_close_venue(home: Path) -> Callable[[list[kb_config.ArmedPack]], Any]:
    """The scheduler's own venue setup (``cli.venue_for_tick``) with its default fetch and transport."""

    def open_venue(armed_list: list[kb_config.ArmedPack]) -> Any:
        from krellbot import cli as kb_cli

        return kb_cli.venue_for_tick(
            home, armed_list, transport=kb_cli._default_transport(), fetch=kb_cli._default_fetch
        )

    return open_venue


def _cancel_stops_for_close(
    venue_obj: Any, pair: str, *, base: str, owned: Decimal, external: Decimal
) -> tuple[Decimal, Any] | str:
    """Cancel the pair's stops; return what may still be sold and the snapshot after, or a refusal code.

    ``stop_cancel_failed`` when ``cancel_stops`` raises or a fresh snapshot
    still shows a stop resting on the pair: selling under a resting stop could
    sell the same units twice, and external ones after. Base the venue lost
    meanwhile may be that stop filling, so it comes off what is sold
    (``external`` is the base that was not the strategy's before the cancel);
    ``nothing_to_close`` when nothing is left.
    """
    try:
        venue_obj.cancel_stops(pair)
        after = venue_obj.snapshot()
    except (InstrumentMetadataError, OSError, RuntimeError, ValueError):
        return "stop_cancel_failed"
    if any(
        getattr(o, "pair", None) == pair and getattr(o, "stop_price", None) is not None for o in _snapshot_orders(after)
    ):
        return "stop_cancel_failed"
    sellable = min(owned, max(_snapshot_balances(after).get(base, Decimal(0)) - external, Decimal(0)))
    if sellable <= 0:
        return "nothing_to_close"
    return sellable, after


def _set_closed_marker(home: Path, armed: kb_config.ArmedPack, at: int) -> bool:
    """Save ``closed_by_operator_at`` on the arm; False when the arm changed or config cannot be written.

    The caller holds the config lock and the venue's tick lock.
    """
    config = kb_config.load_config(home)
    target = kb_config.find_armed(config, armed.venue, armed.pair)
    if target is None or (target.pack_id, target.mode) != (armed.pack_id, armed.mode):
        return False
    target.closed_by_operator_at = at
    try:
        kb_config.save_config(home, config)
    except OSError:
        return False
    return True


def _clear_closed_marker(home: Path, armed: kb_config.ArmedPack, at: int) -> None:
    """Put the marker back as it was before a refused close set it (still under both locks)."""
    config = kb_config.load_config(home)
    target = kb_config.find_armed(config, armed.venue, armed.pair)
    if target is None or target.closed_by_operator_at != at:
        return
    target.closed_by_operator_at = armed.closed_by_operator_at
    try:
        kb_config.save_config(home, config)
    except OSError:
        print("close refused: entries stay held, the config could not be restored", flush=True)


def _unrecorded_unlinked_sell(home: Path, identity: OwnershipIdentity, fills: list) -> bool:
    """True when a sell on the pair that the venue could not link has no exit hold yet.

    The tick would commit a hold for it (``_hold_unlinked_sells``); a close
    only reads, so it refuses instead. A committed hold is an obligation and
    refuses the close earlier while it is unresolved.
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
    held = _committed_coids(home)
    return any(f.id not in accounted and _unlinked_sell_coid(identity, f.id) not in held for f in unknown)


def _resolve_obligations(home: Path, *, venue: str, venue_obj: Any, snap: Any, journal, now: float) -> None:
    """Record evidence for each unresolved intent on ``venue`` and retire what it settles.

    Order of durable writes per intent: observation row, then journal
    accounting (idempotent by ``resolved_coid``), then the resolution row.
    An interruption anywhere leaves a state the next call converges from
    without double counting. Nothing is resubmitted.

    A repaired stop (kind ``stop``) is looked up through the venue's
    ``stop_outcome`` when it has one (the reference ``place_stop`` sent it
    under), else ``order_outcome``. An authoritative ``open`` is that stop's
    success: it rests, so the obligation is retired. For entries and exits
    ``open`` is no outcome.
    """
    view = kb_obligations.read_obligations(home)
    if not view.available:
        return
    lookup = getattr(venue_obj, "order_outcome", None)
    stop_lookup = getattr(venue_obj, "stop_outcome", None) or lookup
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
        find = stop_lookup if intent.kind == "stop" else lookup
        if find is not None:
            try:
                outcome = find(intent.coid, pair=intent.pair)
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
        stop_resting = outcome.status == "open" and intent.kind == "stop" and outcome.authoritative
        needs_accounting = outcome.status in ("filled", "partial") or stop_resting
        if needs_accounting and not _account_intent(home, intent, outcome, venue=venue, journal=journal, now=now):
            continue  # no durable owner context: never assign it to whoever is armed now
        if outcome.status == "absent" and outcome.authoritative:
            kb_obligations.record_resolution(
                store, coid=intent.coid, outcome="absent", filled_qty=Decimal(0), evidence="venue_record_complete"
            )
        elif stop_resting:
            # Accounted above with the stop coid as entry_coid, so the stop's
            # later fill is attributed to this identity once (M28.24).
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


def _executed_exit_qty(mode: str, ref: Any, *, requested: Decimal) -> Decimal | None:
    """What an exit's acknowledgement proves executed; None when it proves nothing.

    Only the paper venue's acknowledgement is its complete record of what
    executed. A live one is not: Kraken reports no executed volume and
    Coinbase echoes the requested size, so a live exit is settled from
    ``order_outcome`` instead.
    """
    if mode != "paper":
        return None
    filled = getattr(ref, "filled_qty", None)
    if not isinstance(filled, Decimal) or not filled.is_finite() or not Decimal(0) <= filled <= requested:
        return None
    return filled


def _unsettled_exits(view: kb_obligations.ObligationsView, identity: OwnershipIdentity) -> list[str]:
    """Coids of ``identity``'s exits whose outcome is not settled yet."""
    return [
        o.coid
        for o in view.unresolved
        if o.kind == "exit"
        and o.venue == identity.venue
        and o.mode == identity.mode
        and o.pair == identity.pair
        and o.pack_id == identity.pack_id
    ]


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
    """What this identity's own resting protective stops show it holds.

    The tick owns ``min(venue_base, max(journal, recognized))``, so a position
    whose journal write was lost after its send can still be protected and
    exited. An open order's requested quantity is not executed inventory: an
    unfilled entry would count units never bought. Only a resting protective
    stop counts, because the engine places one only for quantity it filled or
    owns. Entries, exits and orders that cannot be attributed never count.

    A protective stop of this identity is a sell order with a stop price on the
    armed pair whose client order id is this identity's entry coid (PaperVenue
    rests an entry's stop under it) or stop coid (a repaired stop) for a bar in
    ``candles``. Those coids hash the pack, version, venue and pair, so another
    strategy's, another venue's (KQ-01) and external orders never match. One
    coid listed twice is one order, counted at its smaller quantity. The
    identity holds one position at a time (an entry needs ``owned == 0``) and
    each of its stops covers all of it, so the largest stop counts, never a sum.

    Live arms count nothing. A live venue lists a resting order at its
    requested quantity (Kraken ``vol``, Coinbase ``base_size``), and
    ``OpenOrder`` carries no executed quantity, so a live stop that filled in
    part still shows its full size after its fill was journaled. Kraken's
    entry-attached stop has no ``cl_ord_id`` either. Live ownership comes from
    the journal, which venue evidence keeps. PaperVenue fills a stop in full
    and removes it, so a resting paper stop's quantity is what it still holds.
    """
    if armed.mode != "paper":
        return Decimal(0)
    stop_coids = {
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
    resting: dict[str, Decimal] = {}
    for order in orders:
        coid = getattr(order, "coid", None)
        if coid not in stop_coids or getattr(order, "pair", None) != armed.pair:
            continue
        if getattr(order, "side", None) != "sell" or getattr(order, "stop_price", None) is None:
            continue
        try:
            qty = Decimal(str(getattr(order, "qty", None)))
        except (ArithmeticError, ValueError):
            continue
        if not qty.is_finite() or qty <= 0:
            continue
        resting[coid] = min(qty, resting.get(coid, qty))
    return max(resting.values(), default=Decimal(0))


def _stop_price(pack: dict, candles: list, *, home: Path, identity: OwnershipIdentity) -> tuple[Decimal | None, bool]:
    """The price a missing stop is re-placed at, and whether the pack's rule priced it.

    A percentage stop is re-placed ``pct`` below the latest close, as before.
    Every other stop uses the rule the entry uses, ``pack/evaluate.stop_price``
    over the tick's candles (an ATR stop: the latest close minus ``mult`` x
    ATR), and is never placed below the stop the arm recorded for the position
    (``_recorded_entry_stop``).

    When the rule gives no positive price (too few candles for the ATR, a stop
    rule the engine cannot read), the second value is False and the price is
    the recorded stop, or ``None`` when none is recorded either: then no stop
    is placed. A stop is never guessed. ``StoreError`` from reading the
    recorded stop propagates.
    """
    stop = (pack.get("risk") or {}).get("stop") or {}
    if stop.get("type") == "pct":
        last_close = Decimal(str(candles[-1].close))
        return last_close * (Decimal(1) - Decimal(str(stop["pct"])) / Decimal(100)), True
    rule = kb_evaluate.stop_price(pack, candles)
    recorded = _recorded_entry_stop(home, identity)
    if rule is not None and recorded is not None and rule < recorded:
        rule = recorded
    if rule is not None and rule > 0:
        return rule, True
    return recorded, False


def _recorded_entry_stop(home: Path, identity: OwnershipIdentity) -> Decimal | None:
    """The stop recorded with the entry that opened ``identity``'s position.

    The source is the Outbox: every entry intent is committed to
    ``<home>/ops.sqlite`` before it is sent, with the stop the entry rule
    computed on the signal bar, so it survives restarts and does not depend on
    a venue still listing the stop. An intent counts only when its record
    names this identity's venue and mode and its body this identity's pack and
    pair; earlier rows without that context are never attributed. An identity
    enters only when flat, so its latest entry intent opened the position it
    holds. ``None`` when there is no such intent or its stop is not a positive
    number. ``StoreError`` propagates.
    """
    path = Path(home) / "ops.sqlite"
    if not path.exists():
        return None
    recorded: Decimal | None = None
    for _row_id, kind, payload in OperationalStore(path).read_ledger():
        if kind != "outbox":
            continue
        try:
            record = json.loads(payload)
            body = json.loads(record["body"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if not isinstance(body, dict):
            continue
        if (
            record.get("venue") != identity.venue
            or record.get("mode") != identity.mode
            or body.get("kind") != "entry"
            or body.get("pair") != identity.pair
            or body.get("pack_id") != identity.pack_id
        ):
            continue
        try:
            stop = Decimal(str(body.get("stop")))
        except (InvalidOperation, ValueError):
            stop = None
        recorded = stop if stop is not None and stop.is_finite() and stop > 0 else None
    return recorded


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
                # Neither a reconciled outcome nor an operator close is the
                # tick's own evaluation of the bar, so the bar still runs.
                if isinstance(detail, dict) and detail.get("reason") in (RECONCILED_REASON, CLOSE_REASON):
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
