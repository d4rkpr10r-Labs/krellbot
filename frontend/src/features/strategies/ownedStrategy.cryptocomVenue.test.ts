/**
 * Guided owned-strategy markets admit paper venue id `cryptocom`.
 */
import { describe, expect, it } from "vitest";
import {
  CRYPTOCOM_PAPER_WAVE_PAIR,
  VENUES,
  buildOwnedStrategyPack,
  seedOwnedStrategyForm,
  seedOwnedStrategyPack,
} from "./ownedStrategy";

describe("owned strategy cryptocom paper venue", () => {
  it("lists cryptocom among paper venues and accepts BTC-USDT packs", () => {
    expect(VENUES).toContain("cryptocom");
    expect(CRYPTOCOM_PAPER_WAVE_PAIR).toBe("BTC-USDT");

    const pack = buildOwnedStrategyPack(
      {
        ...seedOwnedStrategyForm(),
        venue: "cryptocom",
        pair: CRYPTOCOM_PAPER_WAVE_PAIR,
        secondPair: "",
      },
      seedOwnedStrategyPack("Crypto.com paper wave"),
    );
    expect(pack?.markets).toEqual([{ venue: "cryptocom", pair: "BTC-USDT" }]);
  });
});
