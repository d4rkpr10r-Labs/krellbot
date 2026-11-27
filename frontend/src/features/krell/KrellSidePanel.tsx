/**
 * The "Ask Krell" header button and the Krell side panel (M38.16).
 *
 * The panel docks right at 340px and becomes a full-screen sheet at
 * 720px and below (`krell.css`). It shows the same conversation as the
 * Krell tab. The button and the panel title are text only: Krell's
 * avatar appears only inside the conversation. Contract: KRELL-SIGNAL-20261010.
 */
import { useEffect, useId, useRef, type JSX } from "react";
import { KrellConversation } from "./KrellConversation";
import { useKrell } from "./KrellConversationProvider";
import "./krell.css";

export const KRELL_PANEL_ID = "krell-panel";

export function AskKrellButton({ open, onToggle }: { open: boolean; onToggle(): void }): JSX.Element {
  return (
    <button
      type="button"
      className="kbot-krell__ask"
      data-testid="ask-krell"
      aria-expanded={open}
      aria-controls={KRELL_PANEL_ID}
      onClick={onToggle}
    >
      {/* Phones show just her name to keep the header on one row; the name stays "Ask Krell". */}
      <span className="kbot-krell__ask-verb">Ask </span>
      Krell
    </button>
  );
}

export type KrellSidePanelProps = {
  onClose(): void;
  onOpenTab(): void;
};

export function KrellSidePanel({ onClose, onOpenTab }: KrellSidePanelProps): JSX.Element {
  const krell = useKrell();
  const titleId = useId();
  const panelRef = useRef<HTMLElement>(null);

  useEffect(() => {
    panelRef.current?.focus();
  }, []);

  return (
    <aside
      id={KRELL_PANEL_ID}
      ref={panelRef}
      tabIndex={-1}
      className="kbot-krell kbot-krell__panel"
      data-testid="krell-panel"
      aria-labelledby={titleId}
    >
      <header className="kbot-krell__panel-head">
        <h2 className="kbot-krell__panel-title" id={titleId}>
          Krell
        </h2>
        <div className="kbot-krell__panel-actions">
          <button type="button" className="kbot-krell__btn" onClick={onOpenTab}>
            Open in Krell tab
          </button>
          <button type="button" className="kbot-krell__btn" onClick={onClose}>
            Close
          </button>
        </div>
      </header>
      {krell.status === "ready" && krell.connection === null ? (
        <p className="kbot-krell__line">Connect an AI model in the Krell tab to start talking to Krell.</p>
      ) : (
        <KrellConversation />
      )}
    </aside>
  );
}
