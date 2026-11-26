/**
 * Each armed row states the risk cap in force for its (venue, pair), read
 * from `GET /api/v1/paper/details?venue=&pair=`. The cap is a percent of the
 * paper wallet's free cash per entry (run.tick sizes a paper entry from the
 * quote balance), never a currency amount. A missing, mismatched or failed
 * read says 'unavailable' — never a guessed number.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ArmedRecordsList } from "./ArmedRecordsList";
import { createHttpClient } from "./client";
import type {
  PaperArmedClient,
  PaperArmedRecord,
  PaperArmedView,
  PaperDetails,
  PaperStatusSelect,
} from "./client";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function viewOf(records: PaperArmedRecord[]): PaperArmedView {
  return { schema_version: "1", armed: records.length > 0, records };
}

function record(extra: Partial<PaperArmedRecord> = {}): PaperArmedRecord {
  return {
    venue: "kraken",
    pair: "SUIUSD",
    mode: "paper",
    pack_id: "trend-follow",
    entries_paused: false,
    ...extra,
  };
}

function details(select: PaperStatusSelect, extra: Partial<PaperDetails> = {}): PaperDetails {
  return {
    schema_version: "1",
    armed: true,
    venue: select.venue,
    pair: select.pair,
    pack_id: "trend-follow",
    starting_cash: "2500",
    risk_cap_pct: "100",
    resting_stop: null,
    ...extra,
  };
}

function rowFor(venue: string, pair: string): HTMLElement {
  const row = screen
    .getAllByTestId("paper-armed-row")
    .find((el) => el.getAttribute("data-venue") === venue && el.getAttribute("data-pair") === pair);
  if (row === undefined) throw new Error(`no armed row for ${venue} ${pair}`);
  return row;
}

describe("ArmedRecordsList risk cap in force", () => {
  it("shows 'Risk cap N% of free cash per entry' inside each armed row, read per (venue, pair)", async () => {
    const caps: Record<string, string> = { "kraken:SUIUSD": "100", "coinbase:BTC-USD": "12.5" };
    const getDetails = vi.fn(async (select?: PaperStatusSelect) => {
      if (select === undefined) throw new Error("details read without a market selection");
      return details(select, { risk_cap_pct: caps[`${select.venue}:${select.pair}`] });
    });
    const client: PaperArmedClient = {
      listArmed: vi.fn().mockResolvedValue(
        viewOf([record(), record({ venue: "coinbase", pair: "BTC-USD", pack_id: "mean-revert" })]),
      ),
      getDetails,
    };
    render(<ArmedRecordsList client={client} />);

    await waitFor(() => {
      expect(
        within(rowFor("kraken", "SUIUSD")).getByTestId("paper-armed-risk-cap-kraken-SUIUSD").textContent,
      ).toBe("Risk cap 100% of free cash per entry");
    });
    await waitFor(() => {
      expect(
        within(rowFor("coinbase", "BTC-USD")).getByTestId("paper-armed-risk-cap-coinbase-BTC-USD").textContent,
      ).toBe("Risk cap 12.5% of free cash per entry");
    });
    // One selected read per row — never the unselected first-record default.
    expect(getDetails).toHaveBeenCalledWith({ venue: "kraken", pair: "SUIUSD" });
    expect(getDetails).toHaveBeenCalledWith({ venue: "coinbase", pair: "BTC-USD" });
    expect(getDetails).toHaveBeenCalledTimes(2);
  });

  it("says the cap is being read while the details request is in flight, without a number", async () => {
    const client: PaperArmedClient = {
      listArmed: vi.fn().mockResolvedValue(viewOf([record()])),
      getDetails: vi.fn(() => new Promise<PaperDetails>(() => undefined)),
    };
    render(<ArmedRecordsList client={client} />);
    const cell = await screen.findByTestId("paper-armed-risk-cap-kraken-SUIUSD");
    expect(cell.textContent).toBe("Risk cap: reading…");
    expect(cell.textContent).not.toMatch(/\d/);
  });

  it.each([
    ["the body carries no cap", (s: PaperStatusSelect) => Promise.resolve(details(s, { risk_cap_pct: null }))],
    ["the body names another pair", (s: PaperStatusSelect) => Promise.resolve(details({ ...s, pair: "ETHUSD" }))],
    ["the body names another venue", (s: PaperStatusSelect) => Promise.resolve(details({ ...s, venue: "coinbase" }))],
    ["the record is not a paper arm", () => Promise.resolve<PaperDetails>({ schema_version: "1", armed: false })],
    ["the read fails", () => Promise.reject(new Error("paper details failed: 503"))],
  ])("shows 'Risk cap unavailable' when %s — never a guessed number", async (_label, read) => {
    const client: PaperArmedClient = {
      listArmed: vi.fn().mockResolvedValue(viewOf([record()])),
      getDetails: vi.fn((select?: PaperStatusSelect) => read(select as PaperStatusSelect)),
    };
    render(<ArmedRecordsList client={client} />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-armed-risk-cap-kraken-SUIUSD").textContent).toBe("Risk cap unavailable");
    });
    expect(rowFor("kraken", "SUIUSD").textContent).not.toMatch(/\d+(?:\.\d+)?%/);
  });

  it("shows 'Risk cap unavailable' for a legacy client that cannot read details", async () => {
    const client: PaperArmedClient = { listArmed: vi.fn().mockResolvedValue(viewOf([record()])) };
    render(<ArmedRecordsList client={client} />);
    const cell = await screen.findByTestId("paper-armed-risk-cap-kraken-SUIUSD");
    expect(cell.textContent).toBe("Risk cap unavailable");
  });

  it("re-reads the cap when the armed list is re-read, and drops a late answer for a superseded list", async () => {
    let resolveFirst: (value: PaperDetails) => void = () => undefined;
    const getDetails = vi
      .fn<(select?: PaperStatusSelect) => Promise<PaperDetails>>()
      .mockImplementationOnce(
        () =>
          new Promise<PaperDetails>((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockImplementation(async (select) => details(select as PaperStatusSelect, { risk_cap_pct: "50" }));
    const client: PaperArmedClient = {
      listArmed: vi.fn().mockResolvedValue(viewOf([record()])),
      getDetails,
    };
    const { rerender } = render(<ArmedRecordsList client={client} refreshKey={0} />);
    await screen.findByTestId("paper-armed-risk-cap-kraken-SUIUSD");
    rerender(<ArmedRecordsList client={client} refreshKey={1} />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-armed-risk-cap-kraken-SUIUSD").textContent).toBe(
        "Risk cap 50% of free cash per entry",
      );
    });
    // The first, superseded read answers late with another cap: ignored.
    resolveFirst(details({ venue: "kraken", pair: "SUIUSD" }, { risk_cap_pct: "100" }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByTestId("paper-armed-risk-cap-kraken-SUIUSD").textContent).toBe(
      "Risk cap 50% of free cash per entry",
    );
  });
});

describe("paper HTTP client details read", () => {
  function jsonResponse(body: unknown, status = 200): Response {
    return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
  }

  it("GETs /api/v1/paper/details for the selected venue and pair and decodes cap and stop verbatim", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        schema_version: "1",
        armed: true,
        venue: "kraken",
        pair: "SUIUSD",
        pack_id: "trend-follow",
        starting_cash: "2500",
        risk_cap_pct: "12.5",
        resting_stop: "3.8",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = await createHttpClient().getDetails({ venue: "kraken", pair: "SUIUSD" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/paper/details?venue=kraken&pair=SUIUSD");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
    expect(body).toEqual({
      schema_version: "1",
      armed: true,
      venue: "kraken",
      pair: "SUIUSD",
      pack_id: "trend-follow",
      starting_cash: "2500",
      risk_cap_pct: "12.5",
      resting_stop: "3.8",
    });
  });

  it("decodes a missing or non-decimal value as null instead of repairing it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          schema_version: "1",
          armed: true,
          venue: "kraken",
          pair: "SUIUSD",
          pack_id: "trend-follow",
          starting_cash: 2500,
          risk_cap_pct: "NaN",
          resting_stop: null,
        }),
      ),
    );
    const body = await createHttpClient().getDetails({ venue: "kraken", pair: "SUIUSD" });
    expect(body.starting_cash).toBeNull();
    expect(body.risk_cap_pct).toBeNull();
    expect(body.resting_stop).toBeNull();
  });

  it("throws on a refused read rather than returning an empty body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "session required" }, 403)));
    await expect(createHttpClient().getDetails({ venue: "kraken", pair: "SUIUSD" })).rejects.toThrow(
      "paper details failed: 403",
    );
  });
});
