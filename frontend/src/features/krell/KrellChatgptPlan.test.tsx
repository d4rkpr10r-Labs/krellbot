/**
 * ChatGPT plan use in the Krell conversation (M38.23). Contract:
 * KRELL-SIGNAL-20261010.
 *
 * When Krell's active connection is a Sign in with ChatGPT one, "Using
 * ChatGPT plan" sits near the composer with a Manage usage link to ChatGPT
 * usage settings. When a turn ends on a ChatGPT usage limit, a compact
 * "Usage limit reached" state takes its place, with Manage usage as its
 * primary action, unless a confirm card's Confirm is already the
 * conversation's one primary. Fetch is a fake.
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

const USAGE_URL = "https://chatgpt.com/settings/usage";
const CHATGPT = {
  id: "c_chatgptaaaaaa",
  label: "OpenAI (ChatGPT plan)",
  host: "api.openai.com",
  model: "gpt-plan-a",
  capability: "tools",
  plan: "chatgpt",
};
const KEYED = { ...CHATGPT, id: "c_keyedaaaaaaaa", label: "OpenAI", plan: undefined };
const OPENING = "Hi, I'm Krell.";
const LIMIT_MESSAGE = "You reached a ChatGPT usage limit, for your plan or for Krellbot, so Krell paused.";

let nextId = 1;

function stored(role: string, payload: Record<string, unknown>) {
  nextId += 1;
  return { id: nextId, role, payload, ts: 1_700_000_000 + nextId };
}

function greeted(connection: Record<string, unknown>) {
  return [
    stored("notice", {
      kind: "disclosure",
      text: "Your conversation with Krell is sent to OpenAI (api.openai.com).",
      connection: { id: connection.id, label: connection.label, host: "api.openai.com", model: "gpt-plan-a" },
    }),
    stored("notice", { kind: "opening", text: OPENING, question: "Standard or advanced?", quick_replies: [] }),
  ];
}

function limitError() {
  return stored("notice", { kind: "error", code: "provider_usage_limit", message: LIMIT_MESSAGE });
}

function conversation(messages: unknown[], connection: Record<string, unknown> = CHATGPT) {
  return {
    schema_version: "1",
    ok: true,
    effect: "unchanged",
    messages,
    profile: { mode: "standard", answers: {} },
    budget: null,
    connection,
    disclosure: "Your conversation with Krell is sent to OpenAI (api.openai.com).",
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

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

function installFetch(handler: (path: string) => Response): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => handler(String(input))),
  );
}

function renderTab() {
  return render(
    <KrellConversationProvider surface="tab">
      <KrellView />
    </KrellConversationProvider>,
  );
}

async function send(text: string): Promise<void> {
  fireEvent.change(await screen.findByTestId("krell-input"), { target: { value: text } });
  fireEvent.click(screen.getByTestId("krell-send"));
}

function manageUsageLinks(): HTMLAnchorElement[] {
  return screen.queryAllByRole("link", { name: "Manage usage" }) as HTMLAnchorElement[];
}

function expectUsageLink(link: HTMLAnchorElement): void {
  expect(link.getAttribute("href")).toBe(USAGE_URL);
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toContain("noopener");
}

describe("ChatGPT plan use in the Krell conversation", () => {
  it("shows Using ChatGPT plan with a Manage usage link near the composer", async () => {
    installFetch(() => jsonResponse(conversation(greeted(CHATGPT))));
    renderTab();
    await screen.findByText(OPENING);

    const plan = await screen.findByTestId("krell-chatgpt-plan");
    expect(plan.textContent).toContain("Using ChatGPT plan");
    const [link] = manageUsageLinks();
    expectUsageLink(link);
    expect(within(plan).getByRole("link", { name: "Manage usage" })).toBe(link);
    // It sits with the composer, not in the message log.
    expect(screen.getByRole("log").contains(plan)).toBe(false);
    expect(screen.queryByTestId("krell-usage-limit")).toBeNull();
  });

  it("says nothing about a ChatGPT plan on another connection", async () => {
    installFetch(() => jsonResponse(conversation(greeted(KEYED), KEYED)));
    renderTab();
    await screen.findByText(OPENING);
    expect(screen.queryByTestId("krell-chatgpt-plan")).toBeNull();
    expect(screen.queryByText(/ChatGPT plan/)).toBeNull();
  });

  it("turns a usage-limit error into a compact state whose primary action is Manage usage", async () => {
    installFetch((path) =>
      path === "/api/v1/krell/messages"
        ? sseResponse([
            { type: "text_delta", text: "Let me check" },
            { type: "error", code: "provider_usage_limit", message: LIMIT_MESSAGE },
            { type: "done" },
          ])
        : jsonResponse(conversation(greeted(CHATGPT))),
    );
    renderTab();
    await screen.findByText(OPENING);
    await send("What is running?");

    const state = await screen.findByTestId("krell-usage-limit");
    expect(state.textContent).toContain("ChatGPT");
    expect(state.textContent).toContain("Usage limit reached");
    // One Manage usage, and it is the state's primary action (sage).
    const links = manageUsageLinks();
    expect(links).toHaveLength(1);
    expectUsageLink(links[0]);
    expect(within(state).getByRole("link", { name: "Manage usage" }).className).toContain("kbot-krell__go");
    expect(screen.queryByTestId("krell-chatgpt-plan")).toBeNull();
    // The engine's message stays in the log.
    expect(screen.getByText(LIMIT_MESSAGE)).toBeTruthy();
  });

  it("pauses Krell after a reload until the person chooses Try again", async () => {
    const posted: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/krell/messages") {
          posted.push(path);
          return sseResponse([{ type: "message_done", text: "Back again." }, { type: "done" }]);
        }
        return jsonResponse(conversation([...greeted(CHATGPT), stored("user", { text: "hi" }), limitError()]));
      }),
    );
    renderTab();
    await screen.findByTestId("krell-usage-limit");

    // Paused: the composer takes text, but nothing can be sent.
    fireEvent.change(screen.getByTestId("krell-input"), { target: { value: "Are you there?" } });
    const sendButton = screen.getByTestId("krell-send") as HTMLButtonElement;
    expect(sendButton.disabled).toBe(true);
    fireEvent.keyDown(screen.getByTestId("krell-input"), { key: "Enter" });
    expect(posted).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.queryByTestId("krell-usage-limit")).toBeNull();
    expect(screen.getByTestId("krell-chatgpt-plan")).toBeTruthy();
    expect((screen.getByTestId("krell-send") as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByTestId("krell-send"));
    await screen.findByText("Back again.");
    expect(posted).toEqual(["/api/v1/krell/messages"]);
    expect(screen.queryByTestId("krell-usage-limit")).toBeNull();
  });

  it("clears the pause on an API-key connection and pauses only a ChatGPT-plan one", async () => {
    installFetch(() =>
      jsonResponse(conversation([...greeted(KEYED), stored("user", { text: "hi" }), limitError()], KEYED)),
    );
    renderTab();
    await screen.findByText(LIMIT_MESSAGE);
    expect(screen.queryByTestId("krell-usage-limit")).toBeNull();
    expect(screen.queryByRole("link", { name: "Manage usage" })).toBeNull();
    fireEvent.change(screen.getByTestId("krell-input"), { target: { value: "hello" } });
    expect((screen.getByTestId("krell-send") as HTMLButtonElement).disabled).toBe(false);
  });

  it("shows the provider's request id with the error, live and after a reload", async () => {
    let reloads = 0;
    installFetch((path) => {
      if (path === "/api/v1/krell/messages") {
        return sseResponse([
          { type: "text_delta", text: "Let me" },
          {
            type: "error",
            code: "provider_usage_limit",
            message: LIMIT_MESSAGE,
            provider: { status: 429, code: "subscription_sharing_usage_limit_exceeded", request_id: "req_LIVE42" },
          },
          { type: "done" },
        ]);
      }
      reloads += 1;
      const messages =
        reloads === 1
          ? greeted(CHATGPT)
          : [
              ...greeted(CHATGPT),
              stored("notice", {
                kind: "error",
                code: "provider_usage_limit",
                message: LIMIT_MESSAGE,
                provider: { status: 429, request_id: "req_STORED7", detail: "{\"error\": {}}" },
              }),
            ];
      return jsonResponse(conversation(messages));
    });
    const view = renderTab();
    await screen.findByText(OPENING);
    await send("hello");
    expect((await screen.findByTestId("krell-request-id")).textContent).toContain("Request ID: req_LIVE42");
    view.unmount();

    renderTab();
    expect((await screen.findByTestId("krell-request-id")).textContent).toContain("Request ID: req_STORED7");
    // The stored body excerpt is never shown.
    expect(screen.queryByText(/"error"/)).toBeNull();
  });

  it("makes Manage usage the one primary and holds every confirm card while paused", async () => {
    const card = stored("tool", {
      tool_call_id: "call_arm",
      name: "paper_arm",
      result: {
        ok: false,
        effect: "needs_confirm",
        code: "agent_confirm_required",
        message: "This call needs the person's confirm.",
        tool: "paper_arm",
        labels: ["paper"],
        confirm: {
          ticket_id: "ticket-secret-1",
          tool: "paper_arm",
          params: { venue: "kraken", pair: "SUI/USD", paper_balance: "500" },
          estimate: { cost: "heavy", bars: null, packs: 1, seconds: 120, result_tokens: 6000 },
          limits: { max_paper_balance: "1000", require_daily_loss_limit: true },
          expires_at: Date.now() / 1000 + 600,
          pack_sha256: "a".repeat(64),
        },
      },
    });
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(CHATGPT),
          stored("assistant", { text: "", tool_calls: [{ id: "call_arm", name: "paper_arm", arguments: {} }] }),
          card,
          limitError(),
        ]),
      ),
    );
    renderTab();
    const state = await screen.findByTestId("krell-usage-limit");
    const link = within(state).getByRole("link", { name: "Manage usage" });
    expect(link.className).toContain("kbot-krell__go");
    // The card's Confirm is demoted and disabled, with the reason beside it.
    const row = screen.getByTestId("krell-item-confirm");
    const confirm = within(row).getByRole("button", { name: /confirm/i }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    expect(confirm.className).not.toContain("kbot-krell__go");
    expect(within(row).getByTestId("krell-confirm-paused").textContent).toContain("Try again");
    expect(document.querySelectorAll(".kbot-krell__go")).toHaveLength(1);

    // Try again lets the card be answered again, and it is the primary once more.
    fireEvent.click(within(state).getByRole("button", { name: "Try again" }));
    const again = within(screen.getByTestId("krell-item-confirm")).getByRole("button", { name: /confirm/i }) as HTMLButtonElement;
    expect(again.disabled).toBe(false);
    expect(again.className).toContain("kbot-krell__go");
    expect(screen.queryByTestId("krell-confirm-paused")).toBeNull();
  });
});
