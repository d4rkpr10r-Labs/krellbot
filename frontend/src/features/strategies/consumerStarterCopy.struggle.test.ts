import { cleanup, render, screen, within } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { ModePackPicker } from "../studio/ModePackPicker";
import type { ModePackCatalogue, ModePackRow } from "../studio/modePackClient";
import {
  CONSUMER_STARTER_CARD_ORDER,
  STRUGGLE_COPY,
  STRUGGLE_COPY_ORDER,
  wordCount,
} from "./consumerStarterCopy";

/**
 * When each starter and built-in mode tends to struggle: plain, number-free
 * copy about market conditions, never a performance figure. The picker test
 * uses an in-memory catalogue instead of the loopback modes route.
 */

afterEach(cleanup);

const MODE_IDS = ["mode-dca", "mode-grid", "mode-rebalance"];

function modeRow(id: string, label: string): ModePackRow {
  return {
    id,
    label,
    description: `Built-in ${label} trading mode. no profit or return claim.`,
    pack: { id, timeframe: "1d", mode: { kind: id.replace("mode-", "") } },
  };
}

const catalogue: ModePackCatalogue = {
  schema_version: "1",
  modes: [modeRow("mode-dca", "DCA"), modeRow("mode-grid", "Grid"), modeRow("mode-rebalance", "Rebalance")],
};

describe("struggle copy coverage", () => {
  it("covers the three starters and the three built-in modes, and nothing else", () => {
    expect(STRUGGLE_COPY_ORDER).toEqual([...CONSUMER_STARTER_CARD_ORDER, ...MODE_IDS]);
    expect(Object.keys(STRUGGLE_COPY).sort()).toEqual([...STRUGGLE_COPY_ORDER].sort());
    for (const id of STRUGGLE_COPY_ORDER) {
      const entry = STRUGGLE_COPY[id]!;
      expect(entry.id).toBe(id);
      expect(entry.title.trim().length).toBeGreaterThan(0);
      expect(entry.struggle.trim().length).toBeGreaterThan(0);
    }
  });
});

describe("struggle copy stays number-free and makes no performance claim", () => {
  it.each(["starter-dual-ma-crossover", "starter-mean-reversion-band", "starter-range-breakout", ...MODE_IDS])(
    "%s",
    (id) => {
      const line = STRUGGLE_COPY[id]!.struggle;
      expect(line).not.toMatch(/\d/);
      expect(line).not.toMatch(/%/);
      expect(line).not.toMatch(
        /\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|hundred|once|twice|half|double)\b/i,
      );
      expect(line).not.toMatch(/return|profit|win rate|sharpe|guarantee|backtest result|outperform/i);
      expect(wordCount(line)).toBeLessThanOrEqual(25);
    },
  );

  it("does not say DCA adds a slice each interval", () => {
    const line = STRUGGLE_COPY["mode-dca"]!.struggle;
    expect(line).not.toMatch(/slice|each (bar|interval|day)|every (bar|interval|day)|per interval|regular|repeated/i);
    expect(line).toMatch(/hold/i);
  });
});

describe("ModePackPicker shows the struggle copy", () => {
  it("shows each listed mode's line and every starter's line", async () => {
    render(createElement(ModePackPicker, { onPick: () => {}, loadModes: () => Promise.resolve(catalogue) }));
    const block = await screen.findByTestId("studio-struggle-copy");
    for (const id of STRUGGLE_COPY_ORDER) {
      const entry = STRUGGLE_COPY[id]!;
      const item = within(block).getByTestId(`studio-struggle-${id}`);
      expect(item.textContent).toContain(entry.title);
      expect(item.textContent).toContain(entry.struggle);
    }
  });

  it("never shows a line for a mode the catalogue did not list", async () => {
    render(
      createElement(ModePackPicker, {
        onPick: () => {},
        loadModes: () => Promise.resolve({ schema_version: "1", modes: [modeRow("mode-dca", "DCA")] }),
      }),
    );
    const block = await screen.findByTestId("studio-struggle-copy");
    expect(within(block).getByTestId("studio-struggle-mode-dca")).toBeDefined();
    expect(within(block).queryByTestId("studio-struggle-mode-grid")).toBeNull();
    expect(within(block).queryByTestId("studio-struggle-mode-rebalance")).toBeNull();
    for (const id of CONSUMER_STARTER_CARD_ORDER) {
      expect(within(block).getByTestId(`studio-struggle-${id}`)).toBeDefined();
    }
  });
});
