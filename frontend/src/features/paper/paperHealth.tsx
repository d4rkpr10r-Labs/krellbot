import { useEffect, useState, type JSX } from "react";
import type { PaperClient, PaperStatusSelect } from "./client";

export type PaperHealth = {
  armed: boolean;
  venue?: string;
  pair?: string;
  pack_id?: string;
  state?: string;
  last_check?: number | null;
  last_success?: number | null;
  scheduler_installed?: boolean;
  scheduler_enabled?: boolean;
  scheduler_error?: boolean;
};

export function paperHealthSentence(health: PaperHealth | null): string {
  if (!health) return "Checking paper run status…";
  if (!health.armed) return "No paper run is active.";
  switch (health.state) {
    case "waiting": return "Waiting for the first market check.";
    case "paused": return "New buys are paused.";
    case "blocked": return "The paper run needs attention before it can place new simulated buys.";
    case "checked": return "Market checked. See the run history for decisions.";
    case "filled": return "Market checked. A simulated trade was made.";
    case "feed_error": return "Market data could not be fetched. Check your connection.";
    case "no_candles": return "No completed market candles are available yet.";
    case "stale_candles": return "Market data is out of date. Check the feed before relying on this run.";
    case "stale_check": return "The last market check is out of date. Check automatic scheduling.";
    case "stale_rules": return "Market rules are out of date. Refresh the exchange connection.";
    case "refused": return "The market check needs attention. Review the run history.";
    default: return "Paper run status is unavailable.";
  }
}

export function usePaperHealth(client: PaperClient | undefined, select?: PaperStatusSelect, refresh?: unknown): PaperHealth | null {
  const key = select ? `${select.venue}\u0000${select.pair}` : "";
  const [result, setResult] = useState<{ key: string; health: PaperHealth } | null>(null);
  useEffect(() => {
    if (!client?.getHealth) return;
    let active = true;
    let latestRequest = 0;
    setResult(null);
    const read = () => {
      const request = ++latestRequest;
      client.getHealth!(select).then(result => {
        if (active && request === latestRequest) setResult({ key, health: result });
      }).catch(() => {
        if (active && request === latestRequest) setResult({ key, health: { armed: true, state: "unavailable" } });
      });
    };
    read();
    const interval = window.setInterval(read, 30_000);
    return () => { active = false; window.clearInterval(interval); };
  }, [client, key, refresh]);
  return result?.key === key ? result.health : null;
}

export function PaperHealthDetails({ health }: { health: PaperHealth | null }): JSX.Element {
  const needsScheduler = health?.armed && (!health.scheduler_installed || !health.scheduler_enabled || health.scheduler_error);
  return <div data-testid="paper-run-health" role="status">
    <p>{paperHealthSentence(health)}</p>
    {health?.last_check ? <p>Last check: {new Date(health.last_check * 1000).toLocaleString()}</p> : null}
    {health?.last_success ? <p>Last successful check: {new Date(health.last_success * 1000).toLocaleString()}</p> : null}
    {needsScheduler ? <p>Automatic checks need attention. <a href="/?view=operations">Review automatic checks</a></p> : null}
  </div>;
}
