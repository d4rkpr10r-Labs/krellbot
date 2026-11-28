/**
 * The Krell conversation: message log and composer, shared by the Krell
 * tab and the side panel (M38.16). Contract: KRELL-SIGNAL-20261010.
 *
 * Krell's avatar appears only here, once at the start of each run of her
 * consecutive messages. Her tool calls and cards sit inside a run; a
 * person's message or an engine line (disclosure, error, decision) ends
 * it. All model text renders as plain text.
 *
 * Result and confirm cards (M38.17) are drawn only from tool results and
 * confirm requests the engine sent, never from model text. A confirm ticket
 * id never reaches a display item, so nothing here can show one.
 *
 * The opening's Standard and Advanced quick replies (M38.18) show only while
 * no mode is set.
 *
 * When a turn uses up its tool budget (M39.3), the engine refuses the calls
 * over it (`agent_tool_cap_reached`, shown as their cards), Krell sums up in
 * her own words and the engine offers Keep going. The quick reply shows while
 * that summary is the latest thing in the log, live or after a reload, and
 * sends "Keep going" as an ordinary message; the composer keeps its draft.
 * When the turn ends on the cap error instead, there is no Keep going.
 *
 * On a Sign in with ChatGPT connection (M38.23) "Using ChatGPT plan" sits
 * near the composer with a Manage usage link to ChatGPT settings. When the
 * latest turn on that connection ended on a ChatGPT usage limit, Krell is
 * paused: a compact "Usage limit reached" state takes its place with Manage
 * usage as the conversation's one primary, Send, the quick replies and every
 * confirm card are disabled (each card says why), and only Try again ends
 * the pause. Switching to another connection, or signing out, clears it. An
 * error that kept the provider's request id shows it, after a reload too.
 */
import { useEffect, useId, useRef, type FormEvent, type JSX } from "react";
import krellAvatar from "../../assets/character/krell-pfp-160.webp";
import { BudgetMeter } from "./BudgetMeter";
import { MODE_REPLIES, type KrellDecision, type KrellMode } from "./client";
import { ConfirmCard, confirmOutcomes, isPressable, type ConfirmOutcome } from "./ConfirmCard";
import { useKrell, type KrellItem } from "./KrellConversationProvider";
import { groupToolRows, ToolSteps, type NonToolItem } from "./ToolResultCard";
import "./krell.css";

/** The engine's limit on one message (`MAX_MESSAGE_CHARS`). */
const MAX_MESSAGE_CHARS = 8000;

/** ChatGPT's own usage page: review usage and change Krellbot's limit. */
export const CHATGPT_USAGE_URL = "https://chatgpt.com/settings/usage";
/** The engine's code for a ChatGPT usage-limit error. */
const CODE_USAGE_LIMIT = "provider_usage_limit";

/** Why a confirm card cannot be answered while Krell is paused. */
export const PAUSED_CARD_REASON = "Paused: ChatGPT usage limit reached. Choose Try again to answer this card.";

/**
 * The key of the usage-limit error that pauses Krell, or null: the latest
 * turn on a ChatGPT-plan connection ended on a usage limit (no answer or tool
 * call from Krell since that error), and the person has not chosen Try again.
 */
function pausingLimit(
  items: KrellItem[],
  plan: string | null | undefined,
  resumed: string | null,
): string | null {
  if (plan !== "chatgpt") return null;
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i];
    if (item.kind === "error") return item.code === CODE_USAGE_LIMIT && item.key !== resumed ? item.key : null;
    if (item.kind === "krell" || item.kind === "tool") return null;
  }
  return null;
}

/** "Using ChatGPT plan", or, while paused, the usage-limit state, near the composer. */
function ChatgptPlan({ paused, resume }: { paused: boolean; resume(): void }): JSX.Element {
  if (paused) {
    return (
      <div className="kbot-krell__plan-limit" role="status" data-testid="krell-usage-limit">
        <p className="kbot-krell__plan-limit-head">ChatGPT</p>
        <p className="kbot-krell__plan-limit-title">Usage limit reached</p>
        <p className="kbot-krell__plan-limit-copy">
          Review your plan's limit, or Krellbot's, in ChatGPT settings. Krell paused and retried nothing; she
          sends nothing more until you choose Try again.
        </p>
        <div className="kbot-krell__plan-limit-actions">
          <a className="kbot-krell__go" href={CHATGPT_USAGE_URL} target="_blank" rel="noopener noreferrer">
            Manage usage
          </a>
          <button type="button" className="kbot-krell__btn" onClick={resume}>
            Try again
          </button>
        </div>
      </div>
    );
  }
  return (
    <p className="kbot-krell__plan" data-testid="krell-chatgpt-plan">
      Using ChatGPT plan{" "}
      <a className="kbot-krell__plan-link" href={CHATGPT_USAGE_URL} target="_blank" rel="noopener noreferrer">
        Manage usage
      </a>
    </p>
  );
}

const MODES: readonly KrellMode[] = ["standard", "advanced"];

/** The quick reply after Krell's summary at the tool budget (the engine's `KEEP_GOING`). */
const KEEP_GOING = "Keep going";
/** The engine's code for a call refused over the turn's tool budget. */
const CODE_TOOL_CAP = "agent_tool_cap_reached";

/** The opening, with its mode question, is in the log. */
function modeAsked(items: KrellItem[]): boolean {
  return items.some((item) => item.kind === "krell" && Boolean(item.question));
}

/**
 * The latest turn used up its tool budget and ended with Krell's summary: her
 * message is the last item, and the item before her run of messages is a call
 * refused over the budget. A cap error after it, or the person's next message,
 * means there is nothing to keep going from.
 */
function keepGoingOffered(items: KrellItem[]): boolean {
  const last = items[items.length - 1];
  if (last?.kind !== "krell" || last.streaming) return false;
  for (let i = items.length - 2; i >= 0; i -= 1) {
    const item = items[i];
    if (item.kind === "krell") continue;
    return item.kind === "tool" && item.result?.code === CODE_TOOL_CAP;
  }
  return false;
}

type QuickReply = { label: string; choose(): void };

/**
 * Quick replies above the composer. Secondary buttons, so an open confirm
 * card's Confirm stays the conversation's one sage primary.
 */
function QuickReplies({
  name,
  testId,
  replies,
  busy,
}: {
  name: string;
  testId: string;
  replies: readonly QuickReply[];
  busy: boolean;
}): JSX.Element {
  return (
    <div className="kbot-krell__replies" role="group" aria-label={name} data-testid={testId}>
      {replies.map((reply) => (
        <button key={reply.label} type="button" className="kbot-krell__btn" disabled={busy} onClick={reply.choose}>
          {reply.label}
        </button>
      ))}
    </div>
  );
}

/** What the confirm cards in the log need from the conversation. */
type Cards = {
  outcomes: Map<string, ConfirmOutcome>;
  /** The card whose Confirm is the one sage primary: the newest that can still be pressed. */
  primary: string | null;
  busy: boolean;
  /** Krell is paused by a ChatGPT usage limit: no card can be answered, and none is the primary. */
  paused: boolean;
  decide(callId: string, decision: KrellDecision): void;
};

function primaryCard(items: KrellItem[], outcomes: Map<string, ConfirmOutcome>): string | null {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i];
    if (item.kind === "confirm" && isPressable(outcomes.get(item.callId))) return item.callId;
  }
  return null;
}

type Row = { item: KrellItem; avatar: boolean };

function withAvatars(items: KrellItem[]): Row[] {
  let inRun = false;
  return items.map((item) => {
    if (item.kind === "krell") {
      const avatar = !inRun;
      inRun = true;
      return { item, avatar };
    }
    if (item.kind !== "tool" && item.kind !== "confirm") inRun = false;
    return { item, avatar: false };
  });
}

function KrellRow({ item, avatar, cards }: { item: NonToolItem; avatar: boolean; cards: Cards }): JSX.Element {
  switch (item.kind) {
    case "user":
      return (
        <li
          className={"kbot-krell__msg kbot-krell__msg--user" + (item.sending ? " kbot-krell__msg--sending" : "")}
          data-testid="krell-message-user"
          aria-busy={item.sending || undefined}
        >
          <span className="kbot-krell__sr">You: </span>
          <p className="kbot-krell__bubble">{item.text}</p>
        </li>
      );
    case "krell":
      return (
        <li className="kbot-krell__msg kbot-krell__msg--krell" data-testid="krell-message-krell">
          {avatar ? (
            <img
              className="kbot-krell__avatar"
              src={krellAvatar}
              alt=""
              width={32}
              height={32}
              data-testid="krell-avatar"
            />
          ) : (
            <span className="kbot-krell__avatar-gap" aria-hidden="true" />
          )}
          <div className="kbot-krell__bubble" aria-busy={item.streaming || undefined}>
            <span className="kbot-krell__sr">Krell: </span>
            <p>{item.text}</p>
            {item.question ? <p>{item.question}</p> : null}
          </div>
        </li>
      );
    case "notice":
      return (
        <li className="kbot-krell__line" data-testid="krell-item-notice">
          {item.text}
        </li>
      );
    case "error":
      return (
        <li className="kbot-krell__line kbot-krell__line--error" data-testid="krell-item-error">
          {item.text}
          {item.requestId ? (
            <span className="kbot-krell__request-id" data-testid="krell-request-id">
              {" "}
              Request ID: {item.requestId}
            </span>
          ) : null}
        </li>
      );
    case "confirm":
      return (
        <li className="kbot-krell__card-row" data-testid="krell-item-confirm">
          <ConfirmCard
            request={item.confirm}
            outcome={cards.outcomes.get(item.callId)}
            busy={cards.busy || cards.paused}
            primary={item.callId === cards.primary && !cards.paused}
            onConfirm={() => cards.decide(item.callId, "confirm")}
            onDecline={() => cards.decide(item.callId, "decline")}
          />
          {cards.paused && isPressable(cards.outcomes.get(item.callId)) ? (
            <p className="kbot-krell__line" data-testid="krell-confirm-paused">
              {PAUSED_CARD_REASON}
            </p>
          ) : null}
        </li>
      );
    case "decision":
      return (
        <li className="kbot-krell__line" data-testid="krell-item-decision">
          You chose {item.decision === "confirm" ? "Confirm" : "Not now"} on {item.tool}.
        </li>
      );
  }
}

export function KrellConversation(): JSX.Element {
  const krell = useKrell();
  const inputId = useId();
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [krell.messages]);

  if (krell.status === "failed") {
    return (
      <div className="kbot-krell__state">
        <p className="kbot-krell__line kbot-krell__line--error" role="alert">
          {krell.loadError}
        </p>
        <button type="button" className="kbot-krell__btn" onClick={krell.reload}>
          Try again
        </button>
      </div>
    );
  }
  if (krell.status !== "ready") {
    return (
      <p className="kbot-krell__line" role="status">
        Loading Krell…
      </p>
    );
  }

  const limit = pausingLimit(krell.messages, krell.connection?.plan, krell.resumedLimit);
  const paused = limit !== null;
  // Paused by a usage limit: nothing is sent until the person chooses Try again.
  const canSend = !krell.pending && !paused && krell.draft.trim() !== "";
  const submit = (event?: FormEvent): void => {
    event?.preventDefault();
    if (canSend) krell.send(krell.draft);
  };
  // Keep going is an ordinary message. Sending clears the composer, so what the person was typing goes back.
  const keepGoing = (): void => {
    const draft = krell.draft;
    krell.send(KEEP_GOING);
    if (draft) krell.setDraft(draft);
  };
  const outcomes = confirmOutcomes(krell.messages);
  const cards: Cards = {
    outcomes,
    primary: primaryCard(krell.messages, outcomes),
    busy: krell.pending,
    paused,
    decide: krell.confirm,
  };

  return (
    <div className="kbot-krell__conversation" data-testid="krell-conversation">
      <div className="kbot-krell__log" ref={logRef} role="log" aria-label="Conversation with Krell">
        <ol className="kbot-krell__list">
          {groupToolRows(withAvatars(krell.messages)).map((unit) =>
            unit.kind === "tools" ? (
              <ToolSteps key={unit.key} steps={unit.steps} />
            ) : (
              <KrellRow key={unit.row.item.key} item={unit.row.item} avatar={unit.row.avatar} cards={cards} />
            ),
          )}
        </ol>
      </div>
      {krell.budget ? <BudgetMeter budget={krell.budget} /> : null}
      {krell.pending ? (
        <p className="kbot-krell__line" role="status">
          Krell is answering…
        </p>
      ) : null}
      {krell.mode === null && modeAsked(krell.messages) ? (
        <QuickReplies
          name="Choose a mode"
          testId="krell-quick-replies"
          busy={krell.pending || paused}
          replies={MODES.map((mode) => ({ label: MODE_REPLIES[mode], choose: () => krell.setMode(mode) }))}
        />
      ) : null}
      {keepGoingOffered(krell.messages) ? (
        <QuickReplies
          name="Carry on"
          testId="krell-keep-going"
          busy={krell.pending || paused}
          replies={[{ label: KEEP_GOING, choose: keepGoing }]}
        />
      ) : null}
      {krell.connection?.plan === "chatgpt" ? (
        <ChatgptPlan paused={paused} resume={() => limit !== null && krell.resumeAfterLimit(limit)} />
      ) : null}
      <form className="kbot-krell__composer" onSubmit={submit}>
        <label className="kbot-krell__sr" htmlFor={inputId}>
          Message Krell
        </label>
        <textarea
          id={inputId}
          className="kbot-krell__input"
          data-testid="krell-input"
          data-krell-composer=""
          rows={2}
          maxLength={MAX_MESSAGE_CHARS}
          placeholder="Ask Krell about Krellbot"
          value={krell.draft}
          onChange={(event) => krell.setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <button type="submit" className="kbot-krell__send" data-testid="krell-send" disabled={!canSend}>
          Send
        </button>
      </form>
    </div>
  );
}
