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

  it("shows the stored usage limit after a reload, until Krell answers again", async () => {
    installFetch((path) =>
      path === "/api/v1/krell/messages"
        ? sseResponse([{ type: "message_done", text: "Back again." }, { type: "done" }])
        : jsonResponse(conversation([...greeted(CHATGPT), stored("user", { text: "hi" }), limitError()])),
    );
    renderTab();
    await screen.findByTestId("krell-usage-limit");

    await send("Try again");
    await screen.findByText("Back again.");
    await waitFor(() => expect(screen.queryByTestId("krell-usage-limit")).toBeNull());
    expect(screen.getByTestId("krell-chatgpt-plan")).toBeTruthy();
  });

  it("keeps Manage usage secondary while a confirm card's Confirm is the one primary", async () => {
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
    expect(link.className).toContain("kbot-krell__btn");
    expect(link.className).not.toContain("kbot-krell__go");
  });
});
