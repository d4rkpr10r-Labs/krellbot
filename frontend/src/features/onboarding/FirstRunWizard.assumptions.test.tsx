/**
 * The first-run status step states what the paper run assumes (F11).
 *
 * Every value on the card is the one the backend serves from the paper
 * engine for the chosen venue and pair: the taker fee with the research
 * fee-model label, the slippage, the decision timeframe, the stop rule and
 * the pretend balance. A value the backend does not give reads
 * 'unavailable'. The card never shows a return or a performance figure.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { PaperClient, PaperStatus } from "../paper/client";
import {
  FirstRunWizard,
  writeFirstRunWizardState,
  type PaperAssumptions,
} from "./FirstRunWizard";
import { FIRST_RUN_WIZARD_STORAGE_KEY } from "./firstRunWizardPersistence";

const STORAGE = FIRST_RUN_WIZARD_STORAGE_KEY;
const ROWS = ["fee", "slippage", "decisions", "stop", "balance"] as const;

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

beforeAll(() => {
  if (typeof globalThis.ResizeObserver !== "function") {
    globalThis.ResizeObserver =
      ResizeObserverStub as unknown as typeof ResizeObserver;
  }
});

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function onStatus(marketVenue: "kraken" | "coinbase" = "kraken"): void {
  writeFirstRunWizardState(
    {
      status: "active",
      stepId: "status",
      selectedStarterId: "starter-dual-ma-crossover",
      revisionId: "rev-starter-1",
      marketVenue,
    },
    STORAGE,
  );
}

function paperStatus(overrides: Partial<PaperStatus> = {}): PaperStatus {
  return {
    schema_version: "1",
    armed: true,
    mode: "paper",
    entries_paused: false,
    venue: "kraken",
    pair: "XXBTZUSD",
    pack_id: "starter-dual-ma-crossover",
    ...overrides,
  };
}

function fakePaper(): PaperClient {
  return {
    getStatus: vi.fn().mockResolvedValue(paperStatus()),
    pauseEntries: vi.fn(),
    resumeEntries: vi.fn(),
    disarm: vi.fn(),
    arm: vi.fn(),
  } as PaperClient;
}

function engineAssumptions(over: Partial<PaperAssumptions> = {}): PaperAssumptions {
  return {
    fee_model: "taker, per side",
    fee_bps: 40,
    slippage_bps: 5,
    timeframe: "1h",
    stop: { type: "pct", pct: "5" },
    starting_cash: "1000",
    quote: "USD",
    ...over,
  };
}

function nothingServed(): PaperAssumptions {
  return {
    fee_model: null,
    fee_bps: null,
    slippage_bps: null,
    timeframe: null,
    stop: null,
    starting_cash: null,
    quote: null,
  };
}

function row(id: (typeof ROWS)[number]): string {
  return (
    within(screen.getByTestId(`first-run-wizard-assumption-${id}`)).getByTestId(
      `first-run-wizard-assumption-${id}-value`,
    ).textContent ?? ""
  );
}

async function settledRows(): Promise<void> {
  await waitFor(() => {
    for (const id of ROWS) {
      expect(row(id)).not.toBe("Checking…");
    }
  });
}

function cardText(): string {
  return screen.getByTestId("first-run-wizard-assumptions").textContent ?? "";
}

function expectNoPerformanceClaim(text: string): void {
  expect(text).not.toMatch(/P&L|profit|% return|returned|win rate|\$/i);
  expect(text.includes("XXBTZUSD")).toBe(false);
}

describe("FirstRunWizard status step states what the paper run assumes", () => {
  it("shows fee and slippage with the research fee label, hourly closed-candle decisions, the stop and the pretend balance", async () => {
    onStatus("kraken");
    const load = vi.fn().mockResolvedValue(engineAssumptions());
    render(
      <FirstRunWizard paperClient={fakePaper()} loadPaperAssumptions={load} />,
    );

    await settledRows();
    expect(load).toHaveBeenCalledWith({ venue: "kraken", pair: "XXBTZUSD" });
    expect(row("fee")).toBe("40 bps (0.40%) · taker, per side");
    expect(row("slippage")).toBe("5 bps (0.05%) against each simulated fill");
    expect(row("decisions")).toBe("Hourly, on closed candles only");
    expect(row("stop")).toBe(
      "5% below the close of the candle that signals the buy, then only raised, never lowered",
    );
    expect(row("balance")).toBe("1,000 USD");

    expect(
      screen.getByTestId("first-run-wizard-assumptions-honesty").textContent,
    ).toMatch(/paper results are not evidence of live returns/i);
    const next = within(screen.getByTestId("first-run-wizard-next-actions"));
    expect(next.getAllByRole("listitem").length).toBeGreaterThanOrEqual(2);
    expectNoPerformanceClaim(cardText());
    expect(screen.getByTestId("first-run-wizard-primary").textContent).toBe(
      "Looks good",
    );
  });

  it("quotes what the backend serves for the chosen venue and pair, not fixed copy", async () => {
    onStatus("coinbase");
    const load = vi.fn().mockResolvedValue(
      engineAssumptions({
        fee_bps: 37,
        slippage_bps: 9,
        timeframe: "4h",
        stop: { type: "atr", len: 21, mult: "1.75" },
        starting_cash: "4321.5",
        quote: "USDT",
      }),
    );
    render(
      <FirstRunWizard paperClient={fakePaper()} loadPaperAssumptions={load} />,
    );

    await settledRows();
    expect(load).toHaveBeenCalledWith({ venue: "coinbase", pair: "BTC-USD" });
    expect(row("fee")).toBe("37 bps (0.37%) · taker, per side");
    expect(row("slippage")).toBe("9 bps (0.09%) against each simulated fill");
    expect(row("decisions")).toBe("Every 4 hours, on closed candles only");
    expect(row("stop")).toBe(
      "1.75 × the 21-candle average true range below the close of the candle that signals the buy, then only raised, never lowered",
    );
    expect(row("balance")).toBe("4,321.5 USDT");
  });

  it("shows unavailable for each value the backend does not give", async () => {
    onStatus("kraken");
    const load = vi.fn().mockResolvedValue(nothingServed());
    render(
      <FirstRunWizard paperClient={fakePaper()} loadPaperAssumptions={load} />,
    );

    await settledRows();
    for (const id of ROWS) {
      expect(row(id)).toBe("unavailable");
    }
    expect(
      screen.getByTestId("first-run-wizard-assumptions-honesty").textContent,
    ).toMatch(/paper results are not evidence of live returns/i);
  });

  it("shows unavailable for a stop rule or timeframe it cannot state", async () => {
    onStatus("kraken");
    const load = vi.fn().mockResolvedValue(
      engineAssumptions({
        timeframe: "7m",
        stop: { type: "trailing", pct: "5" } as unknown as PaperAssumptions["stop"],
      }),
    );
    render(
      <FirstRunWizard paperClient={fakePaper()} loadPaperAssumptions={load} />,
    );

    await settledRows();
    expect(row("decisions")).toBe("unavailable");
    expect(row("stop")).toBe("unavailable");
    expect(row("fee")).toBe("40 bps (0.40%) · taker, per side");
  });

  it("shows unavailable when the read fails and keeps the honesty line and next actions", async () => {
    onStatus("kraken");
    const load = vi.fn().mockRejectedValue(new Error("paper assumptions failed: 503"));
    render(
      <FirstRunWizard paperClient={fakePaper()} loadPaperAssumptions={load} />,
    );

    await settledRows();
    for (const id of ROWS) {
      expect(row(id)).toBe("unavailable");
    }
    expect(screen.getByTestId("first-run-wizard-assumptions-honesty")).toBeTruthy();
    expect(screen.getByTestId("first-run-wizard-next-actions")).toBeTruthy();
    await waitFor(() =>
      expect(
        screen.getByTestId("first-run-wizard-primary").hasAttribute("disabled"),
      ).toBe(false),
    );
  });

  it("reads GET /api/v1/paper/assumptions for the chosen venue and pair by default", async () => {
    onStatus("kraken");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url.startsWith("/api/v1/paper/assumptions")) {
        return Response.json({
          schema_version: "1",
          venue: "kraken",
          pair: "XXBTZUSD",
          fee_model: "taker, per side",
          fee_bps: 37,
          slippage_bps: 9,
          timeframe: "1d",
          stop: { type: "pct", pct: "4.5" },
          starting_cash: "1000",
          quote: "USD",
        });
      }
      return Response.json({ detail: "unused" }, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<FirstRunWizard paperClient={fakePaper()} />);

    await settledRows();
    const call = fetchMock.mock.calls.find(([input]) =>
      String(input).startsWith("/api/v1/paper/assumptions"),
    );
    expect(call?.[0]).toBe("/api/v1/paper/assumptions?venue=kraken&pair=XXBTZUSD");
    const init = (call as unknown as [string, RequestInit])[1];
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
    expect(row("fee")).toBe("37 bps (0.37%) · taker, per side");
    expect(row("slippage")).toBe("9 bps (0.09%) against each simulated fill");
    expect(row("decisions")).toBe("Daily, on closed candles only");
    expect(row("stop")).toMatch(/^4\.5% below the close/);
  });

  it("shows unavailable when the default read is refused", async () => {
    onStatus("kraken");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ detail: "session required" }, { status: 403 })),
    );
    render(<FirstRunWizard paperClient={fakePaper()} />);

    await settledRows();
    for (const id of ROWS) {
      expect(row(id)).toBe("unavailable");
    }
  });

  it("shows the card only on the status step", async () => {
    writeFirstRunWizardState(
      {
        status: "active",
        stepId: "start_paper",
        selectedStarterId: "starter-dual-ma-crossover",
        revisionId: "rev-starter-1",
        marketVenue: "kraken",
      },
      STORAGE,
    );
    const load = vi.fn().mockResolvedValue(engineAssumptions());
    render(
      <FirstRunWizard paperClient={fakePaper()} loadPaperAssumptions={load} />,
    );

    expect(screen.queryByTestId("first-run-wizard-assumptions")).toBeNull();
    expect(load).not.toHaveBeenCalled();
  });
});
