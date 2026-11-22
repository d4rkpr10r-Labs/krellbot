/**
 * Guided owned-strategy markets admit paper venue id `ourbit`.
 */
import { describe, expect, it } from "vitest";
import {
  OURBIT_PAPER_WAVE_PAIR,
  VENUES,
  buildOwnedStrategyPack,
  seedOwnedStrategyForm,
  seedOwnedStrategyPack,
} from "./ownedStrategy";

describe("owned strategy ourbit paper venue", () => {
  it("lists ourbit among paper venues and accepts BTCUSDT packs", () => {
    expect(VENUES).toContain("ourbit");
    expect(OURBIT_PAPER_WAVE_PAIR).toBe("BTCUSDT");

    const pack = buildOwnedStrategyPack(
      {
        ...seedOwnedStrategyForm(),
        venue: "ourbit",
        pair: OURBIT_PAPER_WAVE_PAIR,
        secondPair: "",
      },
      seedOwnedStrategyPack("Ourbit paper wave"),
    );
    expect(pack?.markets).toEqual([{ venue: "ourbit", pair: "BTCUSDT" }]);
  });
});
