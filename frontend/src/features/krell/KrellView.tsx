/**
 * The Krell tab (M38.16). With no usable model it shows only the M38.13
 * connect form; once that form reports a usable, active connection, the
 * tab switches to the shared conversation. Contract: KRELL-SIGNAL-20261010.
 */
import { useId, type JSX } from "react";
import { AiConnectForm } from "../settings/AiConnectForm";
import { KrellConversation } from "./KrellConversation";
import { useKrell } from "./KrellConversationProvider";
import "./krell.css";

export function KrellView(): JSX.Element {
  const krell = useKrell();
  const headingId = useId();
  const noModel = krell.status === "ready" && krell.connection === null;

  return (
    <section className="kbot-krell kbot-krell--tab" data-testid="krell-view" aria-labelledby={headingId}>
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
          <button type="button" className="kbot-krell__btn" onClick={krell.clear} disabled={krell.pending}>
            Clear conversation
          </button>
        ) : null}
      </div>
      {noModel ? (
        <div className="kbot-krell__connect">
          <p className="kbot-krell__line">Connect an AI model to start talking to Krell.</p>
          <AiConnectForm onConnected={krell.reload} />
        </div>
      ) : (
        <KrellConversation />
      )}
    </section>
  );
}
