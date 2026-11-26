/**
 * The armed paper status shows the resting stop in force, read from
 * `GET /api/v1/paper/details?venue=&pair=` for the confirmed market, as a
 * new element beside the unchanged 'Owned position' sentence. It says when
 * the value was read, keeps that time on a failed refresh so an old value is
 * never presented as current, and says 'unavailable' instead of guessing.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StatusPanel } from "./StatusPanel";
import type {
  PaperClient,
  PaperDetails,
  PaperPosition,
  PaperStatus,
  PaperStatusSelect,
} from "./client";
import type { RehearsalClient } from "./rehearsalClient";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const OWNED_SENTENCE = "Owned position: 5 SUI — free cash 2480.50 USD";

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

function position(extra: Partial<PaperPosition> = {}): PaperPosition {
  return {
    schema_version: "1",
    armed: true,
    venue: "kraken",
    pair: "SUIUSD",
    mode: "paper",
    free_cash: "2480.50",
    holdings: [{ symbol: "SUI", qty: "5" }],
    as_of_run: "kraken:trend-follow:SUIUSD",
    ...extra,
  };
}

function details(extra: Partial<PaperDetails> = {}): PaperDetails {
  return {
    schema_version: "1",
    armed: true,
    venue: "kraken",
    pair: "SUIUSD",
    pack_id: "trend-follow",
    starting_cash: "2500",
    risk_cap_pct: "100",
    resting_stop: "3.8",
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

function makeClient(overrides: Partial<PaperClient> = {}): PaperClient {
  return {
    getStatus: vi.fn().mockResolvedValue(armedStatus()),
    getPosition: vi.fn().mockResolvedValue(position()),
    getDetails: vi.fn().mockResolvedValue(details()),
    pauseEntries: vi.fn(),
    resumeEntries: vi.fn(),
    disarm: vi.fn(),
    createRehearsalClient: () => rehearsalIdle(),
    ...overrides,
  } as PaperClient;
}

const AS_OF = /, as of \S*\d/;

describe("StatusPanel resting stop in force", () => {
  it("shows the resting stop read for the confirmed venue and pair beside the unchanged Owned position sentence", async () => {
    const client = makeClient();
    render(<StatusPanel client={client} />);

    const stop = await screen.findByTestId("paper-resting-stop");
    await waitFor(() => {
      expect(stop.textContent).toMatch(/^Resting stop 3\.8 USD, as of /);
    });
    expect(stop.textContent).toMatch(AS_OF);
    expect(stop.tagName).toBe("P");
    expect(client.getDetails).toHaveBeenCalledWith({ venue: "kraken", pair: "SUIUSD" });

    const owned = await screen.findByTestId("paper-position");
    expect(owned.textContent).toBe(OWNED_SENTENCE);
    // Beside the sentence: the very next element, not folded into it.
    expect(owned.nextElementSibling).toBe(stop);
  });

  it("says 'unavailable', with when it was read, when no stop is resting — never a guessed number", async () => {
    const client = makeClient({ getDetails: vi.fn().mockResolvedValue(details({ resting_stop: null })) });
    render(<StatusPanel client={client} />);
    const stop = await screen.findByTestId("paper-resting-stop");
    await waitFor(() => {
      expect(stop.textContent).toMatch(/^Resting stop unavailable, as of /);
    });
    expect(stop.textContent).toMatch(AS_OF);
    expect(stop.textContent).not.toMatch(/USD/);
  });

  it.each([
    ["the read fails", () => Promise.reject(new Error("paper details failed: 503"))],
    ["the body names another pair", () => Promise.resolve(details({ pair: "ETHUSD", resting_stop: "9" }))],
    ["the body names another venue", () => Promise.resolve(details({ venue: "coinbase", resting_stop: "9" }))],
    ["the body is unarmed", () => Promise.resolve<PaperDetails>({ schema_version: "1", armed: false })],
  ])("says 'Resting stop unavailable' without a number or a time when %s", async (_label, read) => {
    const client = makeClient({ getDetails: vi.fn(() => read()) });
    render(<StatusPanel client={client} />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-resting-stop").textContent).toBe("Resting stop unavailable");
    });
    expect(screen.getByTestId("paper-position").textContent).toBe(OWNED_SENTENCE);
  });

  it("says 'Resting stop unavailable' for a legacy client that cannot read details", async () => {
    const client = makeClient({ getDetails: undefined });
    render(<StatusPanel client={client} />);
    await screen.findByTestId("paper-position");
    expect(screen.getByTestId("paper-resting-stop").textContent).toBe("Resting stop unavailable");
  });

  it("says the stop is being read while the request is in flight", async () => {
    const client = makeClient({ getDetails: vi.fn(() => new Promise<PaperDetails>(() => undefined)) });
    render(<StatusPanel client={client} />);
    const stop = await screen.findByTestId("paper-resting-stop");
    expect(stop.textContent).toBe("Reading resting stop…");
  });

  it("re-reads the stop after a confirmed status refresh and shows the new value", async () => {
    const getDetails = vi
      .fn<(select?: PaperStatusSelect) => Promise<PaperDetails>>()
      .mockResolvedValueOnce(details({ resting_stop: "3.8" }))
      .mockResolvedValue(details({ resting_stop: "4.25" }));
    // Like the HTTP client, every status read decodes a fresh body.
    const client = makeClient({ getDetails, getStatus: vi.fn(async () => armedStatus()) });
    render(<StatusPanel client={client} />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-resting-stop").textContent).toMatch(/^Resting stop 3\.8 USD, as of /);
    });
    fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    await waitFor(() => {
      expect(screen.getByTestId("paper-resting-stop").textContent).toMatch(/^Resting stop 4\.25 USD, as of /);
    });
    expect(getDetails).toHaveBeenLastCalledWith({ venue: "kraken", pair: "SUIUSD" });
  });

  it("keeps the last read stop labelled with its read time when a status refresh fails", async () => {
    const getStatus = vi
      .fn<(select?: PaperStatusSelect) => Promise<PaperStatus>>()
      .mockResolvedValueOnce(armedStatus())
      .mockRejectedValue(new Error("Failed to fetch"));
    const client = makeClient({ getStatus });
    render(<StatusPanel client={client} />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-resting-stop").textContent).toMatch(/^Resting stop 3\.8 USD, as of /);
    });
    const before = screen.getByTestId("paper-resting-stop").textContent;

    fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    const alert = await screen.findByTestId("paper-status-unknown");
    expect(alert.textContent).toMatch(/Status refresh failed.*last confirmed state/);
    // The retained value still names when it was read, so it is not shown as current.
    expect(screen.getByTestId("paper-resting-stop").textContent).toBe(before);
    expect(screen.getByTestId("paper-resting-stop").textContent).toMatch(AS_OF);
  });

  it("drops the previous market's stop as soon as the confirmed market changes", async () => {
    let resolveEth: (value: PaperDetails) => void = () => undefined;
    const getDetails = vi.fn((select?: PaperStatusSelect) =>
      select?.pair === "ETHUSD"
        ? new Promise<PaperDetails>((resolve) => {
            resolveEth = resolve;
          })
        : Promise.resolve(details()),
    );
    const getStatus = vi.fn((select?: PaperStatusSelect) =>
      Promise.resolve(armedStatus(select?.pair === "ETHUSD" ? { pair: "ETHUSD" } : {})),
    );
    const getPosition = vi.fn((select?: PaperStatusSelect) =>
      Promise.resolve(position(select?.pair === "ETHUSD" ? { pair: "ETHUSD", holdings: [] } : {})),
    );
    const client = makeClient({ getDetails, getStatus, getPosition });
    const { rerender } = render(<StatusPanel client={client} select={{ venue: "kraken", pair: "SUIUSD" }} />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-resting-stop").textContent).toMatch(/^Resting stop 3\.8 USD/);
    });

    rerender(<StatusPanel client={client} select={{ venue: "kraken", pair: "ETHUSD" }} />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-status-pack").textContent).toContain("ETHUSD");
    });
    expect(screen.getByTestId("paper-resting-stop").textContent).toBe("Reading resting stop…");
    await act(async () => {
      resolveEth(details({ pair: "ETHUSD", resting_stop: "1800" }));
    });
    expect(screen.getByTestId("paper-resting-stop").textContent).toMatch(/^Resting stop 1800 USD, as of /);
    expect(getDetails).toHaveBeenCalledWith({ venue: "kraken", pair: "ETHUSD" });
  });
});
