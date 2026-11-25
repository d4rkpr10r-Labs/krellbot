import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PaperClient } from "./client";
import { PaperHealthDetails, paperHealthSentence, usePaperHealth } from "./paperHealth";

describe("paper run health", () => {
  it("distinguishes waiting, trades, stale data, and feed errors", () => {
    expect(paperHealthSentence({ armed: true, state: "waiting" })).toMatch(/first market check/i);
    expect(paperHealthSentence({ armed: true, state: "checked" })).toMatch(/market checked/i);
    expect(paperHealthSentence({ armed: true, state: "filled" })).toMatch(/simulated trade/i);
    expect(paperHealthSentence({ armed: true, state: "stale_candles" })).toMatch(/out of date/i);
    expect(paperHealthSentence({ armed: true, state: "feed_error" })).toMatch(/could not be fetched/i);
    expect(paperHealthSentence({ armed: true, state: "blocked" })).toMatch(/needs attention/i);
  });

  it("offers scheduler recovery while a run waits", () => {
    render(<PaperHealthDetails health={{ armed: true, state: "waiting", scheduler_installed: false }} />);
    expect(screen.getByRole("link", { name: "Review automatic checks" }).getAttribute("href")).toBe("/?view=operations");
  });

  it("discards a delayed result for the previous market", async () => {
    let resolveFirst!: (value: { armed: boolean; state: string }) => void;
    const first = new Promise<{ armed: boolean; state: string }>(resolve => { resolveFirst = resolve; });
    const client = {
      getHealth: vi.fn().mockReturnValueOnce(first).mockResolvedValueOnce({ armed: true, state: "waiting" }),
    } as unknown as PaperClient;
    function View({ pair }: { pair: string }) {
      const health = usePaperHealth(client, { venue: "kraken", pair });
      return <p>{paperHealthSentence(health)}</p>;
    }
    const { rerender } = render(<View pair="BTCUSD" />);
    rerender(<View pair="SUIUSD" />);
    await screen.findByText("Waiting for the first market check.");
    resolveFirst({ armed: true, state: "filled" });
    await waitFor(() => expect(screen.queryByText(/simulated trade was made/)).toBeNull());
    expect(screen.getByText("Waiting for the first market check.")).toBeTruthy();
  });
});
