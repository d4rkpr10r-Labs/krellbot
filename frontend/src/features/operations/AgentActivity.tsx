import { useEffect, useRef, useState, type JSX } from "react";
import "./AgentActivity.signal.css";
import { buildLocationForView } from "../../shell/route";
import {
  AGENT_OUTCOMES,
  type AgentActivityClient,
  type AgentActivityRow,
  type AgentOutcome,
} from "./agentActivityClient";

const EMPTY_TEXT = "No agent activity for this filter.";
const LOADING_TEXT = "Loading agent activity…";
const ERROR_TEXT =
  "Could not load agent activity — check the workstation is running.";
const HELPER_TEXT =
  "Every tool call by Krell or an MCP client, including refusals. Read-only record; agents never place orders or arm live.";

export type AgentActivityProps = {
  client: AgentActivityClient;
};

function formatLocalTime(ts: number): string {
  const date = new Date(ts * 1000);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function strategyHref(row: AgentActivityRow): string | null {
  if (!row.venue || !row.pair) return null;
  const search = new URLSearchParams({ venue: row.venue, pair: row.pair });
  return buildLocationForView("workstation", `?${search.toString()}`);
}

function addSeen(prev: string[], values: string[]): string[] {
  const next = new Set(prev);
  for (const v of values) next.add(v);
  return next.size === prev.length ? prev : [...next].sort();
}

export function AgentActivity({ client }: AgentActivityProps): JSX.Element {
  const [actor, setActor] = useState("");
  const [tool, setTool] = useState("");
  const [outcome, setOutcome] = useState<AgentOutcome | "">("");
  const [rows, setRows] = useState<AgentActivityRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [actors, setActors] = useState<string[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const seq = useRef(0);

  useEffect(() => {
    const mine = ++seq.current;
    setFailed(false);
    client
      .listAgentActivity({
        actor: actor || undefined,
        tool: tool || undefined,
        outcome: outcome || undefined,
      })
      .then((next) => {
        if (mine !== seq.current) return;
        setRows(next);
        setActors((prev) =>
          addSeen(
            prev,
            next.map((r) => r.actor),
          ),
        );
        setTools((prev) =>
          addSeen(
            prev,
            next.map((r) => r.tool),
          ),
        );
      })
      .catch(() => {
        if (mine !== seq.current) return;
        setFailed(true);
      });
  }, [client, actor, tool, outcome]);

  return (
    <div
      className="kbot-ops__agent-activity"
      data-testid="operations-agent-activity"
    >
      <p className="kbot-ops__agent-activity-helper">{HELPER_TEXT}</p>
      <div className="kbot-ops__agent-activity-filters">
        <label>
          Actor
          <select value={actor} onChange={(e) => setActor(e.target.value)}>
            <option value="">All</option>
            {actors.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        <label>
          Tool
          <select value={tool} onChange={(e) => setTool(e.target.value)}>
            <option value="">All</option>
            {tools.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label>
          Outcome
          <select
            value={outcome}
            onChange={(e) => setOutcome(e.target.value as AgentOutcome | "")}
          >
            <option value="">All</option>
            {AGENT_OUTCOMES.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        </label>
      </div>
      {failed ? (
        <p role="alert" className="kbot-ops__agent-activity-error">
          {ERROR_TEXT}
        </p>
      ) : rows === null ? (
        <p className="kbot-ops__agent-activity-muted">{LOADING_TEXT}</p>
      ) : rows.length === 0 ? (
        <p className="kbot-ops__agent-activity-muted">{EMPTY_TEXT}</p>
      ) : (
        <div className="kbot-ops__agent-activity-scroll">
          <table className="kbot-ops__agent-activity-table">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Actor</th>
                <th scope="col">Tool</th>
                <th scope="col">Outcome</th>
                <th scope="col">Code</th>
                <th scope="col">Strategy</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => {
                const href = strategyHref(row);
                return (
                  <tr
                    key={`${row.ts}-${i}`}
                    data-testid="operations-agent-activity-row"
                  >
                    <td>{formatLocalTime(row.ts)}</td>
                    <td>{row.actor}</td>
                    <td>{row.tool}</td>
                    <td
                      className={`kbot-ops__agent-outcome kbot-ops__agent-outcome--${row.outcome}`}
                    >
                      {row.outcome}
                    </td>
                    <td className="kbot-ops__agent-activity-code">
                      {row.code}
                    </td>
                    <td>
                      {href ? (
                        <a href={href}>
                          {row.venue} {row.pair}
                        </a>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
