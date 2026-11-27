/**
 * One Krell tool call as a card (M38.17). Contract: KRELL-SIGNAL-20261010.
 *
 * Drawn only from a tool result the engine sent, never from model text, so
 * nothing Krell writes can make an "Armed" card. The outcome comes from
 * `result.ok`, `effect`, `code` and `message`; a refusal shows its code and
 * message. The honesty pills come from `result.labels` exactly.
 */
import type { JSX } from "react";
import type { KrellToolResult } from "./client";

const LABELS = new Map([
  ["simulated", "Simulated"],
  ["not_advice", "Not financial advice"],
  ["paper", "Paper"],
]);

/** A successful call's outcome on its card, by the engine's tool name; any other reads "Done". */
const DONE = new Map([["paper_arm", "Armed on paper"]]);

type Outcome = { title: string; refusal?: { code: string; message: string } };

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function outcomeOf(name: string, result: KrellToolResult | null): Outcome {
  if (result === null) return { title: "Running…" };
  if (result.effect === "needs_confirm") return { title: "Asked before running" };
  if (result.ok === true) return { title: DONE.get(str(result.tool, name)) ?? "Done" };
  return {
    title: result.effect === "refused" ? "Refused" : "Did not complete",
    refusal: { code: str(result.code, "unknown"), message: str(result.message) },
  };
}

/** One pill per known label in `result.labels`, in order, each once. */
function labelsOf(result: KrellToolResult | null): string[] {
  const shown: string[] = [];
  for (const label of Array.isArray(result?.labels) ? result.labels : []) {
    const text = typeof label === "string" ? LABELS.get(label) : undefined;
    if (text && !shown.includes(text)) shown.push(text);
  }
  return shown;
}

export type ToolResultCardProps = {
  /** The tool's name as the engine showed it. */
  name: string;
  /** The engine's result, or null while the call runs. */
  result: KrellToolResult | null;
};

export function ToolResultCard({ name, result }: ToolResultCardProps): JSX.Element {
  const outcome = outcomeOf(name, result);
  const labels = labelsOf(result);
  return (
    <div
      className={"kbot-krell__card" + (outcome.refusal ? " kbot-krell__card--refused" : "")}
      aria-busy={result === null || undefined}
    >
      <p className="kbot-krell__card-head">
        <span className="kbot-krell__sr">Tool </span>
        <span className="kbot-krell__card-tool">{name}</span>
        <span className="kbot-krell__sr">: </span>
        <span className="kbot-krell__card-outcome">{outcome.title}</span>
      </p>
      {labels.length > 0 ? (
        <ul className="kbot-krell__pills" aria-label="Result labels">
          {labels.map((label) => (
            <li key={label} className="kbot-krell__pill" data-testid="krell-label">
              {label}
            </li>
          ))}
        </ul>
      ) : null}
      {outcome.refusal ? (
        <p className="kbot-krell__card-detail">
          {outcome.refusal.message ? `${outcome.refusal.message} ` : null}
          <code className="kbot-krell__code">{outcome.refusal.code}</code>
        </p>
      ) : null}
    </div>
  );
}
