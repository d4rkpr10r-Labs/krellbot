/**
 * Continue with ChatGPT from the connect form (M38.23). Contract:
 * KRELL-SIGNAL-20261010.
 *
 * The button sits beside Connect for the OpenAI provider and follows Sign in
 * with OpenRouter's start, waiting, cancel and done pattern. The engine
 * returns its own loopback launch address (which redirects to OpenAI), so
 * the form opens only that. A sign-in that did not allow ChatGPT plan use is
 * kept but not probed, and the form explains the API-key alternative. A
 * connection's first plan-enabled sign-in shows "You're using your ChatGPT
 * plan" with Got it, once: the engine records it. Fetch is a fake.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AiConnectForm } from "./AiConnectForm";

vi.mock("../../session", () => ({
  getCsrf: (): string => "test-csrf",
}));

const FLOW_ID = "0123456789abcdef0123456789abcdef";
const CONN_ID = "bbbbbbbbbbbbbbbb";
const LAUNCH_URL = "http://127.0.0.1:51455/auth/start/Launch-Token_123";

const PROVIDERS = [
  {
    id: "openai",
    label: "OpenAI",
    format: "openai_compatible",
    base_url: "https://api.openai.com/v1",
    custom: false,
    signin: "chatgpt",
    lists_models: true,
    credential: "required",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    format: "openai_compatible",
    base_url: "https://openrouter.ai/api/v1",
    custom: false,
    signin: "openrouter",
    lists_models: true,
    credential: "required",
  },
];

function conn(over: Record<string, unknown> = {}, chatgpt: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: CONN_ID,
    preset: "openai",
    label: "OpenAI (ChatGPT plan)",
    format: "openai_compatible",
    base_url: "https://api.openai.com/v1",
    host: "api.openai.com",
    model: "gpt-plan-a",
    auth: "oauth",
    capability: "tools",
    created_at: "2026-11-27T00:00:00Z",
    active: false,
    disclosure: "sent to OpenAI (ChatGPT plan) (api.openai.com)",
    chatgpt: { plan: "enabled", signed_in: true, welcome: true, account: "trader@example.com", ...chatgpt },
    ...over,
  };
}

type Call = { method: string; path: string; body: unknown };
type Reply = { status?: number; json: unknown };

function ok(body: Record<string, unknown>): Reply {
  return { json: { schema_version: "1", ok: true, effect: "unchanged", ...body } };
}

function installFetch(handler: (call: Call) => Reply): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call: Call = {
        method: init?.method ?? "GET",
        path: String(input),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      };
      calls.push(call);
      const out = handler(call);
      return new Response(JSON.stringify(out.json), {
        status: out.status ?? 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function renderForm(props: Partial<Parameters<typeof AiConnectForm>[0]> = {}) {
  const onConnected = vi.fn();
  const onAdded = vi.fn();
  const view = render(<AiConnectForm onConnected={onConnected} onAdded={onAdded} {...props} />);
  await advance(0);
  return { onConnected, onAdded, view };
}

function continueButton(): HTMLButtonElement | null {
  return screen.queryByRole("button", { name: "Continue with ChatGPT" }) as HTMLButtonElement | null;
}

async function continueWithChatgpt(model = "gpt-plan-a"): Promise<void> {
  fireEvent.change(screen.getByLabelText(/^model$/i), { target: { value: model } });
  fireEvent.click(continueButton() as HTMLButtonElement);
  await advance(0);
}

/** The engine's answers for one sign-in that connects with `plan`. */
function engine(plan: "enabled" | "disabled", chatgpt: Record<string, unknown> = {}) {
  return (c: Call): Reply => {
    if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
    if (c.path === "/api/v1/ai/signin/chatgpt") {
      return ok({ effect: "changed", flow_id: FLOW_ID, authorize_url: LAUNCH_URL });
    }
    if (c.path === `/api/v1/ai/signin/${FLOW_ID}` && c.method === "GET") {
      return ok({ flow_id: FLOW_ID, state: "connected", connection_id: CONN_ID, plan });
    }
    if (c.path === `/api/v1/ai/signin/${FLOW_ID}` && c.method === "DELETE") {
      return ok({ effect: "changed", cancelled: true, flow_id: FLOW_ID, state: "failed", code: "ai_signin_cancelled" });
    }
    if (c.path === `/api/v1/ai/connections/${CONN_ID}/probe`) {
      return ok({
        effect: "changed",
        connection: conn({}, chatgpt),
        probe: { capability: "tools", message: "ok", models: ["gpt-plan-a"], error: null },
      });
    }
    if (c.path === "/api/v1/ai/connections" && c.method === "GET") {
      const capability = plan === "disabled" ? "unprobed" : "tools";
      return ok({ active: null, connections: [conn({ capability }, { plan, ...chatgpt })] });
    }
    if (c.path === `/api/v1/ai/connections/${CONN_ID}/activate`) {
      return ok({ effect: "changed", active: CONN_ID, connection: conn({ active: true }, { welcome: false }) });
    }
    if (c.path === `/api/v1/ai/connections/${CONN_ID}/welcome`) return ok({ effect: "changed" });
    throw new Error(`unexpected ${c.method} ${c.path}`);
  };
}

let openSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  vi.unstubAllGlobals();
  openSpy = vi.fn(() => null);
  vi.stubGlobal("open", openSpy);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("AiConnectForm Continue with ChatGPT", () => {
  it("offers Continue with ChatGPT beside Connect for OpenAI only, as a text label", async () => {
    installFetch(engine("enabled"));
    await renderForm();
    const button = continueButton();
    expect(button).toBeTruthy();
    expect(button?.textContent).toBe("Continue with ChatGPT");
    expect(button?.querySelector("img, svg")).toBeNull();
    expect(button?.parentElement?.contains(screen.getByRole("button", { name: "Connect" }))).toBe(true);
    expect(screen.getByTestId("ai-chatgpt-note").textContent).toContain("ChatGPT plan");
    expect(screen.queryByRole("button", { name: /sign in with openrouter/i })).toBeNull();

    fireEvent.change(screen.getByLabelText(/^provider$/i), { target: { value: "openrouter" } });
    expect(continueButton()).toBeNull();
  });

  it("opens only the engine's launch address, waits, and Stop waiting cancels the sign-in", async () => {
    const calls = installFetch((c) => {
      if (c.path === `/api/v1/ai/signin/${FLOW_ID}` && c.method === "GET") return ok({ flow_id: FLOW_ID, state: "waiting" });
      return engine("enabled")(c);
    });
    await renderForm();
    await continueWithChatgpt("gpt-plan-a");

    const started = calls.find((c) => c.path === "/api/v1/ai/signin/chatgpt");
    expect(started?.method).toBe("POST");
    expect(started?.body).toEqual({ model: "gpt-plan-a" });
    expect(openSpy).toHaveBeenCalledWith(LAUNCH_URL, "_blank", "noopener,noreferrer");
    expect(screen.getByRole("status").textContent).toBe("Waiting for ChatGPT. Finish signing in on the tab that opened.");
    const fallback = screen.getByRole("link", { name: "Open the ChatGPT sign-in page" });
    expect(fallback.getAttribute("href")).toBe(LAUNCH_URL);
    expect(continueButton()?.disabled).toBe(true);

    await advance(2000);
    fireEvent.click(screen.getByRole("button", { name: "Stop waiting" }));
    await advance(0);
    expect(calls.filter((c) => c.method === "DELETE" && c.path === `/api/v1/ai/signin/${FLOW_ID}`)).toHaveLength(1);
    expect(screen.queryByTestId("ai-signin-waiting")).toBeNull();
    expect(continueButton()?.disabled).toBe(false);
  });

  it("cancels the sign-in when the person goes back while it waits", async () => {
    const calls = installFetch((c) => {
      if (c.path === `/api/v1/ai/signin/${FLOW_ID}` && c.method === "GET") return ok({ flow_id: FLOW_ID, state: "waiting" });
      return engine("enabled")(c);
    });
    const onCancel = vi.fn();
    const { view } = await renderForm({ onCancel });
    await continueWithChatgpt();
    const back = screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement;
    expect(back.disabled).toBe(false);
    fireEvent.click(back);
    expect(onCancel).toHaveBeenCalledTimes(1);
    view.unmount();
    await advance(0);
    expect(calls.filter((c) => c.method === "DELETE" && c.path === `/api/v1/ai/signin/${FLOW_ID}`)).toHaveLength(1);
  });

  it("refuses to open an address that is not the engine's launch address", async () => {
    installFetch((c) =>
      c.path === "/api/v1/ai/signin/chatgpt"
        ? ok({ effect: "changed", flow_id: FLOW_ID, authorize_url: "https://auth.openai.com/api/accounts/authorize?client_id=x" })
        : engine("enabled")(c),
    );
    await renderForm();
    await continueWithChatgpt();
    expect(openSpy).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("did not open it");
    expect(screen.queryByTestId("ai-signin-waiting")).toBeNull();
  });

  it("keeps a sign-in without plan use, does not probe it, and explains the API-key alternative", async () => {
    const calls = installFetch(engine("disabled"));
    const { onConnected, onAdded } = await renderForm();
    await continueWithChatgpt();
    await advance(2000);

    expect(calls.some((c) => c.path.endsWith("/probe"))).toBe(false);
    const note = screen.getByTestId("ai-chatgpt-plan-disabled");
    expect(note.textContent).toContain("plan use was not allowed");
    expect(note.textContent).toContain("OpenAI API key");
    expect(onAdded).toHaveBeenCalledTimes(1);
    expect(onAdded.mock.calls[0][0]).toMatchObject({ id: CONN_ID, chatgpt: { plan: "disabled" } });
    expect(onConnected).not.toHaveBeenCalled();
    expect(screen.queryByTestId("ai-chatgpt-welcome")).toBeNull();
  });

  it("shows You're using your ChatGPT plan once, and the engine records Got it", async () => {
    const calls = installFetch(engine("enabled"));
    const { onConnected } = await renderForm();
    await continueWithChatgpt();
    await advance(2000);

    const dialog = screen.getByRole("dialog", { name: "You're using your ChatGPT plan" });
    expect(dialog.textContent).toContain("ChatGPT settings");
    const gotIt = screen.getByRole("button", { name: "Got it" });
    expect(gotIt.className).toContain("kbot-settings__btn--primary");
    // The dialog is the surface's only primary, and the form waits behind it.
    expect(document.querySelectorAll(".kbot-settings__btn--primary")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
    expect(onConnected).not.toHaveBeenCalled();

    fireEvent.click(gotIt);
    await advance(0);
    expect(calls.filter((c) => c.method === "POST" && c.path === `/api/v1/ai/connections/${CONN_ID}/welcome`)).toHaveLength(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onConnected).toHaveBeenCalledTimes(1);
    expect(onConnected.mock.calls[0][0]).toMatchObject({ id: CONN_ID, active: true });
    expect(screen.getByTestId("ai-capability-tools")).toBeTruthy();
  });

  it("does not show the welcome again once the engine says it was shown", async () => {
    const calls = installFetch(engine("enabled", { welcome: false }));
    const { onConnected } = await renderForm();
    await continueWithChatgpt();
    await advance(2000);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls.some((c) => c.path.endsWith("/welcome"))).toBe(false);
    expect(onConnected).toHaveBeenCalledTimes(1);
  });

  it("signs an existing ChatGPT connection in again by its connection id", async () => {
    const calls = installFetch(engine("enabled", { welcome: false }));
    const onAdded = vi.fn();
    render(
      <AiConnectForm
        reauth={{
          id: CONN_ID,
          preset: "openai",
          label: "OpenAI (ChatGPT plan)",
          format: "openai_compatible",
          baseUrl: "https://api.openai.com/v1",
          host: "api.openai.com",
          model: "gpt-plan-a",
          auth: "oauth",
          capability: "tools",
          active: true,
          disclosure: "",
          chatgpt: { plan: "enabled", signedIn: false, welcome: false, account: "trader@example.com" },
        }}
        onConnected={vi.fn()}
        onAdded={onAdded}
      />,
    );
    await advance(0);
    fireEvent.click(continueButton() as HTMLButtonElement);
    await advance(0);
    const started = calls.find((c) => c.path === "/api/v1/ai/signin/chatgpt");
    expect(started?.body).toEqual({ connection_id: CONN_ID });
    expect(openSpy).toHaveBeenCalledWith(LAUNCH_URL, "_blank", "noopener,noreferrer");
    await advance(2000);
    expect(onAdded).toHaveBeenCalledTimes(1);
  });
});
