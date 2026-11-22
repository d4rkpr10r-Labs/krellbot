/**
 * Guided owned-strategy markets admit paper venue id `xt`.
 */
import { describe, expect, it } from "vitest";
import {
  VENUES,
  XT_PAPER_WAVE_PAIR,
  buildOwnedStrategyPack,
  seedOwnedStrategyForm,
  seedOwnedStrategyPack,
} from "./ownedStrategy";

describe("owned strategy xt paper venue", () => {
  it("lists xt among paper venues and accepts BTCUSDT packs", () => {
    expect(VENUES).toContain("xt");
    expect(XT_PAPER_WAVE_PAIR).toBe("BTCUSDT");

    const pack = buildOwnedStrategyPack(
      {
        ...seedOwnedStrategyForm(),
        venue: "xt",
        pair: XT_PAPER_WAVE_PAIR,
        secondPair: "",
      },
      seedOwnedStrategyPack("XT paper wave"),
    );
    expect(pack?.markets).toEqual([{ venue: "xt", pair: "BTCUSDT" }]);
  });
});
