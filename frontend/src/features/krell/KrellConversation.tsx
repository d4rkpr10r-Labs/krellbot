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
 */
import { useEffect, useId, useRef, type FormEvent, type JSX } from "react";
import krellAvatar from "../../assets/character/krell-pfp-160.webp";
import { BudgetMeter } from "./BudgetMeter";
import type { KrellDecision } from "./client";
import { ConfirmCard, confirmOutcomes, isPressable, type ConfirmOutcome } from "./ConfirmCard";
import { useKrell, type KrellItem } from "./KrellConversationProvider";
import { ToolResultCard } from "./ToolResultCard";
import "./krell.css";

/** The engine's limit on one message (`MAX_MESSAGE_CHARS`). */
const MAX_MESSAGE_CHARS = 8000;

/** What the confirm cards in the log need from the conversation. */
type Cards = {
  outcomes: Map<string, ConfirmOutcome>;
  /** The card whose Confirm is the one sage primary: the newest that can still be pressed. */
  primary: string | null;
  busy: boolean;
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

function KrellRow({ item, avatar, cards }: Row & { cards: Cards }): JSX.Element {
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
        </li>
      );
    case "tool":
      return (
        <li className="kbot-krell__card-row" data-testid="krell-item-tool">
          <ToolResultCard name={item.name} result={item.result} ended={item.ended} />
        </li>
      );
    case "confirm":
      return (
        <li className="kbot-krell__card-row" data-testid="krell-item-confirm">
          <ConfirmCard
            request={item.confirm}
            outcome={cards.outcomes.get(item.callId)}
            busy={cards.busy}
            primary={item.callId === cards.primary}
            onConfirm={() => cards.decide(item.callId, "confirm")}
            onDecline={() => cards.decide(item.callId, "decline")}
          />
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

  const canSend = !krell.pending && krell.draft.trim() !== "";
  const submit = (event?: FormEvent): void => {
    event?.preventDefault();
    if (canSend) krell.send(krell.draft);
  };
  const outcomes = confirmOutcomes(krell.messages);
  const cards: Cards = {
    outcomes,
    primary: primaryCard(krell.messages, outcomes),
    busy: krell.pending,
    decide: krell.confirm,
  };

  return (
    <div className="kbot-krell__conversation" data-testid="krell-conversation">
      <div className="kbot-krell__log" ref={logRef} role="log" aria-label="Conversation with Krell">
        <ol className="kbot-krell__list">
          {withAvatars(krell.messages).map((row) => (
            <KrellRow key={row.item.key} item={row.item} avatar={row.avatar} cards={cards} />
          ))}
        </ol>
      </div>
      {krell.budget ? <BudgetMeter budget={krell.budget} /> : null}
      {krell.pending ? (
        <p className="kbot-krell__line" role="status">
          Krell is answering…
        </p>
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
