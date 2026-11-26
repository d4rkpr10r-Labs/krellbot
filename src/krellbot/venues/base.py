"""Venue abstractions: shared dataclasses and the `Venue` protocol.

Money and quantity math is `decimal.Decimal` everywhere. The protocol methods
all take Decimal for size/price. Concrete adapters live in `kraken.py` and
`coinbase.py`; both speak through a Transport that tests inject.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class PairRules:
    """Trading rules for one pair on a venue."""

    ordermin: Decimal
    costmin: Decimal
    lot_decimals: int
    price_decimals: int


@dataclass(frozen=True)
class Balance:
    """A spot asset balance: free (available) + locked (in open orders)."""

    asset: str
    free: Decimal
    locked: Decimal = Decimal(0)


@dataclass(frozen=True)
class OpenOrder:
    """One resting or live order the venue reports."""

    id: str
    coid: str
    pair: str
    side: str  # "buy" or "sell"
    qty: Decimal
    stop_price: Decimal | None = None


@dataclass(frozen=True)
class Fill:
    """A historical fill reported in the venue's truth snapshot."""

    id: str
    coid: str
    pair: str
    side: str
    qty: Decimal
    price: Decimal
    ts_ms: int
    # Client order id of the resting order this fill executed, when the venue
    # reports it (paper stop fills do). A live protective stop's fill names the
    # entry coid that placed the stop. ``coid`` stays the fill's own id.
    order_coid: str | None = None
    # True when the venue looked up which order executed this sell and could
    # not establish it from documented fields, so the fill cannot be shown not
    # to be a strategy's protective stop. ``order_coid`` is then None.
    order_unknown: bool = False


@dataclass(frozen=True)
class OrderOutcome:
    """What a venue can say about one client order id.

    ``status`` is ``filled``, ``partial``, ``open``, ``absent`` or
    ``unknown``. ``absent`` is only meaningful when ``authoritative`` is
    true: the venue's record is complete, so no match proves the order never
    executed. A missing match in a bounded open-order list or recent-fill
    window is ``unknown``, never ``absent``.
    """

    status: str
    filled_qty: Decimal = Decimal(0)
    fill_ids: tuple[str, ...] = ()
    authoritative: bool = False


UNKNOWN_OUTCOME = OrderOutcome(status="unknown")


@dataclass(frozen=True)
class Truth:
    """A point-in-time view of account state."""

    balances: list[Balance] = field(default_factory=list)
    open_orders: list[OpenOrder] = field(default_factory=list)
    recent_fills: list[Fill] = field(default_factory=list)


@dataclass(frozen=True)
class OrderRef:
    """What `place_*` returns: where the order went and how much we got."""

    id: str
    coid: str
    pair: str
    side: str
    qty: Decimal
    filled_qty: Decimal
    stop_price: Decimal | None = None


@dataclass(frozen=True)
class KeyPerms:
    """What the venue says the API key can do."""

    can_trade: bool
    can_withdraw: bool


class _KeyProbeReason:
    """Built-in refusal reasons used by `KeyProbeResult`.

    The strings here are user-facing and printed by `krellbot keys check`
    on stderr. They must be secret-free (no API key, no secret, no raw
    `apiKey` echo) and short enough to read in a terminal. New entries
    must continue that contract.
    """

    WITHDRAW_CAPABLE = "venue confirmed withdraw rights; refused"
    UNKNOWN_PERMISSION = "venue returned an unrecognized permission; refused"
    TRADE_OFF = "no required trade permission; refused"
    INVALID = "key cannot be verified (invalid or permission denied); refused"
    MALFORMED = "venue response shape unknown; refused"
    UNREACHABLE = "transport unreachable; cannot verify"


class KeyProbeOutcome:
    """Closed taxonomy for `KeyProbeResult.outcome`.

    A future wizard key-add must refuse to store on any value other than
    `TRADE_ONLY`. There is no "we'd like to update later" path — once a
    key is on disk, an unrecognized refusal reason is just a refusal.
    Unknown venue permission tokens are their own fail-closed outcome
    (``UNKNOWN_PERMISSION``), distinct from a confirmed-withdraw key.
    """

    TRADE_ONLY = "trade_only"
    WITHDRAW_CAPABLE = "withdraw_capable"
    UNKNOWN_PERMISSION = "unknown_permission"
    TRADE_OFF = "trade_off"
    INVALID = "invalid"
    MALFORMED = "malformed"
    UNREACHABLE = "unreachable"

    _ALL = frozenset(
        {
            TRADE_ONLY,
            WITHDRAW_CAPABLE,
            UNKNOWN_PERMISSION,
            TRADE_OFF,
            INVALID,
            MALFORMED,
            UNREACHABLE,
        }
    )

    @classmethod
    def values(cls) -> frozenset[str]:
        return cls._ALL


@dataclass(frozen=True)
class KeyProbeResult:
    """The result of one `krellbot keys check` probe, typed and secret-free.

    `outcome` is one of `KeyProbeOutcome.*`; `reason` is a short,
    user-facing explanation intended for stderr. The CLI and any future
    wizard key-add gate on `trade_only` (see `KeyProbeOutcome.TRADE_ONLY`).
    """

    outcome: str
    reason: str

    def __post_init__(self) -> None:  # type: ignore[no-untyped-def]
        if self.outcome not in KeyProbeOutcome.values():
            raise ValueError(f"unknown KeyProbeOutcome: {self.outcome!r}")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("KeyProbeResult.reason must be a non-empty string")

    @staticmethod
    def is_trade_only_outcome(outcome: str) -> bool:
        """Storage gate: only `outcome == KeyProbeOutcome.TRADE_ONLY` is `True`.

        A future wizard key-add must call this before any `kb_secrets.store`
        write. Anything else is a refusal, even if it looks "almost" fine.
        """
        return outcome == KeyProbeOutcome.TRADE_ONLY

    @property
    def is_trade_only(self) -> bool:
        """Instance helper for the same gate."""
        return self.outcome == KeyProbeOutcome.TRADE_ONLY

    def to_key_perms(self) -> KeyPerms:
        """Narrow the typed result to the engine's `KeyPerms` shape.

        `trade_only` → ``(can_trade=True, can_withdraw=False)`` so the
        arm site accepts the key.

        `withdraw_capable` → ``(can_trade=False, can_withdraw=True)`` so
        the arm site can distinguish a confirmed-withdraw key (which
        reports ``"withdraw is on; trade-only keys refused"``) from a
        plain trade-off / unknown-permission / invalid / malformed /
        unreachable refusal (which reports ``"trade is off"``). Without
        this distinction the arm-site diagnostic collapses every refusal
        to the same line and the user cannot tell what was actually wrong.

        Every other outcome (including ``unknown_permission``) →
        ``(can_trade=False, can_withdraw=False)`` so the engine refuses
        the key fail-closed. Use this when plumbing the result into
        callers that still inspect `can_trade` / `can_withdraw`
        booleans (e.g. `arm_pack`). Prefer inspecting ``outcome`` when
        typed API refusals are required.
        """
        from krellbot.venues.base import KeyPerms  # local import: avoid cycle

        if self.outcome == KeyProbeOutcome.TRADE_ONLY:
            return KeyPerms(can_trade=True, can_withdraw=False)
        if self.outcome == KeyProbeOutcome.WITHDRAW_CAPABLE:
            return KeyPerms(can_trade=False, can_withdraw=True)
        return KeyPerms(can_trade=False, can_withdraw=False)


class ProtectiveStopRejectedError(RuntimeError):
    """The venue filled an entry and refused the protective stop."""


class WithdrawCapableError(RuntimeError):
    """Raised when `check_key` finds the key can withdraw. Engine refuses it.

    Subclasses (`KeyUnverifiableError`, `KeyTradeOffError`,
    `KeyMalformedError`, `KeyUnknownPermissionError`) refine the
    refusal reason so the CLI can tell invalid keys, shape errors,
    read-only keys, and actually-withdraw-capable keys apart. They all
    remain `isinstance(WithdrawCapableError)` for the engine.
    """


class KeyUnverifiableError(WithdrawCapableError):
    """The key could not be verified at all (e.g. invalid / permission denied).

    Refused: never store, never treat as trade-only.
    """


class KeyTradeOffError(WithdrawCapableError):
    """The key lacks a required trade permission (read-only or partial).

    Refused: never store, never treat as trade-only. The engine still
    sees this as `WithdrawCapableError` so existing `except` blocks
    continue to refuse it.
    """


class KeyMalformedError(WithdrawCapableError):
    """The venue returned a response whose shape we cannot reason about.

    Refused: never store, never treat as trade-only.
    """


class KeyUnknownPermissionError(WithdrawCapableError):
    """The key grants a permission outside the venue's accepted trade-only set.

    Covers permission tokens the adapter does not recognize and known
    permissions beyond spot trading. Fails closed so a venue-added
    permission cannot silently make a trade-only key partially trusted.
    """


class KrakenKeyUnknownPermissionError(KeyUnknownPermissionError):
    """A future Kraken-added permission token was present and unrecognized.

    Fails closed so a new (e.g.) `transfer-funds` token cannot silently
    make a verified-trade-only key partially-trusted.
    """


class Venue(Protocol):
    """The contract every venue adapter satisfies.

    Adapters are constructed with their credentials and a Transport. They never
    contact a live HTTP endpoint during tests because Transport is faked.
    """

    def rules(self, pair: str) -> PairRules: ...

    def snapshot(self) -> Truth: ...

    def place_entry_with_stop(self, coid: str, qty: Decimal, stop: Decimal, *, pair: str) -> OrderRef: ...

    def place_exit(self, coid: str, qty: Decimal, *, pair: str) -> OrderRef: ...

    def cancel_stops(self, pair: str) -> None: ...

    def raise_stop(self, pair: str, new_stop: Decimal) -> None: ...

    def order_by_coid(self, coid: str) -> OpenOrder | None: ...

    def check_key(self) -> KeyPerms: ...
