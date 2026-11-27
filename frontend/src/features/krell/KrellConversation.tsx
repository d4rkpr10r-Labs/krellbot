/**
 * The Krell conversation: message log and composer, shared by the Krell
 * tab and the side panel (M38.16). Contract: KRELL-SIGNAL-20261010.
 *
 * Krell's avatar appears only here, once at the start of each run of her
 * consecutive messages. Her tool calls and cards sit inside a run; a
 * person's message or an engine line (disclosure, error, decision) ends
 * it. All model text renders as plain text.
 */
import { useEffect, useId, useRef, type FormEvent, type JSX } from "react";
import krellAvatar from "../../assets/character/krell-pfp-160.webp";
import { useKrell, type KrellItem } from "./KrellConversationProvider";
import "./krell.css";

/** The engine's limit on one message (`MAX_MESSAGE_CHARS`). */
const MAX_MESSAGE_CHARS = 8000;

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

function toolStatus(result: Record<string, unknown> | null): string {
  if (result === null) return "running…";
  if (result.effect === "needs_confirm") return "waiting for your choice";
  if (result.effect === "refused") return "refused";
  return result.ok === true ? "done" : "did not complete";
}

type PlaceholderItem = Extract<KrellItem, { kind: "tool" | "confirm" | "decision" }>;

/**
 * Extension point (M38.17): result cards, confirm cards and decision
 * records replace these neutral lines. A confirm ticket id never reaches
 * a display item, so nothing here can show one.
 */
function KrellPlaceholder({ item }: { item: PlaceholderItem }): JSX.Element {
  switch (item.kind) {
    case "tool":
      return (
        <li className="kbot-krell__line" data-testid="krell-item-tool">
          Tool {item.name}: {toolStatus(item.result)}
        </li>
      );
    case "confirm":
      return (
        <li className="kbot-krell__line" data-testid="krell-item-confirm">
          Krell asked before running {item.tool}. It has not run.
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

function KrellRow({ item, avatar }: Row): JSX.Element {
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
    default:
      return <KrellPlaceholder item={item} />;
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

  return (
    <div className="kbot-krell__conversation" data-testid="krell-conversation">
      <div className="kbot-krell__log" ref={logRef} role="log" aria-label="Conversation with Krell">
        <ol className="kbot-krell__list">
          {withAvatars(krell.messages).map((row) => (
            <KrellRow key={row.item.key} item={row.item} avatar={row.avatar} />
          ))}
        </ol>
      </div>
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
