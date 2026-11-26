import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ResearchView, type ResearchClient, type StoredResult } from "./ResearchView";

/**
 * Where a strategy lost: the per-year, exposure, CAGR and buy-and-hold
 * figures every receipt already carries, plus the round-trip sample size.
 *
 * Transport note: in-memory vi.fn() clients stand in for the loopback
 * research API. Every number below is a synthetic backtest fixture, never
 * market performance.
 */

afterEach(cleanup);

const HONESTY = "Backtest / pinned fixture — not live market performance.";

const metrics = {
  total_return_pct: 4.25,
  cagr_pct: 1.75,
  max_drawdown_pct: 12.5,
  return_to_dd: 0.14,
  per_year: { "2023": 9.5, "2022": -6.5, "2024": 1.0 },
  trade_count: 3,
  exposure_pct: 41.5,
  buy_and_hold: {
    total_return_pct: 20.0,
    cagr_pct: 6.25,
    max_drawdown_pct: 35.0,
    return_to_dd: 0.18,
    per_year: { "2022": -30.0, "2023": 45.0, "2024": 12.0 },
    trade_count: 0,
    exposure_pct: 99.9,
  },
};

function receiptWith(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    engine_version: "0.9.5",
    pack_sha256: "abc",
    data_manifest_sha256: "feedface",
    venue: "kraken",
    pair: "XXBTZUSD",
    tf: "1d",
    from: "2022-01-01",
    to: "2024-12-31",
    fee_bps: 10,
    slippage_bps: 5,
    slippage_mult: 1.0,
    metrics,
    equity_curve: [0, 4.25],
    ...overrides,
  };
}

function makeClient(receipt: Record<string, unknown>): ResearchClient {
  const stored: StoredResult = { legacy_receipt: receipt, trace: [] };
  return {
    submitRun: vi.fn().mockResolvedValue({ id: "job-years", state: "running" }),
    cancelJob: vi.fn().mockResolvedValue(undefined),
    getResult: vi.fn().mockResolvedValue(stored),
    getJob: vi.fn().mockResolvedValue({ id: "job-years", state: "succeeded" }),
    getResultDownload: vi.fn().mockResolvedValue(new Response("{}")),
  };
}

async function showResult(receipt: Record<string, unknown>): Promise<HTMLElement> {
  render(<ResearchView client={makeClient(receipt)} />);
  fireEvent.change(screen.getByLabelText(/dataset path/i), {
    target: { value: "fixtures/synthetic.csv" },
  });
  fireEvent.change(screen.getByLabelText(/fee basis points/i), {
    target: { value: "10" },
  });
  fireEvent.change(screen.getByLabelText(/^from$/i), { target: { value: "0" } });
  fireEvent.change(screen.getByLabelText(/^to$/i), { target: { value: "1000" } });
  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
  return await screen.findByTestId("research-result");
}

function text(testId: string): string {
  return screen.getByTestId(testId).textContent ?? "";
}

describe("Research per-year results", () => {
  it("lists each calendar year for the strategy beside buy and hold, oldest first", async () => {
    await showResult(receiptWith());
    const table = screen.getByTestId("research-per-year");
    const years = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getByRole("rowheader").textContent);
    expect(years).toEqual(["2022", "2023", "2024"]);

    expect(text("research-per-year-2022-strategy")).toBe("-6.50");
    expect(text("research-per-year-2022-buy-and-hold")).toBe("-30.00");
    expect(text("research-per-year-2023-strategy")).toBe("9.50");
    expect(text("research-per-year-2023-buy-and-hold")).toBe("45.00");
    expect(text("research-per-year-2024-strategy")).toBe("1.00");
    expect(text("research-per-year-2024-buy-and-hold")).toBe("12.00");
    expect(within(table).getByRole("columnheader", { name: /strategy/i })).toBeDefined();
    expect(within(table).getByRole("columnheader", { name: /buy and hold/i })).toBeDefined();
  });

  it("says unavailable for a year one series lacks instead of a zero", async () => {
    await showResult(
      receiptWith({
        metrics: {
          ...metrics,
          per_year: { "2022": -6.5, "2023": 9.5 },
          buy_and_hold: { ...metrics.buy_and_hold, per_year: { "2023": 45.0, "2024": "bad" } },
        },
      }),
    );
    expect(text("research-per-year-2022-buy-and-hold")).toBe("unavailable");
    expect(text("research-per-year-2024-strategy")).toBe("unavailable");
    expect(text("research-per-year-2024-buy-and-hold")).toBe("unavailable");
    expect(text("research-per-year-2023-strategy")).toBe("9.50");
  });

  it("shows per-year results as unavailable when the receipt carries none", async () => {
    await showResult(receiptWith({ metrics: { total_return_pct: 1.0, trade_count: 0 } }));
    const panel = screen.getByTestId("research-per-year");
    expect(within(panel).queryByRole("table")).toBeNull();
    expect(text("research-per-year-unavailable")).toMatch(/unavailable/i);
  });
});

describe("Research CAGR, exposure and buy-and-hold cells", () => {
  it("shows each figure from the receipt with the field it reads", async () => {
    const result = await showResult(receiptWith());
    expect(text("research-result-cagr-pct")).toBe("1.75");
    expect(text("research-result-exposure-pct")).toBe("41.5");
    expect(text("research-result-buy-and-hold-return-pct")).toBe("20");
    expect(text("research-result-buy-and-hold-cagr-pct")).toBe("6.25");
    const body = result.textContent ?? "";
    expect(body).toContain("metrics.cagr_pct");
    expect(body).toContain("metrics.exposure_pct");
    expect(body).toContain("metrics.buy_and_hold.total_return_pct");
    expect(body).toContain("metrics.buy_and_hold.cagr_pct");
  });

  it("says unavailable for a null CAGR instead of a fabricated zero", async () => {
    await showResult(
      receiptWith({
        metrics: {
          ...metrics,
          cagr_pct: null,
          buy_and_hold: { ...metrics.buy_and_hold, cagr_pct: null },
        },
      }),
    );
    expect(text("research-result-cagr-pct")).toBe("unavailable");
    expect(text("research-result-buy-and-hold-cagr-pct")).toBe("unavailable");
  });

  it("keeps every new cell unavailable when the receipt has no metrics", async () => {
    await showResult({ fee_bps: 10 });
    for (const id of [
      "research-result-cagr-pct",
      "research-result-exposure-pct",
      "research-result-buy-and-hold-return-pct",
      "research-result-buy-and-hold-cagr-pct",
    ]) {
      expect(text(id)).toBe("unavailable");
    }
  });
});

describe("Research round-trip sample line", () => {
  it("states the round trips and window and that no confidence is computed", async () => {
    await showResult(receiptWith());
    expect(text("research-result-sample")).toBe(
      "3 round trips, 2022-01-01 to 2024-12-31; no statistical confidence computed",
    );
  });

  it("uses the singular for one round trip", async () => {
    await showResult(receiptWith({ metrics: { ...metrics, trade_count: 1 } }));
    expect(text("research-result-sample")).toBe(
      "1 round trip, 2022-01-01 to 2024-12-31; no statistical confidence computed",
    );
  });

  it("never counts fill legs as round trips", async () => {
    await showResult({
      fee_bps: 10,
      trades: [{ side: "buy" }, { side: "sell" }, { side: "buy" }],
    });
    expect(text("research-result-sample")).toBe(
      "Round trips unavailable, unavailable to unavailable; no statistical confidence computed",
    );
  });
});

describe("Research result honesty", () => {
  it("renders the new figures under the backtest honesty label", async () => {
    const result = await showResult(receiptWith());
    const honesty = within(result).getByTestId("research-result-honesty");
    expect(honesty.textContent).toBe(HONESTY);
    for (const id of ["research-per-year", "research-result-sample", "research-result-cagr-pct"]) {
      const node = within(result).getByTestId(id);
      expect(honesty.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(result.textContent ?? "").not.toMatch(/win rate|confidence interval|\$/i);
  });
});
