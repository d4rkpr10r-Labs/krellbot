import { describe, expect, it } from "vitest";
import { paperMarketAvailability } from "./marketAvailability";

describe("paper market availability", () => {
  it("identifies the BTC starters and existing SUI starter as scheduled", () => {
    expect(paperMarketAvailability("kraken", "XXBTZUSD", "1h")).toMatch(/scheduled public prices/i);
    expect(paperMarketAvailability("coinbase", "BTC-USD", "1h")).toMatch(/scheduled public prices/i);
    expect(paperMarketAvailability("kraken", "SUIUSD", "1h")).toMatch(/scheduled public prices/i);
    expect(paperMarketAvailability("kraken", "ETHUSD", "1h")).toMatch(/scheduled public prices/i);
    expect(paperMarketAvailability("coinbase", "ETH-USD", "1h")).toMatch(/scheduled public prices/i);
  });
  it("distinguishes replay markets and unverified pairs", () => {
    expect(paperMarketAvailability("okx", "BTC-USDT", "1h")).toMatch(/offline candle replay/i);
    expect(paperMarketAvailability("okx", "DOGE-USDT", "1h")).toMatch(/not been verified/i);
    expect(paperMarketAvailability("coinbase", "SUIUSD", "1h")).toMatch(/not been verified/i);
    expect(paperMarketAvailability("kraken", "XXBTZUSD", "5m")).toMatch(/not been verified/i);
    expect(paperMarketAvailability("kraken", "ETHUSD", "4h")).toMatch(/not been verified/i);
  });
});
