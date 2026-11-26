import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StatusPanel, isValidLossLimitPct } from "./StatusPanel";
import type { PaperClient, PaperStatus } from "./client";
import type { RehearsalClient } from "./rehearsalClient";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function armedStatus(extra: Partial<PaperStatus> = {}): PaperStatus {
  return {
    schema_version: "1",
    armed: true,
    venue: "kraken",
    pair: "SUIUSD",
    entries_paused: false,
    mode: "paper",
    pack_id: "trend-follow",
    ...extra,
  };
}

function rehearsalIdle(): RehearsalClient {
  return {
    getStatus: vi.fn().mockResolvedValue({
      schema_version: "1",
      label: "SYNTHETIC PAPER REHEARSAL — simulated fixture, not market data",
      status: "idle",
      cursor: 0,
      total_bars: 20,
      balances: { USD: "1000" },
      position: "0",
      resting_stop: null,
      recent_fills: [],
    }),
    start: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    exit: vi.fn(),
    stop: vi.fn(),
  } as RehearsalClient;
}

function makeClient(status: PaperStatus = armedStatus()): PaperClient {
  return {
    getStatus: vi.fn().mockResolvedValue(status),
    pauseEntries: vi.fn(),
    resumeEntries: vi.fn(),
    disarm: vi.fn(),
    createRehearsalClient: () => rehearsalIdle(),
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

/** The read body the backend serves for the armed kraken SUIUSD paper run. */
function limitRead(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: "1",
    armed: true,
    venue: "kraken",
    pair: "SUIUSD",
    mode: "paper",
    daily_loss_limit_pct: null,
    starting_cash: "1000",
    limit_amount: null,
    day: null,
    day_open: null,
    day_low: null,
    equity: null,
    code: null,
    reason: null,
    ...extra,
  };
}

type Route = (init: RequestInit | undefined) => Response | Promise<Response>;

/** A fetch stub that serves the read route and the command route in order. */
function stubFetch(reads: Array<Record<string, unknown>>, command?: Route) {
  const queue = [...reads];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.startsWith("/api/v1/paper/daily-loss-limit")) {
      return jsonResponse(queue.length > 1 ? queue.shift() : queue[0]);
    }
    if (url === "/api/v1/commands" && command !== undefined) {
      return command(init);
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function commandBodies(fetchMock: ReturnType<typeof stubFetch>): unknown[] {
  return fetchMock.mock.calls
    .filter(([url]) => url === "/api/v1/commands")
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
}

async function openControl(): Promise<void> {
  fireEvent.click(await screen.findByTestId("paper-daily-loss-limit-toggle"));
}

describe("StatusPanel daily loss limit (paper armed surface)", () => {
  it("is collapsed and reads nothing until opened, and appears only for an armed paper run", async () => {
    const fetchMock = stubFetch([limitRead()]);
    render(<StatusPanel client={makeClient()} />);

    const toggle = await screen.findByTestId("paper-daily-loss-limit-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("paper-daily-loss-limit-body")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();

    cleanup();
    render(<StatusPanel client={makeClient({ schema_version: "1", armed: false })} />);
    await screen.findByTestId("paper-status-empty");
    expect(screen.queryByTestId("paper-daily-loss-limit")).toBeNull();

    cleanup();
    render(<StatusPanel client={makeClient(armedStatus({ mode: "live" }))} />);
    await screen.findByText(/Controls unavailable/);
    expect(screen.queryByTestId("paper-daily-loss-limit")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reads the limit when opened, sets it through the command and reads it back", async () => {
    const fetchMock = stubFetch(
      [limitRead(), limitRead({ daily_loss_limit_pct: "5", limit_amount: "50" })],
      () =>
        jsonResponse({
          schema_version: "1",
          code: "daily_loss_limit_set",
          ok: true,
          effect: "changed",
          message: "daily loss limit set: kraken SUIUSD -> 5% of starting cash",
        }),
    );
    render(<StatusPanel client={makeClient()} />);
    await openControl();

    const state = await screen.findByTestId("paper-daily-loss-limit-state");
    expect(state.textContent).toBe("Off. New buys are never held for a daily loss.");
    const [readUrl, readInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(readUrl).toBe("/api/v1/paper/daily-loss-limit?venue=kraken&pair=SUIUSD");
    expect(readInit.method).toBe("GET");
    expect(readInit.credentials).toBe("include");
    expect(screen.queryByTestId("paper-daily-loss-limit-off")).toBeNull();

    fireEvent.change(screen.getByTestId("paper-daily-loss-limit-input"), { target: { value: "5" } });
    fireEvent.click(screen.getByTestId("paper-daily-loss-limit-apply"));

    expect((await screen.findByTestId("paper-daily-loss-limit-ok")).textContent).toBe(
      "daily loss limit set: kraken SUIUSD -> 5% of starting cash",
    );
    expect(commandBodies(fetchMock)).toEqual([
      {
        schema_version: "1",
        command: "risk.set_daily_loss_limit",
        payload: { venue: "kraken", pair: "SUIUSD", pct: "5" },
      },
    ]);
    const [, postInit] = fetchMock.mock.calls.find(([url]) => url === "/api/v1/commands") as [string, RequestInit];
    expect(postInit.method).toBe("POST");
    expect(postInit.credentials).toBe("include");
    await waitFor(() => {
      expect(screen.getByTestId("paper-daily-loss-limit-state").textContent).toBe(
        "Limit: 5% of starting cash (50). When today's loss reaches it, new buys stop until 00:00 UTC; exits and stops keep running.",
      );
    });
    expect((screen.getByTestId("paper-daily-loss-limit-input") as HTMLInputElement).value).toBe("5");
    expect(screen.getByTestId("paper-daily-loss-limit-off")).toBeDefined();
  });

  it("says when new buys are held and why, and turns the limit off with an explicit null", async () => {
    const fetchMock = stubFetch(
      [limitRead({ daily_loss_limit_pct: "5", limit_amount: "50", code: "daily_loss_limit" })],
      () =>
        jsonResponse({
          schema_version: "1",
          code: "daily_loss_limit_off",
          ok: true,
          effect: "changed",
          message: "daily loss limit off: kraken SUIUSD",
        }),
    );
    render(<StatusPanel client={makeClient()} />);
    await openControl();

    expect((await screen.findByTestId("paper-daily-loss-limit-state")).textContent).toBe(
      "New buys held until 00:00 UTC: today's loss reached the limit of 5% of starting cash (50). Exits and stops keep running.",
    );
    fireEvent.click(screen.getByTestId("paper-daily-loss-limit-off"));
    await screen.findByTestId("paper-daily-loss-limit-ok");
    expect(commandBodies(fetchMock)).toEqual([
      {
        schema_version: "1",
        command: "risk.set_daily_loss_limit",
        payload: { venue: "kraken", pair: "SUIUSD", pct: null },
      },
    ]);

    cleanup();
    stubFetch([
      limitRead({
        daily_loss_limit_pct: "5",
        limit_amount: "50",
        code: "loss_limit_unavailable",
        reason: "fill_price_unknown",
      }),
    ]);
    render(<StatusPanel client={makeClient()} />);
    await openControl();
    expect((await screen.findByTestId("paper-daily-loss-limit-state")).textContent).toBe(
      "New buys held: today's loss can't be worked out (fill_price_unknown). Exits and stops keep running.",
    );
  });

  it("shows the backend refusal as-is, and an unknown outcome on a transport failure without reading again", async () => {
    const fetchMock = stubFetch([limitRead()], () =>
      jsonResponse({
        schema_version: "1",
        code: "starting_cash_unknown",
        ok: false,
        effect: "refused",
        message: "This run has no starting cash to take a percent of.",
      }),
    );
    render(<StatusPanel client={makeClient()} />);
    await openControl();
    await screen.findByTestId("paper-daily-loss-limit-state");
    fireEvent.change(screen.getByTestId("paper-daily-loss-limit-input"), { target: { value: "2.5" } });
    fireEvent.click(screen.getByTestId("paper-daily-loss-limit-apply"));
    expect((await screen.findByTestId("paper-daily-loss-limit-refused")).textContent).toBe(
      "This run has no starting cash to take a percent of.",
    );
    expect(screen.queryByTestId("paper-daily-loss-limit-ok")).toBeNull();
    expect(fetchMock.mock.calls.filter(([url]) => url !== "/api/v1/commands")).toHaveLength(1);

    cleanup();
    const failing = stubFetch([limitRead()], () => jsonResponse({ detail: "csrf required" }, 403));
    render(<StatusPanel client={makeClient()} />);
    await openControl();
    await screen.findByTestId("paper-daily-loss-limit-state");
    fireEvent.change(screen.getByTestId("paper-daily-loss-limit-input"), { target: { value: "5" } });
    fireEvent.click(screen.getByTestId("paper-daily-loss-limit-apply"));
    expect((await screen.findByTestId("paper-daily-loss-limit-unknown")).textContent).toMatch(
      /^Daily loss limit outcome unknown: risk\.set_daily_loss_limit failed: 403\. Read the limit again before retrying\.$/,
    );
    expect(failing.mock.calls.filter(([url]) => url !== "/api/v1/commands")).toHaveLength(1);
  });

  it("keeps Set limit disabled until the input is a plain percent above 0 and at most 100", async () => {
    stubFetch([limitRead()]);
    render(<StatusPanel client={makeClient()} />);
    await openControl();
    await screen.findByTestId("paper-daily-loss-limit-state");
    const input = screen.getByTestId("paper-daily-loss-limit-input");
    const apply = screen.getByTestId("paper-daily-loss-limit-apply");

    expect(apply.getAttribute("disabled")).toBe("");
    for (const bad of ["0", "100.5", "-1", "abc", "1e1", ".5", "NaN"]) {
      fireEvent.change(input, { target: { value: bad } });
      expect(apply.getAttribute("disabled")).toBe("");
      expect(screen.getByTestId("paper-daily-loss-limit-error").textContent).toMatch(/percent above 0/);
    }
    fireEvent.change(input, { target: { value: "100" } });
    expect(apply.getAttribute("disabled")).toBeNull();
    expect(screen.queryByTestId("paper-daily-loss-limit-error")).toBeNull();

    expect(isValidLossLimitPct("2.5")).toBe(true);
    expect(isValidLossLimitPct("0.0")).toBe(false);
  });

  it("says the limit is unavailable when the read fails, never that it is off", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "session required" }, 403)));
    render(<StatusPanel client={makeClient()} />);
    await openControl();
    expect((await screen.findByTestId("paper-daily-loss-limit-unavailable")).textContent).toBe(
      "Daily loss limit unavailable: daily loss limit read failed: 403",
    );
    expect(screen.queryByTestId("paper-daily-loss-limit-state")).toBeNull();
  });
});
