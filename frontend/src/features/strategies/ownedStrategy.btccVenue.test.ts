/**
 * Guided owned-strategy markets admit paper venue id `btcc`.
 */
import { describe, expect, it } from "vitest";
import {
  BTCC_PAPER_WAVE_PAIR,
  VENUES,
  buildOwnedStrategyPack,
  seedOwnedStrategyForm,
  seedOwnedStrategyPack,
} from "./ownedStrategy";

describe("owned strategy btcc paper venue", () => {
  it("lists btcc among paper venues and accepts BTCUSDT packs", () => {
    expect(VENUES).toContain("btcc");
    expect(BTCC_PAPER_WAVE_PAIR).toBe("BTCUSDT");

    const pack = buildOwnedStrategyPack(
      {
        ...seedOwnedStrategyForm(),
        venue: "btcc",
        pair: BTCC_PAPER_WAVE_PAIR,
        secondPair: "",
      },
      seedOwnedStrategyPack("BTCC paper wave"),
    );
    expect(pack?.markets).toEqual([{ venue: "btcc", pair: "BTCUSDT" }]);
  });
});
