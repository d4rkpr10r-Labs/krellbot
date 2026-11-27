/**
 * AI model settings: connect, check, activate and remove (M38.13).
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AiConnectForm } from "./AiConnectForm";
import { AiModelSection } from "./AiModelSection";

vi.mock("../../session", () => ({
  getCsrf: (): string => "test-csrf",
}));

const SECRET = "sk-ant-SECRET-1234567890";

const PROVIDERS = [
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
    id: "custom_openai",
    label: "Custom OpenAI-compatible endpoint",
    format: "openai_compatible",
    base_url: null,
    custom: true,
    signin: "none",
    lists_models: true,
    credential: "optional",
  },
  {
    id: "custom_anthropic",
    label: "Custom Anthropic-compatible endpoint",
    format: "anthropic_messages",
    base_url: null,
    custom: true,
    signin: "none",
    lists_models: false,
    credential: "optional",
  },
];

type Conn = Record<string, unknown>;

function conn(over: Conn = {}): Conn {
  return {
    id: "c_aaaaaaaaaaaa",
    preset: "anthropic",
    label: "Anthropic",
    format: "anthropic_messages",
    base_url: "https://api.anthropic.com",
    host: "api.anthropic.com",
    model: "claude-test",
    auth: "key",
    capability: "tools",
    created_at: "2026-11-27T00:00:00Z",
    active: false,
    disclosure:
      "Your conversation with Krell and the Krellbot data she reads are sent to Anthropic (api.anthropic.com). Nothing is sent to krellbot.dev.",
    ...over,
  };
}

type Call = { method: string; path: string; body: unknown; headers: Headers };

function installFetch(handler: (call: Call) => unknown): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call: Call = {
        method: init?.method ?? "GET",
        path: String(input),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: new Headers(init?.headers),
      };
      calls.push(call);
      const out = handler(call) as { status?: number; json: unknown };
      return new Response(JSON.stringify(out.json), {
        status: out.status ?? 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

function ok(body: Record<string, unknown>): { json: unknown } {
  return { json: { schema_version: "1", ok: true, effect: "unchanged", ...body } };
}

function fillAndSubmit(model = "claude-test", key = SECRET): void {
  fireEvent.change(screen.getByLabelText(/api key/i), {
    target: { value: key },
  });
  fireEvent.change(screen.getByLabelText(/^model$/i), {
    target: { value: model },
  });
  fireEvent.click(screen.getByRole("button", { name: /connect/i }));
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AiModelSection", () => {
  it("never renders a stored key and clears the key field after connect", async () => {
    const created = conn();
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/connections" && c.method === "GET") {
        return ok({ active: null, connections: [] });
      }
      if (c.path === "/api/v1/ai/connections" && c.method === "POST") {
        return ok({
          effect: "changed",
          connection: created,
          probe: { capability: "tools", models: [], message: "ok", error: null },
        });
      }
      if (c.path.endsWith("/activate")) {
        return ok({
          effect: "changed",
          active: created.id,
          connection: { ...created, active: true },
        });
      }
      throw new Error(`unexpected ${c.method} ${c.path}`);
    });

    render(<AiModelSection />);
    await screen.findByLabelText(/api key/i);
    const field = screen.getByLabelText(/api key/i) as HTMLInputElement;
    expect(field.type).toBe("password");
    expect(field.autocomplete).toBe("off");

    fillAndSubmit();

    await waitFor(() => {
      expect(calls.some((c) => c.path.endsWith("/activate"))).toBe(true);
    });
    await waitFor(() => {
      expect((screen.getByLabelText(/api key/i) as HTMLInputElement).value).toBe(
        "",
      );
    });
    expect(document.body.innerHTML).not.toContain(SECRET);

    const post = calls.find(
      (c) => c.method === "POST" && c.path === "/api/v1/ai/connections",
    );
    expect(post?.headers.get("X-Krellbot-CSRF")).toBe("test-csrf");
    expect((post?.body as { credential: string }).credential).toBe(SECRET);
    // The key is sent once; the activate call carries no body.
    const activate = calls.find((c) => c.path.endsWith("/activate"));
    expect(activate?.body).toBeUndefined();
  });

  it("clears the key field and hides the key when the server refuses", async () => {
    installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.method === "GET") return ok({ active: null, connections: [] });
      return {
        status: 400,
        json: {
          schema_version: "1",
          ok: false,
          effect: "refused",
          code: "ai_endpoint_refused",
          detail: "That endpoint is not allowed.",
        },
      };
    });
    render(<AiModelSection />);
    await screen.findByLabelText(/api key/i);
    fillAndSubmit();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("That endpoint is not allowed.");
    expect(alert.textContent).toContain("ai_endpoint_refused");
    expect((screen.getByLabelText(/api key/i) as HTMLInputElement).value).toBe("");
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  it("shows chat-only status from the probe", async () => {
    const created = conn({ capability: "chat_only" });
    installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/connections" && c.method === "GET") {
        return ok({ active: null, connections: [] });
      }
      if (c.path === "/api/v1/ai/connections" && c.method === "POST") {
        return ok({
          connection: created,
          probe: {
            capability: "chat_only",
            models: [],
            message: "The model answered without calling the test tool.",
            error: null,
          },
        });
      }
      return ok({ connection: { ...created, active: true } });
    });
    render(<AiModelSection />);
    await screen.findByLabelText(/api key/i);
    fillAndSubmit();
    const status = await screen.findAllByText(
      "Chat only: Krell can explain but not use tools",
    );
    expect(status.length).toBeGreaterThan(0);
  });

  it("shows the server message when the probe failed and does not activate", async () => {
    const created = conn({ capability: "failed" });
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/connections" && c.method === "GET") {
        return ok({ active: null, connections: [] });
      }
      return ok({
        connection: created,
        probe: {
          capability: "failed",
          models: [],
          message: "The provider rejected the key.",
          error: "provider_auth",
        },
      });
    });
    render(<AiModelSection />);
    await screen.findByLabelText(/api key/i);
    fillAndSubmit();
    expect(
      (await screen.findAllByText("The provider rejected the key.")).length,
    ).toBeGreaterThan(0);
    expect(calls.some((c) => c.path.endsWith("/activate"))).toBe(false);
  });

  it("shows the egress disclosure naming the host before connecting", async () => {
    installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      return ok({ active: null, connections: [] });
    });
    render(<AiModelSection />);
    const disclosure = await screen.findByTestId("ai-egress-disclosure");
    expect(disclosure.textContent).toContain("api.anthropic.com");
    expect(disclosure.textContent).toContain("Nothing is sent to krellbot.dev");

    fireEvent.change(screen.getByLabelText(/provider/i), {
      target: { value: "openai" },
    });
    expect(screen.getByTestId("ai-egress-disclosure").textContent).toContain(
      "api.openai.com",
    );
  });

  it("points Claude users to API keys or MCP, not subscriptions", async () => {
    installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      return ok({ active: null, connections: [] });
    });
    render(<AiModelSection />);
    const note = await screen.findByTestId("ai-claude-note");
    expect(note.textContent).toBe(
      "Use an Anthropic API key, or use Krellbot from Claude Desktop or Claude Code through MCP (Settings → Agents & limits).",
    );
    expect(note.textContent?.toLowerCase()).not.toContain("subscription");

    fireEvent.change(screen.getByLabelText(/provider/i), {
      target: { value: "openai" },
    });
    expect(screen.queryByTestId("ai-claude-note")).toBeNull();
  });

  it("asks for a base URL with a loopback-only http hint for custom endpoints", async () => {
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.method === "GET") return ok({ active: null, connections: [] });
      return ok({
        connection: conn({ preset: "custom_openai", capability: "tools" }),
        probe: { capability: "tools", models: [], message: "", error: null },
      });
    });
    render(<AiModelSection />);
    await screen.findByLabelText(/provider/i);
    expect(screen.queryByLabelText(/base url/i)).toBeNull();
    fireEvent.change(screen.getByLabelText(/provider/i), {
      target: { value: "custom_openai" },
    });
    expect(screen.getByTestId("ai-base-url-hint").textContent).toContain(
      "127.0.0.1",
    );
    fireEvent.change(screen.getByLabelText(/base url/i), {
      target: { value: "http://127.0.0.1:11434/v1" },
    });
    fireEvent.change(screen.getByLabelText(/^model$/i), {
      target: { value: "llama" },
    });
    fireEvent.click(screen.getByRole("button", { name: /connect/i }));
    await waitFor(() => {
      expect(calls.some((c) => c.method === "POST")).toBe(true);
    });
    const post = calls.find((c) => c.method === "POST");
    expect(post?.body).toEqual({
      preset: "custom_openai",
      model: "llama",
      base_url: "http://127.0.0.1:11434/v1",
    });
  });

  it("lists connections with Activate and Remove, and re-checks unprobed ones", async () => {
    const active = conn({ id: "c_aaaaaaaaaaaa", active: true });
    const other = conn({
      id: "c_bbbbbbbbbbbb",
      preset: "openai",
      label: "OpenAI",
      host: "api.openai.com",
      model: "gpt-test",
      capability: "unprobed",
    });
    let connections = [active, other];
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/connections" && c.method === "GET") {
        return ok({ active: "c_aaaaaaaaaaaa", connections });
      }
      if (c.path === "/api/v1/ai/connections/c_bbbbbbbbbbbb/probe") {
        connections = [active, { ...other, capability: "tools" }];
        return ok({
          connection: connections[1],
          probe: { capability: "tools", models: [], message: "ok", error: null },
        });
      }
      if (c.path === "/api/v1/ai/connections/c_bbbbbbbbbbbb/activate") {
        connections = [
          { ...active, active: false },
          { ...other, capability: "tools", active: true },
        ];
        return ok({ connection: connections[1] });
      }
      if (c.method === "DELETE") {
        connections = connections.filter((x) => x.id !== "c_aaaaaaaaaaaa");
        return ok({ effect: "changed", removed: "c_aaaaaaaaaaaa" });
      }
      throw new Error(`unexpected ${c.method} ${c.path}`);
    });

    render(<AiModelSection />);
    expect(await screen.findByText("Not known yet: re-check")).toBeTruthy();
    expect(screen.getAllByText("Active").length).toBe(1);
    expect(
      screen.getAllByText(/Nothing is sent to krellbot\.dev/).length,
    ).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /re-check/i }));
    await screen.findAllByText("Ready for Krell");
    expect(calls.some((c) => c.path.endsWith("/c_bbbbbbbbbbbb/probe"))).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /^activate/i }));
    await waitFor(() => {
      expect(
        calls.some((c) => c.path.endsWith("/c_bbbbbbbbbbbb/activate")),
      ).toBe(true);
    });

    const removes = await screen.findAllByRole("button", { name: /^remove/i });
    fireEvent.click(removes[0]);
    await waitFor(() => {
      const del = calls.find((c) => c.method === "DELETE");
      expect(del?.path).toBe("/api/v1/ai/connections/c_aaaaaaaaaaaa");
      expect(del?.headers.get("X-Krellbot-CSRF")).toBe("test-csrf");
    });
  });
});


describe("AiConnectForm contract", () => {
  const OTHER = "c_aaaaaaaaaaaa";
  const NEW = "c_cccccccccccc";

  function setup(opts: {
    capability: string;
    active: string | null;
    activateFails?: boolean;
  }) {
    const created = conn({ id: NEW, capability: opts.capability });
    let added = false;
    const calls = installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.path === "/api/v1/ai/connections" && c.method === "GET") {
        const rows = added ? [created] : [];
        if (opts.active === OTHER) rows.unshift(conn({ id: OTHER, active: true }));
        return ok({ active: opts.active, connections: rows });
      }
      if (c.path.endsWith("/activate")) {
        if (opts.activateFails) {
          return {
            status: 500,
            json: {
              ok: false,
              effect: "refused",
              code: "ai_store_failed",
              detail: "Could not save the active model.",
            },
          };
        }
        return ok({ connection: { ...created, active: true } });
      }
      added = true;
      return ok({
        connection: created,
        probe: {
          capability: opts.capability,
          models: [],
          message: "probe message",
          error: null,
        },
      });
    });
    const onConnected = vi.fn();
    const onAdded = vi.fn();
    return { calls, onConnected, onAdded };
  }

  it("calls onAdded but not onConnected for a failed probe", async () => {
    const { onConnected, onAdded } = setup({ capability: "failed", active: null });
    render(<AiConnectForm onConnected={onConnected} onAdded={onAdded} />);
    await screen.findByLabelText(/api key/i);
    fillAndSubmit();
    await waitFor(() => expect(onAdded).toHaveBeenCalledTimes(1));
    expect(onAdded.mock.calls[0][1].capability).toBe("failed");
    expect(onConnected).not.toHaveBeenCalled();
  });

  it("does not call onConnected when another connection stays active", async () => {
    const { calls, onConnected, onAdded } = setup({
      capability: "tools",
      active: OTHER,
    });
    render(<AiConnectForm onConnected={onConnected} onAdded={onAdded} />);
    await screen.findByLabelText(/api key/i);
    fillAndSubmit();
    await waitFor(() => expect(onAdded).toHaveBeenCalledTimes(1));
    expect(onConnected).not.toHaveBeenCalled();
    expect(calls.some((c) => c.path.endsWith("/activate"))).toBe(false);
  });

  it("offers Activate in the section when another connection is active", async () => {
    setup({ capability: "tools", active: OTHER });
    render(<AiModelSection />);
    fireEvent.click(
      await screen.findByRole("button", { name: /add another model/i }),
    );
    await screen.findByLabelText(/api key/i);
    fillAndSubmit();
    expect(await screen.findByRole("button", { name: /^activate/i })).toBeTruthy();
  });

  it("does not call onConnected and shows the error when activation fails", async () => {
    const { onConnected } = setup({
      capability: "tools",
      active: null,
      activateFails: true,
    });
    render(<AiConnectForm onConnected={onConnected} />);
    await screen.findByLabelText(/api key/i);
    fillAndSubmit();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Could not save the active model.");
    expect(alert.textContent).toContain("ai_store_failed");
    expect(onConnected).not.toHaveBeenCalled();
  });

  it("calls onConnected once the new connection is auto-activated", async () => {
    const { onConnected, onAdded } = setup({
      capability: "chat_only",
      active: null,
    });
    render(<AiConnectForm onConnected={onConnected} onAdded={onAdded} />);
    await screen.findByLabelText(/api key/i);
    fillAndSubmit();
    await waitFor(() => expect(onConnected).toHaveBeenCalledTimes(1));
    expect(onConnected.mock.calls[0][0]).toMatchObject({ id: NEW, active: true });
    expect(onAdded).toHaveBeenCalledTimes(1);
  });
});

describe("AiModelSection empty list", () => {
  it("shows the form again after cancelling and removing every connection", async () => {
    let connections = [conn({ active: true })];
    installFetch((c) => {
      if (c.path === "/api/v1/ai/providers") return ok({ providers: PROVIDERS });
      if (c.method === "DELETE") {
        connections = [];
        return ok({ effect: "changed", removed: "c_aaaaaaaaaaaa" });
      }
      return ok({ active: connections[0]?.id ?? null, connections });
    });
    render(<AiModelSection />);
    await screen.findByRole("button", { name: /^remove/i });
    fireEvent.click(screen.getByRole("button", { name: /add another model/i }));
    fireEvent.click(await screen.findByRole("button", { name: /cancel/i }));
    expect(screen.getByRole("button", { name: /add another model/i })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^remove/i }));
    expect(await screen.findByLabelText(/api key/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /add another model/i })).toBeNull();
  });
});
