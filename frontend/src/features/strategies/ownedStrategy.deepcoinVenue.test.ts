/**
 * Guided owned-strategy markets admit paper venue id `deepcoin`.
 */
import { describe, expect, it } from "vitest";
import {
  DEEPCOIN_PAPER_WAVE_PAIR,
  VENUES,
  buildOwnedStrategyPack,
  seedOwnedStrategyForm,
  seedOwnedStrategyPack,
} from "./ownedStrategy";

describe("owned strategy deepcoin paper venue", () => {
  it("lists deepcoin among paper venues and accepts BTC-USDT packs", () => {
    expect(VENUES).toContain("deepcoin");
    expect(DEEPCOIN_PAPER_WAVE_PAIR).toBe("BTC-USDT");

    const pack = buildOwnedStrategyPack(
      {
        ...seedOwnedStrategyForm(),
        venue: "deepcoin",
        pair: DEEPCOIN_PAPER_WAVE_PAIR,
        secondPair: "",
      },
      seedOwnedStrategyPack("Deepcoin paper wave"),
    );
    expect(pack?.markets).toEqual([{ venue: "deepcoin", pair: "BTC-USDT" }]);
  });
});
