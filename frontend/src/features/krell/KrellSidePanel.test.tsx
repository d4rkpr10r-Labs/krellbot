/**
 * Krell side panel, the "Ask Krell" button and the Krell tab label
 * (M38.16). Contract: KRELL-SIGNAL-20261010.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Navigation } from "../../shell/Navigation";
import { KrellConversationProvider } from "./KrellConversationProvider";
import { AskKrellButton, KrellSidePanel } from "./KrellSidePanel";
import { KrellView } from "./KrellView";

vi.mock("../../session", () => ({
  getCsrf: (): string => "test-csrf",
}));

const here = dirname(fileURLToPath(import.meta.url));

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

function conversationBody() {
  return {
    schema_version: "1",
    ok: true,
    effect: "unchanged",
    messages: [
      { id: 1, role: "notice", payload: { kind: "disclosure", text: "Sent to Anthropic.", connection: CONNECTION }, ts: 1 },
      {
        id: 2,
        role: "notice",
        payload: { kind: "opening", text: "Hi, I'm Krell.", question: "Standard or advanced?", quick_replies: [] },
        ts: 2,
      },
    ],
    profile: { mode: null, answers: {} },
    budget: null,
    connection: CONNECTION,
    disclosure: "Sent to Anthropic.",
  };
}

function frames(events: Array<Record<string, unknown>>): string {
  return events
    .map((e, i) => `id: ${i + 1}\r\nevent: ${String(e.type)}\r\ndata: ${JSON.stringify(e)}\r\n\r\n\r\n`)
    .join("");
}

function installFetch(): string[] {
  const paths: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      paths.push(`${init?.method ?? "GET"} ${path}`);
      if (path === "/api/v1/krell/messages") {
        return new Response(
          frames([
            { type: "text_delta", text: "Paper runs use " },
            { type: "text_delta", text: "simulated funds." },
            { type: "message_done", text: "Paper runs use simulated funds." },
            { type: "done" },
          ]),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      }
      return new Response(JSON.stringify(conversationBody()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return paths;
}

describe("Krell entry points", () => {
  it("renders the tab label and Ask Krell button as text with no image", () => {
    render(
      <>
        <Navigation active="home" onChange={() => {}} />
        <AskKrellButton open={false} onToggle={() => {}} />
      </>,
    );
    const tab = within(screen.getByTestId("kbot-nav-primary")).getByRole("button", { name: "Krell" });
    expect(tab.textContent).toBe("Krell");
    expect(tab.querySelector("img, svg, picture")).toBeNull();
    expect(tab.getAttribute("style")).toBeNull();

    const ask = screen.getByTestId("ask-krell");
    expect(ask.tagName).toBe("BUTTON");
    expect(ask.textContent).toBe("Ask Krell");
    expect(ask.querySelector("img, svg, picture")).toBeNull();
    expect(ask.getAttribute("style")).toBeNull();
    expect(ask.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("krell-avatar")).toBeNull();
  });
});

describe("Krell side panel", () => {
  it("tab and panel share one conversation", async () => {
    const paths = installFetch();
    const onOpenTab = vi.fn();
    const { rerender } = render(
      <KrellConversationProvider active>
        <KrellSidePanel onClose={() => {}} onOpenTab={onOpenTab} />
      </KrellConversationProvider>,
    );
    const panel = screen.getByTestId("krell-panel");
    const input = await within(panel).findByTestId("krell-input");
    fireEvent.change(input, { target: { value: "What is paper?" } });
    fireEvent.click(within(panel).getByTestId("krell-send"));
    await within(panel).findByText("Paper runs use simulated funds.");
    expect(within(panel).getAllByTestId("krell-message-krell")).toHaveLength(2);

    fireEvent.click(within(panel).getByRole("button", { name: "Open in Krell tab" }));
    expect(onOpenTab).toHaveBeenCalledTimes(1);

    rerender(
      <KrellConversationProvider active>
        <KrellView />
      </KrellConversationProvider>,
    );
    const view = screen.getByTestId("krell-view");
    expect(screen.queryByTestId("krell-panel")).toBeNull();
    expect(within(view).getByTestId("krell-message-user").textContent).toContain("What is paper?");
    expect(within(view).getByText("Paper runs use simulated funds.")).toBeDefined();
    // The tab's composer drives the same, idle conversation: it can send at once.
    fireEvent.change(within(view).getByTestId("krell-input"), { target: { value: "Thanks" } });
    await waitFor(() => expect(within(view).getByTestId("krell-send")).toHaveProperty("disabled", false));
    // One shared state: the tab did not load the conversation again.
    expect(paths.filter((p) => p === "GET /api/v1/krell/conversation")).toHaveLength(1);
  });

  it("closes on Escape from inside the panel", async () => {
    installFetch();
    const onClose = vi.fn();
    render(
      <KrellConversationProvider active>
        <KrellSidePanel onClose={onClose} onOpenTab={() => {}} />
      </KrellConversationProvider>,
    );
    const panel = screen.getByTestId("krell-panel");
    expect(document.activeElement).toBe(panel);
    const input = await within(panel).findByTestId("krell-input");
    fireEvent.keyDown(input, { key: "Escape", shiftKey: true });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("is a modal dialog only as a full-screen sheet", async () => {
    installFetch();
    const { rerender } = render(
      <KrellConversationProvider active>
        <KrellSidePanel onClose={() => {}} onOpenTab={() => {}} />
      </KrellConversationProvider>,
    );
    const panel = screen.getByTestId("krell-panel");
    await within(panel).findByTestId("krell-input");
    expect(panel.getAttribute("role")).toBeNull();
    expect(panel.getAttribute("aria-modal")).toBeNull();
    rerender(
      <KrellConversationProvider active>
        <KrellSidePanel modal onClose={() => {}} onOpenTab={() => {}} />
      </KrellConversationProvider>,
    );
    expect(screen.getByRole("dialog", { name: "Krell" })).toBe(panel);
    expect(panel.getAttribute("aria-modal")).toBe("true");
  });

  it("panel becomes a full-screen sheet at phone width", async () => {
    installFetch();
    render(
      <KrellConversationProvider active>
        <KrellSidePanel onClose={() => {}} onOpenTab={() => {}} />
      </KrellConversationProvider>,
    );
    const panel = screen.getByTestId("krell-panel");
    expect(panel.classList.contains("kbot-krell__panel")).toBe(true);
    await within(panel).findByTestId("krell-input");

    const css = readFileSync(join(here, "krell.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const base = css.slice(0, css.indexOf("@media"));
    const docked = base.match(/\.kbot-krell__panel\s*\{([^}]*)\}/);
    expect(docked?.[1]).toMatch(/position:\s*fixed/);
    expect(docked?.[1]).toMatch(/width:\s*340px/);
    expect(docked?.[1]).toMatch(/right:\s*0/);

    const phone = css.match(/@media\s*\(max-width:\s*720px\)\s*\{([\s\S]*?)\n\}/);
    expect(phone).not.toBeNull();
    const sheet = phone?.[1].match(/\.kbot-krell__panel\s*\{([^}]*)\}/);
    expect(sheet?.[1]).toMatch(/inset:\s*0/);
    expect(sheet?.[1]).toMatch(/width:\s*100%/);
    expect(sheet?.[1]).toMatch(/height:\s*100%/);
  });
});
