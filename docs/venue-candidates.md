# Spot venue candidates

Evaluation for [M27.3](milestones/M27.md). It records what each candidate
offers, observed from its public API on 2026-11-25, and whether that is
enough to ship a paper connector. No connector ships with this evaluation.

The rule from M27.3: a connector needs a supported pair, public market
data, public instrument rules, and a release-fence test. Fees are pinned
from a public source in the connector task itself.

## Where breadth stands

The paper spot path admits 18 venues (`secrets.VENUES`): Kraken, Coinbase,
Binance, Gate, Pionex, OKX, Bybit, KuCoin, Ourbit, Toobit, XT, MEXC,
Deepcoin, LBank, Bitget, HTX, BTCC and Crypto.com. That is past the 15+
breadth target in M27. Only Kraken and Coinbase are qualified for live
(`QUALIFIED_LIVE_VENUES`), and only Kraken, Coinbase, Binance and MEXC read
scheduled public candles; the others use the pinned-candle path.

More venues add less than deeper coverage of the existing ones. The
candidates below are ranked by how little new runtime semantics they need.

## Hyperliquid spot

**Verdict: technically feasible for paper, not ready to ship.** The
blockers are product decisions, not missing data.

### Evidence

Sources: `POST https://api.hyperliquid.xyz/info` with `spotMeta`,
`spotMetaAndAssetCtxs`, `candleSnapshot` and `l2Book`, and the Hyperliquid
docs pages *Tick and lot size*, *Fees*, *Error responses*, *Info endpoint*
and *Nonces and API wallets*.

| Requirement | Finding |
| --- | --- |
| Spot vs perps | Separate metadata. `spotMeta` returns `tokens` and `universe` (spot pairs); perps use `meta`. Spot pair names are `PURR/USDC` (the one canonical pair) or `@<index>`. A connector resolves spot pairs from `spotMeta` only and never reads perp metadata. |
| BTC pair | **No native BTC spot token.** BTC exposure is `UBTC` ("Unit Bitcoin", token 197, `szDecimals` 5), a bridged asset. `UBTC/USDC` is pair `@142` (`isCanonical: false`). `UBTC/USDH` is `@234`. |
| Market data | `candleSnapshot` with `coin: "@142"` returns `{t, T, s, i, o, c, h, l, v, n}` rows. The docs cap history at the most recent 5000 candles; `1h`, `4h` and `1d` are among the supported intervals. No credentials needed. |
| Instrument rules | Size decimals = `szDecimals`. Price: up to 5 significant figures and at most `8 − szDecimals` decimals for spot; integer prices are always allowed. Minimum order value is 10 of the quote token (`MinTradeSpotNtl`). |
| Fees | Spot base tier (14-day volume 0): taker 0.070%, maker 0.040%. A paper pin would be 7 bps. |
| Liquidity (observed) | `@142` 24 h notional volume about $37.6M; top of book 57948 / 57951. |
| Authentication | Exchange actions are signed with an Ethereum private key (the master account or an approved API wallet). Account data is queried by the master address, not the API wallet address. |

### Blockers to decide before a connector task

1. **Wrapped asset.** A `UBTC` position is a claim on the Unit bridge, not
   BTC. Packs, the market picker and the Run status would have to say so.
   The owner decides whether bridged assets are in scope.
2. **Price rule.** Instrument rules today carry a fixed `price_increment`.
   Hyperliquid's tick depends on the price (5 significant figures), so the
   rules model needs a significant-figure rule before stops can be rounded
   correctly. This touches the shared rules contract, not just one venue.
3. **Pair spelling.** `@142` is opaque. A pack would name `UBTC/USDC`, and
   the rules fetch would resolve it through `spotMeta` token names,
   refusing when zero or several pairs match.
4. **Key model (live only).** Credentials are a wallet private key, not an
   API key and secret, and there is no permission query to refuse
   withdraw-capable keys the way M20.2 does. Hyperliquid's docs say API
   wallets are "only used to sign"; whether they can withdraw is not stated
   on the pages read. This does not block paper, but it rules Hyperliquid
   out as a live candidate until a key-scope check exists.

If the owner accepts 1, a paper connector task is: spot-only rules fetch
from `spotMeta` (blocker 3), the significant-figure price rule (blocker 2),
a `candleSnapshot` reader for the scheduled tick, a pinned 7 bps fee, and a
release-fence test. Live stays out of scope (blocker 4).

## Other candidates

Each was checked for a credential-free instrument-rules endpoint and a
credential-free candle endpoint on its BTC pair. All responded with HTTP 200.
Fees are not yet pinned for any of them; that belongs to the connector task.

| Candidate | BTC pair | Rules endpoint | Candles endpoint | New semantics | Notes |
| --- | --- | --- | --- | --- | --- |
| Gemini | `btcusd` | `GET /v1/symbols/details/btcusd` (`tick_size`, `quote_increment`, `min_order_size`) | `GET /v2/candles/btcusd/1hr` | none (USD quote, like Coinbase) | Strongest fit; USD funding already exists. |
| Bitstamp | `btcusd` | `GET /api/v2/markets/` (also lists perpetuals; filter spot) | `GET /api/v2/ohlc/btcusd/?step=3600` | none (USD quote) | Rules response mixes spot and perpetual rows. |
| Bitfinex | `tBTCUSD` | `GET /v2/conf/pub:info:pair` (positional arrays) | `GET /v2/candles/trade:1h:tBTCUSD/hist` | none (USD quote) | Candle rows are `[mts, open, close, high, low, volume]`, a different column order. |
| Binance.US | `BTCUSD` | `GET /api/v3/exchangeInfo?symbol=BTCUSD` | `GET /api/v3/klines` | separate host and symbol set | Regional member of the Binance family. Same API shape, but a different host, account system and pair list, so it needs its own venue id rather than an alias. |
| Bitvavo | `BTC-EUR` | `GET /v2/markets?market=BTC-EUR` | `GET /v2/BTC-EUR/candles?interval=1h` | EUR quote | Needs EUR paper funding, which no venue has today. |
| Upbit | `KRW-BTC` | `GET /v1/market/all` (names only; no precision fields) | `GET /v1/candles/minutes/60?market=KRW-BTC` | KRW quote; rules not public in that endpoint | Fails the instrument-rules requirement as observed. |

Account availability differs by region for several of these venues. The
owner reviews venue availability and policy before any of them is added, as
M27.5 does for live.

## Recommendation

1. Next paper candidate: **Gemini** (`btcusd`). It adds no new semantics.
2. Hyperliquid: owner decision on bridged assets first, then the paper
   connector task described above. Not a live candidate.
3. Bitstamp and Bitfinex after Gemini. Binance.US as its own venue id if US
   coverage matters. Bitvavo only with an EUR funding decision. Upbit not
   until instrument rules are public.
