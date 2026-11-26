import { useEffect, useState, type JSX } from "react";
import type { PaperClient, PaperStatusSelect } from "./client";

/** Closed feed failure codes served on `/api/v1/paper/health` (M28.5). */
export const FEED_FAILURE_CODES = [
  "timeout",
  "dns_or_network",
  "tls",
  "region_or_permission",
  "rate_limited",
  "venue_error",
] as const;

export type FeedFailureCode = (typeof FEED_FAILURE_CODES)[number];

/** In-app Diagnostic report (Settings → This computer). */
export const DIAGNOSTIC_REPORT_HREF = "/?view=settings&section=computer";

type FeedRecovery = {
  cause: string;
  /** The recovery action; with `link`, the lead-in to the link text. */
  step: string;
  link?: { href: string; label: string };
};

/** One recovery action per closed failure code. */
const FEED_RECOVERY: Record<FeedFailureCode, FeedRecovery> = {
  timeout: {
    cause: "the exchange did not answer in time",
    step: "Wait for the next automatic check, which tries again.",
  },
  dns_or_network: {
    cause: "the exchange could not be reached",
    step: "Check that this computer is online and can reach the exchange.",
  },
  tls: {
    cause: "the secure connection to the exchange failed",
    step: "Check this computer's date and time, and any proxy or security software that inspects secure connections.",
  },
  region_or_permission: {
    cause: "the exchange refused requests from this network",
    step: "Run this strategy on an exchange that serves your region.",
  },
  rate_limited: {
    cause: "the exchange limited requests from this computer",
    step: "Wait a few minutes and stop other tools that query this exchange.",
  },
  venue_error: {
    cause: "the exchange returned an error",
    step: "If this repeats,",
    link: { href: DIAGNOSTIC_REPORT_HREF, label: "prepare a diagnostic report" },
  },
};

export type PaperHealth = {
  armed: boolean;
  venue?: string;
  pair?: string;
  pack_id?: string;
  state?: string;
  failure_code?: FeedFailureCode | null;
  last_check?: number | null;
  last_success?: number | null;
  scheduler_installed?: boolean;
  scheduler_enabled?: boolean;
  scheduler_error?: boolean;
};

function isFeedFailureCode(value: unknown): value is FeedFailureCode {
  return typeof value === "string" && (FEED_FAILURE_CODES as ReadonlyArray<string>).includes(value);
}

/** The classified feed failure for a feed error, or null when unknown. */
export function feedFailureCode(health: PaperHealth | null): FeedFailureCode | null {
  if (!health?.armed || health.state !== "feed_error") return null;
  return isFeedFailureCode(health.failure_code) ? health.failure_code : null;
}

export function paperHealthSentence(health: PaperHealth | null): string {
  if (!health) return "Checking paper run status…";
  if (!health.armed) return "No paper run is active.";
  switch (health.state) {
    case "waiting": return "Waiting for the first market check.";
    case "paused": return "New buys are paused.";
    case "blocked": return "The paper run needs attention before it can place new simulated buys.";
    case "checked": return "Market checked. See the run history for decisions.";
    case "filled": return "Market checked. A simulated trade was made.";
    case "feed_error": {
      const code = feedFailureCode(health);
      return code
        ? `Market data could not be fetched: ${FEED_RECOVERY[code].cause}.`
        : "Market data could not be fetched. Check your connection.";
    }
    case "check_incomplete": return "The market check did not complete. Review the other runs on this exchange.";
    case "no_candles": return "No completed market candles are available yet.";
    case "stale_candles": return "Market data is out of date. Check the feed before relying on this run.";
    case "stale_check": return "The last market check is out of date. Check automatic scheduling.";
    case "stale_rules": return "Market rules are out of date. Review automatic checks and retry.";
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

function FeedRecoveryStep({ code }: { code: FeedFailureCode }): JSX.Element {
  const recovery = FEED_RECOVERY[code];
  return <p data-testid="paper-feed-recovery">
    Failure code <code data-testid="paper-feed-failure-code">{code}</code>.{" "}
    <span data-testid="paper-feed-recovery-step">
      {recovery.step}
      {recovery.link ? <> <a href={recovery.link.href}>{recovery.link.label}</a>.</> : null}
    </span>
  </p>;
}

export function PaperHealthDetails({ health }: { health: PaperHealth | null }): JSX.Element {
  const needsScheduler = health?.armed && (!health.scheduler_installed || !health.scheduler_enabled || health.scheduler_error);
  const failureCode = feedFailureCode(health);
  return <div data-testid="paper-run-health" role="status">
    <p>{paperHealthSentence(health)}</p>
    {failureCode ? <FeedRecoveryStep code={failureCode} /> : null}
    {health?.last_check ? <p>Last check: {new Date(health.last_check * 1000).toLocaleString()}</p> : null}
    {health?.last_success ? <p>Last successful check: {new Date(health.last_success * 1000).toLocaleString()}</p> : null}
    {needsScheduler ? <p>Automatic checks need attention. <a href="/?view=operations">Review automatic checks</a></p> : null}
  </div>;
}
