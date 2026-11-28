/**
 * Krell's Keep going quick reply (M39.3). Contract: KRELL-SIGNAL-20261010.
 *
 * When a turn uses up its tool budget, the engine refuses the calls over it
 * (`agent_tool_cap_reached`), Krell writes her own summary, and the engine
 * offers "Keep going". The reply shows while that summary is the latest
 * thing in the conversation, live or after a reload, and sends "Keep going"
 * as an ordinary message. It is a secondary button, like the mode replies.
 * When the turn ends on the cap error instead, there is no Keep going.
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
const SUMMARY = "I drafted and checked the strategy; the backtest is left. Keep going?";
const CAP_MESSAGE =
  "Krell stopped after 24 tool calls in one turn. Send a message to carry on, or narrow the question.";
const CAP_REFUSAL = {
  schema_version: "1",
  ok: false,
  effect: "refused",
  code: "agent_tool_cap_reached",
  message: CAP_MESSAGE,
  tool: "tour_step",
  actor: "krell",
  labels: [],
};
const TOUR_OK = { schema_version: "1", ok: true, effect: "read", tool: "tour_step", actor: "krell", labels: [] };

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
    stored("notice", { kind: "opening", text: OPENING, question: "Standard or advanced?", quick_replies: [] }),
    stored("user", { text: "Standard", mode: "standard" }),
  ];
}

/** A stored turn that used up its tool budget and ended with Krell's summary. */
function cappedTurn() {
  return [
    stored("user", { text: "Build me a strategy." }),
    stored("assistant", { text: "", tool_calls: [{ id: "call_1", name: "tour_step", arguments: {} }] }),
    stored("tool", { tool_call_id: "call_1", name: "tour_step", result: TOUR_OK }),
    stored("assistant", { text: "", tool_calls: [{ id: "call_2", name: "tour_step", arguments: {} }] }),
    stored("tool", { tool_call_id: "call_2", name: "tour_step", result: CAP_REFUSAL }),
    stored("assistant", { text: SUMMARY, tool_calls: [] }),
  ];
}

function conversation(messages: unknown[]) {
  return {
    schema_version: "1",
    ok: true,
    effect: "unchanged",
    messages,
    profile: { mode: "standard", answers: {} },
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

/** The live stream of a turn that reaches the cap: one call runs, the next is refused, Krell sums up. */
function cappedStream(): Array<Record<string, unknown>> {
  return [
    { type: "tool_start", id: "call_1", name: "tour_step", arguments: {} },
    { type: "tool_result", id: "call_1", name: "tour_step", result: TOUR_OK },
    { type: "message_done", text: "" },
    { type: "tool_start", id: "call_2", name: "tour_step", arguments: {} },
    { type: "tool_result", id: "call_2", name: "tour_step", result: CAP_REFUSAL },
    { type: "text_delta", text: "I drafted and checked the strategy; " },
    { type: "text_delta", text: "the backtest is left. Keep going?" },
    { type: "message_done", text: SUMMARY },
    { type: "quick_replies", quick_replies: ["Keep going"] },
    { type: "done" },
  ];
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

function keepGoingGroup(): HTMLElement | null {
  return screen.queryByRole("group", { name: "Carry on" });
}

function keepGoing(): HTMLButtonElement {
  return within(keepGoingGroup() as HTMLElement).getByRole("button", { name: "Keep going" }) as HTMLButtonElement;
}

function type(text: string): void {
  fireEvent.change(screen.getByTestId("krell-input"), { target: { value: text } });
}

describe("Krell's Keep going quick reply", () => {
  it("follows Krell's summary at the tool budget, and a press sends Keep going as a message", async () => {
    let turn = 0;
    const calls = installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        turn += 1;
        if (turn === 1) return sseResponse(cappedStream());
        return sseResponse([{ type: "message_done", text: "On it: running the backtest." }, { type: "done" }]);
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    await screen.findByText(OPENING);
    expect(keepGoingGroup()).toBeNull();

    type("Build me a strategy.");
    fireEvent.click(screen.getByTestId("krell-send"));
    await screen.findByText(SUMMARY);

    // The refused call over the budget shows as its card; Keep going follows the summary.
    await waitFor(() => expect(keepGoing().disabled).toBe(false));
    // The ordinary step folds into "1 step" (M39.6); the refusal stays visible on its own, outside the fold.
    const refused = screen.getByText(CAP_MESSAGE, { exact: false }).closest(".kbot-krell__card--refused");
    expect(refused).not.toBeNull();
    expect(refused?.closest("[hidden]")).toBeNull();
    expect(screen.getByRole("button", { name: "1 step" }).getAttribute("aria-expanded")).toBe("false");
    // A secondary button: an open confirm card's Confirm stays the one sage primary.
    expect(keepGoing().className).toContain("kbot-krell__btn");
    expect(keepGoing().className).not.toContain("kbot-krell__go");

    fireEvent.click(keepGoing());

    await screen.findByText("On it: running the backtest.");
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts.map((c) => [c.path, c.body])).toEqual([
      ["/api/v1/krell/messages", { text: "Build me a strategy." }],
      ["/api/v1/krell/messages", { text: "Keep going" }],
    ]);
    expect(posts[1].headers.get("X-Krellbot-CSRF")).toBe("test-csrf");
    const sent = screen.getAllByTestId("krell-message-user").map((el) => el.textContent ?? "");
    expect(sent[sent.length - 1]).toContain("Keep going");
    // Krell answered without reaching the budget again, so Keep going is gone.
    await waitFor(() => expect(keepGoingGroup()).toBeNull());
  });

  it("shows Keep going after a reload when the stored turn ended with the summary", async () => {
    installFetch(() => jsonResponse(conversation([...greeted(), ...cappedTurn()])));
    renderTab();
    await screen.findByText(SUMMARY);
    await waitFor(() => expect(keepGoing().disabled).toBe(false));
  });

  it("offers no Keep going when the turn ends on the tool budget error", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        return sseResponse([
          { type: "tool_start", id: "call_2", name: "tour_step", arguments: {} },
          { type: "tool_result", id: "call_2", name: "tour_step", result: CAP_REFUSAL },
          { type: "error", code: "agent_tool_cap_reached", message: CAP_MESSAGE },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    await screen.findByText(OPENING);

    type("Build me a strategy.");
    fireEvent.click(screen.getByTestId("krell-send"));

    await waitFor(() => expect(screen.getByTestId("krell-item-error").textContent).toContain(CAP_MESSAGE));
    expect(keepGoingGroup()).toBeNull();
  });

  it("offers no Keep going after an ordinary answer", async () => {
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(),
          stored("user", { text: "What is a paper run?" }),
          stored("assistant", { text: "", tool_calls: [{ id: "call_9", name: "tour_step", arguments: {} }] }),
          stored("tool", { tool_call_id: "call_9", name: "tour_step", result: TOUR_OK }),
          stored("assistant", { text: "Simulated funds only.", tool_calls: [] }),
        ]),
      ),
    );
    renderTab();
    await screen.findByText("Simulated funds only.");
    expect(keepGoingGroup()).toBeNull();
  });

  it("goes away once pressed, and keeps what the person was typing", async () => {
    let answer: (response: Response) => void = () => {};
    const calls = installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        return new Promise<Response>((resolve) => {
          answer = resolve;
        });
      }
      return jsonResponse(conversation([...greeted(), ...cappedTurn()]));
    });
    renderTab();
    await screen.findByText(SUMMARY);
    type("and then compare it with BTC");

    fireEvent.click(keepGoing());

    // The press is the person's message now, so the reply is spent; the composer keeps its draft.
    await waitFor(() => expect(keepGoingGroup()).toBeNull());
    expect(screen.getByText("Krell is answering…")).toBeDefined();
    expect((screen.getByTestId("krell-input") as HTMLTextAreaElement).value).toBe("and then compare it with BTC");
    expect(calls.filter((c) => c.method === "POST").map((c) => c.body)).toEqual([{ text: "Keep going" }]);

    answer(sseResponse([{ type: "message_done", text: "Backtest done." }, { type: "done" }]));
    await screen.findByText("Backtest done.");
    expect(keepGoingGroup()).toBeNull();
    expect((screen.getByTestId("krell-input") as HTMLTextAreaElement).value).toBe("and then compare it with BTC");
  });
});
