/**
 * The diagnostic Operations section is a separate, optional download.
 *
 * GET /api/v1/diagnostics/operations serves it on its own route so the
 * report keeps its six keys. The panel previews it beside the report, saves
 * it only as its own file, shows 'unavailable' for a part the engine could
 * not read, and keeps the report usable when the section cannot be read.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DiagnosticReport } from "./DiagnosticReport";

const REPORT = {
  schema_version: "1",
  build: { version: "1.2.3", commit: null },
  os: "Linux",
  scheduler: { installed: true, enabled: true, has_error: false },
  events: [],
  reproduction_steps: "",
};

const OPERATIONS = {
  schema_version: "1",
  alerts: [
    { id: "0123456789abcdef", kind: "metadata_refusal", severity: "warning", code: "metadata_stale", count: 2, first_ts: 10, last_ts: 20 },
    { id: "fedcba9876543210", kind: "tick_failure", severity: "critical", code: null, count: 1, first_ts: 30, last_ts: 30 },
  ],
  unresolved_orders: { count: 3, ages_seconds: [7200, 60, null] },
  paper_runs: [{ venue: "kraken", pair: "SUIUSD", pack_id: "ops-report", pack_version: "1.0.0", mode: "paper" }],
};

type Route = { status: number; body: unknown };

function stubFetch(routes: Record<string, Route>): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const route = routes[String(input)];
    if (route === undefined) throw new Error(`unexpected fetch ${String(input)}`);
    return new Response(JSON.stringify(route.body), { status: route.status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** jsdom has no blob URLs: capture every saved Blob and its file name. */
function stubDownloads(): Array<{ name: string; blob: Blob }> {
  const saved: Array<{ name: string; blob: Blob }> = [];
  let pending: Blob | null = null;
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  URL.createObjectURL = ((input: Blob | MediaSource): string => {
    pending = input instanceof Blob ? input : null;
    return "blob:diagnostic";
  }) as typeof URL.createObjectURL;
  URL.revokeObjectURL = (() => undefined) as typeof URL.revokeObjectURL;
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    if (pending !== null) saved.push({ name: this.download, blob: pending });
    pending = null;
  });
  restoreUrl.push(() => {
    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
  });
  return saved;
}

const restoreUrl: Array<() => void> = [];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  while (restoreUrl.length > 0) restoreUrl.pop()!();
});

async function prepare(): Promise<HTMLElement> {
  render(<DiagnosticReport />);
  fireEvent.click(screen.getByRole("button", { name: "Prepare report" }));
  return await screen.findByTestId("diagnostic-operations");
}

describe("DiagnosticReport Operations section", () => {
  it("previews the section apart from the report and saves it as its own file", async () => {
    const fetchMock = stubFetch({
      "/api/v1/diagnostics/report": { status: 200, body: REPORT },
      "/api/v1/diagnostics/operations": { status: 200, body: OPERATIONS },
    });
    const saved = stubDownloads();
    const section = await prepare();

    expect(fetchMock).toHaveBeenCalledWith("/api/v1/diagnostics/operations", { credentials: "same-origin", cache: "no-store" });
    expect(within(section).getByText("Operations section (optional)")).toBeTruthy();
    expect(within(section).getByTestId("diagnostic-operations-alerts").textContent).toBe("2");
    expect(within(section).getByTestId("diagnostic-operations-unresolved").textContent).toBe("3");
    expect(within(section).getByTestId("diagnostic-operations-paper-runs").textContent).toBe("1");
    expect(within(section).getByTestId("diagnostic-operations-preview").textContent).toContain("metadata_stale");
    expect(screen.getByTestId("diagnostic-preview").textContent).not.toContain("paper_runs");

    fireEvent.click(screen.getByRole("button", { name: "Download report" }));
    fireEvent.click(within(section).getByRole("button", { name: "Download Operations section" }));
    expect(saved.map((item) => item.name)).toEqual([
      "krellbot-diagnostic-report.json",
      "krellbot-diagnostic-operations.json",
    ]);
    const report = JSON.parse(await saved[0].blob.text()) as Record<string, unknown>;
    expect(Object.keys(report).sort()).toEqual(["build", "events", "os", "reproduction_steps", "scheduler", "schema_version"]);
    expect(JSON.parse(await saved[1].blob.text())).toEqual(OPERATIONS);
  });

  it("shows 'unavailable' for each part the engine could not read", async () => {
    stubFetch({
      "/api/v1/diagnostics/report": { status: 200, body: REPORT },
      "/api/v1/diagnostics/operations": {
        status: 200,
        body: { schema_version: "1", alerts: "unavailable", unresolved_orders: "unavailable", paper_runs: "unavailable" },
      },
    });
    const section = await prepare();

    expect(within(section).getByTestId("diagnostic-operations-alerts").textContent).toBe("unavailable");
    expect(within(section).getByTestId("diagnostic-operations-unresolved").textContent).toBe("unavailable");
    expect(within(section).getByTestId("diagnostic-operations-paper-runs").textContent).toBe("unavailable");
  });

  it("keeps the report usable when the section cannot be read", async () => {
    stubFetch({
      "/api/v1/diagnostics/report": { status: 200, body: REPORT },
      "/api/v1/diagnostics/operations": { status: 503, body: { detail: "unavailable" } },
    });
    const saved = stubDownloads();
    const section = await prepare();

    expect(within(section).getByTestId("diagnostic-operations-unavailable").textContent).toBe("Operations section: unavailable.");
    expect(within(section).queryByRole("button", { name: "Download Operations section" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Download report" }));
    await waitFor(() => expect(saved.map((item) => item.name)).toEqual(["krellbot-diagnostic-report.json"]));
  });
});
