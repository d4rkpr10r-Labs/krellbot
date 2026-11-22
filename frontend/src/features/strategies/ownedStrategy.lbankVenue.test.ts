/**
 * Guided owned-strategy markets admit paper venue id `lbank`.
 */
import { describe, expect, it } from "vitest";
import {
  LBANK_PAPER_WAVE_PAIR,
  VENUES,
  buildOwnedStrategyPack,
  seedOwnedStrategyForm,
  seedOwnedStrategyPack,
} from "./ownedStrategy";

describe("owned strategy lbank paper venue", () => {
  it("lists lbank among paper venues and accepts BTC-USDT packs", () => {
    expect(VENUES).toContain("lbank");
    expect(LBANK_PAPER_WAVE_PAIR).toBe("BTC-USDT");

    const pack = buildOwnedStrategyPack(
      {
        ...seedOwnedStrategyForm(),
        venue: "lbank",
        pair: LBANK_PAPER_WAVE_PAIR,
        secondPair: "",
      },
      seedOwnedStrategyPack("LBank paper wave"),
    );
    expect(pack?.markets).toEqual([{ venue: "lbank", pair: "BTC-USDT" }]);
  });
});
