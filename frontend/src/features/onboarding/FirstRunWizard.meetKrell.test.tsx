/**
 * First run, "Meet Krell (connect your AI)" (M38.19).
 *
 * The welcome step's secondary choice opens the AI connect form in the
 * wizard frame. Only a usable, active connection completes the wizard and
 * opens the Krell tab, where Krell's own start produces the opening.
 * Cancel, Back, or an add that is not usable persists nothing.
 * Contract: KRELL-SIGNAL-20261010.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../../App";
import {
  FirstRunWizard,
  readFirstRunWizardState,
  writeFirstRunWizardState,
} from "./FirstRunWizard";
import {
  FIRST_RUN_WIZARD_STORAGE_KEY,
  type FirstRunWizardStepId,
} from "./firstRunWizardPersistence";
import { readWorkspaceSetupTourState } from "./workspaceSetupTourPersistence";

vi.mock("../../session", () => ({
  redeemBootstrap: (): Promise<void> => Promise.resolve(),
  recoverCsrf: (): Promise<boolean> => Promise.resolve(true),
  getCsrf: (): string => "test-csrf",
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

beforeEach(() => {
  window.localStorage.clear();
  window.history.pushState({}, "", "/");
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  window.history.pushState({}, "", "/");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const MEET_KRELL_LABEL = "Meet Krell (connect your AI)";
const NEW_ID = "c_bbbbbbbbbbbb";
const OTHER_ID = "c_aaaaaaaaaaaa";
const FAILED_MESSAGE = "The model did not answer the check.";
const DISCLOSURE =
  "Your conversation with Krell and the Krellbot data she reads are sent to Anthropic (api.anthropic.com). Nothing is sent to krellbot.dev.";
const OPENING = "Hey there. I'm Krell! Welcome to Krellbot.";

const PROVIDERS = [
  {
    id: "anthropic",
    label: "Anthropic",
    format: "anthropic_messages",
    base_url: "https://api.anthropic.com",
    custom: false,
    credential: "required",
  },
];

type Capability = "tools" | "chat_only" | "failed";
type Call = { method: string; path: string };

type Engine = {
  calls: Call[];
  /** How many times Krell's start (the opening) was requested. */
  starts(): number;
};

function frames(events: Array<Record<string, unknown>>): string {
  return events
    .map((e, i) => `id: ${i + 1}\r\nevent: ${String(e.type)}\r\ndata: ${JSON.stringify(e)}\r\n\r\n\r\n`)
    .join("");
}

/**
 * A fake engine for the AI and Krell routes. Everything else answers 404,
 * as an unused route would.
 */
function fakeEngine(
  options: {
    capability?: Capability;
    /** The connection already active before this add, if any. */
    active?: string | null;
    /** Hold the add's answer until this settles. */
    holdAdd?: Promise<void>;
  } = {},
): Engine {
  const capability = options.capability ?? "tools";
  let active = options.active ?? null;
  const calls: Call[] = [];
  const connection = (isActive: boolean) => ({
    id: NEW_ID,
    preset: "anthropic",
    label: "Anthropic",
    format: "anthropic_messages",
    base_url: "https://api.anthropic.com",
    host: "api.anthropic.com",
    model: "claude-test",
    auth: "key",
    capability,
    active: isActive,
    disclosure: DISCLOSURE,
  });
  const krellConnection = {
    id: NEW_ID,
    label: "Anthropic",
    host: "api.anthropic.com",
    model: "claude-test",
    capability,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const path = new URL(url, "http://127.0.0.1").pathname;
      const method = init?.method ?? "GET";
      calls.push({ method, path });
      if (path === "/api/v1/ai/providers") {
        return Response.json({ ok: true, providers: PROVIDERS });
      }
      if (path === "/api/v1/ai/connections" && method === "POST") {
        await options.holdAdd;
        return Response.json({
          ok: true,
          connection: connection(false),
          probe: {
            capability,
            message: capability === "failed" ? FAILED_MESSAGE : "",
          },
        });
      }
      if (path === "/api/v1/ai/connections") {
        return Response.json({ ok: true, active, connections: [] });
      }
      if (path === `/api/v1/ai/connections/${NEW_ID}/activate`) {
        active = NEW_ID;
        return Response.json({ ok: true, connection: connection(true) });
      }
      if (path === "/api/v1/krell/conversation") {
        return Response.json({
          schema_version: "1",
          ok: true,
          messages: [],
          profile: { mode: null, answers: {} },
          budget: null,
          connection: active === NEW_ID ? krellConnection : null,
          disclosure: null,
        });
      }
      if (path === "/api/v1/krell/start") {
        return new Response(
          frames([
            { type: "notice", kind: "disclosure", text: DISCLOSURE, connection: krellConnection },
            { type: "opening", text: OPENING, question: "Standard or advanced?", quick_replies: [] },
            { type: "done" },
          ]),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      }
      return Response.json({ detail: "unused" }, { status: 404 });
    }),
  );
  return {
    calls,
    starts: () =>
      calls.filter((call) => call.path === "/api/v1/krell/start").length,
  };
}

function wizardStep(): string | null {
  return screen.getByTestId("first-run-wizard").getAttribute("data-wizard-step");
}

async function openMeetKrell(): Promise<HTMLElement> {
  fireEvent.click(screen.getByTestId("first-run-wizard-meet-krell"));
  return screen.findByTestId("ai-connect-form");
}

async function connect(): Promise<void> {
  const form = await openMeetKrell();
  fireEvent.change(within(form).getByLabelText(/api key/i), {
    target: { value: "sk-test-not-a-real-key" },
  });
  fireEvent.change(within(form).getByLabelText(/^model$/i), {
    target: { value: "claude-test" },
  });
  fireEvent.click(within(form).getByRole("button", { name: /^connect$/i }));
}

function expectWelcome(): void {
  expect(wizardStep()).toBe("welcome");
  expect(screen.queryByTestId("ai-connect-form")).toBeNull();
  expect(screen.getByTestId("first-run-wizard-primary").textContent).toBe(
    "Let’s go",
  );
  expect(screen.getByTestId("first-run-wizard-meet-krell").textContent).toBe(
    MEET_KRELL_LABEL,
  );
}

describe("Meet Krell on the welcome step", () => {
  it("welcome shows Meet Krell as a secondary button beside the one primary", () => {
    const engine = fakeEngine();
    render(<FirstRunWizard />);
    const wizard = screen.getByTestId("first-run-wizard");
    expect(wizardStep()).toBe("welcome");

    const primaries = wizard.querySelectorAll(".kbot-first-run-wizard__primary");
    expect(primaries).toHaveLength(1);
    expect(primaries[0]!.textContent).toBe("Let’s go");

    const meet = screen.getByTestId("first-run-wizard-meet-krell");
    expect(meet.tagName).toBe("BUTTON");
    expect(meet.getAttribute("type")).toBe("button");
    expect(meet.classList.contains("kbot-first-run-wizard__secondary")).toBe(true);
    expect(meet.classList.contains("kbot-first-run-wizard__primary")).toBe(false);
    expect(meet.textContent).toBe(MEET_KRELL_LABEL);
    expect(screen.queryByTestId("first-run-wizard-skip")).toBeNull();
    // Nothing is asked of the engine until the user chooses Meet Krell.
    expect(engine.calls).toEqual([]);
  });

  it.each<FirstRunWizardStepId>(["choose", "start_paper", "status", "win"])(
    "offers Meet Krell on the welcome step only, not on %s",
    (stepId) => {
      writeFirstRunWizardState({
        status: "active",
        stepId,
        selectedStarterId: "starter-dual-ma-crossover",
      });
      render(<FirstRunWizard />);
      expect(wizardStep()).toBe(stepId);
      expect(screen.queryByTestId("first-run-wizard-meet-krell")).toBeNull();
    },
  );
});

describe("Connecting a model through Meet Krell", () => {
  it.each<Capability>(["tools", "chat_only"])(
    "connecting a %s model completes the wizard and opens the Krell tab",
    async (capability) => {
      const engine = fakeEngine({ capability });
      render(<App />);
      expect(screen.queryByTestId("kbot-shell")).toBeNull();

      await connect();

      await waitFor(() => {
        expect(screen.queryByTestId("first-run-wizard")).toBeNull();
      });
      expect(readFirstRunWizardState().status).toBe("completed");
      expect(new URLSearchParams(window.location.search).get("view")).toBe(
        "krell",
      );
      const view = screen.getByTestId("krell-view");
      expect(await within(view).findByText(OPENING)).toBeDefined();
      // Krell's tab asks for the opening once; the wizard never does.
      expect(engine.starts()).toBe(1);
      // Krell takes over the first run: the workspace tour is not armed.
      expect(readWorkspaceSetupTourState().status).toBe("idle");
    },
  );

  it.each([
    ["a failed probe", { capability: "failed" as const }, "ai-capability-failed"],
    [
      "another model staying active",
      { capability: "tools" as const, active: OTHER_ID },
      "ai-capability-tools",
    ],
  ])(
    "an unusable add stays in the wizard (%s) and Back returns to welcome",
    async (_label, options, statusId) => {
      const engine = fakeEngine(options);
      const onMeetKrell = vi.fn();
      const onComplete = vi.fn();
      render(<FirstRunWizard onMeetKrell={onMeetKrell} onComplete={onComplete} />);

      await connect();

      // The form's own message, in the wizard frame.
      const status = await screen.findByTestId(statusId);
      if (options.capability === "failed") {
        expect(status.textContent).toBe(FAILED_MESSAGE);
      }
      expect(wizardStep()).toBe("welcome");
      expect(screen.getByTestId("ai-connect-form")).toBeDefined();
      expect(engine.calls.some((call) => call.method === "POST")).toBe(true);
      expect(onMeetKrell).not.toHaveBeenCalled();
      expect(onComplete).not.toHaveBeenCalled();
      expect(window.localStorage.getItem(FIRST_RUN_WIZARD_STORAGE_KEY)).toBeNull();
      expect(window.location.search).toBe("");

      fireEvent.click(screen.getByTestId("first-run-wizard-meet-krell-back"));
      expectWelcome();
      expect(window.localStorage.getItem(FIRST_RUN_WIZARD_STORAGE_KEY)).toBeNull();
      expect(onMeetKrell).not.toHaveBeenCalled();
    },
  );

  it.each([
    ["the form's Cancel", () => within(screen.getByTestId("ai-connect-form")).getByRole("button", { name: "Cancel" })],
    ["Back", () => screen.getByTestId("first-run-wizard-meet-krell-back")],
  ])("cancelling with %s returns to welcome with nothing persisted", async (_label, control) => {
    const engine = fakeEngine();
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const removeItem = vi.spyOn(Storage.prototype, "removeItem");
    const onMeetKrell = vi.fn();
    render(<FirstRunWizard onMeetKrell={onMeetKrell} />);

    await openMeetKrell();
    expect(wizardStep()).toBe("welcome");
    fireEvent.click(control());

    expectWelcome();
    expect(setItem).not.toHaveBeenCalled();
    expect(removeItem).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(FIRST_RUN_WIZARD_STORAGE_KEY)).toBeNull();
    // Only reads: no connection was added, activated or removed.
    expect(engine.calls.length).toBeGreaterThan(0);
    expect(engine.calls.every((call) => call.method === "GET")).toBe(true);
    expect(onMeetKrell).not.toHaveBeenCalled();
    expect(window.location.search).toBe("");
  });

  it("ignores a connect that answers after Back", async () => {
    let release = (): void => {};
    const holdAdd = new Promise<void>((resolve) => {
      release = resolve;
    });
    const engine = fakeEngine({ holdAdd });
    const onMeetKrell = vi.fn();
    render(<FirstRunWizard onMeetKrell={onMeetKrell} />);

    await connect();
    fireEvent.click(screen.getByTestId("first-run-wizard-meet-krell-back"));
    expectWelcome();
    release();

    // The add the user made still finishes on the engine side.
    await waitFor(() => {
      expect(engine.calls.some((call) => call.path.endsWith("/activate"))).toBe(true);
    });
    expectWelcome();
    expect(onMeetKrell).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(FIRST_RUN_WIZARD_STORAGE_KEY)).toBeNull();
  });

  it("ignores a connect from before Back even after Meet Krell is opened again", async () => {
    let release = (): void => {};
    const holdAdd = new Promise<void>((resolve) => {
      release = resolve;
    });
    const engine = fakeEngine({ holdAdd });
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const onMeetKrell = vi.fn();
    render(<FirstRunWizard onMeetKrell={onMeetKrell} />);

    await connect();
    fireEvent.click(screen.getByTestId("first-run-wizard-meet-krell-back"));
    expectWelcome();
    const reopened = await openMeetKrell();
    release();

    // The first add still finishes on the engine side and reports usable.
    await waitFor(() => {
      expect(engine.calls.some((call) => call.path.endsWith("/activate"))).toBe(true);
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(onMeetKrell).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(FIRST_RUN_WIZARD_STORAGE_KEY)).toBeNull();
    expect(window.location.search).toBe("");
    // The reopened frame is still the one on screen, untouched.
    expect(wizardStep()).toBe("welcome");
    expect(screen.getByTestId("ai-connect-form")).toBe(reopened);
    expect(within(reopened).queryByRole("status")).toBeNull();
  });

  it("waits for the loopback session, and retries a failed one, before asking for providers", async () => {
    const engine = fakeEngine();
    const retrySession = vi
      .fn<() => Promise<boolean>>()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    render(
      <FirstRunWizard
        sessionReady={Promise.resolve(false)}
        retrySession={retrySession}
      />,
    );

    fireEvent.click(screen.getByTestId("first-run-wizard-meet-krell"));
    const alert = await screen.findByTestId("first-run-wizard-meet-krell-error");
    expect(alert.getAttribute("role")).toBe("alert");
    expect(alert.textContent).toBe(
      "The local workstation session is unavailable. Press Meet Krell again to retry.",
    );
    expect(screen.queryByTestId("ai-connect-form")).toBeNull();
    expect(engine.calls).toEqual([]);

    await openMeetKrell();
    expect(retrySession).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("first-run-wizard-meet-krell-error")).toBeNull();
    expect(engine.calls[0]).toEqual({ method: "GET", path: "/api/v1/ai/providers" });
    expect(window.localStorage.getItem(FIRST_RUN_WIZARD_STORAGE_KEY)).toBeNull();
  });
});
