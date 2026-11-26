import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createHttpClient, type ResearchCosts } from "./client";
import { ResearchView, type ResearchClient, type StoredResult } from "./ResearchView";

/**
 * Research cost assumptions and a stressed-cost rerun.
 *
 * Transport note: the view tests use in-memory vi.fn() clients standing in
 * for the loopback research API, and the client tests stub fetch. Every
 * number below is a synthetic backtest fixture, never market performance.
 */

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

const receipt = {
  engine_version: "0.9.5",
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
  metrics: { total_return_pct: -10.538383771916706, trade_count: 1 },
  equity_curve: [0, 0, 0, -10.538383771916706],
};

const stored: StoredResult = { legacy_receipt: receipt, trace: [] };

// The side-car for the synthetic entry-candle stop in
// tests/test_research_cost_assumptions.py (10 bps fee, 20 bps slippage).
const costs: ResearchCosts = {
  fee_model: "taker, per side",
  fee_bps: 10,
  slippage_bps: 20,
  slippage_mult: 1.0,
  starting_cash: "10030.02",
  fill_count: 2,
  total_fees: "19.00200000000000",
  net_return_pct: "-10.53838377191670604844257539",
  gross_return_pct: "-10.000000",
  stressed: {
    factor: 2,
    fee_bps: 20,
    slippage_bps: 40,
    slippage_mult: 1.0,
    return_pct: "-11.07355010992018759683430342",
  },
};

function makeClient(overrides: Partial<ResearchClient> = {}): ResearchClient {
  return {
    submitRun: vi.fn().mockResolvedValue({ id: "job-costs", state: "running" }),
    cancelJob: vi.fn().mockResolvedValue(undefined),
    getResult: vi.fn().mockResolvedValue(stored),
    getJob: vi.fn().mockResolvedValue({ id: "job-costs", state: "succeeded" }),
    getResultDownload: vi.fn().mockResolvedValue(new Response("{}")),
    getCosts: vi.fn().mockResolvedValue(costs),
    ...overrides,
  };
}

function fillForm(slippage?: string): void {
  fireEvent.change(screen.getByLabelText(/dataset path/i), {
    target: { value: "fixtures/synthetic.csv" },
  });
  fireEvent.change(screen.getByLabelText(/fee basis points/i), {
    target: { value: "10" },
  });
  if (slippage !== undefined) {
    fireEvent.change(screen.getByLabelText(/slippage basis points/i), {
      target: { value: slippage },
    });
  }
  fireEvent.change(screen.getByLabelText(/^from$/i), { target: { value: "0" } });
  fireEvent.change(screen.getByLabelText(/^to$/i), { target: { value: "1000" } });
}

async function runAndShowResult(client: ResearchClient): Promise<HTMLElement> {
  render(<ResearchView client={client} />);
  fillForm();
  fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
  return await screen.findByTestId("research-result");
}

describe("Research slippage input", () => {
  it("sends the typed slippage with the run and echoes it in the in-use strip", async () => {
    const client = makeClient();
    render(<ResearchView client={client} />);
    const panel = screen.getByTestId("research-setup-panel-costs");
    expect(within(panel).getByLabelText(/slippage basis points/i)).toBeDefined();
    expect(screen.getByTestId("research-inuse-slippage").textContent).toBe("default (5)");

    fillForm("12");
    expect(screen.getByTestId("research-inuse-slippage").textContent).toBe("12");
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
    await screen.findByTestId("research-job-id");
    expect(client.submitRun).toHaveBeenCalledWith({
      datasetPath: "fixtures/synthetic.csv",
      feeBps: 10,
      slippageBps: 12,
      fromMs: 0,
      toMs: 1000,
      packPath: "",
    });
  });

  it("leaves slippage to the engine default when the field is blank", async () => {
    const client = makeClient();
    render(<ResearchView client={client} />);
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
    await screen.findByTestId("research-job-id");
    const request = vi.mocked(client.submitRun).mock.calls[0][0];
    expect("slippageBps" in request).toBe(false);
  });

  it.each(["abc", "-1", "1.5", "9007199254740993"])(
    "refuses slippage %s before submitting anything",
    (bad) => {
      const client = makeClient();
      render(<ResearchView client={client} />);
      fillForm(bad);
      fireEvent.click(screen.getByRole("button", { name: /^run$/i }));
      expect(client.submitRun).not.toHaveBeenCalled();
      expect(screen.getByRole("alert").textContent).toBe(
        "Slippage basis points must be a nonnegative integer",
      );
    },
  );
});

describe("Research Costs panel", () => {
  it("sits under the backtest honesty label and states the fee model, fee and slippage", async () => {
    const result = await runAndShowResult(makeClient());
    const honesty = within(result).getByTestId("research-result-honesty");
    expect(honesty.textContent).toBe(
      "Backtest / pinned fixture — not live market performance.",
    );
    const panel = within(result).getByTestId("research-costs");
    expect(
      honesty.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(panel).getByRole("heading", { name: "Costs" })).toBeDefined();
    expect(within(panel).getByTestId("research-costs-fee-model").textContent).toBe(
      "taker, per side",
    );
    expect(within(panel).getByTestId("research-costs-fee-bps").textContent).toBe("10");
    expect(within(panel).getByTestId("research-costs-slippage-bps").textContent).toBe("20");
    expect(panel.textContent ?? "").toMatch(/not live trading costs/i);
  });

  it("loads the job's cost side-car on request and shows fees, gross and stressed returns", async () => {
    const client = makeClient();
    const result = await runAndShowResult(client);
    const panel = within(result).getByTestId("research-costs");
    expect(client.getCosts).not.toHaveBeenCalled();

    fireEvent.click(within(panel).getByRole("button", { name: /show cost figures/i }));
    expect(client.getCosts).toHaveBeenCalledWith("job-costs");
    expect(await within(panel).findByTestId("research-costs-total-fees")).toBeDefined();

    expect(within(panel).getByTestId("research-costs-total-fees").textContent).toBe("19.00");
    expect(within(panel).getByTestId("research-costs-net-return").textContent).toBe("-10.54%");
    expect(within(panel).getByTestId("research-costs-gross-return").textContent).toBe("-10.00%");
    expect(within(panel).getByTestId("research-costs-stressed-return").textContent).toBe("-11.07%");
    expect(panel.textContent ?? "").toContain(
      "costs.total_fees · quote currency, summed over 2 fills",
    );
    expect(panel.textContent ?? "").toContain("rerun at 0 bps fee and 0 bps slippage");
    expect(panel.textContent ?? "").toContain("rerun at 20 bps fee and 40 bps slippage");
    expect(within(panel).queryByRole("button", { name: /show cost figures/i })).toBeNull();
  });

  it("shows unavailable figures honestly when the job has no cost side-car", async () => {
    const result = await runAndShowResult(
      makeClient({ getCosts: vi.fn().mockResolvedValue(null) }),
    );
    const panel = within(result).getByTestId("research-costs");
    fireEvent.click(within(panel).getByRole("button", { name: /show cost figures/i }));
    const unavailable = await within(panel).findByTestId("research-costs-unavailable");
    expect(unavailable.textContent).toMatch(/unavailable/i);
    expect(within(panel).queryByTestId("research-costs-total-fees")).toBeNull();
  });

  it("names a failed read and lets the user retry it", async () => {
    const getCosts = vi
      .fn()
      .mockRejectedValueOnce(new Error("request failed: 503"))
      .mockResolvedValueOnce(costs);
    const result = await runAndShowResult(makeClient({ getCosts }));
    const panel = within(result).getByTestId("research-costs");
    fireEvent.click(within(panel).getByRole("button", { name: /show cost figures/i }));
    const error = await within(panel).findByTestId("research-costs-error");
    expect(error.getAttribute("role")).toBe("alert");
    expect(error.textContent).toContain("request failed: 503");
    fireEvent.click(within(panel).getByRole("button", { name: /show cost figures/i }));
    expect(await within(panel).findByTestId("research-costs-total-fees")).toBeDefined();
    expect(within(panel).queryByTestId("research-costs-error")).toBeNull();
    expect(getCosts).toHaveBeenCalledTimes(2);
  });

  it("keeps the assumptions visible without a cost reader", async () => {
    const client = makeClient();
    delete client.getCosts;
    const result = await runAndShowResult(client);
    const panel = within(result).getByTestId("research-costs");
    expect(within(panel).getByTestId("research-costs-fee-model").textContent).toBe(
      "taker, per side",
    );
    expect(within(panel).queryByRole("button", { name: /show cost figures/i })).toBeNull();
    expect(within(panel).getByTestId("research-costs-unavailable").textContent).toMatch(
      /unavailable/i,
    );
  });

  it("drops loaded figures when another job's result replaces the view", async () => {
    const client = makeClient();
    const { rerender } = render(<ResearchView client={client} openedJob={{ id: "job-a" }} />);
    const panel = await screen.findByTestId("research-costs");
    fireEvent.click(within(panel).getByRole("button", { name: /show cost figures/i }));
    expect(client.getCosts).toHaveBeenLastCalledWith("job-a");
    await screen.findByTestId("research-costs-total-fees");

    await act(async () => {
      rerender(<ResearchView client={client} openedJob={{ id: "job-b" }} />);
    });
    const next = await screen.findByTestId("research-costs");
    expect(within(next).queryByTestId("research-costs-total-fees")).toBeNull();
    fireEvent.click(within(next).getByRole("button", { name: /show cost figures/i }));
    expect(client.getCosts).toHaveBeenLastCalledWith("job-b");
  });
});

describe("Research client costs transport", () => {
  function jsonResponse(body: unknown, status = 200): Response {
    return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
  }

  it("reads the cost side-car with the session read gate and maps 404 to null", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const responses = [jsonResponse(costs), jsonResponse({ code: "costs_unavailable" }, 404)];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return responses[calls.length - 1];
      }),
    );
    const client = createHttpClient();
    await expect(client.getCosts?.("job /1")).resolves.toEqual(costs);
    await expect(client.getCosts?.("job-old")).resolves.toBeNull();
    expect(calls[0].url).toBe("/api/v1/jobs/job%20%2F1/costs");
    expect(calls[0].init.method).toBe("GET");
    expect(calls[0].init.credentials).toBe("include");
    expect((calls[0].init.headers as Record<string, string>)["X-Krellbot-CSRF"]).toBeDefined();
    expect(calls[1].url).toBe("/api/v1/jobs/job-old/costs");
  });

  it("treats a malformed side-car as unavailable instead of inventing numbers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ fee_model: "taker, per side", total_fees: "NaN" })),
    );
    await expect(createHttpClient().getCosts?.("job-bad")).resolves.toBeNull();
  });

  it("sends slippage_bps only when the run names it", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        bodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return jsonResponse({ id: "job", state: "running" });
      }),
    );
    const client = createHttpClient();
    await client.submitRun({ datasetPath: "d.csv", feeBps: 40, fromMs: 0, toMs: 1 });
    await client.submitRun({ datasetPath: "d.csv", feeBps: 40, slippageBps: 0, fromMs: 0, toMs: 1 });
    expect("slippage_bps" in bodies[0]).toBe(false);
    expect(bodies[1].slippage_bps).toBe(0);
  });
});
