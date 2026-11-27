/**
 * The Krell tab (M38.16). With no usable model it shows only the M38.13
 * connect form; once a usable model is active (connected here, or in
 * Settings and the tab shown again), it shows the shared conversation.
 * Contract: KRELL-SIGNAL-20261010.
 */
import { useId, useState, type JSX } from "react";
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

export function KrellView(): JSX.Element {
  const krell = useKrell();
  const headingId = useId();
  const [confirmingClear, setConfirmingClear] = useState(false);
  const noModel = krell.status === "ready" && krell.connection === null;

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
          <p className="kbot-shell__pagesub">
            Krellbot&apos;s guide. She runs on an AI model you connect, and she asks before she changes
            anything.
          </p>
        </div>
        {krell.status === "ready" && !noModel ? (
          confirmingClear ? (
            <div className="kbot-krell__confirm" role="group" aria-label="Clear the conversation">
              <p className="kbot-krell__line">Clear the conversation and any open cards?</p>
              <button
                type="button"
                className="kbot-krell__btn"
                disabled={krell.pending}
                onClick={() => {
                  setConfirmingClear(false);
                  krell.clear();
                }}
              >
                Yes, clear
              </button>
              <button type="button" className="kbot-krell__btn" onClick={() => setConfirmingClear(false)}>
                Cancel
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="kbot-krell__btn"
              onClick={() => setConfirmingClear(true)}
              disabled={krell.pending}
            >
              Clear conversation
            </button>
          )
        ) : null}
      </div>
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
