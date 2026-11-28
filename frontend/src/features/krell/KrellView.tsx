/**
 * The Krell tab (M38.16). With no usable model it shows only the M38.13
 * connect form; once a usable model is active (connected here, or in
 * Settings and the tab shown again), it shows the shared conversation.
 * Contract: KRELL-SIGNAL-20261010.
 */
import { useEffect, useId, useRef, useState, type JSX } from "react";
import { useGuidedPrompts, writeGuidedPrompts } from "../onboarding/guidedPromptsPersistence";
import { AiConnectForm } from "../settings/AiConnectForm";
import { KrellConversation } from "./KrellConversation";
import { useKrell } from "./KrellConversationProvider";
import "./krell.css";

/** Focus the Krell tab's composer, or the tab itself while it has none. */
export function focusKrellComposer(): void {
  const view = document.querySelector<HTMLElement>("[data-krell-view]");
  const target = view?.querySelector<HTMLElement>("[data-krell-composer]") ?? view;
  target?.focus();
}

/** The first-visit welcome (M39.5): flat, no box, two buttons. */
function KrellWelcome({ onTakeTour }: { onTakeTour?: () => void }): JSX.Element {
  return (
    <div className="kbot-krell__welcome" data-testid="krell-welcome">
      <h2 className="kbot-krell__welcome-title">Welcome to Krellbot!</h2>
      <p className="kbot-krell__welcome-copy">
        There are a few ways to get started. Krell, our AI assistant, can help you below. Or take the guided
        tour. If you&apos;re all set, hit Skip and dive in on your own, with no more guided instructions.
      </p>
      <div className="kbot-krell__welcome-actions">
        {onTakeTour ? (
          <button type="button" className="kbot-krell__go" onClick={onTakeTour}>
            Take the tour
          </button>
        ) : null}
        <button type="button" className="kbot-krell__btn" onClick={() => writeGuidedPrompts(false)}>
          Skip
        </button>
      </div>
    </div>
  );
}

export function KrellView({ onTakeTour }: { onTakeTour?: () => void } = {}): JSX.Element {
  const krell = useKrell();
  const guided = useGuidedPrompts();
  const headingId = useId();
  const [confirmingClear, setConfirmingClear] = useState(false);
  const clearRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  /** Where focus goes when the clear question closes. */
  const afterConfirm = useRef<"clear" | "composer" | null>(null);
  const noModel = krell.status === "ready" && krell.connection === null;
  // The welcome shows until the first message; after it, only a small Tour button.
  const started = krell.messages.some((item) => item.kind === "user");
  const showWelcome = guided && krell.status === "ready" && !started;
  const showTourButton = guided && krell.status === "ready" && started && onTakeTour !== undefined;

  useEffect(() => {
    // Opening the question focuses Cancel, the safe choice. Closing it puts
    // focus back on "Clear conversation", or on the composer after a clear.
    if (confirmingClear) {
      cancelRef.current?.focus();
      return;
    }
    if (afterConfirm.current === "clear") clearRef.current?.focus();
    if (afterConfirm.current === "composer") focusKrellComposer();
    afterConfirm.current = null;
  }, [confirmingClear]);

  return (
    <section
      className="kbot-krell kbot-krell--tab"
      data-testid="krell-view"
      data-krell-view=""
      tabIndex={-1}
      aria-labelledby={headingId}
    >
      <div className="kbot-shell__pagehead">
        <div className="kbot-shell__pagehead-copy">
          <h1 className="kbot-shell__heading" id={headingId}>
            Krell
          </h1>
        </div>
        {showTourButton ? (
          <button type="button" className="kbot-krell__btn" onClick={onTakeTour}>
            Tour
          </button>
        ) : null}
        {krell.status === "ready" && !noModel ? (
          confirmingClear ? (
            <div className="kbot-krell__confirm" role="group" aria-label="Clear the conversation">
              <p className="kbot-krell__line">Clear the conversation and any open cards?</p>
              <button
                type="button"
                className="kbot-krell__btn"
                disabled={krell.pending}
                onClick={() => {
                  afterConfirm.current = "composer";
                  setConfirmingClear(false);
                  krell.clear();
                }}
              >
                Yes, clear
              </button>
              <button
                type="button"
                ref={cancelRef}
                className="kbot-krell__btn"
                onClick={() => {
                  afterConfirm.current = "clear";
                  setConfirmingClear(false);
                }}
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              type="button"
              ref={clearRef}
              className="kbot-krell__btn"
              onClick={() => setConfirmingClear(true)}
              disabled={krell.pending}
            >
              Clear conversation
            </button>
          )
        ) : null}
      </div>
      {showWelcome ? <KrellWelcome onTakeTour={onTakeTour} /> : null}
      {noModel ? (
        <div className="kbot-krell__connect">
          <p className="kbot-krell__line">Connect an AI model to start talking to Krell.</p>
          <AiConnectForm onConnected={krell.reload} onAdded={krell.reload} />
        </div>
      ) : (
        <KrellConversation />
      )}
    </section>
  );
}
