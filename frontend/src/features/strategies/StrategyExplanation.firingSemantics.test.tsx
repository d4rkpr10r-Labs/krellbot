/**
 * When the entry and exit rules fire, and what the strategy format cannot
 * do, said where the strategy is explained. The notes mirror the engine:
 *
 * - `krellbot.pack.evaluate._eval_leaf`: `crosses_above` is
 *   `l1 <= r1 and l0 > r0` (the previous closed bar against this one),
 *   `crosses_below` is `l1 >= r1 and l0 < r0`, and `>`, `<`, `>=`, `<=`
 *   compare the two sides on the bar being evaluated, so they hold on every
 *   bar where the comparison is true.
 * - `krellbot.pack.evaluate.Target` is long or flat; `run.tick` and
 *   `backtest.engine` enter only from flat, exit the whole owned quantity
 *   and decide only on closed bars. Only the protective stop is triggered by
 *   price during a bar (`Backtester.run`, `PaperVenue._apply_triggered_stops`,
 *   the venue's resting stop order).
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  CLOSED_BARS_NOTE,
  FORMAT_LIMITS_NOTE,
  conditionSentence,
  explainPack,
  operatorFiringNotes,
  operatorTrigger,
} from "./explainPack";
import { StrategyExplanation } from "./StrategyExplanation";

afterEach(cleanup);

const basePack = {
  schema_version: 1,
  id: "firing-semantics",
  version: "1.0.0",
  label: "Firing semantics",
  timeframe: "1h",
  indicators: { sma20: { fn: "sma", src: "close", len: 20 } },
  entry: ["close", "crosses_above", "sma20"],
  exit: ["close", "<", "sma20"],
  risk: { max_account_pct: 25, stop: { type: "pct", pct: 5 } },
  markets: [{ venue: "kraken", pair: "SUIUSD" }],
};

const COMPARISON_NOTE =
  'Comparisons ("is greater than", "is less than", "is at least", "is at most") are true on every closed bar while they hold, not only on the first bar they hold.';
const CROSSES_ABOVE_NOTE =
  '"X crosses above Y" is true only on the closed bar where X moves from at or below Y on the bar before to above Y. It is not true again until X has first gone back to at or below Y.';
const CROSSES_BELOW_NOTE =
  '"X crosses below Y" is true only on the closed bar where X moves from at or above Y on the bar before to below Y. It is not true again until X has first gone back to at or above Y.';

function packWith(entry: unknown, exit: unknown): Record<string, unknown> {
  return { ...basePack, entry, exit };
}

describe("operatorTrigger", () => {
  it("classifies crosses_* as events and comparisons as states", () => {
    expect(operatorTrigger("crosses_above")).toBe("event");
    expect(operatorTrigger("crosses_below")).toBe("event");
    for (const op of [">", "<", ">=", "<="]) {
      expect(operatorTrigger(op)).toBe("state");
    }
  });

  it("does not classify an operator the format does not have", () => {
    for (const op of ["!=", "==", "crosses", "", null, 3]) {
      expect(operatorTrigger(op)).toBeNull();
    }
  });
});

describe("operatorFiringNotes", () => {
  it("says crosses_above is true only on the bar it moves from at-or-below to above", () => {
    const notes = operatorFiringNotes(packWith(["close", "crosses_above", "sma20"], ["close", "crosses_above", 5]));
    expect(notes).toEqual([{ key: "crosses_above", trigger: "event", text: CROSSES_ABOVE_NOTE }]);
  });

  it("says crosses_below is true only on the bar it moves from at-or-above to below", () => {
    const notes = operatorFiringNotes(packWith(["close", ">", 1], ["close", "crosses_below", "sma20"]));
    expect(notes.map((note) => note.key)).toEqual(["comparison", "crosses_below"]);
    expect(notes[1]).toEqual({ key: "crosses_below", trigger: "event", text: CROSSES_BELOW_NOTE });
  });

  it("says comparisons are true on every bar while they hold, once for all four", () => {
    const notes = operatorFiringNotes(
      packWith(
        { all: [["close", ">", "sma20"], ["volume", ">=", 100]] },
        { any: [["close", "<", "sma20"], ["close", "<=", 2]] },
      ),
    );
    expect(notes).toEqual([{ key: "comparison", trigger: "state", text: COMPARISON_NOTE }]);
  });

  it("returns notes only for operators the entry and exit rules use, in a fixed order", () => {
    const notes = operatorFiringNotes(
      packWith(
        { all: [["close", "crosses_below", "sma20"], { any: [["close", "crosses_above", "sma20"]] }] },
        { any: [["close", ">", "sma20"], ["close", "crosses_below", 1]] },
      ),
    );
    expect(notes.map((note) => note.key)).toEqual(["comparison", "crosses_above", "crosses_below"]);
    expect(notes.map((note) => note.text)).toEqual([COMPARISON_NOTE, CROSSES_ABOVE_NOTE, CROSSES_BELOW_NOTE]);
  });

  it("gives no note for an unsupported operator or construct", () => {
    expect(operatorFiringNotes(packWith(["close", "!=", "sma20"], ["close", "=", 1]))).toEqual([]);
    expect(operatorFiringNotes(packWith({ all: [] }, { none: [["close", ">", 1]] }))).toEqual([]);
    // Deeper than the DSL allows: the explanation shows it as unknown, so no note.
    const overDepth = { all: [{ any: [{ all: [{ any: [["close", "crosses_above", 10]] }] }] }] };
    expect(operatorFiringNotes(packWith(overDepth, ["close", "<", "sma20"])).map((n) => n.key)).toEqual([
      "comparison",
    ]);
    for (const pack of [null, [], "pack", {}, { entry: 1 }]) {
      expect(operatorFiringNotes(pack)).toEqual([]);
    }
  });

  it("is deterministic and makes no performance claim", () => {
    const pack = packWith(["close", "crosses_above", "sma20"], { any: [["close", "<", 1], ["close", "crosses_below", "sma20"]] });
    expect(operatorFiringNotes(pack)).toEqual(operatorFiringNotes(pack));
    for (const text of [...operatorFiringNotes(pack).map((n) => n.text), FORMAT_LIMITS_NOTE, CLOSED_BARS_NOTE]) {
      expect(text).not.toMatch(/%\s*(return|profit)|win rate|sharpe|backtest result/i);
    }
  });
});

describe("format limits", () => {
  it("says what the strategy format cannot do", () => {
    expect(FORMAT_LIMITS_NOTE).toBe(
      "This strategy format cannot go short, take partial exits, add to a position, or act inside a bar.",
    );
  });

  it("says the rules act on closed bars and only the stop reacts to price inside a bar", () => {
    expect(CLOSED_BARS_NOTE).toBe(
      "Entry and exit rules are checked only after a bar closes, and any entry or exit order is sent after that check. Only the protective stop can be triggered by price during a bar.",
    );
  });
});

describe("explainPack carries the firing notes", () => {
  it("adds the notes for the pack's operators and leaves the condition sentences unchanged", () => {
    const result = explainPack(JSON.stringify(basePack), "rev-1");
    expect(result.firing).toEqual(operatorFiringNotes(basePack));
    expect(result.firing?.map((n) => n.key)).toEqual(["comparison", "crosses_above"]);
    expect(conditionSentence(result.entry)).toBe('the close price crosses above the indicator "sma20".');
    expect(conditionSentence(result.exit)).toBe('the close price is less than the indicator "sma20".');
  });

  it("has no notes when the bytes do not parse", () => {
    expect(explainPack("{not json", "rev-bad").firing ?? []).toEqual([]);
  });
});

describe("StrategyExplanation firing block", () => {
  it("renders the operator notes and the format limits as a separate block", () => {
    render(<StrategyExplanation bytes={JSON.stringify(basePack)} revisionId="rev-1" />);
    const block = screen.getByTestId("strategy-explanation-firing");
    expect(block.getAttribute("role")).toBe("group");
    expect(within(block).getByRole("heading", { level: 4 }).textContent).toBe("When the rules fire");
    expect(block.getAttribute("aria-labelledby")).toBe(
      within(block).getByRole("heading", { level: 4 }).getAttribute("id"),
    );

    const notes = within(block).getByTestId("strategy-explanation-firing-notes");
    const items = within(notes).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([COMPARISON_NOTE, CROSSES_ABOVE_NOTE]);
    expect(items.map((li) => li.getAttribute("data-trigger"))).toEqual(["state", "event"]);

    const limits = within(block).getByTestId("strategy-explanation-format-limits");
    expect(limits.textContent).toBe(`${FORMAT_LIMITS_NOTE} ${CLOSED_BARS_NOTE}`);

    // Separate from the rule lists: not inside the entry or exit block.
    for (const id of ["strategy-explanation-entry", "strategy-explanation-exit"]) {
      const list = screen.getByTestId(id);
      expect(list.contains(block)).toBe(false);
      expect(block.contains(list)).toBe(false);
      expect(list.textContent).not.toContain("is true only on the closed bar");
      expect(list.textContent).not.toContain("cannot go short");
    }
    // The existing entry text stays as it was.
    expect(screen.getByTestId("strategy-explanation-entry").textContent).toBe(
      'the close price crosses above the indicator "sma20"',
    );
  });

  it("shows only the notes for operators the pack uses", () => {
    render(
      <StrategyExplanation
        bytes={JSON.stringify(packWith(["close", ">", "sma20"], ["close", "crosses_below", "sma20"]))}
        revisionId="rev-2"
      />,
    );
    const notes = screen.getByTestId("strategy-explanation-firing-notes");
    expect(within(notes).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      COMPARISON_NOTE,
      CROSSES_BELOW_NOTE,
    ]);
    expect(notes.textContent).not.toContain("crosses above");
  });

  it("keeps the format limits when no operator can be explained", () => {
    render(
      <StrategyExplanation
        bytes={JSON.stringify(packWith(["close", "!=", "sma20"], ["close", "=", 1]))}
        revisionId="rev-3"
      />,
    );
    const block = screen.getByTestId("strategy-explanation-firing");
    expect(within(block).queryByTestId("strategy-explanation-firing-notes")).toBeNull();
    expect(within(block).getByTestId("strategy-explanation-format-limits").textContent).toContain(
      FORMAT_LIMITS_NOTE,
    );
  });

  it("renders no firing block when the bytes do not parse", () => {
    render(<StrategyExplanation bytes="not json at all" revisionId="rev-bad" />);
    expect(screen.queryByTestId("strategy-explanation-firing")).toBeNull();
  });

  it("makes no return or win-rate claim in the block", () => {
    render(<StrategyExplanation bytes={JSON.stringify(basePack)} revisionId="rev-1" />);
    const text = screen.getByTestId("strategy-explanation-firing").textContent ?? "";
    expect(text).not.toMatch(/%\s*(return|profit)|win rate/i);
  });
});
