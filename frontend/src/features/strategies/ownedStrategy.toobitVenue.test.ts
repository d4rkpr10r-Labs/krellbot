/**
 * Guided owned-strategy markets admit paper venue id `toobit`.
 */
import { describe, expect, it } from "vitest";
import {
  TOOBIT_PAPER_WAVE_PAIR,
  VENUES,
  buildOwnedStrategyPack,
  seedOwnedStrategyForm,
  seedOwnedStrategyPack,
} from "./ownedStrategy";

describe("owned strategy toobit paper venue", () => {
  it("lists toobit among paper venues and accepts BTCUSDT packs", () => {
    expect(VENUES).toContain("toobit");
    expect(TOOBIT_PAPER_WAVE_PAIR).toBe("BTCUSDT");

    const pack = buildOwnedStrategyPack(
      {
        ...seedOwnedStrategyForm(),
        venue: "toobit",
        pair: TOOBIT_PAPER_WAVE_PAIR,
        secondPair: "",
      },
      seedOwnedStrategyPack("Toobit paper wave"),
    );
    expect(pack?.markets).toEqual([{ venue: "toobit", pair: "BTCUSDT" }]);
  });
});
