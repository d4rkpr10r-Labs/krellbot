import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AlertsFilter } from "./AlertsFilter";
import { OperationsView } from "./OperationsView";
import type { AlertRow, OperationsClient, OperationsViewModel } from "./client";

/**
 * Operations alert filters are a per-viewer convenience: severity, kind and
 * query survive a reload through localStorage, and any storage failure
 * (blocked, throwing, or holding garbage) falls back to the default filters
 * without breaking the panel. The search also matches the alert's pair.
 */

// The key Operations persists under. Pinned literally so a rename (which
// would silently drop every viewer's saved filters) shows up here.
const STORAGE_KEY = "krellbot.ops.alerts-filter";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

function alert(over: Partial<AlertRow> = {}): AlertRow {
  return {
    id: "a1",
    kind: "live_refused",
    severity: "warning",
    code: "live_disabled",
    venue: "kraken",
    pair: "SUIUSD",
    count: 1,
    first_ts: 1700000000,
    last_ts: 1700000600,
    acknowledged: false,
    ...over,
  };
}

const sample: AlertRow[] = [
  alert({ id: "a1", severity: "warning", kind: "live_refused", code: "live_disabled", venue: "kraken", pair: "SUIUSD" }),
  alert({ id: "a2", severity: "critical", kind: "feed_stale", code: "candles_stale", venue: "coinbase", pair: "ETHUSD" }),
  alert({ id: "a3", severity: "warning", kind: "entries_paused", code: "entries_paused", venue: "kraken", pair: "ETHUSD" }),
  alert({ id: "a4", severity: "info", kind: "live_refused", code: null, venue: null, pair: null }),
];

// `null` mounts the filter without a storage key (in-memory only).
function mount(alerts: readonly AlertRow[] = sample, storageKey: string | null = STORAGE_KEY) {
  return render(
    <AlertsFilter alerts={alerts} storageKey={storageKey ?? undefined}>
      {(visible) => (
        <ul data-testid="rendered">
          {visible.map((a) => (
            <li key={a.id} data-testid={`row-${a.id}`} />
          ))}
        </ul>
      )}
    </AlertsFilter>,
  );
}

function visibleIds(): string[] {
  return Array.from(screen.getByTestId("rendered").children).map(
    (el) => el.getAttribute("data-testid") ?? "",
  );
}

function severitySelect(): HTMLSelectElement {
  return screen.getByTestId("ops-alerts-filter-severity") as HTMLSelectElement;
}

function kindSelect(): HTMLSelectElement {
  return screen.getByTestId("ops-alerts-filter-kind") as HTMLSelectElement;
}

function queryInput(): HTMLInputElement {
  return screen.getByTestId("ops-alerts-filter-query") as HTMLInputElement;
}

function expectDefaults(): void {
  expect(severitySelect().value).toBe("all");
  expect(kindSelect().value).toBe("all");
  expect(queryInput().value).toBe("");
  expect(visibleIds()).toEqual(["row-a1", "row-a2", "row-a3", "row-a4"]);
  expect(screen.queryByTestId("ops-alerts-filter-clear")).toBeNull();
  expect(screen.queryByTestId("ops-alerts-empty")).toBeNull();
}

describe("Operations alert filters across reloads", () => {
  it("restores severity, kind and query after the filter remounts", () => {
    mount();
    fireEvent.change(severitySelect(), { target: { value: "warning" } });
    fireEvent.change(kindSelect(), { target: { value: "entries_paused" } });
    fireEvent.change(queryInput(), { target: { value: "kraken" } });
    expect(visibleIds()).toEqual(["row-a3"]);

    cleanup();
    mount();

    expect(severitySelect().value).toBe("warning");
    expect(kindSelect().value).toBe("entries_paused");
    expect(queryInput().value).toBe("kraken");
    expect(visibleIds()).toEqual(["row-a3"]);
    expect(screen.getByTestId("ops-alerts-filter-clear")).toBeTruthy();
  });

  it("keeps a restored severity selectable after the alerts that carried it are gone", () => {
    mount();
    fireEvent.change(severitySelect(), { target: { value: "critical" } });
    cleanup();

    mount([sample[0]]);

    expect(severitySelect().value).toBe("critical");
    expect(Array.from(severitySelect().options).map((o) => o.value)).toContain("critical");
    expect(visibleIds()).toEqual([]);
    expect(screen.getByTestId("ops-alerts-empty").textContent).toBe(
      "No alerts match the current filters.",
    );
  });

  it("stores only the severity, kind and query under its own key", () => {
    mount();
    fireEvent.change(severitySelect(), { target: { value: "critical" } });
    fireEvent.change(queryInput(), { target: { value: "eth" } });

    expect(window.localStorage.length).toBe(1);
    const stored = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null");
    expect(stored).toEqual({ severity: "critical", kind: "all", query: "eth" });
  });

  it("forgets the stored filters once Clear filters restores the defaults", () => {
    mount();
    fireEvent.change(severitySelect(), { target: { value: "warning" } });
    fireEvent.change(queryInput(), { target: { value: "kraken" } });
    fireEvent.click(screen.getByTestId("ops-alerts-filter-clear"));
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();

    cleanup();
    mount();
    expectDefaults();
  });

  it("keeps filters in memory only when no storage key is given", () => {
    mount(sample, null);
    fireEvent.change(severitySelect(), { target: { value: "warning" } });
    fireEvent.change(queryInput(), { target: { value: "kraken" } });
    expect(window.localStorage.length).toBe(0);

    cleanup();
    mount(sample, null);
    expectDefaults();
  });

  it("Operations restores its alert filters after a reload", async () => {
    const model: OperationsViewModel = {
      schema_version: "1",
      live: {
        schema_version: "1",
        live_enabled: false,
        authorized: [],
        kill_switch: { engaged: false, reason: null, engaged_at: null },
        promotion_available: false,
        promotion_code: "live_promotion_owner_deferred",
      },
      deployments: [],
      alerts: sample,
    };
    const client = {
      getOperations: vi.fn().mockResolvedValue(model),
      promote: vi.fn(),
      pauseEntries: vi.fn(),
      resumeEntries: vi.fn(),
      engageKill: vi.fn(),
      releaseKill: vi.fn(),
      ackAlert: vi.fn(),
      listDeploymentRecords: vi.fn().mockResolvedValue([]),
      getDeploymentRecord: vi.fn(),
    } as unknown as OperationsClient;

    render(<OperationsView client={client} />);
    await screen.findByTestId("ops-alert-a2");
    fireEvent.change(severitySelect(), { target: { value: "warning" } });
    fireEvent.change(queryInput(), { target: { value: "ETHUSD" } });
    expect(screen.queryByTestId("ops-alert-a1")).toBeNull();
    expect(screen.getByTestId("ops-alert-a3")).toBeTruthy();

    cleanup();
    render(<OperationsView client={client} />);
    await screen.findByTestId("ops-alert-a3");

    expect(severitySelect().value).toBe("warning");
    expect(queryInput().value).toBe("ETHUSD");
    expect(screen.queryByTestId("ops-alert-a1")).toBeNull();
    expect(screen.queryByTestId("ops-alert-a2")).toBeNull();
    expect(screen.queryByTestId("ops-alert-a4")).toBeNull();
  });
});

describe("Operations alert filters when storage fails", () => {
  it.each([
    ["unparseable text", "{not json"],
    ["JSON null", "null"],
    ["an array", "[]"],
    ["a bare string", '"warning"'],
    ["a number", "42"],
    ["a non-string severity", '{"severity":5,"kind":"all","query":""}'],
    ["a missing field", '{"severity":"warning","kind":"all"}'],
    ["an empty severity", '{"severity":"","kind":"all","query":""}'],
    ["markup as a kind", '{"severity":"all","kind":"<img src=x>","query":""}'],
    ["a non-string query", '{"severity":"all","kind":"all","query":{"x":1}}'],
  ])("falls back to the default filters when storage holds %s", (_label, raw) => {
    window.localStorage.setItem(STORAGE_KEY, raw);
    mount();
    expectDefaults();
  });

  it("renders the defaults and still filters when localStorage refuses every call", () => {
    const denied = () => {
      throw new DOMException("storage disabled", "SecurityError");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(denied);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(denied);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(denied);

    mount();
    expectDefaults();

    fireEvent.change(severitySelect(), { target: { value: "warning" } });
    fireEvent.change(queryInput(), { target: { value: "ethusd" } });
    expect(visibleIds()).toEqual(["row-a3"]);
    fireEvent.click(screen.getByTestId("ops-alerts-filter-clear"));
    expectDefaults();
  });

  it("renders the defaults and still filters when the localStorage accessor itself throws", () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("storage blocked", "SecurityError");
      },
    });
    try {
      mount();
      expectDefaults();
      fireEvent.change(kindSelect(), { target: { value: "live_refused" } });
      expect(visibleIds()).toEqual(["row-a1", "row-a4"]);
    } finally {
      cleanup();
      if (original) {
        Object.defineProperty(window, "localStorage", original);
      } else {
        delete (window as { localStorage?: Storage }).localStorage;
      }
    }
  });
});

describe("Operations alert search by pair", () => {
  it("matches the alert pair case-insensitively", () => {
    mount(sample, null);
    fireEvent.change(queryInput(), { target: { value: "ethusd" } });
    expect(visibleIds()).toEqual(["row-a2", "row-a3"]);
    fireEvent.change(queryInput(), { target: { value: "SuiUsd" } });
    expect(visibleIds()).toEqual(["row-a1"]);
  });

  it("composes a pair search with the severity filter", () => {
    mount(sample, null);
    fireEvent.change(severitySelect(), { target: { value: "critical" } });
    fireEvent.change(queryInput(), { target: { value: "ETH" } });
    expect(visibleIds()).toEqual(["row-a2"]);
  });

  it("tells the viewer that the search covers pairs", () => {
    mount(sample, null);
    expect(queryInput().placeholder).toMatch(/\bpair\b/);
  });
});

describe("Operations alert filter touch targets at 640px and below", () => {
  const css = readFileSync(resolve(__dirname, "OperationsView.signal.css"), "utf8");

  function narrowBlock(source: string): string {
    const start = source.indexOf("@media (max-width: 640px)");
    expect(start).toBeGreaterThanOrEqual(0);
    const open = source.indexOf("{", start);
    let depth = 0;
    for (let i = open; i < source.length; i += 1) {
      if (source[i] === "{") depth += 1;
      if (source[i] === "}") {
        depth -= 1;
        if (depth === 0) return source.slice(open + 1, i);
      }
    }
    throw new Error("unterminated narrow media block");
  }

  it("renders native controls inside the filter root the narrow rules target", () => {
    mount();
    fireEvent.change(queryInput(), { target: { value: "kraken" } });
    const root = screen.getByTestId("ops-alerts-filter");
    expect(root.classList.contains("kbot-ops__alerts-filter")).toBe(true);
    expect(severitySelect().tagName).toBe("SELECT");
    expect(kindSelect().tagName).toBe("SELECT");
    expect(queryInput().tagName).toBe("INPUT");
    expect(queryInput().type).toBe("text");
    const clear = screen.getByTestId("ops-alerts-filter-clear");
    expect(clear.tagName).toBe("BUTTON");
    for (const control of [severitySelect(), kindSelect(), queryInput(), clear]) {
      expect(root.contains(control)).toBe(true);
    }
  });

  it("gives the selects, the search and the clear button a 44px floor at 640px and below", () => {
    const narrow = narrowBlock(css).replace(/\/\*[\s\S]*?\*\//g, "");
    const rules = Array.from(narrow.matchAll(/([^{}]+)\{([^{}]*)\}/g)).filter(([, , body]) =>
      /min-height:\s*44px/.test(body),
    );
    const selectors = rules.flatMap(([, list]) => list.split(",").map((s) => s.trim()));
    expect(selectors).toContain(".kbot-ops .kbot-ops__alerts-filter select");
    expect(selectors).toContain(
      '.kbot-ops .kbot-ops__alerts-filter input:not([type="checkbox"]):not([type="radio"])',
    );
    expect(selectors).toContain(".kbot-ops button");
  });
});
