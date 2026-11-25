# Offer verified BTC/USD starters and label how each market runs

Give newcomers a usable BTC/USD starter for Kraken and Coinbase and show scheduled versus replay availability before arming.

## Implementation

- Add Kraken `XXBTZUSD` and Coinbase `BTC-USD` as curated choices for the first starter, with BTC/USD as the readable label. Show venue, market, timeframe, starting paper balance, and quote currency before Start. Preserve access to current SUI/USD strategies and existing saved packs.
- Reuse shared pack validation and venue metadata. Provide a single market capability contract for wizard and Run, with scheduled public data for qualified pairs on Kraken, Coinbase, and Binance and a clearly explained replay path for the other registered venues. Keep unsupported, stale, and mismatched rules actionable before funds can move, even in simulation.
- Qualify additional Kraken and Coinbase pairs separately through public candle, instrument precision, minimum size, fee and quote-currency evidence. Publish only the precise pairs and timeframes covered; avoid exchange-wide availability claims.

## Acceptance

- Fresh-profile browser journeys on both BTC/USD paths with a known candle window produce the correct simulated fill, position, quote cash, and history. The existing SUI/USD path and a saved legacy pack remain usable.
- Replay-only selection is visible before arming and offers replay guidance. Unsupported pairs and stale metadata produce an explicit reason and never invent a fill. The all-venue acceptance matrix continues to pass.
- Cover selected venue precision, lot/minimum refusals and symbols with documented fake transport responses. Keep the venue support table aligned with the build.

During implementation, add the acceptance coverage to the existing test suites and record the actual results in the pull request. The audited baseline is Krellbot `main` at `1a3f4cc`.
