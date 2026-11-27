export type AgentOutcome = "ok" | "refused" | "needs_confirm" | "error";

export const AGENT_OUTCOMES: ReadonlyArray<AgentOutcome> = [
  "ok",
  "refused",
  "needs_confirm",
  "error",
];

export type AgentActivityRow = {
  ts: number;
  actor: string;
  tool: string;
  outcome: AgentOutcome;
  code: string;
  venue: string | null;
  pair: string | null;
  confirm_ticket: string | null;
  tokens: Record<string, unknown> | null;
};

export type AgentActivityQuery = {
  actor?: string;
  tool?: string;
  outcome?: AgentOutcome;
  since?: number;
};

export type AgentActivityClient = {
  listAgentActivity(query?: AgentActivityQuery): Promise<AgentActivityRow[]>;
};

export function createAgentActivityHttpClient(): AgentActivityClient {
  return {
    async listAgentActivity(query = {}) {
      const params = new URLSearchParams();
      if (query.actor) params.set("actor", query.actor);
      if (query.tool) params.set("tool", query.tool);
      if (query.outcome) params.set("outcome", query.outcome);
      if (query.since !== undefined) params.set("since", String(query.since));
      const qs = params.toString();
      const response = await fetch(
        `/api/v1/agents/activity${qs ? `?${qs}` : ""}`,
        { credentials: "include" },
      );
      if (!response.ok) {
        throw new Error(`agent activity failed: ${response.status}`);
      }
      const body = (await response.json()) as { rows?: AgentActivityRow[] };
      return Array.isArray(body.rows) ? body.rows : [];
    },
  };
}
