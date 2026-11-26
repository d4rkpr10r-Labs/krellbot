import { useEffect, useId, useRef, useState, type JSX, type KeyboardEvent as ReactKeyboardEvent } from "react";
import "./ArmedRecordsList.signal.css";
import type { PaperArmedClient, PaperArmedRecord, PaperClient } from "./client";
import { PAUSE_NEW_BUYS_DISCLOSURE, STOP_RUN_DISCLOSURE } from "./stopRunDisclosure";

export type { PaperArmedClient, PaperArmedRecord } from "./client";

export type ArmedRecordsListProps = {
  client: PaperArmedClient;
  /** Reports the current armed-row selection to the shell inspector. */
  onSelectionChange?: (record: PaperArmedRecord | null) => void;
  /**
   * Incrementing this key triggers a quiet re-read (for example right
   * after StatusPanel confirms an arm, pause, resume, or stop) without
   * flashing the loading state.
   */
  refreshKey?: number;
};

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; records: PaperArmedRecord[] }
  | { kind: "unavailable" };

type EntriesSentence = "Entries active" | "Entries paused";

/**
 * One row's risk cap, read from `GET /api/v1/paper/details` for that exact
 * (venue, pair). The closed `/api/v1/paper/armed` row never carries it.
 */
type RiskCapRead =
  | { kind: "reading" }
  | { kind: "read"; pct: string }
  | { kind: "unavailable" };

/** Cap reads belong to the list read they were started for, never a later one. */
type RiskCaps = {
  list: LoadState;
  byKey: Record<string, RiskCapRead>;
};

function sentenceFor(entries_paused: boolean): EntriesSentence {
  return entries_paused ? "Entries paused" : "Entries active";
}

function rowKey(record: PaperArmedRecord): string {
  return `${record.venue}:${record.pair}`;
}

/**
 * Paper entries are sized as this percent of the paper wallet's free quote
 * cash at entry (run.tick's balance * cap rule), so the sentence names free
 * cash. A missing value says 'unavailable' — never a guessed number.
 */
function riskCapSentence(read: RiskCapRead): string {
  if (read.kind === "read") {
    return `Risk cap ${read.pct}% of free cash per entry`;
  }
  return read.kind === "reading" ? "Risk cap: reading…" : "Risk cap unavailable";
}

/** Armed-run count chip copy: the real total read from the wire. */
function armedCountLabel(count: number): string {
  return count === 1 ? "1 armed run" : `${count} armed runs`;
}

/** Entries chip tone: sage for an active armed record, coral for a
 *  paused one. The verbatim sentence carries the state. */
function entriesChipTone(entries_paused: boolean): string {
  return entries_paused ? "coral" : "sage";
}

export function ArmedRecordsList({
  client,
  onSelectionChange,
  refreshKey = 0,
}: ArmedRecordsListProps): JSX.Element {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [selected, setSelected] = useState<PaperArmedRecord | null>(null);
  const [inFlight, setInFlight] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  const [rowActionError, setRowActionError] = useState<string | null>(null);
  const [rowActionKind, setRowActionKind] = useState<"pause" | "resume" | "disarm" | null>(null);
  const [rowActionInFlight, setRowActionInFlight] = useState(false);
  const rowActionKey = useRef<string | null>(null);
  const runControlsDisclosureId = useId();

  // The shell may pass a legacy client that does not implement
  // listArmed (the pre-armed-records injected test client). Calling it
  // would throw synchronously, so probe for the method; anything that
  // fails resolves to the truthful unavailable state rather than a
  // manufactured empty list.
  const canListArmed =
    typeof client === "object" &&
    client !== null &&
    typeof client.listArmed === "function";
  const canReadDetails =
    typeof client === "object" &&
    client !== null &&
    typeof client.getDetails === "function";

  const listGeneration = useRef(0);
  const capGeneration = useRef(0);
  const [riskCaps, setRiskCaps] = useState<RiskCaps | null>(null);

  // Every list read re-reads each row's cap for its own (venue, pair). A
  // late answer for a superseded list read is dropped, and a row never shows
  // a cap read for another market.
  useEffect(() => {
    if (state.kind !== "ready") {
      return;
    }
    const list = state;
    const generation = ++capGeneration.current;
    const byKey: Record<string, RiskCapRead> = {};
    for (const row of list.records) {
      byKey[rowKey(row)] = { kind: canReadDetails ? "reading" : "unavailable" };
    }
    setRiskCaps({ list, byKey });
    const read = client.getDetails;
    if (!canReadDetails || read === undefined) {
      return;
    }
    const settle = (row: PaperArmedRecord, next: RiskCapRead): void => {
      if (generation !== capGeneration.current) {
        return;
      }
      setRiskCaps((current) =>
        current === null || current.list !== list
          ? current
          : { list, byKey: { ...current.byKey, [rowKey(row)]: next } },
      );
    };
    for (const row of list.records) {
      read
        .call(client, { venue: row.venue, pair: row.pair })
        .then((details) => {
          const pct =
            details.armed === true &&
            details.venue === row.venue &&
            details.pair === row.pair &&
            typeof details.risk_cap_pct === "string"
              ? details.risk_cap_pct
              : null;
          settle(row, pct === null ? { kind: "unavailable" } : { kind: "read", pct });
        })
        .catch(() => {
          settle(row, { kind: "unavailable" });
        });
    }
    return () => {
      capGeneration.current += 1;
    };
  }, [client, canReadDetails, state]);

  const riskCapFor = (row: PaperArmedRecord): RiskCapRead => {
    if (riskCaps !== null && riskCaps.list === state) {
      const read = riskCaps.byKey[rowKey(row)];
      if (read !== undefined) {
        return read;
      }
    }
    return { kind: canReadDetails ? "reading" : "unavailable" };
  };

  useEffect(() => {
    if (!canListArmed) {
      setState({ kind: "unavailable" });
      return;
    }
    const generation = ++listGeneration.current;
    let cancelled = false;
    setState({ kind: "loading" });
    client
      .listArmed()
      .then((view) => {
        if (cancelled || generation !== listGeneration.current) {
          return;
        }
        setState({ kind: "ready", records: view.records });
        setSelected((current) => {
          if (current === null) {
            return null;
          }
          const match = view.records.find(
            (row) => rowKey(row) === rowKey(current),
          );
          return match ?? null;
        });
      })
      .catch((err: unknown) => {
        if (cancelled || generation !== listGeneration.current) {
          return;
        }
        // A failed lookup means the list is unknown; never render an
        // empty-list claim from a failure.
        setState({ kind: "unavailable" });
        setLastError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [client, canListArmed]);

  const lastRefreshKeyRef = useRef(refreshKey);
  useEffect(() => {
    if (refreshKey === lastRefreshKeyRef.current) {
      return;
    }
    lastRefreshKeyRef.current = refreshKey;
    if (!canListArmed) {
      return;
    }
    const generation = ++listGeneration.current;
    let cancelled = false;
    client
      .listArmed()
      .then((view) => {
        if (cancelled || generation !== listGeneration.current) {
          return;
        }
        setState({ kind: "ready", records: view.records });
        setLastError(null);
        setSelected((current) => {
          if (current === null) {
            return null;
          }
          const match = view.records.find(
            (row) => rowKey(row) === rowKey(current),
          );
          return match ?? null;
        });
      })
      .catch((err: unknown) => {
        if (cancelled || generation !== listGeneration.current) {
          return;
        }
        setLastError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, client, canListArmed]);

  const refresh = async (): Promise<void> => {
    if (!canListArmed || inFlight) {
      return;
    }
    setInFlight(true);
    try {
      const view = await client.listArmed();
      setState({ kind: "ready", records: view.records });
      setLastError(null);
      setSelected((current) => {
        if (current === null) {
          return null;
        }
        const match = view.records.find(
          (row) => rowKey(row) === rowKey(current),
        );
        return match ?? null;
      });
    } catch (err) {
      // Keep the last good rows on a transient failure, but surface
      // the failure honestly rather than pretending the retry worked.
      setLastError(err instanceof Error ? err.message : String(err));
      if (state.kind !== "ready") {
        setState({ kind: "unavailable" });
      }
    } finally {
      setInFlight(false);
    }
  };

  const runRowAction = async (kind: "pause" | "resume" | "disarm"): Promise<void> => {
    if (selected === null || rowActionInFlight) {
      return;
    }
    const record = selected;
    const label =
      kind === "pause"
        ? "Pause new buys"
        : kind === "resume"
          ? "Resume new buys"
          : "Stop paper run";
    rowActionKey.current = rowKey(record);
    setRowActionKind(kind);
    setRowActionInFlight(true);
    setRowActionError(null);
    try {
      // The shell supplies the combined client; preserve the list-only prop contract.
      const commands = client as PaperArmedClient & PaperClient;
      const result = await (kind === "pause"
        ? commands.pauseEntries(record.venue, record.pair)
        : kind === "resume"
          ? commands.resumeEntries(record.venue, record.pair)
          : commands.disarm(record.venue, record.pair));
      if (result?.ok !== true) {
        throw new Error(result?.message || result?.code || "Command success was not confirmed");
      }
      setRowActionError(null);
      setSelected(null);
      try {
        const view = await client.listArmed();
        setState({ kind: "ready", records: view.records });
        setLastError(null);
      } catch (err) {
        // A confirmed command must not be reported as failed when only its readback fails.
        setLastError(err instanceof Error ? err.message : String(err));
      }
    } catch (err) {
      setRowActionError(`${label} failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setRowActionInFlight(false);
    }
  };

  const rows = state.kind === "ready" ? state.records : [];

  // Pointer and keyboard toggle the exact same selection state.
  const toggleRow = (row: PaperArmedRecord): void => {
    const next =
      selected !== null && rowKey(selected) === rowKey(row) ? null : row;
    setSelected(next);
    onSelectionChange?.(next);
  };

  // Keyboard activation with native-button semantics: plain and Shift
  // chords of Enter/Space toggle the detail, Ctrl/Meta/Alt chords and
  // auto-repeats are left alone, unrelated keys keep their default.
  // Space is canceled on every press (repeats included) so a focused
  // row never scrolls the page.
  const handleRowKeyDown = (
    event: ReactKeyboardEvent<HTMLTableRowElement>,
    row: PaperArmedRecord,
  ): void => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) {
      return;
    }
    event.preventDefault();
    if (event.repeat) {
      return;
    }
    toggleRow(row);
  };

  return (
    <section
      className="kbot-paper-armed"
      role="region"
      aria-label="Armed records"
      data-testid="paper-armed-records"
    >
      <header
        className="kbot-paper-armed__head"
        data-testid="paper-armed-head"
      >
        <div className="kbot-paper-armed__head-titles">
          <h2 className="kbot-paper-armed__title">Armed paper runs</h2>
          {state.kind === "ready" ? (
            <p
              className="kbot-paper-armed__count"
              data-testid="paper-armed-count"
            >
              {armedCountLabel(rows.length)}
            </p>
          ) : null}
        </div>
        <button
          type="button"
          className="kbot-paper-armed__action"
          onClick={() => {
            void refresh();
          }}
          disabled={inFlight || !canListArmed}
        >
          {inFlight ? "Refreshing…" : "Refresh"}
        </button>
      </header>
      <div className="kbot-paper-armed__body">
        {state.kind === "loading" ? (
          <section
            className="kbot-paper-armed__panel"
            aria-label="Armed paper run list"
            data-testid="paper-armed-list-panel"
          >
            <h3 className="kbot-paper-armed__panel-title">Armed</h3>
            <p
              className="kbot-paper-armed__empty"
              data-testid="paper-armed-loading"
            >
              Loading armed records…
            </p>
          </section>
        ) : state.kind === "unavailable" ? (
          <section
            className="kbot-paper-armed__panel"
            aria-label="Armed paper run list"
            data-testid="paper-armed-list-panel"
          >
            <h3 className="kbot-paper-armed__panel-title">Armed</h3>
            <p
              className="kbot-paper-armed__empty"
              role="alert"
              data-testid="paper-armed-unavailable"
            >
              Armed records unavailable
            </p>
            {lastError !== null ? (
              <p className="kbot-paper-armed__error" data-testid="paper-armed-error">
                {lastError}
              </p>
            ) : null}
          </section>
        ) : rows.length === 0 ? (
          <section
            className="kbot-paper-armed__panel"
            aria-label="Armed paper run list"
            data-testid="paper-armed-list-panel"
          >
            <h3 className="kbot-paper-armed__panel-title">Armed</h3>
            <p
              className="kbot-paper-armed__empty kbot-paper-armed__empty--none"
              data-testid="paper-armed-empty"
            >
              No packs armed
            </p>
          </section>
        ) : (
          <>
            <section
              className="kbot-paper-armed__panel"
              aria-label="Armed paper run list"
              data-testid="paper-armed-list-panel"
            >
              <h3 className="kbot-paper-armed__panel-title">Armed</h3>
              <div
                className="kbot-paper-armed__scroll"
                data-testid="paper-armed-scroll"
              >
                <table
                  className="kbot-paper-armed__table"
                  data-testid="paper-armed-table"
                >
                  <thead>
                    <tr>
                      <th scope="col">Venue</th>
                      <th scope="col">Pair</th>
                      <th scope="col">Mode</th>
                      <th scope="col">Pack</th>
                      <th scope="col">Entries</th>
                      <th scope="col">Risk cap</th>
                      <th scope="col">
                        <span aria-hidden="true">Open</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => {
                      const isSelected =
                        selected !== null && rowKey(selected) === rowKey(row);
                      const tone = entriesChipTone(row.entries_paused);
                      return (
                        <tr
                          key={rowKey(row)}
                          className={`kbot-paper-armed__row${
                            isSelected
                              ? " kbot-paper-armed__row--selected"
                              : ""
                          }`}
                          data-testid="paper-armed-row"
                          data-venue={row.venue}
                          data-pair={row.pair}
                          tabIndex={0}
                          aria-label={`Toggle armed record detail for ${row.venue} ${row.pair}`}
                          onClick={() => {
                            toggleRow(row);
                          }}
                          onKeyDown={(event) => {
                            handleRowKeyDown(event, row);
                          }}
                        >
                          <td className="kbot-paper-armed__mono">{row.venue}</td>
                          <td className="kbot-paper-armed__pair">{row.pair}</td>
                          <td className="kbot-paper-armed__mono">{row.mode}</td>
                          <td className="kbot-paper-armed__mono">{row.pack_id}</td>
                          <td>
                            <span
                              className={`kbot-paper-armed__chip kbot-paper-armed__chip--${tone}`}
                              data-testid={`paper-armed-status-${row.venue}-${row.pair}`}
                            >
                              {sentenceFor(row.entries_paused)}
                            </span>
                          </td>
                          <td data-testid={`paper-armed-risk-cap-${row.venue}-${row.pair}`}>
                            {riskCapSentence(riskCapFor(row))}
                          </td>
                          <td className="kbot-paper-armed__row-open">
                            <span aria-hidden="true">›</span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
            {selected !== null ? (
              <section
                className="kbot-paper-armed__panel kbot-paper-armed__detail"
                role="region"
                aria-label="Armed record detail"
                data-testid="paper-armed-detail"
              >
                <h3 className="kbot-paper-armed__detail-title">
                  {selected.pack_id} on {selected.venue} {selected.pair}
                </h3>
                <dl>
                  <div>
                    <dt>Venue</dt>
                    <dd>{selected.venue}</dd>
                  </div>
                  <div>
                    <dt>Pair</dt>
                    <dd>{selected.pair}</dd>
                  </div>
                  <div>
                    <dt>Mode</dt>
                    <dd>{selected.mode}</dd>
                  </div>
                  <div>
                    <dt>Pack</dt>
                    <dd>{selected.pack_id}</dd>
                  </div>
                  <div>
                    <dt>Entries</dt>
                    <dd>{sentenceFor(selected.entries_paused)}</dd>
                  </div>
                </dl>
                <button
                  type="button"
                  className="kbot-paper-armed__row-action"
                  data-testid="paper-armed-row-pause"
                  aria-label={`Pause new buys on ${selected.venue} ${selected.pair}`}
                  aria-describedby={runControlsDisclosureId}
                  disabled={rowActionInFlight || !selected || selected.entries_paused === true}
                  onClick={() => { void runRowAction("pause"); }}
                >
                  Pause new buys
                </button>
                <button
                  type="button"
                  className="kbot-paper-armed__row-action"
                  data-testid="paper-armed-row-resume"
                  aria-label={`Resume new buys on ${selected.venue} ${selected.pair}`}
                  disabled={rowActionInFlight || !selected || selected.entries_paused === false}
                  onClick={() => { void runRowAction("resume"); }}
                >
                  Resume new buys
                </button>
                <button
                  type="button"
                  className="kbot-paper-armed__row-action"
                  data-testid="paper-armed-row-disarm"
                  aria-label={`Stop paper run ${selected.venue} ${selected.pair}`}
                  aria-describedby={runControlsDisclosureId}
                  disabled={rowActionInFlight}
                  onClick={() => { void runRowAction("disarm"); }}
                >
                  Stop paper run
                </button>
                <p
                  className="kbot-paper-armed__note"
                  id={runControlsDisclosureId}
                  data-testid="paper-armed-stop-run-disclosure"
                >
                  {PAUSE_NEW_BUYS_DISCLOSURE} {STOP_RUN_DISCLOSURE}
                </p>
                {rowActionError !== null && rowActionKind !== null && rowActionKey.current === rowKey(selected) ? (
                  <p
                    data-testid="paper-armed-row-action-error"
                    role="alert"
                    className="kbot-paper-armed__row-action-error"
                  >
                    {rowActionError}
                  </p>
                ) : null}
              </section>
            ) : null}
            {lastError !== null ? (
              <p
                className="kbot-paper-armed__error"
                role="alert"
                data-testid="paper-armed-refresh-error"
              >
                {lastError}
              </p>
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}
