/**
 * Krell's result cards, confirm cards, budget meter and highlights
 * (M38.17). Contract: KRELL-SIGNAL-20261010.
 *
 * Cards come only from tool results and confirm requests the engine sent;
 * model text alone never draws one. A confirm ticket id never reaches the
 * DOM: the card's buttons go through the provider, which sends the ticket
 * only in the confirm request body.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BudgetMeter } from "./BudgetMeter";
import { ConfirmCard } from "./ConfirmCard";
import { KrellConversationProvider } from "./KrellConversationProvider";
import { KrellView } from "./KrellView";
import { ToolResultCard } from "./ToolResultCard";
import { useKrellHighlight } from "./useKrellHighlight";

vi.mock("../../session", () => ({
  getCsrf: (): string => "test-csrf",
}));

const here = dirname(fileURLToPath(import.meta.url));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

const CONNECTION = {
  id: "c_aaaaaaaaaaaa",
  label: "Anthropic",
  host: "api.anthropic.com",
  model: "claude-test",
  capability: "tools",
};

const TICKET = "tkt-NEVER-IN-THE-DOM-0123456789abcdef";
const BUDGET_TEXT = "Token budget reached. Raise it in Settings → Agents & limits.";
const UNKNOWN_TEXT = "Expired. Ask Krell again.";
const UNCLEAR_TEXT = "Couldn't tell whether it ran. Reload to check.";

let nextId = 1;

function stored(role: string, payload: Record<string, unknown>) {
  nextId += 1;
  return { id: nextId, role, payload, ts: 1_700_000_000 + nextId };
}

function greeted() {
  return [
    stored("notice", { kind: "disclosure", text: "Sent to Anthropic.", connection: CONNECTION }),
    stored("notice", { kind: "opening", text: "Hi, I'm Krell.", question: "Standard or advanced?", quick_replies: [] }),
  ];
}

function budget(overrides: Record<string, unknown> = {}) {
  return {
    conversation_used: 0,
    conversation_limit: 200000,
    month_used: 0,
    month_limit: 2000000,
    exhausted: false,
    reason: null,
    ...overrides,
  };
}

function conversation(messages: unknown[], budgetBody: unknown = budget()) {
  return {
    schema_version: "1",
    ok: true,
    effect: "unchanged",
    messages,
    profile: { mode: null, answers: {} },
    budget: budgetBody,
    connection: CONNECTION,
    disclosure: "Sent to Anthropic.",
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

type Call = { method: string; path: string; body: unknown };

function installFetch(handler: (call: Call) => Response | Promise<Response>): Call[] {
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
      return handler(call);
    }),
  );
  return calls;
}

function renderTab(extra?: ReactNode) {
  return render(
    <KrellConversationProvider surface="tab">
      <KrellView />
      {extra}
    </KrellConversationProvider>,
  );
}

async function send(text: string): Promise<void> {
  fireEvent.change(await screen.findByTestId("krell-input"), { target: { value: text } });
  fireEvent.click(screen.getByTestId("krell-send"));
}

async function idle(): Promise<void> {
  await waitFor(() => expect(screen.queryByText("Krell is answering…")).toBeNull());
}

function inFuture(seconds = 600): number {
  return Date.now() / 1000 + seconds;
}

function confirmRequest(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    tool: "paper_arm",
    params: { venue: "kraken", pair: "SUI/USD", paper_balance: "500" },
    estimate: { cost: "heavy", bars: null, packs: 1, seconds: 120, result_tokens: 6000 },
    limits: { max_paper_balance: "1000", require_daily_loss_limit: true },
    expires_at: inFuture(),
    pack_sha256: "a".repeat(64),
    ...overrides,
  };
}

/** A stored `needs_confirm` tool row: the card the person decides on, with its ticket. */
function cardRow(callId: string, ticket = TICKET, overrides: Record<string, unknown> = {}) {
  return stored("tool", {
    tool_call_id: callId,
    name: "paper_arm",
    result: {
      ok: false,
      effect: "needs_confirm",
      code: "agent_confirm_required",
      message: "This call needs the person's confirm.",
      tool: "paper_arm",
      labels: ["paper"],
      confirm: { ticket_id: ticket, ...confirmRequest(overrides) },
    },
  });
}

function askedToArm(callId = "call_arm") {
  return [
    ...greeted(),
    stored("user", { text: "Arm the starter strategy on paper" }),
    stored("assistant", {
      text: "",
      tool_calls: [{ id: callId, name: "paper_arm", arguments: { venue: "kraken" } }],
    }),
    cardRow(callId),
  ];
}

const DECIDED_CONFIRM = { type: "notice", kind: "confirm_decision", decision: "confirm", ticket_id: TICKET, tool: "paper_arm" };
const DECIDED_DECLINE = { type: "notice", kind: "confirm_decision", decision: "decline", ticket_id: TICKET, tool: "paper_arm" };
const ARMED = { ok: true, effect: "changed", tool: "paper_arm", actor: "krell", labels: ["paper"] };

function confirmedStream(result: Record<string, unknown>, after: Array<Record<string, unknown>> = []) {
  return [
    DECIDED_CONFIRM,
    { type: "tool_start", id: "call_arm", name: "paper_arm", arguments: { venue: "kraken" } },
    { type: "tool_result", id: "call_arm", name: "paper_arm", result },
    ...after,
    { type: "done" },
  ];
}

function confirmCard(): HTMLElement {
  return screen.getByTestId("krell-item-confirm");
}

function cardButton(name: "Confirm" | "Not now", card: HTMLElement = confirmCard()): HTMLButtonElement {
  return within(card).getByRole("button", { name }) as HTMLButtonElement;
}

function expectNoTicket(): void {
  expect(document.body.innerHTML).not.toContain(TICKET);
}

describe("Result cards", () => {
  it("draws Simulated from the tool result even when the model text omits it", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        return sseResponse([
          { type: "message_done", text: "Let me run that." },
          { type: "tool_start", id: "call_bt", name: "backtest", arguments: {} },
          {
            type: "tool_result",
            id: "call_bt",
            name: "backtest",
            result: { ok: true, effect: "unchanged", tool: "backtest", labels: ["simulated", "not_advice"] },
          },
          { type: "message_done", text: "Here is how it behaved on past data." },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    await send("Backtest the starter strategy");
    await screen.findByText("Here is how it behaved on past data.");

    const card = screen.getByTestId("krell-item-tool");
    expect(within(card).getAllByTestId("krell-label").map((pill) => pill.textContent)).toEqual([
      "Simulated",
      "Not financial advice",
    ]);
    for (const message of screen.getAllByTestId("krell-message-krell")) {
      expect(message.textContent).not.toMatch(/simulated|financial advice/i);
    }
  });

  it("draws one pill per label, named exactly", () => {
    const { rerender } = render(
      <ToolResultCard
        name="backtest"
        result={{ ok: true, effect: "unchanged", tool: "backtest", labels: ["simulated", "not_advice", "paper"] }}
      />,
    );
    expect(screen.getAllByTestId("krell-label").map((pill) => pill.textContent)).toEqual([
      "Simulated",
      "Not financial advice",
      "Paper",
    ]);
    rerender(<ToolResultCard name="read" result={{ ok: true, effect: "unchanged", tool: "read", labels: [] }} />);
    expect(screen.queryAllByTestId("krell-label")).toHaveLength(0);
  });

  it("model text cannot produce an 'Armed' card without a tool result", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        return sseResponse([
          { type: "text_delta", text: "Armed on paper! " },
          { type: "text_delta", text: "Simulated. Not financial advice." },
          { type: "message_done", text: "Armed on paper! Simulated. Not financial advice." },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    await send("Arm it");
    await screen.findByText("Armed on paper! Simulated. Not financial advice.");
    await idle();

    expect(screen.queryAllByTestId("krell-item-tool")).toHaveLength(0);
    expect(screen.queryAllByTestId("krell-item-confirm")).toHaveLength(0);
    expect(screen.queryAllByTestId("krell-label")).toHaveLength(0);
    expect(document.querySelectorAll(".kbot-krell__card")).toHaveLength(0);
    // The words live only in Krell's own bubble, as plain text.
    const bubble = screen.getByText("Armed on paper! Simulated. Not financial advice.");
    expect(bubble.closest("[data-testid='krell-message-krell']")).not.toBeNull();
  });

  it("a paper_arm tool result draws the Armed card with its Paper label", async () => {
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(),
          stored("assistant", { text: "", tool_calls: [{ id: "call_1", name: "paper_arm", arguments: {} }] }),
          stored("tool", { tool_call_id: "call_1", name: "paper_arm", result: ARMED }),
        ]),
      ),
    );
    renderTab();
    const card = await screen.findByTestId("krell-item-tool");
    expect(card.textContent).toContain("paper_arm");
    expect(card.textContent).toContain("Armed on paper");
    expect(within(card).getAllByTestId("krell-label").map((pill) => pill.textContent)).toEqual(["Paper"]);
  });

  it("a refusal card shows the code and message, and a live target gets no confirm card", async () => {
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(),
          stored("assistant", { text: "", tool_calls: [{ id: "call_1", name: "paper_arm", arguments: { mode: "live" } }] }),
          stored("tool", {
            tool_call_id: "call_1",
            name: "paper_arm",
            result: {
              ok: false,
              effect: "refused",
              code: "mcp_live_arm_refused",
              message: "MCP refuses live arm; paper-arm only",
              tool: "paper_arm",
              labels: [],
            },
          }),
        ]),
      ),
    );
    renderTab();
    const card = await screen.findByTestId("krell-item-tool");
    expect(card.textContent).toContain("Refused");
    expect(card.textContent).toContain("mcp_live_arm_refused");
    expect(card.textContent).toContain("MCP refuses live arm; paper-arm only");
    expect(card.textContent).not.toContain("Armed");
    expect(screen.queryByTestId("krell-item-confirm")).toBeNull();
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });

  it("labels the model's tool name as the tool, so a name that reads like an outcome is not one", async () => {
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(),
          stored("assistant", { text: "", tool_calls: [{ id: "call_1", name: "Armed on paper", arguments: {} }] }),
          stored("tool", {
            tool_call_id: "call_1",
            name: "Armed on paper",
            result: {
              ok: false,
              effect: "refused",
              code: "mcp_unknown_tool",
              message: "MCP tool is not in the allowlist",
              tool: "armed_on_paper",
              labels: [],
            },
          }),
        ]),
      ),
    );
    renderTab();
    const card = await screen.findByTestId("krell-item-tool");
    // The name is the model's text: it shows quoted as a name, never as a plain-words outcome.
    expect(card.textContent).toContain('Couldn\'t run "Armed on paper"');
    expect(card.textContent).not.toContain("Ran ");
    expect(within(card).queryByText("Armed on paper")).toBeNull();
    // The outcome is the engine's: a refusal, never "Armed".
    expect(card.querySelector(".kbot-krell__card-outcome")?.textContent).toBe("Refused");
    expect(card.textContent).toContain("mcp_unknown_tool");
    expect(within(card).queryAllByTestId("krell-label")).toHaveLength(0);
  });
});

describe("Activity in plain words", () => {
  const OK = { ok: true, effect: "changed", labels: [] };

  function toolRows(...calls: Array<{ name: string; result: Record<string, unknown> }>) {
    return calls.flatMap((call, i) => [
      stored("assistant", { text: "", tool_calls: [{ id: `call_${i}`, name: call.name, arguments: {} }] }),
      stored("tool", { tool_call_id: `call_${i}`, name: call.name, result: { tool: call.name, ...call.result } }),
    ]);
  }

  it("says what a step did in plain words, never 'Tool: name, outcome'", () => {
    render(<ToolResultCard name="set_profile" result={{ ...OK, tool: "set_profile" }} />);
    expect(screen.getByText("Saved your profile")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/Tool:|outcome|set_profile/);
  });

  it("names a draft by its label, and a market by the pair and timeframe the result carries", () => {
    const { rerender } = render(
      <ToolResultCard
        name="draft"
        result={{ ...OK, tool: "draft", result: { outcome: "created", pack: { label: "Rebound Rider" } } }}
      />,
    );
    expect(screen.getByText("Drafted 'Rebound Rider'")).toBeTruthy();
    rerender(
      <ToolResultCard
        name="market_snapshot"
        result={{ ...OK, effect: "unchanged", tool: "market_snapshot", pair: "BTC/USD", timeframe: "4h" }}
      />,
    );
    expect(screen.getByText("Loaded BTC/USD 4h prices")).toBeTruthy();
  });

  it("reads a missing-data refusal in plain words and keeps the engine's message and code", () => {
    render(
      <ToolResultCard
        name="market_snapshot"
        result={{
          ok: false,
          effect: "refused",
          code: "market_snapshot_not_found",
          message: "no cached candles for this venue, pair and timeframe",
          tool: "market_snapshot",
          labels: [],
        }}
      />,
    );
    const card = document.querySelector(".kbot-krell__card--refused") as HTMLElement;
    expect(card.textContent).toContain("No price data for that market yet");
    expect(card.textContent).toContain("no cached candles for this venue, pair and timeframe");
    expect(card.textContent).toContain("market_snapshot_not_found");
    expect(card.querySelector(".kbot-krell__card-outcome")?.textContent).toBe("Refused");
  });

  const NOT_FOUND = {
    ok: false,
    effect: "refused",
    code: "market_snapshot_not_found",
    message: "no cached candles for this venue, pair and timeframe",
    labels: [],
  };

  it("names the market a refused call asked for, from the call's own arguments", async () => {
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(),
          stored("assistant", {
            text: "",
            tool_calls: [
              { id: "call_a", name: "market_snapshot", arguments: { venue: "kraken", pair: "BTC/USD", timeframe: "4h" } },
              { id: "call_b", name: "market_snapshot", arguments: { venue: "kraken" } },
            ],
          }),
          stored("tool", { tool_call_id: "call_a", name: "market_snapshot", result: { tool: "market_snapshot", ...NOT_FOUND } }),
          stored("tool", { tool_call_id: "call_b", name: "market_snapshot", result: { tool: "market_snapshot", ...NOT_FOUND } }),
        ]),
      ),
    );
    renderTab();
    await screen.findAllByTestId("krell-item-tool");
    const rows = screen.getAllByTestId("krell-item-tool");
    expect(rows[0].textContent).toContain("No price data for BTC/USD 4h yet");
    expect(rows[1].textContent).toContain("No price data for that market yet");
    expect(rows[1].textContent).not.toContain("BTC/USD");
  });

  function refusedWith(args: Record<string, unknown>): string {
    const { container } = render(
      <ToolResultCard name="market_snapshot" result={{ tool: "market_snapshot", ...NOT_FOUND }} asked={args} />,
    );
    return container.querySelector(".kbot-krell__card-line")?.textContent ?? "";
  }

  it("reads the plain pair and timeframe a refused call sent, upper-cased", () => {
    expect(refusedWith({ pair: "btc/usd", timeframe: "4h" })).toBe("No price data for BTC/USD 4h yet");
  });

  it("falls back to the generic line when the pair is model text, not a pair", () => {
    for (const pair of ["Armed on paper. All good", "BTC USD", "BTC/USD!", "<b>BTC</b>"]) {
      cleanup();
      expect(refusedWith({ pair, timeframe: "4h" })).toBe("No price data for 4h yet");
      cleanup();
      expect(refusedWith({ pair })).toBe("No price data for that market yet");
    }
  });

  it("falls back to the generic line when the pair is over-long", () => {
    expect(refusedWith({ pair: "A".repeat(13) })).toBe("No price data for that market yet");
    expect(refusedWith({ pair: "BTC/" + "U".repeat(13) })).toBe("No price data for that market yet");
  });

  it("drops a bad timeframe and keeps a good pair", () => {
    expect(refusedWith({ pair: "BTC/USD", timeframe: "all day long" })).toBe("No price data for BTC/USD yet");
    expect(refusedWith({ pair: "BTC/USD", timeframe: "4 hours" })).toBe("No price data for BTC/USD yet");
  });

  it("names the market of a streamed refusal from the call's start event", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        return sseResponse([
          { type: "tool_start", id: "call_s", name: "market_snapshot", arguments: { pair: "ETH/USD", timeframe: "1h" } },
          { type: "tool_result", id: "call_s", name: "market_snapshot", result: { tool: "market_snapshot", ...NOT_FOUND } },
          { type: "message_done", text: "No data." },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab();
    await send("Show ETH");
    await screen.findByText("No data.");
    expect(screen.getByTestId("krell-item-tool").textContent).toContain("No price data for ETH/USD 1h yet");
  });

  it("collapses a reply's steps into 'N steps' that expands to one plain line each", async () => {
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(),
          stored("user", { text: "Set me up" }),
          ...toolRows(
            { name: "set_profile", result: OK },
            { name: "draft", result: { ...OK, result: { outcome: "created", pack: { label: "Rebound Rider" } } } },
            { name: "backtest", result: { ...OK, effect: "unchanged", labels: ["simulated", "not_advice"] } },
          ),
          stored("assistant", { text: "All three are done." }),
        ]),
      ),
    );
    renderTab();
    const group = await screen.findByTestId("krell-item-tool");
    expect(screen.getAllByTestId("krell-item-tool")).toHaveLength(1);
    const toggle = within(group).getByRole("button", { name: /3 steps/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const list = group.querySelector("ol") as HTMLElement;
    expect(list.hidden).toBe(true);
    // Pills stay on the summary while collapsed, once each.
    expect(within(group).getAllByTestId("krell-label").map((pill) => pill.textContent)).toEqual([
      "Simulated",
      "Not financial advice",
    ]);
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(list.hidden).toBe(false);
    expect(within(list).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Done: Saved your profile",
      "Done: Drafted 'Rebound Rider'",
      "Done: Ran a backtest on past data",
    ]);
    expect(document.body.textContent).not.toMatch(/Tool:|outcome:/);
    fireEvent.click(toggle);
    expect(list.hidden).toBe(true);
  });

  it("calls one step '1 step'", async () => {
    installFetch(() =>
      jsonResponse(conversation([...greeted(), ...toolRows({ name: "set_profile", result: OK })])),
    );
    renderTab();
    expect(await screen.findByRole("button", { name: /1 step$/ })).toBeTruthy();
  });

  it("keeps every refusal visible outside the collapse, in order, even when repeated", async () => {
    const refused = {
      ok: false,
      effect: "refused",
      code: "market_snapshot_not_found",
      message: "no cached candles for this venue, pair and timeframe",
      labels: [],
    };
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(),
          ...toolRows(
            { name: "set_profile", result: OK },
            { name: "read", result: { ...OK, effect: "unchanged" } },
            { name: "market_snapshot", result: refused },
            { name: "market_snapshot", result: refused },
            { name: "market_snapshot", result: refused },
            { name: "market_snapshot", result: refused },
            { name: "draft", result: OK },
          ),
        ]),
      ),
    );
    renderTab();
    await screen.findAllByTestId("krell-item-tool");
    const rows = screen.getAllByTestId("krell-item-tool");
    expect(rows).toHaveLength(6);
    expect(within(rows[0]).getByRole("button", { name: /2 steps/ })).toBeTruthy();
    for (const row of rows.slice(1, 5)) {
      expect(within(row).queryByRole("button")).toBeNull();
      expect(row.textContent).toContain("No price data for that market yet");
      expect(row.textContent).toContain("market_snapshot_not_found");
    }
    expect(within(rows[5]).getByRole("button", { name: /1 step$/ })).toBeTruthy();
  });

  it("starts a new group after Krell speaks, and leaves confirm and Armed cards ungrouped", async () => {
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(),
          ...toolRows({ name: "set_profile", result: OK }),
          stored("assistant", { text: "Profile saved." }),
          ...toolRows({ name: "validate", result: { ...OK, result: { state: "validated", errors: [], pack: { label: "Rebound Rider" } } } }),
          stored("assistant", { text: "", tool_calls: [{ id: "call_a", name: "paper_arm", arguments: {} }] }),
          stored("tool", { tool_call_id: "call_a", name: "paper_arm", result: ARMED }),
        ]),
      ),
    );
    renderTab();
    await screen.findByText("Profile saved.");
    const rows = screen.getAllByTestId("krell-item-tool");
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByRole("button", { name: /1 step$/ })).toBeTruthy();
    expect(within(rows[1]).getByRole("button", { name: /1 step$/ })).toBeTruthy();
    // The Armed card is the existing card, not a step.
    expect(within(rows[2]).queryByRole("button")).toBeNull();
    expect(rows[2].textContent).toContain("Armed on paper");
  });

  it("never lets an unknown tool name read as an outcome, in any state", () => {
    const { rerender } = render(<ToolResultCard name="Saved your profile" result={{ ok: true, effect: "changed", tool: "x" }} />);
    expect(document.body.textContent).toContain('Ran "Saved your profile"');
    expect(screen.queryByText("Saved your profile")).toBeNull();
    rerender(<ToolResultCard name="Saved your profile" result={null} />);
    expect(document.body.textContent).toContain('Running "Saved your profile"');
    expect(screen.queryByText("Saved your profile")).toBeNull();
  });

  it("styles a refusal with the caution colour, not a success colour", () => {
    const css = readFileSync(join(here, "krell.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = css.match(/\.kbot-krell__card--refused[^{]*\.kbot-krell__card-outcome[^{]*\{([^}]*)\}/);
    expect(rule?.[1]).toMatch(/var\(--caution-text\)/);
    expect(rule?.[1]).not.toMatch(/sage|seafoam|success/);
  });
});

describe("Confirm cards", () => {
  it("shows the tool, params, limits and estimate, with Confirm as the one primary button", async () => {
    installFetch(() => jsonResponse(conversation(askedToArm())));
    renderTab();
    const card = await screen.findByTestId("krell-item-confirm");
    const text = card.textContent ?? "";
    expect(text).toContain("paper_arm");
    for (const shown of ["venue", "kraken", "pair", "SUI/USD", "paper_balance", "500"]) expect(text).toContain(shown);
    expect(text).toContain("max paper balance");
    expect(text).toContain("1000");
    expect(text).toContain("require daily loss limit");
    expect(text).toContain("about 120 s");
    expect(text).toContain("about 6,000 tokens");

    const confirm = cardButton("Confirm", card);
    const notNow = cardButton("Not now", card);
    expect(confirm.disabled).toBe(false);
    expect(notNow.disabled).toBe(false);
    expect(confirm.className).toContain("kbot-krell__go");
    expect(notNow.className).not.toContain("kbot-krell__go");
    // One sage primary in the conversation: the composer's Send is not one.
    expect(document.querySelectorAll(".kbot-krell__go")).toHaveLength(1);
    expect(screen.getByTestId("krell-send").className).not.toContain("kbot-krell__go");
    const css = readFileSync(join(here, "krell.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(css.match(/\.kbot-krell \.kbot-krell__go\s*\{([^}]*)\}/)?.[1]).toMatch(/background:\s*var\(--action-primary-bg\)/);
    expect(css.match(/\.kbot-krell \.kbot-krell__send\s*\{([^}]*)\}/)?.[1]).not.toMatch(/action-primary|sage/);
    expectNoTicket();
  });

  it("confirm card fires onConfirm once and then disables", () => {
    const onConfirm = vi.fn();
    const onDecline = vi.fn();
    render(<ConfirmCard request={confirmRequest()} onConfirm={onConfirm} onDecline={onDecline} />);
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onDecline).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Confirm" })).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Not now" })).toHaveProperty("disabled", true);
  });

  it("expired confirm card cannot be confirmed", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmCard request={confirmRequest({ expires_at: Date.now() / 1000 - 1 })} onConfirm={onConfirm} onDecline={vi.fn()} />,
    );
    expect(screen.getByText("Expired")).toBeDefined();
    expect(screen.getByRole("button", { name: "Confirm" })).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Not now" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("turns to Expired at expires_at while it is shown", () => {
    vi.useFakeTimers();
    const onConfirm = vi.fn();
    render(<ConfirmCard request={confirmRequest({ expires_at: inFuture(5) })} onConfirm={onConfirm} onDecline={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Confirm" })).toHaveProperty("disabled", false);
    expect(screen.queryByText("Expired")).toBeNull();
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.getByText("Expired")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Not now settles the card and disables it", async () => {
    const calls = installFetch((call) => {
      if (call.path === "/api/v1/krell/confirm") {
        return sseResponse([DECIDED_DECLINE, { type: "message_done", text: "Okay, I left it." }, { type: "done" }]);
      }
      return jsonResponse(conversation(askedToArm()));
    });
    renderTab();
    await screen.findByTestId("krell-item-confirm");
    fireEvent.click(cardButton("Not now"));
    await screen.findByText("Okay, I left it.");
    await idle();

    expect(calls.filter((c) => c.path === "/api/v1/krell/confirm").map((c) => c.body)).toEqual([
      { ticket_id: TICKET, decision: "decline" },
    ]);
    expect(confirmCard().textContent).toContain("Not now. Nothing ran.");
    expect(cardButton("Confirm").disabled).toBe(true);
    expect(cardButton("Not now").disabled).toBe(true);
    expectNoTicket();
  });

  it("a Confirm that ran settles the card and draws the result card", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/confirm") {
        return sseResponse(confirmedStream(ARMED, [{ type: "message_done", text: "It is armed on paper." }]));
      }
      return jsonResponse(conversation(askedToArm()));
    });
    renderTab();
    await screen.findByTestId("krell-item-confirm");
    fireEvent.click(cardButton("Confirm"));
    await screen.findByText("It is armed on paper.");
    await idle();

    expect(confirmCard().textContent).toContain("Confirmed.");
    expect(cardButton("Confirm").disabled).toBe(true);
    expect(cardButton("Not now").disabled).toBe(true);
    const results = screen.getAllByTestId("krell-item-tool");
    expect(results[results.length - 1].textContent).toContain("Armed on paper");
    // A settled card gives up the primary: the composer is the way on.
    expect(document.querySelectorAll(".kbot-krell__go")).toHaveLength(0);
    expectNoTicket();
  });

  it("a refused Confirm shows the refusal and stays pressable so the person can press again", async () => {
    let presses = 0;
    const calls = installFetch((call) => {
      if (call.path === "/api/v1/krell/confirm") {
        presses += 1;
        if (presses === 1) {
          return sseResponse(
            confirmedStream(
              {
                ok: false,
                effect: "refused",
                code: "agent_limit_balance",
                message: "The paper balance is over the agent limit.",
                tool: "paper_arm",
                labels: ["paper"],
              },
              [{ type: "message_done", text: "Raise the limit and press Confirm again." }],
            ),
          );
        }
        return sseResponse(confirmedStream(ARMED, [{ type: "message_done", text: "It is armed on paper." }]));
      }
      return jsonResponse(conversation(askedToArm()));
    });
    renderTab();
    await screen.findByTestId("krell-item-confirm");
    fireEvent.click(cardButton("Confirm"));
    await screen.findByText("Raise the limit and press Confirm again.");
    await idle();

    expect(confirmCard().textContent).toContain("agent_limit_balance");
    expect(confirmCard().textContent).toContain("The paper balance is over the agent limit.");
    expect(cardButton("Confirm").disabled).toBe(false);
    expect(cardButton("Not now").disabled).toBe(false);

    fireEvent.click(cardButton("Confirm"));
    await screen.findByText("It is armed on paper.");
    await idle();
    expect(calls.filter((c) => c.path === "/api/v1/krell/confirm").map((c) => c.body)).toEqual([
      { ticket_id: TICKET, decision: "confirm" },
      { ticket_id: TICKET, decision: "confirm" },
    ]);
    expect(confirmCard().textContent).toContain("Confirmed.");
    expect(cardButton("Confirm").disabled).toBe(true);
    expectNoTicket();
  });

  it("an expired or forgotten ticket reads 'Expired. Ask Krell again.' and disables the card", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/confirm") {
        return sseResponse(
          confirmedStream(
            {
              ok: false,
              effect: "refused",
              code: "agent_confirm_unknown",
              message: "That confirm is no longer active.",
              tool: "paper_arm",
              labels: [],
            },
            [{ type: "error", code: "agent_confirm_unknown", message: "That confirm is no longer active." }],
          ),
        );
      }
      return jsonResponse(conversation(askedToArm()));
    });
    renderTab();
    await screen.findByTestId("krell-item-confirm");
    fireEvent.click(cardButton("Confirm"));
    await waitFor(() => expect(confirmCard().textContent).toContain(UNKNOWN_TEXT));
    await idle();
    expect(cardButton("Confirm").disabled).toBe(true);
    expect(cardButton("Not now").disabled).toBe(true);
    expectNoTicket();
  });

  it("a press the engine no longer knows before any decision also expires the card", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/confirm") {
        return sseResponse([
          { type: "error", code: "agent_confirm_unknown", message: "That confirm is no longer active." },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(askedToArm()));
    });
    renderTab();
    await screen.findByTestId("krell-item-confirm");
    fireEvent.click(cardButton("Not now"));
    await waitFor(() => expect(confirmCard().textContent).toContain(UNKNOWN_TEXT));
    await idle();
    expect(cardButton("Confirm").disabled).toBe(true);
    expect(cardButton("Not now").disabled).toBe(true);
  });

  it("disables both buttons while a press is in flight", async () => {
    const encoder = new TextEncoder();
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const calls = installFetch((call) => {
      if (call.path === "/api/v1/krell/confirm") {
        const body = new ReadableStream<Uint8Array>({
          start(c) {
            controller = c;
          },
        });
        return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      return jsonResponse(conversation(askedToArm()));
    });
    renderTab();
    await screen.findByTestId("krell-item-confirm");
    fireEvent.click(cardButton("Confirm"));
    expect(cardButton("Confirm").disabled).toBe(true);
    expect(cardButton("Not now").disabled).toBe(true);
    fireEvent.click(cardButton("Confirm"));
    fireEvent.click(cardButton("Not now"));
    await waitFor(() => expect(calls.filter((c) => c.path === "/api/v1/krell/confirm")).toHaveLength(1));

    controller.enqueue(encoder.encode(confirmedStream(ARMED).map((e, i) => frame(e, i + 1)).join("")));
    controller.close();
    await waitFor(() => expect(confirmCard().textContent).toContain("Confirmed."));
    await idle();
    expect(cardButton("Confirm").disabled).toBe(true);
    expect(calls.filter((c) => c.path === "/api/v1/krell/confirm")).toHaveLength(1);
  });

  it.each([
    ["the connection drops mid-stream", [] as Array<Record<string, unknown>>],
    [
      "the store fails after the call ran",
      [
        {
          type: "error",
          code: "krell_store_unavailable",
          message: "Krell's conversation store could not be read or written, so nothing changed.",
        },
        { type: "done" },
      ],
    ],
  ])("a press whose stream ends with no result says it cannot tell and stays disabled: %s", async (_case, tail) => {
    const encoder = new TextEncoder();
    let later!: ReadableStreamDefaultController<Uint8Array>;
    const calls = installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        const body = new ReadableStream<Uint8Array>({
          start(c) {
            later = c;
          },
        });
        return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      if (call.path === "/api/v1/krell/confirm") {
        return sseResponse([
          DECIDED_CONFIRM,
          { type: "tool_start", id: "call_arm", name: "paper_arm", arguments: { venue: "kraken" } },
          ...tail,
        ]);
      }
      return jsonResponse(conversation(askedToArm()));
    });
    renderTab();
    await screen.findByTestId("krell-item-confirm");
    fireEvent.click(cardButton("Confirm"));
    await waitFor(() => expect(confirmCard().textContent).toContain(UNCLEAR_TEXT));
    await idle();

    expect(confirmCard().textContent).not.toContain("Running…");
    expect(cardButton("Confirm").disabled).toBe(true);
    expect(cardButton("Not now").disabled).toBe(true);
    // The call's own card does not claim it is still running either.
    const results = screen.getAllByTestId("krell-item-tool");
    expect(results[results.length - 1].textContent).not.toContain("Running…");
    expect(results[results.length - 1].textContent).toContain(UNCLEAR_TEXT);
    // Reconcile, never resubmit: nothing is sent again.
    fireEvent.click(cardButton("Confirm"));
    fireEvent.click(cardButton("Not now"));
    expect(calls.filter((c) => c.path === "/api/v1/krell/confirm")).toHaveLength(1);

    // A later stream does not make either card claim the call is running again.
    await send("Did it run?");
    await screen.findByText("Krell is answering…");
    expect(confirmCard().textContent).toContain(UNCLEAR_TEXT);
    expect(document.body.textContent).not.toContain("Running…");
    later.enqueue(encoder.encode(frame({ type: "message_done", text: "Reload to check." }, 1) + frame({ type: "done" }, 2)));
    later.close();
    await idle();
    expect(calls.filter((c) => c.path === "/api/v1/krell/confirm")).toHaveLength(1);
    expectNoTicket();
  });

  it("a card decided before a reload shows as decided", async () => {
    const OTHER = "tkt-ALSO-NEVER-IN-THE-DOM-fedcba987654";
    installFetch(() =>
      jsonResponse(
        conversation([
          ...greeted(),
          stored("assistant", { text: "", tool_calls: [{ id: "call_a", name: "paper_arm", arguments: {} }] }),
          cardRow("call_a", TICKET),
          // Not now is stored without a call id; the provider matches it to its card.
          stored("notice", { kind: "confirm_decision", decision: "decline", ticket_id: TICKET, tool: "paper_arm" }),
          stored("assistant", { text: "", tool_calls: [{ id: "call_b", name: "paper_arm", arguments: {} }] }),
          cardRow("call_b", OTHER),
          stored("notice", {
            kind: "confirm_decision",
            decision: "confirm",
            ticket_id: OTHER,
            tool: "paper_arm",
            call_id: "call_b",
            result: {
              ok: false,
              effect: "refused",
              code: "agent_limit_balance",
              message: "The paper balance is over the agent limit.",
              tool: "paper_arm",
              labels: ["paper"],
            },
          }),
        ]),
      ),
    );
    renderTab();
    await waitFor(() => expect(screen.getAllByTestId("krell-item-confirm")).toHaveLength(2));
    const [declined, refused] = screen.getAllByTestId("krell-item-confirm");
    expect(declined.textContent).toContain("Not now. Nothing ran.");
    expect(cardButton("Confirm", declined).disabled).toBe(true);
    expect(refused.textContent).toContain("agent_limit_balance");
    expect(cardButton("Confirm", refused).disabled).toBe(false);
    // The one primary is the card still waiting for the person.
    expect(cardButton("Confirm", refused).className).toContain("kbot-krell__go");
    expect(cardButton("Confirm", declined).className).not.toContain("kbot-krell__go");
    expect(document.body.innerHTML).not.toContain(TICKET);
    expect(document.body.innerHTML).not.toContain(OTHER);
  });
});

describe("Budget meter", () => {
  it("shows conversation and month usage against the limits", () => {
    render(
      <BudgetMeter
        budget={budget({ conversation_used: 1200, conversation_limit: 200000, month_used: 41000, month_limit: 500000 })}
      />,
    );
    const meter = screen.getByTestId("krell-budget");
    expect(meter.textContent).toContain("1,200 of 200,000 tokens");
    expect(meter.textContent).toContain("41,000 of 500,000 tokens");
    const bars = Array.from(meter.querySelectorAll("meter"));
    expect(bars.map((bar) => [bar.value, bar.max])).toEqual([
      [1200, 200000],
      [41000, 500000],
    ]);
    expect(screen.queryByText(BUDGET_TEXT)).toBeNull();
  });

  it("reads 'Token budget reached. Raise it in Settings → Agents & limits.' when exhausted", () => {
    render(
      <BudgetMeter budget={budget({ month_used: 500000, month_limit: 500000, exhausted: true, reason: "month" })} />,
    );
    expect(screen.getByText(BUDGET_TEXT).textContent).toBe(BUDGET_TEXT);
  });

  it("fills the bar of a reached limit, even a limit of 0", () => {
    render(
      <BudgetMeter
        budget={budget({
          conversation_used: 0,
          conversation_limit: 0,
          month_used: 1000,
          month_limit: 2000000,
          exhausted: true,
          reason: "conversation",
        })}
      />,
    );
    const [conversationBar, monthBar] = Array.from(screen.getByTestId("krell-budget").querySelectorAll("meter"));
    expect(conversationBar.max).toBeGreaterThan(0);
    expect(conversationBar.value).toBe(conversationBar.max);
    expect(monthBar.value).toBe(1000);
    expect(screen.getByText(BUDGET_TEXT)).toBeDefined();
  });

  it("follows the conversation payload and the budget events", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        return sseResponse([
          { type: "message_done", text: "Hello." },
          { type: "usage", input_tokens: 1000, output_tokens: 500 },
          { type: "budget", ...budget({ conversation_used: 1500, month_used: 9500 }) },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(greeted(), budget({ month_used: 8000 })));
    });
    renderTab();
    const meter = await screen.findByTestId("krell-budget");
    expect(meter.textContent).toContain("0 of 200,000 tokens");
    expect(meter.textContent).toContain("8,000 of 2,000,000 tokens");
    await send("Hi");
    await screen.findByText("Hello.");
    await waitFor(() => expect(screen.getByTestId("krell-budget").textContent).toContain("1,500 of 200,000 tokens"));
    expect(screen.getByTestId("krell-budget").textContent).toContain("9,500 of 2,000,000 tokens");
  });
});

describe("Highlights", () => {
  /** Every selector string the page is queried with from here on. */
  function recordSelectors(): string[] {
    const selectors: string[] = [];
    type Queryable = Record<"querySelector" | "querySelectorAll" | "matches", (selector: string) => unknown>;
    const spy = (proto: object, method: keyof Queryable): void => {
      const target = proto as Queryable;
      const original = target[method];
      vi.spyOn(target, method).mockImplementation(function (this: unknown, selector: string) {
        selectors.push(selector);
        return original.call(this, selector);
      });
    };
    for (const proto of [Document.prototype, Element.prototype, DocumentFragment.prototype]) {
      spy(proto, "querySelector");
      spy(proto, "querySelectorAll");
    }
    spy(Element.prototype, "matches");
    return selectors;
  }

  function target(testId: string): HTMLElement {
    const el = document.createElement("button");
    el.setAttribute("data-testid", testId);
    document.body.append(el);
    return el;
  }

  it("highlight outlines the named element and removes it", () => {
    vi.useFakeTimers();
    const nav = target("nav-strategies");
    const { rerender } = renderHook(({ id, nonce }) => useKrellHighlight(id, nonce), {
      initialProps: { id: "nav-strategies" as string | null, nonce: 1 },
    });
    expect(nav.getAttribute("data-krell-highlight")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(3999);
    });
    expect(nav.getAttribute("data-krell-highlight")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(nav.hasAttribute("data-krell-highlight")).toBe(false);

    // Krell highlighting the same element again outlines it again.
    rerender({ id: "nav-strategies", nonce: 2 });
    expect(nav.getAttribute("data-krell-highlight")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(nav.hasAttribute("data-krell-highlight")).toBe(false);
  });

  it("an unknown test id is a no-op", () => {
    const nav = target("nav-strategies");
    expect(() => renderHook(() => useKrellHighlight("not-on-this-screen", 1))).not.toThrow();
    expect(nav.hasAttribute("data-krell-highlight")).toBe(false);
    expect(document.querySelectorAll("[data-krell-highlight]")).toHaveLength(0);
  });

  it("matches the test id exactly and never builds a selector from it", () => {
    const HOSTILE = 'a"], *, [data-testid="nav-strategies';
    const nav = target("nav-strategies");
    const odd = target(HOSTILE);
    const selectors = recordSelectors();

    renderHook(() => useKrellHighlight(HOSTILE, 1));
    expect(odd.getAttribute("data-krell-highlight")).toBe("true");
    expect(nav.hasAttribute("data-krell-highlight")).toBe(false);
    renderHook(() => useKrellHighlight("*", 2));
    expect(nav.hasAttribute("data-krell-highlight")).toBe(false);
    for (const selector of selectors) {
      expect(selector).not.toContain(HOSTILE);
      expect(selector).not.toContain("nav-strategies");
    }
  });

  it("a highlight event from Krell outlines the element on the current screen", async () => {
    installFetch((call) => {
      if (call.path === "/api/v1/krell/messages") {
        return sseResponse([
          { type: "tool_start", id: "call_hl", name: "highlight", arguments: { test_id: "nav-strategies" } },
          {
            type: "tool_result",
            id: "call_hl",
            name: "highlight",
            result: { ok: true, effect: "unchanged", tool: "highlight", labels: [], test_id: "nav-strategies" },
          },
          { type: "highlight", test_id: "nav-strategies" },
          { type: "message_done", text: "Strategies is up there." },
          { type: "done" },
        ]);
      }
      return jsonResponse(conversation(greeted()));
    });
    renderTab(
      <button type="button" data-testid="nav-strategies">
        Strategies
      </button>,
    );
    await send("Where are my strategies?");
    await screen.findByText("Strategies is up there.");
    expect(screen.getByTestId("nav-strategies").getAttribute("data-krell-highlight")).toBe("true");
  });

  it("is an outline only: no motion and nothing laid over controls", () => {
    const css = readFileSync(join(here, "krell.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const rules = [...css.matchAll(/([^{}]*\[data-krell-highlight="true"\][^{}]*)\{([^}]*)\}/g)];
    expect(rules.length).toBeGreaterThan(0);
    const body = rules.map((rule) => rule[2]).join(";");
    // Dashed, so a highlighted element does not read as focused (the focus ring is solid).
    expect(body).toMatch(/outline:\s*2px dashed var\(--seafoam\)/);
    for (const banned of ["animation", "transition", "position", "z-index", "background", "box-shadow", "content"]) {
      expect(body).not.toContain(banned);
    }
    expect(rules.some((rule) => /::?(before|after)/.test(rule[1]))).toBe(false);
  });
});
