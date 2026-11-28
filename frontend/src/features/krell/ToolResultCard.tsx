/**
 * Krell's tool calls as plain-words activity (M38.17, M39.6). Contract: KRELL-SIGNAL-20261010.
 *
 * Drawn only from a tool result the engine sent, never from model text, so
 * nothing Krell writes can make an "Armed" card. The outcome comes from
 * `result.ok`, `effect`, `code` and `message`; each step reads as one line
 * from `toolPhrases.ts`. A name the map does not know shows quoted, as
 * `Ran "name"`, so a model-chosen name never reads as an outcome. The
 * honesty pills come from `result.labels` exactly.
 *
 * Consecutive ordinary steps in one reply collapse into "N steps", which
 * expands to one line each; their pills stay on the summary line. A refusal,
 * a call that needs confirm, a call still running or of unclear outcome, and
 * the Armed card stay visible on their own.
 */
import { useId, useState, type JSX } from "react";
import type { KrellToolResult } from "./client";
import { UNCLEAR_OUTCOME } from "./ConfirmCard";
import type { KrellItem } from "./KrellConversationProvider";
import { attemptLine, confirmLine, doneLine, refusalLine, runningLine } from "./toolPhrases";

const LABELS = new Map([
  ["simulated", "Simulated"],
  ["not_advice", "Not financial advice"],
  ["paper", "Paper"],
]);

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/** One tool call as the log holds it. */
export type ToolStep = {
  key: string;
  /** The tool's name as the engine showed it. */
  name: string;
  /** The engine's result, or null while the call runs. */
  result: KrellToolResult | null;
  /** The stream that started this call has ended: with no result, it is unclear whether it ran. */
  ended?: boolean;
};

type Kind = "step" | "armed" | "confirm" | "refused" | "running" | "unclear";

function kindOf({ name, result, ended }: ToolStep): Kind {
  // No result once its stream has ended: the call may or may not have run.
  if (result === null) return ended ? "unclear" : "running";
  if (result.effect === "needs_confirm") return "confirm";
  if (result.ok !== true) return "refused";
  return str(result.tool) === "paper_arm" ? "armed" : "step";
}

/** One pill per known label in `result.labels`, in order, each once. */
function labelsOf(results: Array<KrellToolResult | null>): string[] {
  const shown: string[] = [];
  for (const result of results) {
    for (const label of Array.isArray(result?.labels) ? result.labels : []) {
      const text = typeof label === "string" ? LABELS.get(label) : undefined;
      if (text && !shown.includes(text)) shown.push(text);
    }
  }
  return shown;
}

function Pills({ labels }: { labels: string[] }): JSX.Element | null {
  if (labels.length === 0) return null;
  return (
    <ul className="kbot-krell__pills" aria-label="Result labels">
      {labels.map((label) => (
        <li key={label} className="kbot-krell__pill" data-testid="krell-label">
          {label}
        </li>
      ))}
    </ul>
  );
}

export type ToolResultCardProps = {
  /** The tool's name as the engine showed it. */
  name: string;
  /** The engine's result, or null while the call runs. */
  result: KrellToolResult | null;
  /** The stream that started this call has ended: with no result, it is unclear whether it ran. */
  ended?: boolean;
};

/** One call as a card of its own. */
export function ToolResultCard({ name, result, ended = false }: ToolResultCardProps): JSX.Element {
  const kind = kindOf({ key: "", name, result, ended });
  const labels = labelsOf([result]);
  if (kind === "armed") {
    // The Armed card keeps its own wording: it is the one outcome a person acts on.
    return (
      <div className="kbot-krell__card">
        <p className="kbot-krell__card-head">
          <span className="kbot-krell__card-label">Tool:</span>
          <span className="kbot-krell__card-tool">{name}</span>
          <span className="kbot-krell__sr">, outcome: </span>
          <span className="kbot-krell__card-outcome">Armed on paper</span>
        </p>
        <Pills labels={labels} />
      </div>
    );
  }
  if (result === null) {
    return (
      <div className="kbot-krell__card" aria-busy={kind === "running" || undefined}>
        <p className="kbot-krell__card-head">
          {kind === "running" ? (
            <span className="kbot-krell__card-line">{runningLine(name)}</span>
          ) : (
            <>
              <span className="kbot-krell__card-line">{attemptLine(name)}</span>
              <span className="kbot-krell__sr">, outcome: </span>
              <span className="kbot-krell__card-outcome">{UNCLEAR_OUTCOME}</span>
            </>
          )}
        </p>
      </div>
    );
  }
  if (kind === "confirm") {
    return (
      <div className="kbot-krell__card">
        <p className="kbot-krell__card-head">
          <span className="kbot-krell__card-line">{confirmLine(name, result)}</span>
        </p>
        <Pills labels={labels} />
      </div>
    );
  }
  if (kind === "refused") {
    const message = str(result.message);
    return (
      <div className="kbot-krell__card kbot-krell__card--refused">
        <p className="kbot-krell__card-head">
          <span className="kbot-krell__card-line">{refusalLine(name, result)}</span>
          <span className="kbot-krell__sr">, outcome: </span>
          <span className="kbot-krell__card-outcome">{result.effect === "refused" ? "Refused" : "Did not complete"}</span>
        </p>
        <Pills labels={labels} />
        <p className="kbot-krell__card-detail">
          {message ? `${message} ` : null}
          <code className="kbot-krell__code">{str(result.code, "unknown")}</code>
        </p>
      </div>
    );
  }
  return (
    <div className="kbot-krell__card">
      <p className="kbot-krell__card-head">
        <span className="kbot-krell__card-line">{doneLine(name, result)}</span>
      </p>
      <Pills labels={labels} />
    </div>
  );
}

/** "3 steps" over its pills; the one line per step is shown on request. */
function StepGroup({ steps }: { steps: ToolStep[] }): JSX.Element {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const results = steps.map((step) => step.result);
  return (
    <div className="kbot-krell__card kbot-krell__steps">
      <div className="kbot-krell__steps-head">
        <button
          type="button"
          className="kbot-krell__steps-toggle"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((value) => !value)}
        >
          <span className="kbot-krell__steps-chevron" aria-hidden="true">
            {open ? "▾" : "▸"}
          </span>
          {steps.length === 1 ? "1 step" : `${steps.length} steps`}
        </button>
        <Pills labels={labelsOf(results)} />
      </div>
      <ol id={listId} className="kbot-krell__step-list" hidden={!open}>
        {steps.map((step) => (
          <li key={step.key} className="kbot-krell__step">
            <span className="kbot-krell__sr">Done: </span>
            {doneLine(step.name, step.result as KrellToolResult)}
          </li>
        ))}
      </ol>
    </div>
  );
}

/**
 * The list items for a run of consecutive tool calls in one reply: ordinary
 * steps grouped, everything else a card of its own, in the order they came.
 */
export function ToolSteps({ steps }: { steps: ToolStep[] }): JSX.Element {
  const rows: JSX.Element[] = [];
  let run: ToolStep[] = [];
  const flush = (): void => {
    if (run.length === 0) return;
    rows.push(
      <li key={run[0].key} className="kbot-krell__card-row" data-testid="krell-item-tool">
        <StepGroup steps={run} />
      </li>,
    );
    run = [];
  };
  for (const step of steps) {
    if (kindOf(step) === "step") {
      run.push(step);
      continue;
    }
    flush();
    rows.push(
      <li key={step.key} className="kbot-krell__card-row" data-testid="krell-item-tool">
        <ToolResultCard name={step.name} result={step.result} ended={step.ended} />
      </li>,
    );
  }
  flush();
  return <>{rows}</>;
}

/** The log's rows with each run of consecutive tool calls folded into one unit. */
export type LogUnit<T> = { kind: "row"; row: T } | { kind: "tools"; key: string; steps: ToolStep[] };

/** A reply's consecutive tool calls become one unit; any other item (Krell's text, a card) ends the run. */
export function groupToolRows<T extends { item: KrellItem }>(rows: T[]): Array<LogUnit<T>> {
  const units: Array<LogUnit<T>> = [];
  for (const row of rows) {
    const { item } = row;
    if (item.kind !== "tool") {
      units.push({ kind: "row", row });
      continue;
    }
    const step: ToolStep = { key: item.key, name: item.name, result: item.result, ended: item.ended };
    const last = units[units.length - 1];
    if (last?.kind === "tools") last.steps.push(step);
    else units.push({ kind: "tools", key: item.key, steps: [step] });
  }
  return units;
}
