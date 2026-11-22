/**
 * Guided owned-strategy markets admit paper venue id `htx`.
 */
import { describe, expect, it } from "vitest";
import { HTX_PAPER_WAVE_PAIR, VENUES, buildOwnedStrategyPack, seedOwnedStrategyForm, seedOwnedStrategyPack } from "./ownedStrategy";

describe("owned strategy htx paper venue", () => {
  it("lists htx among paper venues and accepts BTCUSDT packs", () => {
    expect(VENUES).toContain("htx");
    expect(HTX_PAPER_WAVE_PAIR).toBe("BTCUSDT");

    const pack = buildOwnedStrategyPack(
      {
        ...seedOwnedStrategyForm(),
        venue: "htx",
        pair: HTX_PAPER_WAVE_PAIR,
        secondPair: "",
      },
      seedOwnedStrategyPack("HTX paper wave"),
    );
    expect(pack?.markets).toEqual([{ venue: "htx", pair: "BTCUSDT" }]);
  });
});
