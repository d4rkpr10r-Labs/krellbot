/**
 * Krell's token budget (M38.17). Contract: KRELL-SIGNAL-20261010.
 *
 * Conversation and month usage against the operator's limits, as the
 * conversation payload and the engine's `budget` events report them. Once a
 * limit is reached the engine sends nothing more to the model, and this says
 * where to raise it.
 */
import type { JSX } from "react";
import type { KrellBudget } from "./client";

const NUMBER = new Intl.NumberFormat("en-US");
const BUDGET_REACHED = "Token budget reached. Raise it in Settings → Agents & limits.";

function Usage({ label, used, limit }: { label: string; used: number; limit: number }): JSX.Element {
  const text = `${NUMBER.format(used)} of ${NUMBER.format(limit)} tokens`;
  return (
    <div className="kbot-krell__budget-row">
      <span className="kbot-krell__budget-label">{label}</span>
      <span className="kbot-krell__budget-count">{text}</span>
      <meter
        className="kbot-krell__budget-bar"
        min={0}
        max={Math.max(limit, 1)}
        value={Math.min(used, Math.max(limit, 1))}
        aria-label={`${label}: ${text}`}
      />
    </div>
  );
}

export function BudgetMeter({ budget }: { budget: KrellBudget }): JSX.Element {
  return (
    <section className="kbot-krell__budget" data-testid="krell-budget" aria-label="Krell's token budget">
      <Usage label="This conversation" used={budget.conversation_used} limit={budget.conversation_limit} />
      <Usage label="This month" used={budget.month_used} limit={budget.month_limit} />
      {budget.exhausted ? (
        <p className="kbot-krell__line kbot-krell__line--error" role="status">
          {BUDGET_REACHED}
        </p>
      ) : null}
    </section>
  );
}
