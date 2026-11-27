/**
 * Krell conversation: messages, streaming, avatar runs and the connect
 * form on the Krell tab (M38.16). Contract: KRELL-SIGNAL-20261010.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { KrellConversationProvider, useKrell } from "./KrellConversationProvider";
import { KrellView } from "./KrellView";

vi.mock("../../session", () => ({
  getCsrf: (): string => "test-csrf",
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const CONNECTION = {
  id: "c_aaaaaaaaaaaa",
  label: "Anthropic",
  host: "api.anthropic.com",
  model: "claude-test",
  capability: "tools",
};

const DISCLOSURE =
  "Your conversation with Krell and the Krellbot data she reads are sent to Anthropic (api.anthropic.com). Nothing is sent to krellbot.dev.";

let nextId = 1;

function stored(role: string, payload: Record<string, unknown>) {
  nextId += 1;
  return { id: nextId, role, payload, ts: 1_700_000_000 + nextId };
}

function greeted() {
  return [
    stored("notice", {
      kind: "disclosure",
      text: DISCLOSURE,
      connection: { id: CONNECTION.id, label: "Anthropic", host: "api.anthropic.com", model: "claude-test" },
    }),
    stored("notice", {
      kind: "opening",
      text: "Hi, I'm Krell.",
      question: "Standard or advanced?",
      quick_replies: ["Standard", "Advanced"],
    }),
  ];
}

function conversation(
  messages: unknown[],
  connection: Record<string, unknown> | null = CONNECTION,
) {
  return {
    schema_version: "1",
    ok: true,
    effect: "unchanged",
    messages,
    profile: { mode: null, answers: {} },
    budget: {
      conversation_used: 0,
      conversation_limit: 200000,
      month_used: 0,
      month_limit: 2000000,
      exhausted: false,
      reason: null,
    },
    connection,
    disclosure: connection ? DISCLOSURE : null,
  };
}

function frame(event: Record<string, unknown>, id: number): string {
  return `id: ${id}\r\nevent: ${String(event.type)}\r\ndata: ${JSON.stringify(event)}\r\n\r\n\r\n`;
}

function sseResponse(events: Array<Record<string, unknown>>): Response {
  return new Response(events.map((event, i) => frame(event, i + 1)).join(""), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

type Call = { method: string; path: string; body: unknown; headers: Headers };

function installFetch(handler: (call: Call) => Response | Promise<Response>): Call[] {
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
      return handler(call);
    }),
  );
  return calls;
}

function renderTab() {
  return render(
    <KrellConversationProvider active>
      <KrellView />
    </KrellConversationProvider>,
  );
}

function krellMessages(): HTMLElement[] {
  return screen.queryAllByTestId("krell-message-krell");
}

describe("Krell conversation", () => {
  it("shows the avatar once per run of Krell messages, not on every message", async () => {
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(),
          stored("user", { text: "What can you do?" }),
          stored("assistant", {
            text: "Let me look.",
            tool_calls: [{ id: "call_1", name: "list_strategies", arguments: {} }],
          }),
          stored("tool", {
            tool_call_id: "call_1",
            name: "list_strategies",
            result: { ok: true, effect: "unchanged", tool: "list_strategies", labels: ["paper"] },
          }),
          stored("assistant", { text: "You have two paper strategies.", tool_calls: [] }),
          stored("assistant", { text: "Want a closer look?", tool_calls: [] }),
          stored("user", { text: "Thanks" }),
          stored("assistant", { text: "Any time.", tool_calls: [] }),
        ]),
      ),
    );
    renderTab();
    await waitFor(() => expect(krellMessages()).toHaveLength(5));
    const krell = krellMessages();
    // Runs: [opening] / [Let me look., tool, two paper, closer look] / [Any time.]
    expect(screen.getAllByTestId("krell-avatar")).toHaveLength(3);
    const withAvatar = krell.map((m) => within(m).queryByTestId("krell-avatar") !== null);
    expect(withAvatar).toEqual([true, true, false, false, true]);
    for (const user of screen.getAllByTestId("krell-message-user")) {
      expect(within(user).queryByTestId("krell-avatar")).toBeNull();
    }
    const avatar = screen.getAllByTestId("krell-avatar")[0] as HTMLImageElement;
    expect(avatar.tagName).toBe("IMG");
    expect(avatar.getAttribute("src")).toContain("krell-pfp-160");
  });

  it("streams text deltas into one message", async () => {
    const encoder = new TextEncoder();
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        controller = c;
      },
    });
    const calls = installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        return new Response(body, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    const input = await screen.findByTestId("krell-input");
    fireEvent.change(input, { target: { value: "Hello Krell" } });
    fireEvent.click(screen.getByTestId("krell-send"));

    await waitFor(() =>
      expect(screen.getByTestId("krell-message-user").textContent).toContain("Hello Krell"),
    );
    const sent = calls.find((c) => c.path === "/api/v1/krell/messages");
    expect(sent?.method).toBe("POST");
    expect(sent?.body).toEqual({ text: "Hello Krell" });
    expect(sent?.headers.get("X-Krellbot-CSRF")).toBe("test-csrf");

    controller.enqueue(encoder.encode(frame({ type: "text_delta", text: "Hi " }, 1)));
    await waitFor(() => expect(krellMessages()).toHaveLength(2));
    expect(krellMessages()[1].textContent).toContain("Hi");

    controller.enqueue(encoder.encode(frame({ type: "text_delta", text: "there, " }, 2)));
    controller.enqueue(encoder.encode(frame({ type: "text_delta", text: "friend." }, 3)));
    await waitFor(() => expect(krellMessages()[1].textContent).toContain("Hi there, friend."));
    expect(krellMessages()).toHaveLength(2);
    expect(screen.getByTestId("krell-send")).toHaveProperty("disabled", true);

    controller.enqueue(
      encoder.encode(
        frame({ type: "message_done", text: "Hi there, friend." }, 4) + frame({ type: "done" }, 5),
      ),
    );
    controller.close();
    fireEvent.change(screen.getByTestId("krell-input"), { target: { value: "next" } });
    await waitFor(() => expect(screen.getByTestId("krell-send")).toHaveProperty("disabled", false));
    expect(krellMessages()).toHaveLength(2);
    expect(krellMessages()[1].textContent).toContain("Hi there, friend.");
  });

  it("renders tool and confirm events as neutral lines and never shows the confirm ticket", async () => {
    const TICKET = "tkt-NEVER-SHOWN-0123456789";
    const log = vi.spyOn(console, "log");
    const calls = installFetch((call) => {
      if (call.path === "/api/v1/krell/confirm") {
        return sseResponse([
          { type: "notice", kind: "confirm_decision", decision: "decline", ticket_id: TICKET, tool: "pause_strategy" },
          { type: "message_done", text: "Okay, left it running." },
          { type: "done" },
        ]);
      }
      return jsonResponse(
        conversation([
          ...greeted(),
          stored("user", { text: "Pause it" }),
          stored("assistant", {
            text: "",
            tool_calls: [{ id: "call_9", name: "pause_strategy", arguments: { id: "s1" } }],
          }),
          stored("tool", {
            tool_call_id: "call_9",
            name: "pause_strategy",
            result: {
              ok: true,
              effect: "needs_confirm",
              tool: "pause_strategy",
              labels: ["paper"],
              confirm: { ticket_id: TICKET, tool: "pause_strategy", params: { id: "s1" } },
            },
          }),
        ]),
      );
    });

    function Decline() {
      const krell = useKrell();
      return (
        <button type="button" onClick={() => krell.confirm("call_9", "decline")}>
          probe-decline
        </button>
      );
    }

    render(
      <KrellConversationProvider active>
        <KrellView />
        <Decline />
      </KrellConversationProvider>,
    );
    await screen.findByTestId("krell-item-confirm");
    expect(screen.getByTestId("krell-item-tool").textContent).toContain("pause_strategy");
    expect(screen.getByTestId("krell-item-confirm").textContent).toContain("pause_strategy");
    expect(document.body.textContent).not.toContain(TICKET);
    expect(document.body.innerHTML).not.toContain(TICKET);

    fireEvent.click(screen.getByRole("button", { name: "probe-decline" }));
    await screen.findByText("Okay, left it running.");
    const posted = calls.find((c) => c.path === "/api/v1/krell/confirm");
    expect(posted?.body).toEqual({ ticket_id: TICKET, decision: "decline" });
    expect(screen.getByTestId("krell-item-decision").textContent).toContain("Not now");
    expect(document.body.innerHTML).not.toContain(TICKET);
    for (const args of log.mock.calls) {
      expect(JSON.stringify(args)).not.toContain(TICKET);
    }
  });
});

describe("Krell tab", () => {
  it("shows the connect form with no model, then the conversation once one connects", async () => {
    let connected = false;
    const calls = installFetch((call) => {
      if (call.path === "/api/v1/krell/conversation") {
        return jsonResponse(conversation([], connected ? CONNECTION : null));
      }
      if (call.path === "/api/v1/ai/providers") {
        return jsonResponse({
          ok: true,
          providers: [
            {
              id: "anthropic",
              label: "Anthropic",
              format: "anthropic_messages",
              base_url: "https://api.anthropic.com",
              custom: false,
              credential: "required",
            },
          ],
        });
      }
      if (call.path === "/api/v1/ai/connections" && call.method === "POST") {
        return jsonResponse({
          ok: true,
          connection: { ...CONNECTION, preset: "anthropic", active: false },
          probe: { capability: "tools", message: "" },
        });
      }
      if (call.path === "/api/v1/ai/connections") {
        return jsonResponse({ ok: true, connections: [], active: null });
      }
      if (call.path.endsWith("/activate")) {
        connected = true;
        return jsonResponse({ ok: true, connection: { ...CONNECTION, active: true } });
      }
      if (call.path === "/api/v1/krell/start") {
        return sseResponse([
          { type: "notice", kind: "disclosure", text: DISCLOSURE, connection: CONNECTION },
          { type: "opening", text: "Hi, I'm Krell.", question: "Standard or advanced?", quick_replies: ["Standard", "Advanced"] },
          { type: "done" },
        ]);
      }
      return jsonResponse({ ok: false, code: "unexpected", detail: call.path }, 404);
    });

    renderTab();
    await screen.findByTestId("ai-connect-form");
    expect(screen.queryByTestId("krell-input")).toBeNull();
    expect(calls.some((c) => c.path === "/api/v1/krell/start")).toBe(false);

    fireEvent.change(screen.getByLabelText(/api key/i), { target: { value: "sk-test-key" } });
    fireEvent.change(screen.getByLabelText(/^model$/i), { target: { value: "claude-test" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    await screen.findByTestId("krell-input");
    expect(screen.queryByTestId("ai-connect-form")).toBeNull();
    await screen.findByText("Hi, I'm Krell.");
    expect(screen.getByText(DISCLOSURE)).toBeDefined();
    expect(calls.filter((c) => c.path === "/api/v1/krell/start")).toHaveLength(1);
  });
});
