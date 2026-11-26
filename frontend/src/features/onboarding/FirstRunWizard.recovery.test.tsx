/**
 * First-run recovery after a lost arm response or a failed session (KQ-04).
 *
 * - Check status advances only when the chosen venue and pair show this
 *   revision's paper arm; otherwise Start stays a safe retry.
 * - A refused repeat Start of the same revision reads as already running.
 * - A failed loopback session is retried on the next action, keeping the
 *   chosen starter and venue.
 * Nothing in these flows sends a second arm for a run that already exists.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const loopback = vi.hoisted(() => ({
  ensureLoopbackSession: vi.fn<() => Promise<boolean>>(),
  retryLoopbackSession: vi.fn<() => Promise<boolean>>(),
}));
vi.mock("../../loopbackSession", () => loopback);

import { App } from "../../App";
import type {
  PaperArmDraftSummary,
  PaperClient,
  PaperCommandResult,
  PaperStatus,
} from "../paper/client";
import type { StrategyClient } from "../strategies/client";
import {
  CONSUMER_STARTER_CARDS,
  CONSUMER_STARTER_CARD_ORDER,
} from "../strategies/consumerStarterCopy";
import type { StarterCatalogue, StarterExample } from "../strategies/StarterPicker.client";
import {
  FirstRunWizard,
  readFirstRunWizardState,
  writeFirstRunWizardState,
} from "./FirstRunWizard";
import { FIRST_RUN_WIZARD_STORAGE_KEY } from "./firstRunWizardPersistence";

const STORAGE = FIRST_RUN_WIZARD_STORAGE_KEY;
const REVISION = "rev-starter-1";
const STARTER = "starter-dual-ma-crossover";

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

beforeEach(() => {
  window.localStorage.clear();
  loopback.ensureLoopbackSession.mockReset();
  loopback.retryLoopbackSession.mockReset();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function catalogue(): StarterCatalogue {
  const examples: StarterExample[] = CONSUMER_STARTER_CARD_ORDER.map((id) => ({
    id,
    label: CONSUMER_STARTER_CARDS[id]!.title,
    description: "Educational example.",
    pack: { schema_version: 1, id, label: id, timeframe: "1h" },
  }));
  return { schema_version: "1", catalogue_version: "1", examples };
}

function fakeStrategy(): StrategyClient {
  return {
    create: vi.fn().mockImplementation(async (pack) => ({
      revision_id: REVISION,
      state: "draft",
      pack,
      errors: [],
    })),
    edit: vi.fn(),
    validate: vi.fn().mockResolvedValue({
      revision_id: REVISION,
      state: "validated",
      pack: {},
      errors: [],
    }),
    arm: vi.fn(),
  };
}

function krakenArm(overrides: Partial<PaperStatus> = {}): PaperStatus {
  return {
    schema_version: "1",
    armed: true,
    mode: "paper",
    entries_paused: false,
    venue: "kraken",
    pair: "XXBTZUSD",
    pack_id: STARTER,
    ...overrides,
  };
}

function ownedDraft(): PaperArmDraftSummary {
  return {
    revision_id: REVISION,
    strategy_id: STARTER,
    state: "deployed",
    runnable: true,
    label: "Dual moving average",
    pair: "XXBTZUSD",
  };
}

const LOST: Error = new TypeError("Failed to fetch");

function fakePaper(over: Partial<PaperClient> = {}): PaperClient {
  return {
    getStatus: vi.fn().mockResolvedValue(krakenArm()),
    listOwnedDrafts: vi.fn().mockResolvedValue([ownedDraft()]),
    pauseEntries: vi.fn(),
    resumeEntries: vi.fn(),
    disarm: vi.fn(),
    arm: vi.fn().mockResolvedValue({ ok: true, code: "armed", message: "armed" }),
    ...over,
  } as PaperClient;
}

function onStartPaper(): void {
  writeFirstRunWizardState(
    {
      status: "active",
      stepId: "start_paper",
      selectedStarterId: STARTER,
      revisionId: REVISION,
      marketVenue: "kraken",
    },
    STORAGE,
  );
}

function step(): string | null {
  return screen.getByTestId("first-run-wizard").getAttribute("data-wizard-step");
}

function surfaceText(): string {
  return screen.getByTestId("first-run-wizard").textContent ?? "";
}

async function loseFirstArmResponse(): Promise<void> {
  fireEvent.click(screen.getByTestId("first-run-wizard-primary"));
  const alert = await screen.findByTestId("first-run-wizard-arm-error");
  expect(alert.textContent).toMatch(/Start paper run outcome unknown/);
  // The instruction names the control that is actually on screen.
  expect(alert.textContent).toMatch(/Check status/);
  expect(alert.textContent).not.toMatch(/Refresh status/);
  expect(screen.getByTestId("first-run-wizard-check-status").textContent).toBe(
    "Check status",
  );
  expect(step()).toBe("start_paper");
}

describe("FirstRunWizard Check status after a lost arm response", () => {
  it("advances only when the chosen venue and pair show this revision's paper arm", async () => {
    onStartPaper();
    const paperClient = fakePaper({ arm: vi.fn().mockRejectedValue(LOST) });
    render(<FirstRunWizard paperClient={paperClient} />);

    await loseFirstArmResponse();
    fireEvent.click(screen.getByTestId("first-run-wizard-check-status"));

    await waitFor(() => expect(step()).toBe("status"));
    expect(paperClient.getStatus).toHaveBeenCalledWith({
      venue: "kraken",
      pair: "XXBTZUSD",
    });
    expect(paperClient.arm).toHaveBeenCalledTimes(1);
    expect(
      screen.getByTestId("first-run-wizard-already-running").textContent,
    ).toMatch(/Already running on Kraken · BTC\/USD/);
    expect(readFirstRunWizardState(STORAGE)).toMatchObject({
      stepId: "status",
      revisionId: REVISION,
      marketVenue: "kraken",
    });
  });

  it.each<[string, PaperStatus]>([
    ["no arm", krakenArm({ armed: false, venue: undefined, pair: undefined, pack_id: undefined })],
    ["another strategy", krakenArm({ pack_id: "someone-else" })],
    ["another market", krakenArm({ venue: "coinbase", pair: "BTC-USD" })],
    ["a non-paper arm", krakenArm({ mode: "live" })],
  ])("stays on Start with %s and lets Start retry once", async (_label, status) => {
    onStartPaper();
    const arm = vi
      .fn<(input: unknown) => Promise<PaperCommandResult>>()
      .mockRejectedValueOnce(LOST)
      .mockResolvedValueOnce({ ok: true, code: "armed", message: "armed" });
    const paperClient = fakePaper({
      arm,
      getStatus: vi.fn().mockResolvedValue(status),
    });
    render(<FirstRunWizard paperClient={paperClient} />);

    await loseFirstArmResponse();
    fireEvent.click(screen.getByTestId("first-run-wizard-check-status"));

    const note = await screen.findByTestId("first-run-wizard-check-result");
    expect(note.textContent).toMatch(
      /not running on Kraken · BTC\/USD yet\. Press Start paper run/,
    );
    expect(step()).toBe("start_paper");
    expect(screen.queryByTestId("first-run-wizard-check-status")).toBeNull();
    expect(screen.queryByTestId("first-run-wizard-arm-error")).toBeNull();
    expect(arm).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId("first-run-wizard-primary"));
    await waitFor(() => expect(step()).toBe("status"));
    expect(arm).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("first-run-wizard-already-running")).toBeNull();
  });

  it("keeps Check status when the read fails and never arms on a guess", async () => {
    onStartPaper();
    const paperClient = fakePaper({
      arm: vi.fn().mockRejectedValue(LOST),
      getStatus: vi.fn().mockRejectedValue(new Error("paper status failed: 503")),
    });
    render(<FirstRunWizard paperClient={paperClient} />);

    await loseFirstArmResponse();
    fireEvent.click(screen.getByTestId("first-run-wizard-check-status"));
    await waitFor(() =>
      expect(screen.getByTestId("first-run-wizard-arm-error").textContent).toMatch(
        /Could not check status: paper status failed: 503\. Press Check status again/,
      ),
    );
    expect(screen.getByTestId("first-run-wizard-check-status")).toBeTruthy();

    fireEvent.click(screen.getByTestId("first-run-wizard-primary"));
    await waitFor(() => expect(paperClient.getStatus).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByTestId("first-run-wizard-primary").hasAttribute("disabled")).toBe(false),
    );
    expect(paperClient.arm).toHaveBeenCalledTimes(1);
    expect(step()).toBe("start_paper");
    expect(screen.getByTestId("first-run-wizard-check-status")).toBeTruthy();
  });

  it("Start after an unknown outcome finds the existing arm instead of arming again", async () => {
    onStartPaper();
    const paperClient = fakePaper({ arm: vi.fn().mockRejectedValue(LOST) });
    render(<FirstRunWizard paperClient={paperClient} />);

    await loseFirstArmResponse();
    fireEvent.click(screen.getByTestId("first-run-wizard-primary"));

    await waitFor(() => expect(step()).toBe("status"));
    expect(paperClient.arm).toHaveBeenCalledTimes(1);
    expect(
      screen.getByTestId("first-run-wizard-already-running").textContent,
    ).toMatch(/Already running/);
  });
});

describe("FirstRunWizard maps a refused repeat of the same revision to already running", () => {
  it.each([
    ["over_reserved", "combined paper reservation would exceed account cash"],
    ["already_armed", "already armed: kraken XXBTZUSD"],
  ])("%s with this revision armed on the chosen market reads as already running", async (code, message) => {
    onStartPaper();
    const paperClient = fakePaper({
      arm: vi.fn().mockResolvedValue({ ok: false, code, message }),
    });
    render(<FirstRunWizard paperClient={paperClient} />);

    fireEvent.click(screen.getByTestId("first-run-wizard-primary"));

    await waitFor(() => expect(step()).toBe("status"));
    expect(
      screen.getByTestId("first-run-wizard-already-running").textContent,
    ).toBe("Already running on Kraken · BTC/USD. Nothing was started twice.");
    expect(surfaceText()).not.toContain(message);
    expect(paperClient.arm).toHaveBeenCalledTimes(1);
  });

  it("over_reserved without this revision's arm keeps the backend refusal", async () => {
    onStartPaper();
    const paperClient = fakePaper({
      arm: vi.fn().mockResolvedValue({
        ok: false,
        code: "over_reserved",
        message: "combined paper reservation would exceed account cash",
      }),
      getStatus: vi.fn().mockResolvedValue(krakenArm({ pack_id: "someone-else" })),
    });
    render(<FirstRunWizard paperClient={paperClient} />);

    fireEvent.click(screen.getByTestId("first-run-wizard-primary"));

    expect(
      (await screen.findByTestId("first-run-wizard-arm-error")).textContent,
    ).toBe("combined paper reservation would exceed account cash");
    expect(step()).toBe("start_paper");
    expect(screen.queryByTestId("first-run-wizard-already-running")).toBeNull();
  });
});

describe("FirstRunWizard retries a failed loopback session", () => {
  it("retries on the next action and keeps the chosen starter and venue", async () => {
    writeFirstRunWizardState(
      { status: "active", stepId: "choose", selectedStarterId: null },
      STORAGE,
    );
    const strategyClient = fakeStrategy();
    const paperClient = fakePaper();
    const retrySession = vi
      .fn<() => Promise<boolean>>()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    render(
      <FirstRunWizard
        strategyClient={strategyClient}
        paperClient={paperClient}
        loadStarters={async () => catalogue()}
        sessionReady={Promise.resolve(false)}
        retrySession={retrySession}
      />,
    );

    fireEvent.change(screen.getByTestId("first-run-wizard-market"), {
      target: { value: "coinbase" },
    });
    const use = "first-run-wizard-use-starter-mean-reversion-band";
    fireEvent.click(screen.getByTestId(use));
    expect(
      (await screen.findByTestId("first-run-wizard-choose-error")).textContent,
    ).toMatch(/session is unavailable\. Press Use again to retry/);
    expect(retrySession).toHaveBeenCalledTimes(1);
    expect(strategyClient.create).not.toHaveBeenCalled();
    expect(step()).toBe("choose");
    expect(
      (screen.getByTestId("first-run-wizard-market") as HTMLSelectElement).value,
    ).toBe("coinbase");

    fireEvent.click(screen.getByTestId(use));
    await waitFor(() => expect(step()).toBe("start_paper"));
    expect(retrySession).toHaveBeenCalledTimes(2);
    expect(strategyClient.create).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "starter-mean-reversion-band",
        markets: [{ venue: "coinbase", pair: "BTC-USD" }],
      }),
    );
    expect(readFirstRunWizardState(STORAGE)).toMatchObject({
      selectedStarterId: "starter-mean-reversion-band",
      marketVenue: "coinbase",
      revisionId: REVISION,
    });

    fireEvent.click(screen.getByTestId("first-run-wizard-primary"));
    await waitFor(() =>
      expect(paperClient.arm).toHaveBeenCalledWith(
        expect.objectContaining({ venue: "coinbase", revision_id: REVISION }),
      ),
    );
    expect(retrySession).toHaveBeenCalledTimes(2);
  });

  it("App retries the loopback session instead of keeping the first failed promise", async () => {
    loopback.ensureLoopbackSession.mockResolvedValue(false);
    loopback.retryLoopbackSession.mockResolvedValue(true);
    const posted: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : null;
        if (url.endsWith("/api/v1/packs/starters")) {
          return Response.json(catalogue());
        }
        if (url.endsWith("/api/v1/strategies/drafts") && init?.method === "POST") {
          posted.push(body);
          return Response.json({ revision_id: REVISION, state: "draft", pack: {}, errors: [] });
        }
        if (url.includes("/validate")) {
          return Response.json({ revision_id: REVISION, state: "validated", pack: {}, errors: [] });
        }
        return Response.json({ detail: "unused" }, { status: 404 });
      }),
    );

    render(<App />);
    fireEvent.click(screen.getByTestId("first-run-wizard-primary"));
    fireEvent.change(screen.getByTestId("first-run-wizard-market"), {
      target: { value: "coinbase" },
    });
    fireEvent.click(screen.getByTestId("first-run-wizard-use-starter-range-breakout"));

    await waitFor(() => expect(step()).toBe("start_paper"));
    expect(loopback.ensureLoopbackSession).toHaveBeenCalledTimes(1);
    expect(loopback.retryLoopbackSession).toHaveBeenCalledTimes(1);
    expect(posted).toEqual([
      expect.objectContaining({
        pack: expect.objectContaining({
          id: "starter-range-breakout",
          markets: [{ venue: "coinbase", pair: "BTC-USD" }],
        }),
      }),
    ]);
    expect(screen.getByTestId("first-run-wizard-market-summary").textContent).toContain(
      "Coinbase · BTC/USD",
    );
  });
});
