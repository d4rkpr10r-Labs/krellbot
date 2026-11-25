/** Known paper markets with scheduled candles or verified offline replay. */
const scheduledMarkets: Record<string, readonly string[]> = {
  kraken: ["XXBTZUSD", "SUIUSD", "ETHUSD"], coinbase: ["BTC-USD", "ETH-USD"], binance: ["BTCUSDT"],
};
const replayMarkets: Record<string, string> = {
  gate: "BTC-USDT", pionex: "BTC-USDT", okx: "BTC-USDT", bybit: "BTCUSDT",
  kucoin: "BTC-USDT", ourbit: "BTCUSDT", toobit: "BTCUSDT", xt: "BTCUSDT",
  mexc: "BTCUSDT", deepcoin: "BTC-USDT", lbank: "BTC-USDT", bitget: "BTCUSDT",
  htx: "BTCUSDT", btcc: "BTCUSDT", cryptocom: "BTC-USDT",
};
export function paperMarketAvailability(venue: string, pair: string, timeframe: string): string {
  if (scheduledMarkets[venue]?.includes(pair) &&
      ((pair === "ETHUSD" || pair === "ETH-USD") ? timeframe === "1h" : ["1h", "4h", "1d"].includes(timeframe))) {
    return "Scheduled public prices are available for this market.";
  }
  if (replayMarkets[venue] === pair) {
    return "This market needs an offline candle replay. Scheduled prices are not available yet.";
  }
  return "This market has not been verified for a scheduled paper run. Check the strategy market and available data.";
}
