/**
 * The person's Confirm / Not now card for a call Krell asked to run
 * (M38.17). Contract: KRELL-SIGNAL-20261010.
 *
 * Drawn only from a `needs_confirm` result or a `confirm_request` event the
 * engine sent. It shows the exact tool and parameters, the agent limits that
 * apply and the estimate. Confirm is the conversation's one sage primary;
 * Not now is secondary. The engine decides on every press:
 *
 * - Not now, or a Confirm that ran: the card settles and is disabled.
 * - A Confirm the engine refused (say a limit lowered since the card
 *   appeared): the refusal shows and the card stays pressable, so the person
 *   can fix it and press again.
 * - `agent_confirm_unknown` (expired, evicted, or forgotten by a restart):
 *   "Expired. Ask Krell again." and disabled.
 * - Past `expires_at`: "Expired" and disabled, even before any press.
 * - While a press (or any Krell answer) runs, both buttons are disabled.
 * - A Confirm whose stream ended with no result (the connection dropped, or
 *   the store failed after the call ran): "Couldn't tell whether it ran.
 *   Reload to check." and disabled, even while a later stream runs. The call
 *   may have run, so it is never sent again from here (reconcile, never
 *   resubmit); a reload reads what the engine stored.
 *
 * The request never holds a ticket id: the provider keeps tickets to itself
 * and sends one only in the confirm request body.
 */
import { Fragment, useEffect, useId, useReducer, useState, type JSX, type ReactNode } from "react";
import type { KrellItem } from "./KrellConversationProvider";

export type ConfirmOutcome =
  | { kind: "open" }
  /** Confirm was pressed and the call is running. */
  | { kind: "running" }
  | { kind: "declined" }
  | { kind: "confirmed" }
  | { kind: "refused"; code: string; message: string }
  /** The engine no longer knows the ticket. */
  | { kind: "unknown" }
  /** Confirm was pressed and its stream ended with no result: the call may have run. */
  | { kind: "unclear" };

const CODE_CONFIRM_UNKNOWN = "agent_confirm_unknown";
/** A call that started and whose stream ended with no result: it may have run. */
export const UNCLEAR_OUTCOME = "Couldn't tell whether it ran. Reload to check.";
const OPEN: ConfirmOutcome = { kind: "open" };
/** The longest wait `setTimeout` takes. */
const MAX_WAIT_MS = 2_147_483_647;
const NUMBER = new Intl.NumberFormat("en-US");

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function resultOutcome(result: Record<string, unknown>): ConfirmOutcome {
  if (result.ok === true) return { kind: "confirmed" };
  if (result.code === CODE_CONFIRM_UNKNOWN) return { kind: "unknown" };
  return { kind: "refused", code: str(result.code, "unknown"), message: str(result.message) };
}

/**
 * Each confirm card's outcome, keyed by its tool call id, read from the
 * items after it: the latest decision on it, the result of a Confirm, and
 * an `agent_confirm_unknown` answer to a press. A Confirm whose stream has
 * ended (`ended`) with no result after it is unclear, not running.
 */
export function confirmOutcomes(items: KrellItem[]): Map<string, ConfirmOutcome> {
  const outcomes = new Map<string, ConfirmOutcome>();
  const confirming = new Set<string>();
  for (const item of items) {
    if (item.kind === "confirm") {
      if (!outcomes.has(item.callId)) outcomes.set(item.callId, OPEN);
    } else if (item.kind === "decision" && item.callId) {
      if (item.decision === "decline") {
        confirming.delete(item.callId);
        outcomes.set(item.callId, { kind: "declined" });
      } else {
        confirming.add(item.callId);
        outcomes.set(item.callId, { kind: item.ended ? "unclear" : "running" });
      }
    } else if (item.kind === "tool" && item.result !== null && confirming.has(item.callId)) {
      confirming.delete(item.callId);
      outcomes.set(item.callId, resultOutcome(item.result));
    } else if (item.kind === "error" && item.callId && item.code === CODE_CONFIRM_UNKNOWN) {
      outcomes.set(item.callId, { kind: "unknown" });
    }
  }
  return outcomes;
}

/** Whether a card with this outcome can still be pressed (the clock aside). */
export function isPressable(outcome: ConfirmOutcome | undefined): boolean {
  return outcome === undefined || outcome.kind === "open" || outcome.kind === "refused";
}

function expiresAtMs(request: Record<string, unknown>): number | null {
  const at = request.expires_at;
  return typeof at === "number" && Number.isFinite(at) ? at * 1000 : null;
}

/** True once `expiresAt` (ms) has passed; re-renders at that moment while `active`. */
function useExpired(expiresAt: number | null, active: boolean): boolean {
  const [tick, bump] = useReducer((n: number) => n + 1, 0);
  const expired = expiresAt !== null && Date.now() >= expiresAt;
  useEffect(() => {
    if (!active || expiresAt === null || expired) return;
    const timer = window.setTimeout(bump, Math.min(Math.max(expiresAt - Date.now(), 0), MAX_WAIT_MS));
    return () => window.clearTimeout(timer);
  }, [active, expiresAt, expired, tick]);
  return expired;
}

/** A parameter exactly as the engine will use it. */
function exact(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function limitText(value: unknown): string {
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (value === null) return "none";
  return exact(value);
}

function about(value: unknown, unit: string): string {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? `about ${NUMBER.format(Math.ceil(value))} ${unit}`
    : "not known";
}

type FactsProps = { label: string; rows: Array<[string, string]>; empty?: string };

function Facts({ label, rows, empty = "" }: FactsProps): JSX.Element {
  return (
    <>
      <p className="kbot-krell__card-sub">{label}</p>
      {rows.length > 0 ? (
        <dl className="kbot-krell__facts">
          {rows.map(([name, value]) => (
            <Fragment key={name}>
              <dt>{name}</dt>
              <dd>{value}</dd>
            </Fragment>
          ))}
        </dl>
      ) : (
        <p className="kbot-krell__card-detail">{empty}</p>
      )}
    </>
  );
}

export type ConfirmCardProps = {
  /** The engine's confirm request, without its ticket. */
  request: Record<string, unknown>;
  outcome?: ConfirmOutcome;
  /** A Krell stream is running (this card's press or another), so nothing can be pressed. */
  busy?: boolean;
  /** Confirm is the conversation's one sage primary. */
  primary?: boolean;
  onConfirm(): void;
  onDecline(): void;
};

export function ConfirmCard({
  request,
  outcome = OPEN,
  busy = false,
  primary = true,
  onConfirm,
  onDecline,
}: ConfirmCardProps): JSX.Element {
  const titleId = useId();
  // Pressed here and not yet answered. Cleared when the stream the press
  // started ends; the outcome then says whether the card can be pressed again.
  const [pressed, setPressed] = useState(false);
  const [wasBusy, setWasBusy] = useState(busy);
  if (busy !== wasBusy) {
    setWasBusy(busy);
    if (!busy) setPressed(false);
  }
  const settled = ["declined", "confirmed", "unknown", "unclear"].includes(outcome.kind);
  const expired = useExpired(expiresAtMs(request), !settled);
  const disabled = settled || expired || busy || pressed || outcome.kind === "running";
  const press = (handler: () => void) => (): void => {
    if (disabled) return;
    setPressed(true);
    handler();
  };

  let status: ReactNode;
  if (outcome.kind === "unknown") status = "Expired. Ask Krell again.";
  else if (outcome.kind === "declined") status = "Not now. Nothing ran.";
  else if (outcome.kind === "confirmed") status = "Confirmed. Krellbot ran it once, exactly as shown.";
  else if (outcome.kind === "unclear") status = UNCLEAR_OUTCOME;
  else if (outcome.kind === "running") status = "Running…";
  else if (expired) status = "Expired";
  else if (pressed) status = "Sending your choice…";
  else if (outcome.kind === "refused") {
    status = (
      <>
        Refused, so nothing ran: {outcome.message ? `${outcome.message} ` : null}
        <code className="kbot-krell__code">{outcome.code}</code>
      </>
    );
  } else status = "Nothing runs until you press Confirm.";

  const params = isRecord(request.params) ? request.params : {};
  const limits = isRecord(request.limits) ? request.limits : {};
  const estimate = isRecord(request.estimate) ? request.estimate : {};

  return (
    <div className="kbot-krell__card kbot-krell__card--confirm" role="group" aria-labelledby={titleId}>
      <p className="kbot-krell__card-title" id={titleId}>
        Run <span className="kbot-krell__card-tool">{str(request.tool, "this tool")}</span>?
      </p>
      <Facts
        label="Parameters"
        rows={Object.entries(params).map(([name, value]) => [name, exact(value)])}
        empty="No parameters."
      />
      <Facts
        label="Agent limits that apply"
        rows={Object.entries(limits).map(([name, value]) => [name.replace(/_/g, " "), limitText(value)])}
        empty="No agent limits apply to this tool."
      />
      <Facts
        label="Estimate"
        rows={[
          ["Time", about(estimate.seconds, "s")],
          ["Tokens", about(estimate.result_tokens, "tokens")],
        ]}
      />
      <div className="kbot-krell__card-actions">
        <button
          type="button"
          className={primary && !settled && !expired ? "kbot-krell__go" : "kbot-krell__btn"}
          disabled={disabled}
          onClick={press(onConfirm)}
        >
          Confirm
        </button>
        <button type="button" className="kbot-krell__btn" disabled={disabled} onClick={press(onDecline)}>
          Not now
        </button>
      </div>
      <p className="kbot-krell__card-status" role="status">
        {status}
      </p>
    </div>
  );
}
