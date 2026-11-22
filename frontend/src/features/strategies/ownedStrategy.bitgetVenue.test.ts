/**
 * Guided owned-strategy markets admit paper venue id `bitget`.
 */
import { describe, expect, it } from "vitest";
import {
  BITGET_PAPER_WAVE_PAIR,
  VENUES,
  buildOwnedStrategyPack,
  seedOwnedStrategyForm,
  seedOwnedStrategyPack,
} from "./ownedStrategy";

describe("owned strategy bitget paper venue", () => {
  it("lists bitget among paper venues and accepts BTCUSDT packs", () => {
    expect(VENUES).toContain("bitget");
    expect(BITGET_PAPER_WAVE_PAIR).toBe("BTCUSDT");

    const pack = buildOwnedStrategyPack(
      {
        ...seedOwnedStrategyForm(),
        venue: "bitget",
        pair: BITGET_PAPER_WAVE_PAIR,
        secondPair: "",
      },
      seedOwnedStrategyPack("Bitget paper wave"),
    );
    expect(pack?.markets).toEqual([{ venue: "bitget", pair: "BTCUSDT" }]);
  });
});
