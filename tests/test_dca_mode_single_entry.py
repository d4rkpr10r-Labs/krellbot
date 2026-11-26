"""The built-in DCA mode buys once while flat and holds; it adds no slice per bar.

The DCA copy in ``docs/trading-modes.md``, ``docs/launch-pack-family.md`` and
the ``mode-dca`` pack origin rests on this engine behaviour: one buy sized at
``risk.max_account_pct`` of equity while flat, held until the exit or the
stop leaves it flat, and only then another buy. Synthetic candles only; no
venue, no keys, no network.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from krellbot.backtest.engine import Backtester, BarRecord
from krellbot.pack.model import Candle
from krellbot.packs import mode_packs

ROOT = Path(__file__).resolve().parents[1]
DAY_MS = 86_400_000
STARTING_CASH = Decimal(10000)

# The phrasings that implied a fresh buy each interval.
_SLICE_CLAIMS = re.compile(
    r"slice\s*=|per-buy sizing|when a slice is eligible|slice / position sizing",
    re.IGNORECASE,
)


def _candles(closes: list[str], lows: dict[int, str] | None = None) -> list[Candle]:
    lows = lows or {}
    out = []
    for i, close in enumerate(closes):
        price = Decimal(close)
        out.append(
            Candle(
                ts_ms=i * DAY_MS,
                open=price,
                high=price,
                low=Decimal(lows.get(i, close)),
                close=price,
                volume=Decimal(1),
            )
        )
    return out


def _fills(records: list[BarRecord]) -> list[tuple[int, str, bool]]:
    return [(r.t, r.fill["side"], bool(r.fill["is_stop"])) for r in records if r.fill is not None]


def test_dca_mode_places_one_buy_while_flat_and_holds_it():
    pack = mode_packs.load_mode_pack("dca")
    closes = [str(100 + i) for i in range(30)]
    records = Backtester(pack, _candles(closes), starting_cash=STARTING_CASH).run()

    fills = _fills(records)
    assert fills == [(2, "buy", False)]
    entry = records[2]
    assert entry.qty_after > 0
    assert all(r.qty_after == entry.qty_after for r in records[2:])
    spend = entry.fill["qty"] * entry.fill["price"] + entry.fill["fee"]
    assert 0 < spend <= STARTING_CASH * pack["risk"]["max_account_pct"] / 100


def test_dca_mode_buys_again_only_after_the_stop_leaves_it_flat():
    pack = mode_packs.load_mode_pack("dca")
    assert pack["risk"]["stop"] == {"type": "pct", "pct": 15}
    # Long from bar 2 at 100; the bar-5 low of 80 breaches the 85 stop.
    closes = ["100"] * 5 + ["90"] * 6
    records = Backtester(pack, _candles(closes, lows={5: "80"}), starting_cash=STARTING_CASH).run()

    assert _fills(records) == [(2, "buy", False), (5, "sell", True), (7, "buy", False)]
    assert records[6].qty_after == 0


def test_dca_copy_does_not_claim_a_buy_each_interval():
    origin = mode_packs.load_mode_pack("dca")["origin"]
    texts = {
        "mode-dca origin": origin,
        "docs/trading-modes.md": (ROOT / "docs" / "trading-modes.md").read_text(encoding="utf-8"),
        "docs/launch-pack-family.md": (ROOT / "docs" / "launch-pack-family.md").read_text(encoding="utf-8"),
    }
    for name, text in texts.items():
        assert not _SLICE_CLAIMS.search(text), name
        assert "while flat" in text, name
    assert "max_account_pct" in origin
