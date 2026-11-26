# Paper trading

Paper mode lets a pack run end-to-end against a local fill simulator — no
exchange API call, no live money, no license required. The engine and the
pack see the same prices and the same stop behavior as a real venue would,
just inside `$KRELLBOT_HOME/run/paper-<venue>.json`.

## Arm a pack in paper mode

```
krellbot arm tests/fixtures/packs/sma_cross.json \
    --venue kraken \
    --mode paper \
    --paper-balance 1000
```

The arm step records the pack path, the sha256 of its bytes, the venue,
the pair, the cap from the pack's `risk.max_account_pct`, and the starting
cash. It refuses if the cap cannot meet the pair minimum. It never places
an order.

A second pack on the same venue+pair is refused — only one armed pack
per (venue, pair).

## Run a tick

```
krellbot tick --venue kraken \
    --offline-candles tests/fixtures/candles/kraken_SUIUSD_1h_sample.csv
```

The scheduler can fetch public candles for Kraken, Coinbase, Binance, and
MEXC spot. Binance and MEXC use their credential-free public klines endpoints
and fetch instrument rules separately. MEXC requests the `60m` interval for
`1h` packs and returns at most 500 bars per tick. Order simulation remains local. For paper
venues without a scheduled candle reader, the pinned CSV path is the
verified end-to-end tick path.

The first-run wizard can save a BTC/USD strategy for Kraken (`XXBTZUSD`)
or Coinbase (`BTC-USD`). It shows the exchange, market, one-hour timeframe,
and simulated USD starting balance before the run is armed. Existing saved
SUI/USD strategies remain available. The Run form indicates whether the
selected market has scheduled candles, needs offline replay, or has not yet
been verified. Scheduled coverage in this interface currently names Kraken
BTC/USD and SUI/USD, Coinbase BTC/USD, and Binance BTC/USDT at 1h, 4h, and 1d.
Kraken ETH/USD (`ETHUSD`) and Coinbase ETH/USD (`ETH-USD`) have fixture
coverage at 1h, including public response parsing, instrument minimums and
increments, the venue fee model, simulated fills, and metadata refusals.

The paper acceptance suite exercises one spot market per registered venue
using pinned candles and instrument rules. For each market it validates and
arms a strategy, checks the simulated fill and balance in the workstation,
and confirms that the release refuses live arming and live ticks.

| Tested pair | Venues |
| --- | --- |
| `XXBTZUSD` (BTC/USD) | Kraken |
| `BTC-USD` | Coinbase |
| `BTCUSDT` | Binance, Bybit, Ourbit, Toobit, XT, MEXC, Bitget, HTX, BTCC |
| `BTC-USDT` | Gate, Pionex, OKX, KuCoin, Deepcoin, LBank, Crypto.com |

The tick engine:

1. Acquires `<home>/run/<venue>.lock`. If another tick is already running,
   prints `another tick running` and exits 0.
2. Loads the license cache and the armed-pack config.
3. Snapshots the paper venue.
4. For each armed pack on that venue: if the pack owns base on the pair
   and no stop is resting on the pair, place a stop. If placing the stop
   fails, market-exit the owned qty in the same tick.
5. Acts on the latest closed bar. Missed bars are coalesced by replaying
   every candle into `evaluate.run`, so indicator state includes them.
6. Entry when `Target.long`, owned qty is 0, entries are allowed, and the
   sized qty meets `ordermin` and `costmin`.
7. Exit only when the evaluator says `exit` and the pack owns qty. A flat or warmup bar does not sell.
8. While long and already in, calls `raise_stop` only if the new stop is
   strictly above the resting stop.
9. Journals one `kind=tick` record per pack per bar.
10. If entries are blocked by the license gate, prints
    `License lapsed or unreachable: entries off, exits on`.

## Stop, pause, Stop paper run and the kill switch

This is what the engine does today.

- **Protective stop.** An entry rests a stop computed from the signal bar's
  close. The signal bar is the latest closed bar, the one on which the entry
  condition fired. A `pct` stop is `close * (1 - pct/100)` and an `atr` stop
  is `close - mult * ATR(len)` on that bar. The fill price is not used. On later
  bars the stop is only raised, never lowered. While the entry condition
  still holds and the exit condition does not, the tick recomputes it from
  that bar's close and calls `raise_stop` when the new price is strictly
  above the resting stop. On a flat bar the stop stays where it is.
- **Missing stop.** If the pack owns base and no stop rests on the pair, the
  tick re-places one with the pack's own stop rule on the latest closed bars
  (`run._stop_price`). A `pct` stop uses `close * (1 - pct/100)`. An `atr`
  stop uses the entry rule, `close - mult * ATR(len)`, and is never placed
  below the stop recorded at entry (the committed entry intent's `stop` in
  `ops.sqlite`). If the rule gives no price (too few candles, a rule the
  engine cannot read), the tick re-places the stop recorded at entry, or
  places no stop when none was recorded. It journals
  `stop_price_unavailable` and Operations raises a critical alert. If
  placing the stop fails, the tick market-exits the owned qty.
- **Pause new buys** sets `entries_paused`. It stops new entries only.
  Exits, stop repair and `raise_stop` keep running.
- **Stop paper run** (`paper.disarm`) removes the armed record only. The
  paper position and its resting stop stay in the state file, unmanaged.
  Ticks evaluate armed records only, so nothing raises or re-places that stop
  or acts on an exit signal for it. There is no close-position control yet
  ([M28.32](milestones/M28.md)).
- **Kill switch, paper.** New paper arms are refused with
  `kill_switch_engaged`. Every paper tick suppresses entries and journals
  `kill_switch: true`, while exits, stop repair and `raise_stop` run.
- **Kill switch, live.** The whole live tick halts. `check_live_send`
  refuses with `kill_switch_engaged` before any venue call, in the
  scheduler's venue setup and in `run.tick`, so no entry, exit, stop repair
  or stop raise is sent. This release is paper-only, so the same check
  refuses a live tick before it reaches the kill switch, with
  `live_disabled_paper_only_release`.

## Costs

| Venue    | Taker fee | Slippage |
| -------- | --------- | -------- |
| Kraken   | 40 bps    | 5 bps    |
| Coinbase | 120 bps   | 5 bps    |

A market entry fills at `pricing_close * (1 + buy_slip)`. A market exit
fills at `pricing_close * (1 - sell_slip)`. When the candle reader still
includes the in-progress bar, `pricing_close` is that forming close (not
only the last closed close); otherwise it is the latest closed close. A
stop fill lands at `min(stop, open) * (1 - sell_slip)` before any signal
fill on the same bar. Strategy evaluation still acts on closed bars only.

## State file

The paper venue's state lives at
`$KRELLBOT_HOME/run/paper-<venue>.json` and is written via
`paths.atomic_write`. A crash mid-write leaves the previous file intact
and never leaves a stray `.tmp` sibling.

```
{
  "venue": "kraken",
  "balances": {"USD": "...", "SUI": "..."},
  "open_orders": [
    {"coid": "...", "pair": "SUIUSD", "side": "sell",
     "qty": "...", "stop_price": "..."}
  ],
  "recent_fills": [
    {"coid": "...", "pair": "SUIUSD", "side": "buy",
     "qty": "...", "price": "...", "ts_ms": 0}
  ]
}
```

## Kraken validate-only

Paper ticks use public market data or a local replay. They simulate fills
without reading exchange credentials or calling a private exchange API.
The separate Kraken validate-only smoke in [the live qualification
milestone](milestones/M10.md#owner-gates) checks a signed request with
`validate=true` without submitting an order.

## Inspect

```
krellbot status [--venue kraken]
krellbot journal --tail 10
```

`status` prints one row per armed pack:
`pack_id pack_version venue pair cap stop mode requires_license`.

`journal` prints the most recent N journal records (newest last). Each
record has the canonical keys: `ts`, `kind`, `venue`, `pack`, `bar_ts`,
`detail`.

## Disarm

```
krellbot disarm --venue kraken --pair SUIUSD
```

Disarm removes the armed record. The paper state file is left on disk
until you delete it manually.

## Raise the stop

```
krellbot stop --venue kraken --pair SUIUSD --price 12.50
```

Raises the resting stop on the armed pack. The new stop must be strictly
above the current stop — the engine never lowers a stop.

## What this phase does NOT do

- Does not place a real order. Paper only.
- Does not require a license for fixture or community packs.
- Does not lower a stop.
- Does not sell more base than this pack's journal says it filled.
