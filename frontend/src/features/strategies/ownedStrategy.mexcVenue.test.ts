/**
 * Guided owned-strategy markets admit paper venue id `mexc`.
 */
import { describe, expect, it } from "vitest";
import {
  MEXC_PAPER_WAVE_PAIR,
  VENUES,
  buildOwnedStrategyPack,
  seedOwnedStrategyForm,
  seedOwnedStrategyPack,
} from "./ownedStrategy";

describe("owned strategy mexc paper venue", () => {
  it("lists mexc among paper venues and accepts BTCUSDT packs", () => {
    expect(VENUES).toContain("mexc");
    expect(MEXC_PAPER_WAVE_PAIR).toBe("BTCUSDT");

    const pack = buildOwnedStrategyPack(
      {
        ...seedOwnedStrategyForm(),
        venue: "mexc",
        pair: MEXC_PAPER_WAVE_PAIR,
        secondPair: "",
      },
      seedOwnedStrategyPack("Mexc paper wave"),
    );
    expect(pack?.markets).toEqual([{ venue: "mexc", pair: "BTCUSDT" }]);
  });
});
