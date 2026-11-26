/**
 * What the protective stop does in the engine today, said where the strategy
 * is explained. The sentences mirror `krellbot.pack.evaluate._decide` (stop
 * from the signal bar's close), the raise branch of `krellbot.run.tick`
 * (raise only while the entry condition holds, never lower) and
 * `krellbot.run._stop_price` (a missing stop is re-placed from the latest
 * closed bar's close; every non-percentage stop at half that close).
 */
import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { explainPack } from "./explainPack";
import { StrategyExplanation } from "./StrategyExplanation";

afterEach(cleanup);

const basePack = {
  schema_version: 1,
  id: "stop-semantics",
  version: "1.0.0",
  label: "Stop semantics",
  timeframe: "1h",
  indicators: { sma20: { fn: "sma", src: "close", len: 20 } },
  entry: ["close", ">", "sma20"],
  exit: ["close", "<", "sma20"],
  markets: [{ venue: "kraken", pair: "SUIUSD" }],
};

function packWithStop(stop: unknown): string {
  return JSON.stringify({ ...basePack, risk: { max_account_pct: 25, stop } });
}

const pctBytes = packWithStop({ type: "pct", pct: 5 });
const atrBytes = packWithStop({ type: "atr", len: 14, mult: 2 });

describe("explainPack stop semantics", () => {
  it("says a percentage stop is set from the signal bar's close, not the fill", () => {
    const { stop } = explainPack(pctBytes, "rev-1");
    expect(stop.supported).toBe(true);
    const lines = stop.semantics ?? [];
    expect(lines).toHaveLength(3);
    expect(lines[0]).toBe(
      "The engine sets this stop from the signal bar's close, 5% below it. The signal bar is the closed bar on which the entry condition fired; the fill price is not used.",
    );
  });

  it("says the stop is only raised, and only while the entry condition holds", () => {
    for (const bytes of [pctBytes, atrBytes]) {
      const lines = explainPack(bytes, "rev-1").stop.semantics ?? [];
      expect(lines[1]).toBe(
        "After entry it is only raised, never lowered: on each later closed bar where the entry condition still holds, it is recomputed from that bar's close and moves up if the new price is higher. Once the entry condition stops holding, the stop stays where it is.",
      );
    }
  });

  it("says a missing percentage stop is re-placed from the latest closed bar's close", () => {
    const lines = explainPack(pctBytes, "rev-1").stop.semantics ?? [];
    expect(lines[2]).toBe(
      "If the position has no resting stop, the engine re-places one 5% below the latest closed bar's close.",
    );
  });

  it("says a missing ATR stop is re-placed at half the close, not with its ATR rule", () => {
    const { stop } = explainPack(atrBytes, "rev-2");
    expect(stop.supported).toBe(true);
    const lines = stop.semantics ?? [];
    expect(lines).toHaveLength(3);
    expect(lines[0]).toBe(
      "The engine sets this stop from the signal bar's close, 2 × that bar's 14-bar average true range below it. The signal bar is the closed bar on which the entry condition fired; the fill price is not used.",
    );
    expect(lines[2]).toBe(
      "If the position has no resting stop, the engine re-places one at half the latest closed bar's close (50% below it), not with this ATR rule.",
    );
  });

  it("keeps the existing stop sentence unchanged beside the semantics", () => {
    expect(explainPack(pctBytes, "rev-1").stop.text).toBe(
      "a percentage stop 5% below the entry price",
    );
    expect(explainPack(atrBytes, "rev-2").stop.text).toBe(
      "an ATR stop 2 × the 14-bar average true range below the entry price",
    );
  });

  it("states no semantics for a stop it cannot read", () => {
    for (const stop of [{ type: "weird" }, { type: "pct", pct: 0 }, { type: "atr", len: 1, mult: 2 }, null]) {
      const result = explainPack(packWithStop(stop), "rev-3");
      expect(result.stop.supported).toBe(false);
      expect(result.stop.semantics ?? []).toEqual([]);
    }
    const noRisk = explainPack(JSON.stringify(basePack), "rev-4");
    expect(noRisk.stop.semantics ?? []).toEqual([]);
  });

  it("makes no performance claim and is deterministic", () => {
    for (const bytes of [pctBytes, atrBytes]) {
      for (const line of explainPack(bytes, "rev-1").stop.semantics ?? []) {
        expect(line).not.toMatch(/return|profit|win rate|sharpe|backtest result/i);
      }
      expect(explainPack(bytes, "rev-1")).toEqual(explainPack(bytes, "rev-1"));
    }
  });

  it("renders the semantics beside the stop sentence in the explanation", () => {
    render(createElement(StrategyExplanation, { bytes: atrBytes, revisionId: "rev-2" }));
    const sentence = screen.getByTestId("strategy-explanation-stop");
    const semantics = screen.getByTestId("strategy-explanation-stop-semantics");
    expect(sentence.textContent).toBe(
      "an ATR stop 2 × the 14-bar average true range below the entry price",
    );
    expect(semantics.parentElement).toBe(sentence.parentElement);
    const text = semantics.textContent ?? "";
    expect(text).toContain("from the signal bar's close");
    expect(text).toContain("only raised, never lowered");
    expect(text).toContain("half the latest closed bar's close");
  });

  it("renders no semantics when the stop is unsupported", () => {
    render(
      createElement(StrategyExplanation, {
        bytes: packWithStop({ type: "weird" }),
        revisionId: "rev-3",
      }),
    );
    expect(screen.getByTestId("strategy-explanation-stop")).toBeTruthy();
    expect(screen.queryByTestId("strategy-explanation-stop-semantics")).toBeNull();
  });
});
