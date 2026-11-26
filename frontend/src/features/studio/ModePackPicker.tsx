import { useEffect, useState, type JSX } from "react";
import type { Pack } from "../strategies/client";
import {
  CONSUMER_STARTER_CARD_ORDER,
  STRUGGLE_COPY,
  type StruggleCopy,
} from "../strategies/consumerStarterCopy";
import {
  fetchModePacks,
  type ModePackCatalogue,
  type ModePackRow,
} from "./modePackClient";
import "./ModePackPicker.signal.css";

export type ModePackPickerProps = {
  onPick: (pack: Pack) => void;
  loadModes?: () => Promise<ModePackCatalogue>;
};

/**
 * Struggle lines for the modes the catalogue listed, in its order, then the
 * three starters. A mode the catalogue omitted gets no line.
 */
function struggleEntries(rows: ModePackRow[]): StruggleCopy[] {
  const ids = [
    ...rows.map((row) => row.id),
    ...CONSUMER_STARTER_CARD_ORDER,
  ];
  return ids.flatMap((id) => {
    const entry = STRUGGLE_COPY[id];
    return entry ? [entry] : [];
  });
}

/**
 * Studio control to open a built-in trading-mode pack as a real graph.
 * Selecting a mode replaces demo nodes with ``nodesFromPack`` output.
 */
export function ModePackPicker({
  onPick,
  loadModes = fetchModePacks,
}: ModePackPickerProps): JSX.Element {
  const [rows, setRows] = useState<ModePackRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    setError(null);
    void loadModes()
      .then((doc) => {
        if (cancelled) {
          return;
        }
        setRows(doc.modes);
        setBusy(false);
      })
      .catch((err: unknown) => {
        if (cancelled) {
          return;
        }
        setRows(null);
        setError(err instanceof Error ? err.message : String(err));
        setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [loadModes]);

  return (
    <div className="kbot-mode-pack-picker" data-testid="studio-mode-pack-picker">
      <p className="kbot-mode-pack-picker__eyebrow">Built-in modes</p>
      <p className="kbot-mode-pack-picker__hint">
        Open DCA, grid, or rebalance as a readable graph. Save execution still
        validates. Not live market performance.
      </p>
      {busy ? (
        <p data-testid="studio-mode-pack-loading">Loading modes…</p>
      ) : null}
      {error ? (
        <p
          className="kbot-shell__error"
          role="alert"
          data-testid="studio-mode-pack-error"
        >
          {error}
        </p>
      ) : null}
      {rows && rows.length === 0 ? (
        <p data-testid="studio-mode-pack-empty">No built-in mode packs shipped.</p>
      ) : null}
      {rows && rows.length > 0 ? (
        <ul className="kbot-mode-pack-picker__list">
          {rows.map((row) => (
            <li key={row.id}>
              <button
                type="button"
                className="kbot-mode-pack-picker__button"
                data-testid={`studio-mode-pack-${row.id}`}
                onClick={() => {
                  onPick(row.pack);
                }}
              >
                {row.label}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {rows ? (
        <div
          className="kbot-mode-pack-picker__struggles"
          data-testid="studio-struggle-copy"
        >
          <p className="kbot-mode-pack-picker__eyebrow">
            When each tends to struggle
          </p>
          <p className="kbot-mode-pack-picker__hint">
            Market conditions that work against each rule set. Not a forecast
            or a performance claim.
          </p>
          <dl className="kbot-mode-pack-picker__struggle-list">
            {struggleEntries(rows).map((entry) => (
              <div
                key={entry.id}
                className="kbot-mode-pack-picker__struggle-item"
                data-testid={`studio-struggle-${entry.id}`}
              >
                <dt className="kbot-mode-pack-picker__struggle-title">
                  {entry.title}
                </dt>
                <dd className="kbot-mode-pack-picker__struggle-text">
                  {entry.struggle}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}
    </div>
  );
}
