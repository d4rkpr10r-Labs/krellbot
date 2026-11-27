import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createHttpClient,
  readResearchRunIdentity,
  type ResearchRunIdentity,
} from "./client";
import { ResearchView, type ResearchClient, type StoredResult } from "./ResearchView";

/**
 * The run record beside every research result (F22): the result provenance
 * strip names the run id and how many pack variants were tried for this
 * strategy on this dataset, read from `GET /api/v1/jobs/{id}/run`.
 *
 * Transport note: the view tests use in-memory vi.fn() clients standing in
 * for the loopback research API, and the client tests stub fetch. Every
 * number below is a synthetic backtest fixture, never market performance.
 */

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

const RUN_ID = "3f".repeat(32);
const OTHER_RUN_ID = "a1".repeat(32);

const receipt = {
  engine_version: "0.9.7",
  pack_sha256: "abc",
  data_manifest_sha256: "feedface",
  venue: "kraken",
  pair: "SUIUSD",
  tf: "1h",
  from: "1970-01-01",
  to: "1970-01-01",
  fee_bps: 10,
  slippage_bps: 20,
  slippage_mult: 1.0,
  metrics: { total_return_pct: 1.5, trade_count: 1 },
  equity_curve: [0, 1.5],
};

const stored: StoredResult = { legacy_receipt: receipt, trace: [] };

const identity: ResearchRunIdentity = { run_id: RUN_ID, variants_tried: 3 };

function makeClient(overrides: Partial<ResearchClient> = {}): ResearchClient {
  return {
    submitRun: vi.fn().mockResolvedValue({ id: "job-run", state: "running" }),
    cancelJob: vi.fn().mockResolvedValue(undefined),
    getResult: vi.fn().mockResolvedValue(stored),
    getJob: vi.fn().mockResolvedValue({ id: "job-run", state: "succeeded" }),
    getResultDownload: vi.fn().mockResolvedValue(new Response("{}")),
    getRun: vi.fn().mockResolvedValue(identity),
    ...overrides,
  };
}

function fillForm(): void {
  fireEvent.change(screen.getByLabelText(/dataset path/i), {
    target: { value: "fixtures/synthetic.csv" },
  });
  fireEvent.change(screen.getByLabelText(/fee basis points/i), {
    target: { value: "10" },
  });
  fireEvent.change(screen.getByLabelText(/^from$/i), { target: { value: "0" } });
  fireEvent.change(screen.getByLabelText(/^to$/i), { target: { value: "1000" } });
}

async function runAndShowStrip(client: ResearchClient): Promise<HTMLElement> {
  render(<ResearchView client={client} />);
  fillForm();
  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
  await screen.findByTestId("research-result");
  return screen.getByTestId("research-result-context");
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function showRunIdentity(): void {
  fireEvent.click(screen.getByRole("button", { name: "Show run identity" }));
}

function runIdText(): string | null {
  return screen.getByTestId("research-result-context-run-id").textContent;
}

function variantsText(): string | null {
  return screen.getByTestId("research-result-context-variants-tried").textContent;
}

describe("Research result provenance: run identity", () => {
  it("shows the run id and the variants tried as their own strip items, read on request for the result's job", async () => {
    const client = makeClient();
    const strip = await runAndShowStrip(client);

    // An ordinary result costs no extra request until the reader asks.
    expect(runIdText()).toBe("not loaded");
    expect(variantsText()).toBe("not loaded");
    expect(client.getRun).not.toHaveBeenCalled();

    showRunIdentity();
    await waitFor(() => {
      expect(within(strip).getByTestId("research-result-context-run-id").textContent).toBe(RUN_ID);
    });
    expect(within(strip).getByTestId("research-result-context-variants-tried").textContent).toBe("3");
    expect(strip.textContent).toContain("Run id");
    expect(strip.textContent).toContain("Variants tried on this data");
    expect(client.getRun).toHaveBeenCalledTimes(1);
    expect(client.getRun).toHaveBeenCalledWith("job-run");
    expect(screen.queryByTestId("research-result-context-run-load")).toBeNull();

    // The existing provenance items stay as they were.
    expect(within(strip).getByTestId("research-result-context-job").textContent).toBe("job-run");
    expect(within(strip).getByTestId("research-result-context-fee").textContent).toBe("10");
    expect(within(strip).getByTestId("research-result-context-slippage").textContent).toBe("20");
    expect(within(strip).getByTestId("research-result-context-sha").textContent).toBe("feedface");
    expect(within(strip).getByTestId("research-result-context-window")).toBeDefined();
  });

  it("says loading and holds the control busy while the read is in flight", async () => {
    const pending = deferred<ResearchRunIdentity | null>();
    await runAndShowStrip(makeClient({ getRun: vi.fn().mockReturnValue(pending.promise) }));

    showRunIdentity();
    await waitFor(() => {
      expect(runIdText()).toBe("loading");
    });
    expect(variantsText()).toBe("loading");
    const control = screen.getByTestId("research-result-context-run-load") as HTMLButtonElement;
    expect(control.disabled).toBe(true);
    expect(control.getAttribute("aria-busy")).toBe("true");
    expect(control.textContent).toBe("Loading run identity…");

    pending.resolve(identity);
    await waitFor(() => {
      expect(runIdText()).toBe(RUN_ID);
    });
  });

  it("says unavailable for a job without a run record and never invents a count", async () => {
    await runAndShowStrip(makeClient({ getRun: vi.fn().mockResolvedValue(null) }));

    showRunIdentity();
    await waitFor(() => {
      expect(runIdText()).toBe("unavailable");
    });
    expect(variantsText()).toBe("unavailable");
  });

  it("says unavailable for fields the run never resolved", async () => {
    await runAndShowStrip(
      makeClient({ getRun: vi.fn().mockResolvedValue({ run_id: null, variants_tried: null }) }),
    );

    showRunIdentity();
    await waitFor(() => {
      expect(runIdText()).toBe("unavailable");
    });
    expect(variantsText()).toBe("unavailable");
  });

  it("reports a failed read and offers a retry without hiding the result", async () => {
    const getRun = vi
      .fn()
      .mockRejectedValueOnce(new Error("request failed: 503"))
      .mockResolvedValueOnce(identity);
    await runAndShowStrip(makeClient({ getRun }));

    showRunIdentity();
    const alert = await screen.findByTestId("research-result-context-run-error");
    expect(alert.getAttribute("role")).toBe("alert");
    expect(alert.textContent).toContain("request failed: 503");
    expect(runIdText()).toBe("read failed");
    expect(variantsText()).toBe("read failed");
    expect(screen.getByTestId("research-result")).toBeDefined();

    showRunIdentity();
    await waitFor(() => {
      expect(runIdText()).toBe(RUN_ID);
    });
    expect(screen.queryByTestId("research-result-context-run-error")).toBeNull();
    expect(getRun).toHaveBeenCalledTimes(2);
  });

  it("says unavailable and offers no control when the client cannot read run records", async () => {
    const client = makeClient();
    delete client.getRun;
    await runAndShowStrip(client);

    expect(runIdText()).toBe("unavailable");
    expect(variantsText()).toBe("unavailable");
    expect(screen.queryByTestId("research-result-context-run-load")).toBeNull();
  });

  it("reads the run record of a job reopened from Jobs", async () => {
    const client = makeClient();
    render(<ResearchView client={client} openedJob={{ id: "saved-job" }} />);
    await screen.findByTestId("research-result-context");

    showRunIdentity();
    await waitFor(() => {
      expect(runIdText()).toBe(RUN_ID);
    });
    expect(client.getRun).toHaveBeenCalledWith("saved-job");
  });

  it("drops a replaced result's run identity and never lands a late answer on the next result", async () => {
    const first = deferred<ResearchRunIdentity | null>();
    let submits = 0;
    const client = makeClient({
      submitRun: vi.fn().mockImplementation(async () => {
        submits += 1;
        return { id: `job-${submits}`, state: "running" };
      }),
      getJob: vi.fn().mockImplementation(async (id: string) => ({ id, state: "succeeded" })),
      getRun: vi
        .fn()
        .mockImplementation((id: string) =>
          id === "job-1" ? first.promise : Promise.resolve({ run_id: OTHER_RUN_ID, variants_tried: 4 }),
        ),
    });
    render(<ResearchView client={client} />);
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
    await screen.findByTestId("research-result");
    showRunIdentity();
    await waitFor(() => {
      expect(runIdText()).toBe("loading");
    });

    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
    await waitFor(() => {
      expect(screen.getByTestId("research-result-context-job").textContent).toBe("job-2");
    });
    expect(runIdText()).toBe("not loaded");

    // The first job's late answer never lands on the second job's result.
    first.resolve(identity);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(runIdText()).toBe("not loaded");

    showRunIdentity();
    await waitFor(() => {
      expect(runIdText()).toBe(OTHER_RUN_ID);
    });
    expect(variantsText()).toBe("4");
    expect(client.getRun).toHaveBeenLastCalledWith("job-2");
  });
});

describe("research http client run read", () => {
  it("reads /api/v1/jobs/{id}/run with the session and returns the run id and count", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        schema_version: "1",
        job_id: "job/1",
        run: {
          schema_version: "1",
          run_id: RUN_ID,
          inputs: {},
          receipt_canonical_sha256: null,
          request: { revision_id: null, strategy_id: "s" },
          state: "succeeded",
        },
        variants_tried: 2,
      }),
    } as Response);
    vi.stubGlobal("fetch", fetchMock);

    const body = await createHttpClient().getRun!("job/1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/jobs/job%2F1/run");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
    expect(body).toEqual({ run_id: RUN_ID, variants_tried: 2 });
  });

  it("returns null for a 404", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({ code: "run_unavailable" }) } as Response),
    );
    expect(await createHttpClient().getRun!("job-1")).toBeNull();
  });

  it("keeps null fields null and refuses a malformed body instead of guessing", () => {
    expect(readResearchRunIdentity({ run: { run_id: null }, variants_tried: null })).toEqual({
      run_id: null,
      variants_tried: null,
    });
    expect(readResearchRunIdentity(null)).toBeNull();
    expect(readResearchRunIdentity({ variants_tried: 1 })).toBeNull();
    expect(readResearchRunIdentity({ run: { run_id: "not-a-digest" }, variants_tried: 1 })).toBeNull();
    expect(readResearchRunIdentity({ run: { run_id: RUN_ID.toUpperCase() }, variants_tried: 1 })).toBeNull();
    expect(readResearchRunIdentity({ run: { run_id: RUN_ID }, variants_tried: -1 })).toBeNull();
    expect(readResearchRunIdentity({ run: { run_id: RUN_ID }, variants_tried: 1.5 })).toBeNull();
    expect(readResearchRunIdentity({ run: { run_id: RUN_ID }, variants_tried: "3" })).toBeNull();
    expect(readResearchRunIdentity({ run: { run_id: RUN_ID } })).toBeNull();
  });
});
