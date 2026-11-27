/**
 * The "Ask Krell" header button and the Krell side panel (M38.16).
 *
 * The panel docks right at 340px and becomes a full-screen sheet at 720px
 * and below (`krell.css`). As a sheet it is a modal dialog: the shell makes
 * everything behind it inert. Escape closes it. It shows the same
 * conversation as the Krell tab. The button and the panel title are text
 * only: Krell's avatar appears only inside the conversation.
 * Contract: KRELL-SIGNAL-20261010.
 */
import {
  useEffect,
  useId,
  useRef,
  useState,
  type JSX,
  type KeyboardEvent,
  type Ref,
} from "react";
import { KrellConversation } from "./KrellConversation";
import { useKrell } from "./KrellConversationProvider";
import "./krell.css";

export const KRELL_PANEL_ID = "krell-panel";

/** Where the panel stops docking and covers the screen (matches `krell.css`). */
const SHEET_QUERY = "(max-width: 720px)";

function sheetMatches(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(SHEET_QUERY).matches
    : false;
}

/** True while the viewport shows the panel as a full-screen sheet. */
export function useKrellSheet(): boolean {
  const [sheet, setSheet] = useState(sheetMatches);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(SHEET_QUERY);
    const update = (): void => setSheet(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return sheet;
}

export type AskKrellButtonProps = {
  open: boolean;
  onToggle(): void;
  ref?: Ref<HTMLButtonElement>;
};

export function AskKrellButton({ open, onToggle, ref }: AskKrellButtonProps): JSX.Element {
  return (
    <button
      type="button"
      ref={ref}
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
  /** Full-screen sheet: a modal dialog over an inert workstation. */
  modal?: boolean;
  /** Close button or Escape. */
  onClose(): void;
  onOpenTab(): void;
};

export function KrellSidePanel({ modal = false, onClose, onOpenTab }: KrellSidePanelProps): JSX.Element {
  const krell = useKrell();
  const titleId = useId();
  const panelRef = useRef<HTMLElement>(null);

  useEffect(() => {
    panelRef.current?.focus();
  }, []);

  // A plain Escape inside the panel closes it, as the Jobs drawer does.
  const onKeyDown = (event: KeyboardEvent<HTMLElement>): void => {
    const native = event.nativeEvent;
    if (
      event.key !== "Escape" ||
      event.defaultPrevented ||
      native.isComposing ||
      native.ctrlKey ||
      native.altKey ||
      native.metaKey ||
      native.shiftKey
    ) {
      return;
    }
    event.preventDefault();
    onClose();
  };

  return (
    <aside
      id={KRELL_PANEL_ID}
      ref={panelRef}
      tabIndex={-1}
      className="kbot-krell kbot-krell__panel"
      data-testid="krell-panel"
      role={modal ? "dialog" : undefined}
      aria-modal={modal || undefined}
      aria-labelledby={titleId}
      onKeyDown={onKeyDown}
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
