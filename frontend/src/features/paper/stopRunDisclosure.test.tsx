/**
 * Every 'Stop paper run' control says what it does today: it removes the arm
 * only, the paper position and its resting stop stay in place unmanaged, and
 * there is no close-position control yet. Beside it, 'Pause new buys' says it
 * stops new entries only.
 *
 * Engine evidence: `PaperService.disarm` and `run.disarm_pack` only remove the
 * armed record (the paper venue state keeps the balance and the resting stop);
 * `run.tick` evaluates armed records only. `entries_paused` suppresses the
 * entry branch while the exit, stop-repair and `raise_stop` branches run.
 *
 * Every client here is an in-memory vi.fn() fake; no engine state is touched.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ArmedRecordsList } from "./ArmedRecordsList";
import { FleetArmedList } from "./FleetArmedList";
import { StatusPanel } from "./StatusPanel";
import { PAUSE_NEW_BUYS_DISCLOSURE, STOP_RUN_DISCLOSURE } from "./stopRunDisclosure";
import type {
  FleetArmedClient,
  FleetArmedDeployment,
  PaperArmedClient,
  PaperArmedRecord,
  PaperClient,
  PaperStatus,
} from "./client";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function describedBy(control: HTMLElement): HTMLElement[] {
  const ids = (control.getAttribute("aria-describedby") ?? "").split(/\s+/).filter(Boolean);
  return ids
    .map((id) => document.getElementById(id))
    .filter((element): element is HTMLElement => element !== null);
}

/** The disclosure the control points at, asserted to sit in the same panel. */
function expectDisclosedBeside(control: HTMLElement): void {
  const described = describedBy(control);
  const disclosure = described.find((element) =>
    (element.textContent ?? "").includes(STOP_RUN_DISCLOSURE),
  );
  expect(disclosure, `no stop-run disclosure for ${control.outerHTML}`).toBeDefined();
  expect(disclosure?.closest("section")).toBe(control.closest("section"));
}

function stopRunControls(): HTMLElement[] {
  return screen.getAllByRole("button", { name: /stop paper run/i });
}

describe("stop-run disclosure copy", () => {
  it("says the position and stop stay unmanaged and no close control exists yet", () => {
    expect(STOP_RUN_DISCLOSURE).toBe(
      "Stop paper run removes the arm only. Any open paper position and its resting stop stay in place, unmanaged: nothing raises or re-places that stop or acts on an exit signal for it. There is no close-position control yet.",
    );
  });

  it("says pause stops new buys only while exits and the stop keep running", () => {
    expect(PAUSE_NEW_BUYS_DISCLOSURE).toBe(
      "Pause new buys stops new entries only. Exits, stop repair and stop raises keep running.",
    );
  });
});

describe("StatusPanel stop-run disclosure", () => {
  const armed: PaperStatus = {
    schema_version: "1",
    armed: true,
    venue: "kraken",
    pair: "SUIUSD",
    entries_paused: false,
    mode: "paper",
    pack_id: "trend-follow",
  };

  function client(): PaperClient {
    return {
      getStatus: vi.fn().mockResolvedValue(armed),
      getPosition: vi.fn().mockResolvedValue(null),
      pauseEntries: vi.fn(),
      resumeEntries: vi.fn(),
      disarm: vi.fn(),
      raiseStop: vi.fn(),
    } as PaperClient;
  }

  it("sits beside Stop paper run and Pause new buys with unchanged labels", async () => {
    render(<StatusPanel client={client()} />);
    const stop = await screen.findByRole("button", { name: /^stop paper run$/i });
    const pause = screen.getByRole("button", { name: /^pause new buys \(doesn't sell\)$/i });
    expect(stop.textContent).toBe("Stop paper run");
    expectDisclosedBeside(stop);
    for (const control of stopRunControls()) expectDisclosedBeside(control);
    const pauseText = describedBy(pause).map((element) => element.textContent ?? "").join(" ");
    expect(pauseText).toContain(PAUSE_NEW_BUYS_DISCLOSURE);
    expect(screen.getByRole("region", { name: "Paper controls" }).textContent).toContain(
      STOP_RUN_DISCLOSURE,
    );
  });
});

describe("FleetArmedList stop-run disclosure", () => {
  function deployment(overrides: Partial<FleetArmedDeployment> = {}): FleetArmedDeployment {
    return {
      venue: "kraken",
      pair: "SUIUSD",
      mode: "paper",
      pack_id: "trend-follow",
      entries_paused: false,
      last_tick_ts_ms: 1_700_000_100_000,
      last_tick_result: "2 ticks, no refusal",
      last_refusal_code: null,
      detail_href: "/?view=workstation&venue=kraken&pair=SUIUSD",
      ...overrides,
    };
  }

  const rows = [
    deployment(),
    deployment({ venue: "coinbase", pair: "BTC-USD", entries_paused: true }),
  ];

  function client() {
    return {
      listFleet: vi.fn<FleetArmedClient["listFleet"]>().mockResolvedValue({
        schema_version: "1",
        armed: true,
        deployments: rows,
      }),
      pauseEntries: vi.fn<PaperClient["pauseEntries"]>().mockResolvedValue({ ok: true }),
      resumeEntries: vi.fn<PaperClient["resumeEntries"]>().mockResolvedValue({ ok: true }),
      disarm: vi.fn<PaperClient["disarm"]>().mockResolvedValue({ ok: true }),
    };
  }

  it("is described by every row and selected Stop paper run control", async () => {
    render(<FleetArmedList client={client()} />);
    await waitFor(() => {
      expect(screen.getAllByTestId("fleet-armed-row")).toHaveLength(2);
    });
    const rowStops = screen.getAllByTestId("fleet-armed-row-disarm");
    expect(rowStops).toHaveLength(2);
    expect(rowStops.map((control) => control.getAttribute("aria-label"))).toEqual([
      "Stop paper run kraken SUIUSD",
      "Stop paper run coinbase BTC-USD",
    ]);
    for (const control of rowStops) expectDisclosedBeside(control);

    screen.getAllByTestId("fleet-armed-row")[1].click();
    const selectedStop = await screen.findByTestId("fleet-armed-selected-disarm");
    expect(selectedStop.getAttribute("aria-label")).toBe("Stop paper run selected coinbase BTC-USD");
    expect(selectedStop.textContent).toBe("Stop paper run");
    expectDisclosedBeside(selectedStop);
    for (const control of stopRunControls()) expectDisclosedBeside(control);
    expect(screen.getAllByText(STOP_RUN_DISCLOSURE, { exact: false })).toHaveLength(1);
  });
});

describe("ArmedRecordsList stop-run disclosure", () => {
  const record: PaperArmedRecord = {
    venue: "kraken",
    pair: "SUIUSD",
    mode: "paper",
    pack_id: "trend-follow",
    entries_paused: false,
  };

  function client() {
    return {
      listArmed: vi.fn<PaperArmedClient["listArmed"]>().mockResolvedValue({
        schema_version: "1",
        armed: true,
        records: [record],
      }),
      pauseEntries: vi.fn<PaperClient["pauseEntries"]>().mockResolvedValue({ ok: true }),
      resumeEntries: vi.fn<PaperClient["resumeEntries"]>().mockResolvedValue({ ok: true }),
      disarm: vi.fn<PaperClient["disarm"]>().mockResolvedValue({ ok: true }),
    };
  }

  it("is described by the detail Stop paper run control", async () => {
    render(<ArmedRecordsList client={client()} />);
    const [row] = await screen.findAllByTestId("paper-armed-row");
    row.click();
    const stop = await screen.findByTestId("paper-armed-row-disarm");
    expect(stop.getAttribute("aria-label")).toBe("Stop paper run kraken SUIUSD");
    expect(stop.textContent).toBe("Stop paper run");
    expectDisclosedBeside(stop);
    for (const control of stopRunControls()) expectDisclosedBeside(control);
    const pauseText = describedBy(screen.getByTestId("paper-armed-row-pause"))
      .map((element) => element.textContent ?? "")
      .join(" ");
    expect(pauseText).toContain(PAUSE_NEW_BUYS_DISCLOSURE);
  });
});
