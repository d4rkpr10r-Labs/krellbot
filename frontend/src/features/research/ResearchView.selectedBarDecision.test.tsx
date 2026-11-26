import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ResearchView } from "./ResearchView";
import {
  createHttpClient,
  type FillLeg,
  type ResearchClient,
  type ResearchFills,
  type StoredResult,
} from "./client";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// Synthetic trace (test data, not market data). Each bar carries the
// stored trace shape: input, conditions with operands, and the target.
type Reason = "warmup" | "flat" | "entry" | "exit";

function bar(
  barTs: number,
  close: number,
  reason: Reason,
  stop: number | null = null,
  sma: number | null = close - 1,
) {
  const entryTrue = reason === "entry";
  const exitTrue = reason === "exit";
  const unknown = reason === "warmup";
  return {
    bar_ts: barTs,
    warmup: unknown,
    input: { ts_ms: barTs, open: close, high: close, low: close, close, volume: 1 },
    indicators: { sma2: unknown ? null : sma },
    conditions: [
      {
        path: "entry",
        outcome: unknown ? "unknown" : entryTrue,
        operands: {
          left: "close",
          op: "crosses_above",
          right: "sma2",
          left_value: close,
          right_value: unknown ? null : sma,
        },
      },
      {
        path: "exit",
        outcome: unknown ? "unknown" : exitTrue,
        operands: {
          left: "close",
          op: "crosses_below",
          right: "sma2",
          left_value: close,
          right_value: unknown ? null : sma,
        },
      },
    ],
    target: { long: entryTrue, stop_price: stop, reason },
  };
}

const TRACE = [
  bar(1000, 90, "warmup"),
  bar(2000, 90, "flat"),
  bar(3000, 100, "entry", 90, 95),
  bar(4000, 105, "flat"),
  bar(5000, 110, "entry", 99),
  bar(6000, 95, "exit"),
  bar(7000, 94, "flat"),
  bar(8000, 100, "entry", 80, 97),
  bar(9000, 70, "flat"),
  bar(10000, 99, "entry", 89.1),
];

const PACK_SHA = "4f1c0ffee0000000000000000000000000000000000000000000000000000a1";

const STORED: StoredResult = {
  legacy_receipt: {
    pack_sha256: PACK_SHA,
    fee_bps: 10,
    slippage_bps: 5,
    metrics: { trade_count: 2 },
  },
  trace: TRACE,
};

const leg = (side: "buy" | "sell", price: string, qty: string, fee: string, isStop = false): FillLeg => ({
  side,
  price,
  qty,
  fee,
  is_stop: isStop,
});

const FILLS: ResearchFills = {
  fills: {
    // Buy at bar 4000's open after the entry decided on bar 3000.
    "4000": [leg("buy", "105.0525", "95.12345678", "9.99271573")],
    // Sell at bar 7000's open after the exit decided on bar 6000.
    "7000": [leg("sell", "93.9530", "95.12345678", "8.93719498")],
    // Entry-candle stop on bar 9000: the buy and the stop both fill there.
    "9000": [
      leg("buy", "100.0500", "88.12345678", "8.81675185"),
      leg("sell", "79.9600", "88.12345678", "7.04632857", true),
    ],
  },
};

function makeClient(overrides: Partial<ResearchClient> = {}): ResearchClient {
  return {
    submitRun: vi.fn(),
    cancelJob: vi.fn(),
    getJob: vi.fn(),
    getResultDownload: vi.fn(),
    getResult: vi.fn().mockResolvedValue(STORED),
    getFills: vi.fn().mockResolvedValue(FILLS),
    ...overrides,
  };
}

async function openBar(client: ResearchClient, barTs: number): Promise<HTMLElement> {
  render(<ResearchView client={client} openedJob={{ id: "job-fills" }} />);
  const scroll = await screen.findByTestId("research-trace-scroll");
  fireEvent.click(within(scroll).getByRole("button", { name: String(barTs) }));
  return screen.findByTestId("research-bar-detail");
}

async function decisionFor(client: ResearchClient, barTs: number): Promise<HTMLElement> {
  const detail = await openBar(client, barTs);
  await within(detail).findByText(/^Decision: (enter|exit|hold|stay flat|unavailable)$/);
  return detail;
}

function text(detail: HTMLElement, testId: string): string {
  return (within(detail).getByTestId(testId).textContent ?? "").trim();
}

// The J4.1 journey reads the detail's text with this pattern.
const J4_DECISION = /(?:decision|action|target)\s*:?\s*(?:enter|exit|hold|stay flat)/i;

describe("selected research bar names its decision and fill", () => {
  it("names enter on an entry signal while flat, with its stop, operand values and configuration hash", async () => {
    const client = makeClient();
    const detail = await decisionFor(client, 3000);

    expect(text(detail, "research-bar-decision")).toBe("Decision: enter");
    expect(detail.textContent ?? "").toMatch(J4_DECISION);
    expect(text(detail, "research-bar-decision-note")).toMatch(/next bar's open/i);
    expect(text(detail, "research-bar-stop")).toMatch(/^Stop price: 90\b/);
    expect(text(detail, "research-bar-stop")).toMatch(/this bar's close/i);
    expect(text(detail, "research-bar-config-hash")).toBe(
      `Configuration hash (pack sha256): ${PACK_SHA}`,
    );

    const operands = Array.from(
      detail.querySelectorAll(".kbot-research__condition-operands"),
    ).map((node) => node.textContent);
    expect(operands).toEqual([
      "close = 100 crosses_above sma2 = 95",
      "close = 100 crosses_below sma2 = 95",
    ]);
    // Outcome tokens are untouched by the new rows.
    const outcomes = Array.from(
      detail.querySelectorAll(".kbot-research__condition-outcome"),
    ).map((node) => node.textContent);
    expect(outcomes).toEqual(["true", "false"]);

    expect(text(detail, "research-bar-fills")).toMatch(/no fill on this bar/i);
  });

  it("names hold while the backtest is long and shows the buy that filled on that bar", async () => {
    const detail = await decisionFor(makeClient(), 4000);
    expect(text(detail, "research-bar-decision")).toBe("Decision: hold");
    expect(text(detail, "research-bar-stop")).toMatch(/^Stop price: 90\b/);
    expect(text(detail, "research-bar-stop")).toMatch(/resting since the buy on bar 4000/i);
    const fills = within(detail).getAllByTestId("research-bar-fill");
    expect(fills.map((node) => node.textContent)).toEqual([
      "buy 95.12345678 @ 105.0525 · fee 9.99271573",
    ]);
    expect(text(detail, "research-bar-fills")).toMatch(/simulated/i);
    expect(text(detail, "research-bar-fills")).toMatch(/not venue fills/i);
  });

  it("names hold, not enter, when the entry rule fires again while long", async () => {
    const detail = await decisionFor(makeClient(), 5000);
    expect(text(detail, "research-bar-decision")).toBe("Decision: hold");
    expect(text(detail, "research-bar-decision-note")).toMatch(/already long/i);
    // The resting stop is the one placed with the buy, not this bar's signal stop.
    expect(text(detail, "research-bar-stop")).toMatch(/^Stop price: 90\b/);
  });

  it("names exit when the exit rule fires while long", async () => {
    const detail = await decisionFor(makeClient(), 6000);
    expect(text(detail, "research-bar-decision")).toBe("Decision: exit");
    expect(detail.textContent ?? "").toMatch(J4_DECISION);
    expect(text(detail, "research-bar-decision-note")).toMatch(/next bar's open/i);
  });

  it.each([
    [1000, /warming up/i],
    [2000, /no entry signal/i],
    [7000, /no entry signal/i],
  ])("names stay flat on bar %s", async (barTs, note) => {
    const detail = await decisionFor(makeClient(), barTs);
    expect(text(detail, "research-bar-decision")).toBe("Decision: stay flat");
    expect(detail.textContent ?? "").toMatch(J4_DECISION);
    expect(text(detail, "research-bar-decision-note")).toMatch(note);
    expect(text(detail, "research-bar-stop")).toBe("Stop price: none (no open position)");
  });

  it("shows the exit fill on the bar after the exit decision", async () => {
    const detail = await decisionFor(makeClient(), 7000);
    const fills = within(detail).getAllByTestId("research-bar-fill");
    expect(fills.map((node) => node.textContent)).toEqual([
      "sell 95.12345678 @ 93.9530 · fee 8.93719498",
    ]);
  });

  it("shows both legs of an entry-candle stop and does not act on that bar's signal", async () => {
    const detail = await decisionFor(makeClient(), 9000);
    expect(text(detail, "research-bar-decision")).toBe("Decision: stay flat");
    expect(text(detail, "research-bar-decision-note")).toMatch(/stop closed the position on this bar/i);
    expect(text(detail, "research-bar-stop")).toMatch(/^Stop price: 80\b/);
    expect(text(detail, "research-bar-stop")).toMatch(/hit on this bar/i);
    const fills = within(detail).getAllByTestId("research-bar-fill");
    expect(fills.map((node) => node.textContent)).toEqual([
      "buy 88.12345678 @ 100.0500 · fee 8.81675185",
      "sell 88.12345678 @ 79.9600 · fee 7.04632857 · stop",
    ]);
  });

  it("an entry signal on the last bar stays flat because no later bar can fill it", async () => {
    const detail = await decisionFor(makeClient(), 10000);
    expect(text(detail, "research-bar-decision")).toBe("Decision: stay flat");
    expect(text(detail, "research-bar-decision-note")).toMatch(/no later bar/i);
  });

  it("says unavailable instead of guessing a decision once a position could exist without recorded fills", async () => {
    const detail = await decisionFor(
      makeClient({ getFills: vi.fn().mockResolvedValue(null) }),
      4000,
    );
    expect(text(detail, "research-bar-decision")).toBe("Decision: unavailable");
    expect(detail.textContent ?? "").not.toMatch(J4_DECISION);
    expect(text(detail, "research-bar-stop")).toBe("Stop price: unavailable");
    expect(text(detail, "research-bar-fills")).toMatch(/fills unavailable for this run/i);
    // The configuration hash and operand values do not depend on fills.
    expect(text(detail, "research-bar-config-hash")).toContain(PACK_SHA);
    expect(detail.querySelectorAll(".kbot-research__condition-operands")).toHaveLength(2);
  });

  it("says unavailable when the fills read fails or the client cannot read fills", async () => {
    const failing = await decisionFor(
      makeClient({ getFills: vi.fn().mockRejectedValue(new Error("request failed: 503")) }),
      6000,
    );
    expect(text(failing, "research-bar-decision")).toBe("Decision: unavailable");
    expect(text(failing, "research-bar-fills")).toMatch(/request failed: 503/);
    cleanup();

    const legacy = makeClient();
    delete legacy.getFills;
    const detail = await decisionFor(legacy, 5000);
    expect(text(detail, "research-bar-decision")).toBe("Decision: unavailable");
    expect(text(detail, "research-bar-stop")).toBe("Stop price: unavailable");
  });

  it("names decisions up to the first entry signal from the trace alone, since nothing can fill before it", async () => {
    const detail = await decisionFor(
      makeClient({ getFills: vi.fn().mockResolvedValue(null) }),
      3000,
    );
    expect(text(detail, "research-bar-decision")).toBe("Decision: enter");
    expect(text(detail, "research-bar-stop")).toMatch(/^Stop price: 90\b/);
    expect(text(detail, "research-bar-fills")).toMatch(/no fill on this bar/i);

    // A read that never settles does not hold the first bar's decision back.
    cleanup();
    const pending = await openBar(
      makeClient({ getFills: vi.fn().mockReturnValue(new Promise(() => undefined)) }),
      1000,
    );
    expect(text(pending, "research-bar-decision")).toBe("Decision: stay flat");
    expect(pending.textContent ?? "").toMatch(J4_DECISION);
    expect(text(pending, "research-bar-fills")).toMatch(/no fill on this bar/i);
  });

  it("reads fills once per loaded result, only after a bar is selected", async () => {
    const getFills = vi.fn().mockResolvedValue(FILLS);
    render(<ResearchView client={makeClient({ getFills })} openedJob={{ id: "job-fills" }} />);
    const scroll = await screen.findByTestId("research-trace-scroll");
    expect(getFills).not.toHaveBeenCalled();

    fireEvent.click(within(scroll).getByRole("button", { name: "4000" }));
    const detail = await screen.findByTestId("research-bar-detail");
    await within(detail).findByText("Decision: hold");
    fireEvent.click(within(scroll).getByRole("button", { name: "6000" }));
    await within(detail).findByText("Decision: exit");
    expect(getFills).toHaveBeenCalledTimes(1);
    expect(getFills).toHaveBeenCalledWith("job-fills");
  });

  it("shows unavailable for a missing configuration hash and unknown operand values", async () => {
    const client = makeClient({
      getResult: vi.fn().mockResolvedValue({ legacy_receipt: {}, trace: TRACE }),
    });
    const detail = await decisionFor(client, 1000);
    expect(text(detail, "research-bar-config-hash")).toBe(
      "Configuration hash (pack sha256): unavailable",
    );
    const operands = Array.from(
      detail.querySelectorAll(".kbot-research__condition-operands"),
    ).map((node) => node.textContent);
    expect(operands).toEqual([
      "close = 90 crosses_above sma2 = unknown",
      "close = 90 crosses_below sma2 = unknown",
    ]);
  });
});

describe("selected research bar without rule targets", () => {
  it("does not claim a bar has no fill when earlier bars carry no readable target", async () => {
    const untargeted = TRACE.map((row) => ({ ...row, target: undefined }));
    const detail = await decisionFor(
      makeClient({
        getResult: vi.fn().mockResolvedValue({ legacy_receipt: {}, trace: untargeted }),
        getFills: vi.fn().mockResolvedValue(null),
      }),
      4000,
    );
    expect(text(detail, "research-bar-decision")).toBe("Decision: unavailable");
    expect(text(detail, "research-bar-fills")).toMatch(/fills unavailable for this run/i);
    expect(text(detail, "research-bar-fills")).not.toMatch(/no fill on this bar/i);
  });
});

describe("research http client fills read", () => {
  it("reads /api/v1/jobs/{id}/fills with the session and returns legs keyed by bar_ts", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ schema_version: "1", job_id: "job/1", fills: FILLS.fills }),
    } as Response);
    vi.stubGlobal("fetch", fetchMock);

    const body = await createHttpClient().getFills!("job/1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/jobs/job%2F1/fills");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
    expect(body).toEqual(FILLS);
  });

  it("returns null for a 404 and for a malformed body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({ code: "fills_unavailable" }) } as Response),
    );
    expect(await createHttpClient().getFills!("job-1")).toBeNull();

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ fills: { "1000": [{ side: "buy", price: 1, qty: "1", fee: "0", is_stop: false }] } }),
      } as Response),
    );
    expect(await createHttpClient().getFills!("job-1")).toBeNull();
  });
});
