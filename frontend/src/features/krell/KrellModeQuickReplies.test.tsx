/**
 * Krell's Standard and Advanced quick replies (M38.18). Contract:
 * KRELL-SIGNAL-20261010.
 *
 * The quick replies answer the opening's mode question. They show only
 * while no mode is set, send the mode through `POST /api/v1/krell/profile`,
 * and are secondary buttons, so an open confirm card's Confirm stays the
 * conversation's one sage primary. Krell can also switch the mode herself
 * with `set_profile`; the engine's result, not her words, hides them.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { KrellConversationProvider } from "./KrellConversationProvider";
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
const OPENING = "Hi, I'm Krell.";
const QUESTION = "Before we start: are you a standard user or an advanced user?";

let nextId = 1;

function stored(role: string, payload: Record<string, unknown>) {
  nextId += 1;
  return { id: nextId, role, payload, ts: 1_700_000_000 + nextId };
}

function greeted() {
  return [
    stored("notice", {
      kind: "disclosure",
      text: "Your conversation with Krell is sent to Anthropic (api.anthropic.com).",
      connection: { id: CONNECTION.id, label: "Anthropic", host: "api.anthropic.com", model: "claude-test" },
    }),
    stored("notice", { kind: "opening", text: OPENING, question: QUESTION, quick_replies: ["Standard", "Advanced"] }),
  ];
}

function conversation(messages: unknown[], mode: string | null = null) {
  return {
    schema_version: "1",
    ok: true,
    effect: "unchanged",
    messages,
    profile: { mode, answers: {} },
    budget: null,
    connection: CONNECTION,
    disclosure: "Your conversation with Krell is sent to Anthropic (api.anthropic.com).",
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
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
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
    <KrellConversationProvider surface="tab">
      <KrellView />
    </KrellConversationProvider>,
  );
}

function quickReplies(): HTMLElement | null {
  return screen.queryByRole("group", { name: "Choose a mode" });
}

function reply(name: "Standard" | "Advanced"): HTMLButtonElement {
  return within(quickReplies() as HTMLElement).getByRole("button", { name }) as HTMLButtonElement;
}

describe("Krell mode quick replies", () => {
  it("offers Standard and Advanced while no mode is set, and a press sends the mode to the profile route", async () => {
    const calls = installFetch((call) => {
      if (call.path === "/api/v1/krell/profile") {
        return sseResponse([
          { type: "message_done", text: "Straight to the deep end, then." },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    await screen.findByText(OPENING);

    expect(quickReplies()).not.toBeNull();
    // Secondary buttons: an open confirm card's Confirm stays the one sage primary.
    for (const name of ["Standard", "Advanced"] as const) {
      expect(reply(name).className).toContain("kbot-krell__btn");
      expect(reply(name).className).not.toContain("kbot-krell__go");
    }

    fireEvent.click(reply("Advanced"));

    await screen.findByText("Straight to the deep end, then.");
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts.map((c) => [c.path, c.body])).toEqual([["/api/v1/krell/profile", { mode: "advanced" }]]);
    expect(posts[0].headers.get("X-Krellbot-CSRF")).toBe("test-csrf");
    expect(screen.getByTestId("krell-message-user").textContent).toContain("Advanced");
    await waitFor(() => expect(quickReplies()).toBeNull());
  });

  it("shows no quick replies once a mode is stored", async () => {
    installFetch(() => jsonResponse(conversation(greeted(), "standard")));
    renderTab();
    await screen.findByText(OPENING);
    expect(screen.getByTestId("krell-input")).toBeDefined();
    expect(quickReplies()).toBeNull();
  });

  it("hides the quick replies when Krell switches the mode with set_profile, and only then", async () => {
    let turn = 0;
    installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        turn += 1;
        const result =
          turn === 1
            ? { ok: false, effect: "refused", code: "krell_profile_invalid", tool: "set_profile", labels: [] }
            : { ok: true, effect: "changed", tool: "set_profile", labels: [], profile: { mode: "advanced", answers: {} } };
        return sseResponse([
          { type: "tool_start", id: `call_${turn}`, name: "set_profile", arguments: { mode: "advanced" } },
          { type: "tool_result", id: `call_${turn}`, name: "set_profile", result },
          { type: "message_done", text: turn === 1 ? "I switched you to advanced." : "Now you are in advanced." },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    await screen.findByText(OPENING);

    // Krell's words alone change nothing: the refused call keeps the quick replies.
    fireEvent.change(screen.getByTestId("krell-input"), { target: { value: "Switch me to advanced." } });
    fireEvent.click(screen.getByTestId("krell-send"));
    await screen.findByText("I switched you to advanced.");
    await waitFor(() => expect(reply("Advanced").disabled).toBe(false));

    fireEvent.change(screen.getByTestId("krell-input"), { target: { value: "Please, really." } });
    fireEvent.click(screen.getByTestId("krell-send"));
    await screen.findByText("Now you are in advanced.");
    await waitFor(() => expect(quickReplies()).toBeNull());
  });

  it("keeps the quick replies when the engine refuses the press", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/profile") {
        return jsonResponse(
          { ok: false, effect: "refused", code: "krell_busy", detail: "Krell is already answering. Wait for that answer to finish." },
          409,
        );
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    await screen.findByText(OPENING);

    fireEvent.click(reply("Standard"));

    await screen.findByText("Krell is already answering. Wait for that answer to finish.");
    await waitFor(() => expect(reply("Standard").disabled).toBe(false));
    expect(screen.queryByTestId("krell-message-user")).toBeNull();
  });

  it("keeps the quick replies when the press's stream fails after a disclosure", async () => {
    // The engine showed a new disclosure, then could not store the mode.
    installFetch((call) => {
      if (call.path === "/api/v1/krell/profile") {
        return sseResponse([
          { type: "notice", kind: "disclosure", text: "Now sent to OpenAI.", connection: CONNECTION },
          {
            type: "error",
            code: "krell_store_unavailable",
            message: "Krell's conversation store could not be read or written, so nothing changed.",
          },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    await screen.findByText(OPENING);

    fireEvent.click(reply("Advanced"));

    await screen.findByText("Krell's conversation store could not be read or written, so nothing changed.");
    await waitFor(() => expect(reply("Advanced").disabled).toBe(false));
    expect(reply("Standard")).toBeDefined();
  });

  it("hides the quick replies after a failed turn when the engine did store the mode", async () => {
    // The engine stored the mode, then the provider failed: the stored conversation says so.
    let pressed = false;
    installFetch((call) => {
      if (call.path === "/api/v1/krell/profile") {
        pressed = true;
        return sseResponse([
          { type: "text_delta", text: "Standard it" },
          { type: "error", code: "provider_unavailable", message: "The provider could not be reached." },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(greeted(), pressed ? "standard" : null));
    });
    renderTab();
    await screen.findByText(OPENING);

    fireEvent.click(reply("Standard"));

    await screen.findByText("The provider could not be reached.");
    await waitFor(() => expect(quickReplies()).toBeNull());
  });

  it("disables the quick replies while Krell answers", async () => {
    let answer: (response: Response) => void = () => {};
    installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        return new Promise<Response>((resolve) => {
          answer = resolve;
        });
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    await screen.findByText(OPENING);

    fireEvent.change(screen.getByTestId("krell-input"), { target: { value: "What is a paper run?" } });
    fireEvent.click(screen.getByTestId("krell-send"));
    await waitFor(() => expect(reply("Standard").disabled).toBe(true));
    expect(reply("Advanced").disabled).toBe(true);

    answer(sseResponse([{ type: "message_done", text: "Simulated funds only." }, { type: "done" }]));
    await screen.findByText("Simulated funds only.");
    await waitFor(() => expect(reply("Standard").disabled).toBe(false));
  });

  it("offers the quick replies again after the conversation is cleared", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/conversation" && call.method === "DELETE") {
        return jsonResponse({ ok: true, effect: "changed" });
      }
      if (call.path === "/api/v1/krell/start") {
        return sseResponse([
          { type: "opening", text: OPENING, question: QUESTION, quick_replies: ["Standard", "Advanced"] },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation([...greeted(), stored("user", { text: "Standard", mode: "standard" })], "standard"));
    });
    renderTab();
    await screen.findByText(OPENING);
    expect(quickReplies()).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Clear conversation" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, clear" }));

    await waitFor(() => expect(quickReplies()).not.toBeNull());
    await waitFor(() => expect(reply("Standard").disabled).toBe(false));
  });
});
