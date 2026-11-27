import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SavedStrategyLibrary, type LibraryClient, type OwnedDraftSummary } from "./SavedStrategyLibrary";

afterEach(cleanup);

function row(revision: string, origin: string): OwnedDraftSummary {
  return {
    revision_id: revision, strategy_id: "local-test", parent_revision_id: null,
    state: "validated", runnable: true, created_at: "2026-11-27T10:00:00Z",
    id: "local-test", label: `LOCAL TEST ${revision}`, pair: "SUIUSD", timeframe: "1h",
    origin,
  };
}

function client(rows: OwnedDraftSummary[]): LibraryClient {
  return {
    listOwned: async () => rows,
    getJson: async () => { throw new Error("Unexpected revision read"); },
  };
}

async function mount(rows: OwnedDraftSummary[]) {
  render(<SavedStrategyLibrary client={client(rows)} onReopen={() => {}} onCopyAsNew={() => {}} />);
  await screen.findByTestId("library-search-count");
}

function rowFor(revision: string): HTMLElement {
  const found = screen.getAllByTestId("library-row").find((el) => el.getAttribute("data-revision-id") === revision);
  if (!found) throw new Error(`no row ${revision}`);
  return found;
}

describe("SavedStrategyLibrary agent origin", () => {
  it("badges agent drafts 'Drafted by agent' and leaves workstation drafts plain", async () => {
    await mount([row("agent-rev", "mcp:cursor"), row("krell-rev", "krell"), row("own-rev", "workstation")]);

    const badge = within(rowFor("agent-rev")).getByTestId("library-row-agent-origin");
    expect(badge.textContent).toBe("Drafted by agent");
    expect(badge.getAttribute("title")).toBe("mcp:cursor");
    expect(within(rowFor("krell-rev")).getByTestId("library-row-agent-origin").getAttribute("title")).toBe("krell");
    expect(within(rowFor("own-rev")).queryByTestId("library-row-agent-origin")).toBeNull();
  });

  it("treats a row without an origin field as a workstation draft", async () => {
    const legacy = row("legacy-rev", "workstation") as Partial<OwnedDraftSummary> as OwnedDraftSummary;
    delete (legacy as Partial<OwnedDraftSummary>).origin;
    await mount([legacy]);
    expect(within(rowFor("legacy-rev")).queryByTestId("library-row-agent-origin")).toBeNull();
  });

  it("renders a hostile origin as inert text only", async () => {
    await mount([row("evil-rev", "mcp:<img src=x onerror=alert(1)>")]);
    const badge = within(rowFor("evil-rev")).getByTestId("library-row-agent-origin");
    expect(badge.textContent).toBe("Drafted by agent");
    expect(badge.querySelector("img")).toBeNull();
  });

  it("does not block paper arm for agent drafts", async () => {
    await mount([row("agent-rev", "mcp:cursor")]);
    const actions = rowFor("agent-rev");
    for (const name of ["Reopen", "Compare", "Copy as new"]) {
      const button = within(actions).getByRole("button", { name: new RegExp(name) });
      expect((button as HTMLButtonElement).disabled).toBe(false);
    }
  });
});
