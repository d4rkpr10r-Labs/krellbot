import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentActivity } from "./AgentActivity";
import type { AgentActivityClient, AgentActivityRow } from "./agentActivityClient";

afterEach(cleanup);

function row(over: Partial<AgentActivityRow> = {}): AgentActivityRow {
  return {
    ts: 1_700_000_200,
    actor: "mcp:cursor",
    tool: "arm_live",
    outcome: "refused",
    code: "mcp_live_arm_refused",
    venue: null,
    pair: null,
    confirm_ticket: null,
    tokens: null,
    ...over,
  };
}

function makeClient(rows: AgentActivityRow[]): AgentActivityClient {
  return { listAgentActivity: vi.fn().mockResolvedValue(rows) };
}

describe("AgentActivity", () => {
  it("shows who did what with the refusal code and links to the strategy", async () => {
    const client = makeClient([
      row(),
      row({
        actor: "krell",
        tool: "paper_arm",
        outcome: "ok",
        code: "ok",
        venue: "kraken",
        pair: "SUIUSD",
      }),
    ]);
    render(<AgentActivity client={client} />);

    const rows = await screen.findAllByTestId("operations-agent-activity-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("mcp:cursor")).toBeTruthy();
    expect(within(rows[0]).getByText("arm_live")).toBeTruthy();
    expect(within(rows[0]).getByText("refused")).toBeTruthy();
    const code = within(rows[0]).getByText("mcp_live_arm_refused");
    expect(code.className).toContain("kbot-ops__agent-activity-code");
    expect(within(rows[0]).queryByRole("link")).toBeNull();

    const link = within(rows[1]).getByRole("link");
    const url = new URL(link.getAttribute("href") ?? "", "http://localhost");
    expect(url.searchParams.get("view")).toBe("workstation");
    expect(url.searchParams.get("venue")).toBe("kraken");
    expect(url.searchParams.get("pair")).toBe("SUIUSD");
  });

  it("refetches with the chosen outcome filter", async () => {
    const client = makeClient([row()]);
    render(<AgentActivity client={client} />);
    await screen.findAllByTestId("operations-agent-activity-row");

    fireEvent.change(screen.getByLabelText("Outcome"), {
      target: { value: "refused" },
    });

    await waitFor(() =>
      expect(client.listAgentActivity).toHaveBeenLastCalledWith({
        actor: undefined,
        tool: undefined,
        outcome: "refused",
      }),
    );
  });

  it("says so when there is no activity and when the read fails", async () => {
    const { unmount } = render(<AgentActivity client={makeClient([])} />);
    expect(await screen.findByText("No agent activity for this filter.")).toBeTruthy();
    unmount();

    const failing: AgentActivityClient = {
      listAgentActivity: vi.fn().mockRejectedValue(new Error("boom")),
    };
    render(<AgentActivity client={failing} />);
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});
