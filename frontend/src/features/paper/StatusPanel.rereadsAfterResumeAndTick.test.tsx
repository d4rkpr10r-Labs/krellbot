/**
 * Run shows the current paper status and owned position after the operator
 * resumes new buys and a paper tick lands out of band (the scheduler or the
 * offline CLI), then returns to Run.
 *
 * The panel already re-reads status and position once after Resume. A tick
 * that runs after that read changes the persisted position, and the panel
 * must not keep showing the pre-tick "no open position" sentence once the
 * operator asks for Run again — including re-selecting Run while it is
 * already the active view, which keeps every panel mounted.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { WorkstationShell } from "../../shell/WorkstationShell";
import { clickShellNav } from "../../shell/navTestUtils";
import type {
  PaperArmedClient,
  PaperClient,
  PaperCommandResult,
  PaperPosition,
  PaperStatus,
} from "./client";
import { StatusPanel } from "./StatusPanel";

vi.mock("../../session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../session")>()),
  redeemBootstrap: vi.fn(async () => undefined),
  recoverCsrf: vi.fn(async () => true),
}));

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

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

/** The persisted paper state the fake backend serves. */
type PaperServer = {
  entriesPaused: boolean;
  freeCash: string;
  holdings: { symbol: string; qty: string }[];
};

const PRE_TICK_SENTENCE = "Owned position: no open position — free cash 1000 USD";
const POST_TICK_QTY = "10.47913";

function ok(code: string): PaperCommandResult {
  return {
    schema_version: "1",
    code,
    ok: true,
    message: "ok",
    effect: "changed",
    revision_before: "a",
    revision_after: "b",
  };
}

/**
 * Every read returns a fresh body, as the HTTP client does: the panel must
 * learn about the tick from a new read, never from a mutated object.
 */
function fakePaperClient(server: PaperServer) {
  const getStatus = vi.fn(
    async (): Promise<PaperStatus> => ({
      schema_version: "1",
      armed: true,
      venue: "kraken",
      pair: "SUIUSD",
      mode: "paper",
      pack_id: "sma-cross",
      entries_paused: server.entriesPaused,
    }),
  );
  const getPosition = vi.fn(
    async (): Promise<PaperPosition> => ({
      schema_version: "1",
      armed: true,
      venue: "kraken",
      pair: "SUIUSD",
      mode: "paper",
      free_cash: server.freeCash,
      holdings: server.holdings.map((row) => ({ ...row })),
      as_of_run: "kraken:sma-cross:SUIUSD",
    }),
  );
  const client = {
    getStatus,
    getPosition,
    pauseEntries: vi.fn(async () => {
      server.entriesPaused = true;
      return ok("entries_paused");
    }),
    resumeEntries: vi.fn(async () => {
      server.entriesPaused = false;
      return ok("entries_resumed");
    }),
    disarm: vi.fn(async () => ok("disarmed")),
    listArmed: vi.fn(async () => ({
      schema_version: "1",
      armed: true,
      records: [
        {
          venue: "kraken",
          pair: "SUIUSD",
          mode: "paper",
          pack_id: "sma-cross",
          entries_paused: server.entriesPaused,
        },
      ],
    })),
    listRuns: vi.fn(async () => ({ schema_version: "1", runs: [] })),
  } as PaperClient & PaperArmedClient;
  return { client, getStatus, getPosition };
}

/** A tick that enters: the persisted wallet now holds base asset. */
function tickEnters(server: PaperServer): void {
  server.holdings = [{ symbol: "SUI", qty: POST_TICK_QTY }];
  server.freeCash = "0.000891325";
}

function paperPanel(): HTMLElement {
  return screen.getByTestId("paper-status-panel");
}

async function resumeAndSettle(getPosition: ReturnType<typeof vi.fn>): Promise<void> {
  await waitFor(() => {
    expect(screen.getByTestId("paper-status-entries").textContent).toBe(
      "Entries paused",
    );
    expect(screen.getByTestId("paper-position").textContent).toBe(
      PRE_TICK_SENTENCE,
    );
  });
  const readsBeforeResume = getPosition.mock.calls.length;
  fireEvent.click(
    within(paperPanel()).getByRole("button", { name: "Resume new buys" }),
  );
  // The panel's own post-Resume reconciliation finishes before the tick.
  await waitFor(() => {
    expect(screen.getByTestId("paper-status-entries").textContent).toBe(
      "Entries active",
    );
    expect(getPosition.mock.calls.length).toBeGreaterThan(readsBeforeResume);
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  expect(screen.getByTestId("paper-position").textContent).toBe(
    PRE_TICK_SENTENCE,
  );
}

describe("Run re-reads paper status after Resume and an out-of-band tick", () => {
  it("re-selecting Run while it is already shown re-reads status and the owned position", async () => {
    const server: PaperServer = {
      entriesPaused: true,
      freeCash: "1000",
      holdings: [],
    };
    const { client, getStatus, getPosition } = fakePaperClient(server);
    render(<WorkstationShell paperClient={client} />);
    await act(async () => {
      await Promise.resolve();
    });
    clickShellNav("Run");
    await resumeAndSettle(getPosition);

    tickEnters(server);
    const statusReads = getStatus.mock.calls.length;
    clickShellNav("Run");

    await waitFor(() => {
      expect(screen.getByTestId("paper-position").textContent).toBe(
        `Owned position: ${POST_TICK_QTY} SUI — free cash 0.000891325 USD`,
      );
    });
    expect(getStatus.mock.calls.length).toBe(statusReads + 1);
    expect(screen.getByTestId("paper-status-entries").textContent).toBe(
      "Entries active",
    );
  });

  it("a refreshKey change re-reads status and the owned position in place", async () => {
    const server: PaperServer = {
      entriesPaused: true,
      freeCash: "1000",
      holdings: [],
    };
    const { client, getStatus, getPosition } = fakePaperClient(server);
    const view = render(<StatusPanel client={client} refreshKey={0} />);
    await resumeAndSettle(getPosition);

    tickEnters(server);
    const statusReads = getStatus.mock.calls.length;
    const positionReads = getPosition.mock.calls.length;
    // Re-rendering with the same key is not a request to re-read.
    view.rerender(<StatusPanel client={client} refreshKey={0} />);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(getStatus.mock.calls.length).toBe(statusReads);
    expect(getPosition.mock.calls.length).toBe(positionReads);

    view.rerender(<StatusPanel client={client} refreshKey={1} />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-position").textContent).toBe(
        `Owned position: ${POST_TICK_QTY} SUI — free cash 0.000891325 USD`,
      );
    });
    expect(getStatus.mock.calls.length).toBe(statusReads + 1);
    expect(getPosition.mock.calls.length).toBe(positionReads + 1);
  });

  it("an automatic re-read never swallows the operator's own Refresh: the click gets its own read", async () => {
    const server: PaperServer = {
      entriesPaused: false,
      freeCash: "1000",
      holdings: [],
    };
    const { client, getStatus } = fakePaperClient(server);
    const view = render(<StatusPanel client={client} refreshKey={0} />);
    await waitFor(() => {
      expect(screen.getByTestId("paper-position").textContent).toBe(
        PRE_TICK_SENTENCE,
      );
    });

    // The automatic read (a refreshKey bump) is still in flight when the
    // operator clicks Refresh, and the operator's read then fails.
    let finishAutoRead: (() => void) | undefined;
    const baseline = getStatus.getMockImplementation();
    getStatus
      .mockImplementationOnce(
        () =>
          new Promise<PaperStatus>((resolve) => {
            finishAutoRead = () => {
              void baseline?.().then(resolve);
            };
          }),
      )
      .mockImplementationOnce(async () => {
        throw new TypeError("Failed to fetch");
      });
    const statusReads = getStatus.mock.calls.length;
    view.rerender(<StatusPanel client={client} refreshKey={1} />);
    await waitFor(() => {
      expect(getStatus.mock.calls.length).toBe(statusReads + 1);
    });
    fireEvent.click(
      within(paperPanel()).getByRole("button", { name: "Refresh status" }),
    );
    await act(async () => {
      finishAutoRead?.();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(getStatus.mock.calls.length).toBe(statusReads + 2);
      const alerts = within(paperPanel())
        .getAllByRole("alert")
        .map((node) => node.textContent ?? "");
      expect(alerts.some((text) => /status refresh failed/i.test(text))).toBe(true);
    });
  });
});
