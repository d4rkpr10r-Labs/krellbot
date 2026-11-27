/**
 * Sign in with OpenRouter from the connect form (M38.20).
 *
 * The engine returns an authorize URL; the form opens it in a new tab
 * (noopener), polls the sign-in status every 2 s while it is `waiting`, and
 * on `connected` runs the same probe, activate and onConnected path as a key
 * add. `failed` and `expired` show the server's message. Stopping, leaving
 * and giving up cancel the sign-in on the engine, so an abandoned sign-in can
 * never add a connection later. Fetch is a fake.
 */
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AiConnectForm } from "./AiConnectForm";

vi.mock("../../session", () => ({
  getCsrf: (): string => "test-csrf",
}));

const FLOW_ID = "0123456789abcdef0123456789abcdef";
const AUTHORIZE_URL =
  "https://openrouter.ai/auth?callback_url=http%3A%2F%2F127.0.0.1%3A51423%2Fcallback%2Ftok&code_challenge=abc&code_challenge_method=S256";

const PROVIDERS = [
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
  {
    id: "anthropic",
    label: "Anthropic",
    format: "anthropic_messages",
    base_url: "https://api.anthropic.com",
    custom: false,
    signin: "none",
    lists_models: true,
    credential: "required",
  },
];

function conn(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "aaaaaaaaaaaaaaaa",
    preset: "openrouter",
    label: "OpenRouter",
    format: "openai_compatible",
    base_url: "https://openrouter.ai/api/v1",
    host: "openrouter.ai",
    model: "openrouter/auto",
    auth: "oauth",
    capability: "tools",
    created_at: "2026-11-27T00:00:00Z",
    active: false,
    disclosure: "sent to OpenRouter (openrouter.ai)",
    ...over,
  };
}

type Call = { method: string; path: string; body: unknown; at: number };
type Reply = { status?: number; json: unknown };

function ok(body: Record<string, unknown>): Reply {
  return { json: { schema_version: "1", ok: true, effect: "unchanged", ...body } };
}

function installFetch(handler: (call: Call) => Reply | Promise<Reply>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call: Call = {
        method: init?.method ?? "GET",
        path: String(input),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        at: Date.now(),
      };
      calls.push(call);
      const out = await handler(call);
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
  render(<AiConnectForm onConnected={onConnected} onAdded={onAdded} {...props} />);
  await advance(0);
  return { onConnected, onAdded };
}

async function signIn(model = "openrouter/auto"): Promise<void> {
  fireEvent.change(screen.getByLabelText(/^model$/i), {
    target: { value: model },
  });
  fireEvent.click(screen.getByRole("button", { name: /sign in with openrouter/i }));
  // Let the start request resolve so the 2 s poll timer starts at "now".
  await advance(0);
}

const statusCalls = (calls: Call[]): Call[] =>
  calls.filter((c) => c.method === "GET" && c.path === `/api/v1/ai/signin/${FLOW_ID}`);
const cancelCalls = (calls: Call[]): Call[] =>
  calls.filter((c) => c.method === "DELETE" && c.path === `/api/v1/ai/signin/${FLOW_ID}`);

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const STARTED = (): Reply =>
  ok({ effect: "changed", flow_id: FLOW_ID, authorize_url: AUTHORIZE_URL });

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

describe("AiConnectForm sign in with OpenRouter", () => {
  it("offers the sign-in only for OpenRouter", async () => {
    installFetch(() => ok({ providers: PROVIDERS }));
    await renderForm();
    expect(
      screen.getByRole("button", { name: /sign in with openrouter/i }),
    ).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/^provider$/i), {
      target: { value: "anthropic" },
    });
    expect(
      screen.queryByRole("button", { name: /sign in with openrouter/i }),
    ).toBeNull();
  });

  it("opens OpenRouter with noopener, polls every 2 s, then probes and activates", async () => {
    const statuses = ["waiting", "connected"];
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/signin/openrouter") {
        return ok({ effect: "changed", flow_id: FLOW_ID, authorize_url: AUTHORIZE_URL });
      }
      if (c.path === `/api/v1/ai/signin/${FLOW_ID}`) {
        const state = statuses.shift() ?? "connected";
        return ok({
          flow_id: FLOW_ID,
          state,
          ...(state === "connected" ? { connection_id: "aaaaaaaaaaaaaaaa" } : {}),
        });
      }
      if (c.path === "/api/v1/ai/connections/aaaaaaaaaaaaaaaa/probe") {
        return ok({
          effect: "changed",
          connection: conn(),
          probe: { capability: "tools", message: "ok", models: [], error: null },
        });
      }
      if (c.path === "/api/v1/ai/connections" && c.method === "GET") {
        return ok({ active: null, connections: [conn()] });
      }
      if (c.path === "/api/v1/ai/connections/aaaaaaaaaaaaaaaa/activate") {
        return ok({ effect: "changed", active: "aaaaaaaaaaaaaaaa", connection: conn({ active: true }) });
      }
      throw new Error(`unexpected ${c.method} ${c.path}`);
    });
    const { onConnected, onAdded } = await renderForm();

    await signIn("openrouter/auto");
    await advance(0);
    const started = calls.find((c) => c.path === "/api/v1/ai/signin/openrouter");
    expect(started?.method).toBe("POST");
    expect(started?.body).toEqual({ model: "openrouter/auto" });
    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(openSpy).toHaveBeenCalledWith(AUTHORIZE_URL, "_blank", "noopener,noreferrer");
    expect(screen.getByTestId("ai-signin-waiting")).toBeTruthy();
    const fallback = screen.getByRole("link", { name: /open the openrouter sign-in page/i });
    expect(fallback.getAttribute("href")).toBe(AUTHORIZE_URL);
    expect(fallback.getAttribute("rel")).toBe("noopener noreferrer");

    await advance(1999);
    expect(statusCalls(calls)).toHaveLength(0);
    await advance(1);
    expect(statusCalls(calls)).toHaveLength(1);
    expect(onConnected).not.toHaveBeenCalled();

    await advance(2000);
    expect(statusCalls(calls)).toHaveLength(2);
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      "GET /api/v1/ai/providers",
      "POST /api/v1/ai/signin/openrouter",
      `GET /api/v1/ai/signin/${FLOW_ID}`,
      `GET /api/v1/ai/signin/${FLOW_ID}`,
      "POST /api/v1/ai/connections/aaaaaaaaaaaaaaaa/probe",
      "GET /api/v1/ai/connections",
      "POST /api/v1/ai/connections/aaaaaaaaaaaaaaaa/activate",
    ]);
    expect(onAdded).toHaveBeenCalledTimes(1);
    expect(onConnected).toHaveBeenCalledTimes(1);
    expect(onConnected.mock.calls[0][0]).toMatchObject({ id: "aaaaaaaaaaaaaaaa", active: true });
    expect(screen.getByTestId("ai-capability-tools")).toBeTruthy();
    expect(screen.queryByTestId("ai-signin-waiting")).toBeNull();

    // Polling stopped.
    await advance(10_000);
    expect(statusCalls(calls)).toHaveLength(2);
  });

  it.each([
    ["failed", "ai_signin_rejected", "OpenRouter did not accept the sign-in, so nothing was stored."],
    ["expired", "ai_signin_expired", "The sign-in was not finished within 5 minutes. Start it again."],
  ])("shows the message and stops polling when the sign-in is %s", async (state, code, detail) => {
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/signin/openrouter") {
        return ok({ effect: "changed", flow_id: FLOW_ID, authorize_url: AUTHORIZE_URL });
      }
      if (c.path === `/api/v1/ai/signin/${FLOW_ID}`) {
        return ok({ flow_id: FLOW_ID, state, code, detail });
      }
      throw new Error(`unexpected ${c.method} ${c.path}`);
    });
    const { onConnected, onAdded } = await renderForm();
    await signIn();
    await advance(2000);
    expect(screen.getByRole("alert").textContent).toBe(`${detail} (${code})`);
    expect(screen.queryByTestId("ai-signin-waiting")).toBeNull();
    await advance(10_000);
    expect(statusCalls(calls)).toHaveLength(1);
    expect(onConnected).not.toHaveBeenCalled();
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("shows a refusal to start and opens nothing", async () => {
    installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      return {
        status: 400,
        json: { ok: false, code: "ai_model_invalid", detail: "Enter the model name the provider uses." },
      };
    });
    await renderForm();
    await signIn("");
    await advance(0);
    expect(screen.getByRole("alert").textContent).toBe(
      "Enter the model name the provider uses. (ai_model_invalid)",
    );
    expect(openSpy).not.toHaveBeenCalled();
  });

  it("never opens an authorize URL that is not OpenRouter's", async () => {
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/signin/openrouter") {
        return ok({ effect: "changed", flow_id: FLOW_ID, authorize_url: "https://evil.example/auth?x=1" });
      }
      throw new Error(`unexpected ${c.method} ${c.path}`);
    });
    await renderForm();
    await signIn();
    await advance(4000);
    expect(openSpy).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /openrouter sign-in page/i })).toBeNull();
    expect(statusCalls(calls)).toHaveLength(0);
  });

  it("cancels the sign-in on the engine and stops polling when the user stops waiting", async () => {
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/signin/openrouter") return STARTED();
      if (c.method === "DELETE") {
        return ok({
          effect: "changed",
          cancelled: true,
          flow_id: FLOW_ID,
          state: "failed",
          code: "ai_signin_cancelled",
          detail: "The sign-in was cancelled, so nothing was stored.",
        });
      }
      return ok({ flow_id: FLOW_ID, state: "waiting" });
    });
    const { onAdded } = await renderForm();
    await signIn();
    await advance(2000);
    expect(statusCalls(calls)).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /stop waiting/i }));
    await advance(0);
    expect(cancelCalls(calls)).toHaveLength(1);
    expect(cancelCalls(calls)[0].body).toBeUndefined();
    await advance(10_000);
    expect(statusCalls(calls)).toHaveLength(1);
    expect(screen.queryByTestId("ai-signin-waiting")).toBeNull();
    const button = screen.getByRole("button", { name: /sign in with openrouter/i });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    // Unmounting later does not cancel again.
    cleanup();
    await advance(0);
    expect(cancelCalls(calls)).toHaveLength(1);
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("keeps following a sign-in whose callback already arrived when the user stops waiting", async () => {
    const statuses = ["waiting", "connected"];
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/signin/openrouter") return STARTED();
      if (c.method === "DELETE") {
        return ok({ cancelled: false, flow_id: FLOW_ID, state: "waiting" });
      }
      if (c.path === `/api/v1/ai/signin/${FLOW_ID}`) {
        const state = statuses.shift() ?? "connected";
        return ok({
          flow_id: FLOW_ID,
          state,
          ...(state === "connected" ? { connection_id: "aaaaaaaaaaaaaaaa" } : {}),
        });
      }
      if (c.path.endsWith("/probe")) {
        return ok({ effect: "changed", connection: conn(), probe: { capability: "tools", message: "ok" } });
      }
      if (c.path === "/api/v1/ai/connections") return ok({ active: null, connections: [conn()] });
      if (c.path.endsWith("/activate")) {
        return ok({ effect: "changed", active: "aaaaaaaaaaaaaaaa", connection: conn({ active: true }) });
      }
      throw new Error(`unexpected ${c.method} ${c.path}`);
    });
    const { onConnected, onAdded } = await renderForm();
    await signIn();
    await advance(2000);
    fireEvent.click(screen.getByRole("button", { name: /stop waiting/i }));
    await advance(0);
    expect(cancelCalls(calls)).toHaveLength(1);
    expect(screen.getByTestId("ai-signin-waiting")).toBeTruthy();
    await advance(2000);
    expect(statusCalls(calls)).toHaveLength(2);
    expect(onAdded).toHaveBeenCalledTimes(1);
    expect(onConnected).toHaveBeenCalledTimes(1);
  });

  it("hides Stop waiting during the post-connect probe and never leaves the controls disabled", async () => {
    const probe = deferred<Reply>();
    installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/signin/openrouter") return STARTED();
      if (c.path === `/api/v1/ai/signin/${FLOW_ID}` && c.method === "GET") {
        return ok({ flow_id: FLOW_ID, state: "connected", connection_id: "aaaaaaaaaaaaaaaa" });
      }
      if (c.path.endsWith("/probe")) return probe.promise;
      if (c.path === "/api/v1/ai/connections") return ok({ active: null, connections: [conn()] });
      if (c.path.endsWith("/activate")) {
        return ok({ effect: "changed", active: "aaaaaaaaaaaaaaaa", connection: conn({ active: true }) });
      }
      throw new Error(`unexpected ${c.method} ${c.path}`);
    });
    const { onConnected } = await renderForm();
    await signIn();
    expect(screen.getByRole("button", { name: /stop waiting/i })).toBeTruthy();
    await advance(2000);
    // The probe is in flight: nothing can strand the form in its busy state.
    expect(screen.queryByRole("button", { name: /stop waiting/i })).toBeNull();
    probe.resolve(
      ok({ effect: "changed", connection: conn(), probe: { capability: "tools", message: "ok" } }),
    );
    await advance(0);
    expect(onConnected).toHaveBeenCalledTimes(1);
    for (const name of [/^connect$/i, /sign in with openrouter/i]) {
      expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(false);
    }
    expect((screen.getByLabelText(/^model$/i) as HTMLInputElement).disabled).toBe(false);
    expect((screen.getByLabelText(/^provider$/i) as HTMLSelectElement).disabled).toBe(false);
  });

  it("cancels the sign-in on the engine when the form goes away", async () => {
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/signin/openrouter") return STARTED();
      if (c.method === "DELETE") return ok({ cancelled: true, flow_id: FLOW_ID, state: "failed" });
      return ok({ flow_id: FLOW_ID, state: "waiting" });
    });
    await renderForm();
    await signIn();
    await advance(2000);
    cleanup();
    await advance(0);
    expect(cancelCalls(calls)).toHaveLength(1);
    await advance(10_000);
    expect(statusCalls(calls)).toHaveLength(1);
  });

  it("cancels the sign-in on the engine when it gives up waiting", async () => {
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/signin/openrouter") return STARTED();
      if (c.method === "DELETE") return ok({ cancelled: true, flow_id: FLOW_ID, state: "failed" });
      return ok({ flow_id: FLOW_ID, state: "waiting" });
    });
    await renderForm();
    await signIn();
    await advance(400_000);
    expect(screen.getByRole("alert").textContent).toBe("The sign-in did not finish. Start it again.");
    expect(cancelCalls(calls)).toHaveLength(1);
    const polls = statusCalls(calls).length;
    expect(polls).toBeLessThanOrEqual(170);
    await advance(10_000);
    expect(statusCalls(calls)).toHaveLength(polls);
  });

  it("disables the sign-in with a one-line reason when the engine says it is local-only", async () => {
    const reason =
      "Sign in with OpenRouter from Krellbot on this computer. OpenRouter returns to this computer's own address, which a remote browser cannot reach.";
    installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      return { status: 403, json: { ok: false, effect: "refused", code: "ai_signin_local_only", detail: reason } };
    });
    await renderForm();
    await signIn();
    const button = screen.getByRole("button", { name: /sign in with openrouter/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByTestId("ai-signin-unavailable").textContent).toBe(reason);
    expect(screen.queryByTestId("ai-signin-note")).toBeNull();
    expect(openSpy).not.toHaveBeenCalled();
    // Connecting with a key still works.
    expect((screen.getByRole("button", { name: /^connect$/i }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("still reports the stored connection through onAdded when the post-connect probe fails", async () => {
    const stored = conn({ capability: "unprobed" });
    installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/signin/openrouter") return STARTED();
      if (c.path === `/api/v1/ai/signin/${FLOW_ID}`) {
        return ok({ flow_id: FLOW_ID, state: "connected", connection_id: "aaaaaaaaaaaaaaaa" });
      }
      if (c.path.endsWith("/probe")) {
        return {
          status: 503,
          json: {
            ok: false,
            effect: "refused",
            code: "ai_keychain_unavailable",
            detail: "The OS keychain is not available, so the key was not stored or changed.",
          },
        };
      }
      if (c.path === "/api/v1/ai/connections") return ok({ active: null, connections: [stored] });
      throw new Error(`unexpected ${c.method} ${c.path}`);
    });
    const { onConnected, onAdded } = await renderForm();
    await signIn();
    await advance(2000);
    expect(screen.getByRole("alert").textContent).toBe(
      "The OS keychain is not available, so the key was not stored or changed. (ai_keychain_unavailable)",
    );
    expect(onAdded).toHaveBeenCalledTimes(1);
    expect(onAdded.mock.calls[0][0]).toMatchObject({ id: "aaaaaaaaaaaaaaaa", auth: "oauth" });
    expect(onAdded.mock.calls[0][1]).toEqual({
      capability: "unprobed",
      message: "The OS keychain is not available, so the key was not stored or changed.",
    });
    expect(onConnected).not.toHaveBeenCalled();
    expect(
      (screen.getByRole("button", { name: /sign in with openrouter/i }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });
});
