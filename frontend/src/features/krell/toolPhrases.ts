/**
 * Plain-words phrases for Krell's tool calls (M39.6). Contract: KRELL-SIGNAL-20261010.
 *
 * One entry per tool the engine exposes to Krell (`ToolSpec` in
 * `src/krellbot/agent/core.py` and `tools_*.py`, plus Krell's own
 * `set_profile` and `highlight`). A phrase is drawn only from fields the
 * engine's result carries, never from model text. The map is keyed on
 * `result.tool`, which the engine sets; the name the model chose is only a
 * fallback while no result exists. A name that is not in the map renders as
 * `Ran "<name>"`, quoted, so it can never read as an outcome.
 */
import type { KrellToolResult } from "./client";

/** The pair and timeframe a call itself sent; only these are ever read from its arguments. */
export type AskedMarket = { pair?: string; timeframe?: string };

type Phrase = {
  /** What the call is doing, lower case: "saving your profile". */
  doing: string;
  /** One line for a call that completed. */
  done(result: KrellToolResult): string;
  /** A headline for a refused call whose code has no plainer line of its own. */
  refused: string;
};

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function count(value: unknown): number | null {
  return Array.isArray(value) ? value.length : null;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The strategy's label from a draft or validate summary, quoted, or "". */
function strategyOf(result: KrellToolResult): string {
  const label = str(record(record(result.result).pack).label);
  return label ? `'${label}'` : "";
}

/** "kraken SUIUSD", "SUIUSD" or "" from the venue and pair a change result carries. */
function marketOf(result: KrellToolResult): string {
  return [str(result.venue), str(result.pair)].filter(Boolean).join(" ");
}

function onMarket(result: KrellToolResult): string {
  const market = marketOf(result);
  return market ? ` on ${market}` : "";
}

const PHRASES = new Map<string, Phrase>([
  [
    "set_profile",
    { doing: "saving your profile", done: () => "Saved your profile", refused: "Couldn't save your profile" },
  ],
  [
    "highlight",
    { doing: "pointing at it", done: () => "Pointed it out on screen", refused: "Couldn't point at that" },
  ],
  [
    "read",
    {
      doing: "checking your workspace",
      done: (r) => {
        const drafts = count(r.drafts);
        const armed = count(r.armed);
        return drafts !== null && armed !== null
          ? `Checked your workspace: ${plural(drafts, "draft", "drafts")}, ${plural(armed, "armed strategy", "armed strategies")}`
          : "Checked your workspace";
      },
      refused: "Couldn't check your workspace",
    },
  ],
  [
    "draft",
    {
      doing: "drafting a strategy",
      done: (r) => {
        const name = strategyOf(r);
        const existing = str(record(r.result).outcome) === "existing";
        if (!name) return existing ? "Found the draft you already had" : "Drafted a strategy";
        return existing ? `Already had ${name} drafted` : `Drafted ${name}`;
      },
      refused: "Couldn't save that draft",
    },
  ],
  [
    "validate",
    {
      doing: "checking a draft",
      done: (r) => {
        const summary = record(r.result);
        const name = strategyOf(r) || "the draft";
        const problems = count(summary.errors) ?? 0;
        if (problems > 0) return `Checked ${name}: ${plural(problems, "problem", "problems")} to fix`;
        return str(summary.state) === "validated" ? `Checked ${name}: ready to run on paper` : `Checked ${name}`;
      },
      refused: "Couldn't check that draft",
    },
  ],
  [
    "backtest",
    { doing: "running a backtest", done: () => "Ran a backtest on past data", refused: "Couldn't run that backtest" },
  ],
  [
    "compare",
    {
      doing: "comparing strategies",
      done: (r) => {
        const rows = count(r.rows);
        return rows === null ? "Compared strategies on the same data" : `Compared ${rows} strategies on the same data`;
      },
      refused: "Couldn't compare those strategies",
    },
  ],
  [
    "paper_arm",
    {
      doing: "arming on paper",
      done: () => "Armed on paper",
      refused: "Couldn't arm on paper",
    },
  ],
  [
    "pause_entries",
    {
      doing: "pausing new entries",
      done: (r) => `Paused new entries${onMarket(r)}`,
      refused: "Couldn't pause new entries",
    },
  ],
  [
    "resume_entries",
    {
      doing: "resuming entries",
      done: (r) => `Resumed entries${onMarket(r)}`,
      refused: "Couldn't resume entries",
    },
  ],
  [
    "disarm",
    {
      doing: "disarming a strategy",
      done: (r) => `Disarmed${marketOf(r) ? ` ${marketOf(r)}` : " the strategy"}`,
      refused: "Couldn't disarm that strategy",
    },
  ],
  [
    "positions",
    { doing: "checking paper positions", done: () => "Checked your paper positions", refused: "Couldn't check positions" },
  ],
  [
    "journal_recent",
    {
      doing: "reading the paper journal",
      done: () => "Read your recent paper journal",
      refused: "Couldn't read the journal",
    },
  ],
  [
    "alerts",
    {
      doing: "checking alerts",
      done: (r) => {
        const alerts = count(r.alerts);
        if (alerts === null) return "Checked your alerts";
        return alerts === 0 ? "Checked your alerts: none right now" : `Checked your alerts: ${plural(alerts, "alert", "alerts")}`;
      },
      refused: "Couldn't check alerts",
    },
  ],
  [
    "unresolved_orders",
    {
      doing: "checking orders still waiting",
      done: (r) => {
        const orders = count(r.orders);
        if (orders === null) return "Checked orders still waiting";
        return orders === 0
          ? "No orders waiting on a check"
          : `${plural(orders, "order is", "orders are")} still waiting on a check`;
      },
      refused: "Couldn't check waiting orders",
    },
  ],
  [
    "market_snapshot",
    {
      doing: "loading prices",
      done: (r) => {
        const market = [str(r.pair), str(r.timeframe)].filter(Boolean).join(" ");
        return market ? `Loaded ${market} prices` : "Loaded prices";
      },
      refused: "Couldn't load prices",
    },
  ],
  [
    "catalogue",
    {
      doing: "looking through strategy packs",
      done: () => "Looked through the strategy packs",
      refused: "Couldn't read the pack catalogue",
    },
  ],
  [
    "explain_bar",
    {
      doing: "looking into a bar",
      done: () => "Looked into why a bar decided what it did",
      refused: "Couldn't explain that bar",
    },
  ],
  [
    "tour_step",
    {
      doing: "opening the tour",
      done: (r) => {
        const title = str(record(r.step).title);
        return title ? `Opened the tour: ${title}` : "Opened the tour";
      },
      refused: "Couldn't open that tour step",
    },
  ],
]);

const PAIR_SHAPE = /^[A-Za-z0-9]{2,12}([/:_-][A-Za-z0-9]{2,12})?$/;
const TIMEFRAME_SHAPE = /^[0-9]{1,3}[smhdwM]$/;

/** The pair the call sent, upper-cased, only if it has a plain pair shape; model text never passes. */
function plainPair(pair: string | undefined): string {
  return pair !== undefined && PAIR_SHAPE.test(pair) ? pair.toUpperCase() : "";
}

function plainTimeframe(timeframe: string | undefined): string {
  return timeframe !== undefined && TIMEFRAME_SHAPE.test(timeframe) ? timeframe : "";
}

/** Refusal codes with a plainer headline than their tool's. */
const REFUSAL_LINES = new Map<string, (asked?: AskedMarket) => string>([
  [
    "market_snapshot_not_found",
    (asked) => {
      const market = [plainPair(asked?.pair), plainTimeframe(asked?.timeframe)].filter(Boolean).join(" ");
      return market ? `No price data for ${market} yet` : "No price data for that market yet";
    },
  ],
  ["explain_bar_not_found", () => "No decision stored for that bar"],
  ["tour_step_not_found", () => "No such tour step"],
  ["agent_live_read_refused", () => "Live markets stay out of Krell's sight"],
  ["agent_live_change_refused", () => "Krell only changes paper strategies"],
  ["mcp_live_arm_refused", () => "Krell can't arm anything live"],
  ["user_policy_denied", () => "Your Agents & limits settings don't allow that"],
]);

/** The engine's own tool name from a result, else the name Krell's call showed. */
function keyOf(name: string, result: KrellToolResult | null): string {
  const engine = result === null ? undefined : str(result.tool);
  return engine || name;
}

function phraseOf(name: string, result: KrellToolResult | null): Phrase | undefined {
  return PHRASES.get(keyOf(name, result));
}

/** The model-chosen name, quoted, in front of `verb`: it reads as a name, never an outcome. */
function quoted(name: string): string {
  return `"${name}"`;
}

/** One plain line for a call that completed. */
export function doneLine(name: string, result: KrellToolResult): string {
  const phrase = phraseOf(name, result);
  return phrase ? phrase.done(result) : `Ran ${quoted(name)}`;
}

/** The headline of a refused or failed call, naming the market the call asked for when it sent one. The engine's message and code follow it. */
export function refusalLine(name: string, result: KrellToolResult, asked?: AskedMarket): string {
  const phrase = phraseOf(name, result);
  if (!phrase) return `Couldn't run ${quoted(name)}`;
  return REFUSAL_LINES.get(str(result.code))?.(asked) ?? phrase.refused;
}

/** The line for a call that needs the person's confirm before it runs. */
export function confirmLine(name: string, result: KrellToolResult): string {
  const phrase = phraseOf(name, result);
  return phrase ? `Asked before ${phrase.doing}` : `Asked before running ${quoted(name)}`;
}

/** The line for a call still running. */
export function runningLine(name: string): string {
  const phrase = PHRASES.get(name);
  if (!phrase) return `Running ${quoted(name)}…`;
  return `${phrase.doing.charAt(0).toUpperCase()}${phrase.doing.slice(1)}…`;
}

/** The subject of a call whose outcome is unclear: what it tried, with no claim that it ran. */
export function attemptLine(name: string): string {
  const phrase = PHRASES.get(name);
  if (!phrase) return `Tried to run ${quoted(name)}`;
  return `${phrase.doing.charAt(0).toUpperCase()}${phrase.doing.slice(1)}`;
}
