import { Fragment, useCallback, useEffect, useRef, useState, type JSX } from "react";
import "./UnresolvedOrdersList.signal.css";
import type { OperationsClient, ResolveOrderRequest, UnresolvedOrderRow } from "./client";

const LOADING_TEXT = "Loading unresolved orders…";
const EMPTY_TEXT = "No unresolved orders.";
const LIST_ERROR_PREFIX = "Could not load unresolved orders — check the workstation is running.";
const RETRY_TEXT = "Retry";
const PANEL_TITLE = "Unresolved orders";
const HELPER_TEXT =
  "Krellbot could not confirm these orders when it sent them. Check the last lookup and match the client order ID to your exchange history before placing another order. Refresh reloads the list without resending orders.";
const COL_COID = "Client order ID";
const COL_AGE = "Age";
const COL_LOOKUP = "Last lookup";
const COL_RESOLVE = "Resolve";
const AGE_UNAVAILABLE = "—";
const LOOKUP_LABELS: Readonly<Record<string, string>> = {
  filled: "filled",
  open: "open",
  absent: "absent",
  none: "none",
};
const RESOLVE_OPEN_LABEL = "Resolve…";
const RESOLVE_CLOSE_LABEL = "Close";
const EVIDENCE_HELP_PREFIX = "The last venue lookup recorded";
const EVIDENCE_HELP_SUFFIX =
  "Resolving accepts that record as this order's outcome and journals any executed quantity once. Nothing is sent to the exchange.";
const ACCEPT_EVIDENCE_LABEL = "Resolve from venue record";
const ASSERT_HELP =
  "No venue lookup is recorded for this order. Check your exchange history, then say what happened. Krellbot journals what you assert once and sends nothing to the exchange.";
const WORKING_HELP =
  "The last venue lookup does not show a final outcome: the order may still be working. Cancel it at the exchange if it is, check your exchange history, then say what happened. Krellbot journals what you assert once and sends nothing to the exchange.";
const ASSERT_LEGEND = "What happened at the exchange?";
const NOT_EXECUTED_LABEL = "It did not execute";
const EXECUTED_LABEL = "It executed";
const QTY_LABEL = "Executed quantity";
const CONFIRM_LABEL = "Type the client order ID to confirm:";
const SUBMIT_LABEL = "Resolve order";
const SUBMITTING_LABEL = "Resolving…";
const RESOLVED_FALLBACK = "Order resolved. Nothing was sent to the exchange.";
const REFUSED_FALLBACK = "Krellbot refused to resolve this order. Nothing changed.";
const RESOLVE_FAILED_PREFIX = "Could not resolve this order — nothing changed.";

type Assertion = "executed" | "not_executed";

export type UnresolvedOrdersListProps = {
  client: OperationsClient;
};

function rowTestId(clientOrderId: string): string {
  return `ops-unresolved-row-${clientOrderId}`;
}

function formatAge(ageSeconds: number | null): string {
  if (ageSeconds === null || !Number.isFinite(ageSeconds) || ageSeconds < 0) {
    return AGE_UNAVAILABLE;
  }
  const seconds = Math.floor(ageSeconds);
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const rem = seconds % 60;
  if (minutes < 60) {
    return rem === 0 ? `${minutes}m` : `${minutes}m ${rem}s`;
  }
  const hours = Math.floor(minutes / 60);
  const remMin = minutes % 60;
  return remMin === 0 ? `${hours}h` : `${hours}h ${remMin}m`;
}

function lookupLabel(result: string): string {
  return LOOKUP_LABELS[result] ?? result;
}

// Only a lookup that is an outcome settles an order without typed confirmation.
// An open order may still fill. A partly executed one is listed as "filled" by
// the closed row shape; the server refuses it as still working and the form
// then asks for an assertion instead.
const SETTLED_LOOKUPS: ReadonlySet<string> = new Set(["filled", "absent"]);
const ASSERT_INSTEAD_CODES: ReadonlySet<string> = new Set([
  "order_still_working",
  "no_venue_evidence",
]);

function settlesFromLookup(row: UnresolvedOrderRow): boolean {
  return SETTLED_LOOKUPS.has(row.last_lookup_result);
}

export function UnresolvedOrdersList({ client }: UnresolvedOrdersListProps): JSX.Element {
  const [rows, setRows] = useState<UnresolvedOrderRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef(0);
  const [openCoid, setOpenCoid] = useState<string | null>(null);
  const [assertion, setAssertion] = useState<Assertion | null>(null);
  const [executedQty, setExecutedQty] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [pending, setPending] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [assertOnly, setAssertOnly] = useState<ReadonlySet<string>>(() => new Set());
  const canResolve = typeof client.resolveOrder === "function";

  const load = useCallback(async (): Promise<void> => {
    if (typeof client.listUnresolvedOrders !== "function") {
      setRows([]);
      setError(null);
      setLoading(false);
      return;
    }
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const next = await client.listUnresolvedOrders();
      if (requestId !== requestRef.current) {
        return;
      }
      setRows(next);
    } catch (err) {
      if (requestId !== requestRef.current) {
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
      setRows((prev) => prev ?? []);
    } finally {
      if (requestId === requestRef.current) {
        setLoading(false);
      }
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  const toggleResolve = useCallback((clientOrderId: string): void => {
    setOpenCoid((prev) => (prev === clientOrderId ? null : clientOrderId));
    setAssertion(null);
    setExecutedQty("");
    setConfirmation("");
    setResolveError(null);
  }, []);

  const submitResolve = useCallback(
    async (request: ResolveOrderRequest): Promise<void> => {
      if (typeof client.resolveOrder !== "function") {
        return;
      }
      setPending(true);
      setResolveError(null);
      setFeedback(null);
      try {
        const result = await client.resolveOrder(request);
        if (result.ok === true) {
          setFeedback(`${request.client_order_id}: ${result.message ?? RESOLVED_FALLBACK}`);
          setOpenCoid(null);
          await load();
        } else {
          setResolveError(result.message ?? result.code ?? REFUSED_FALLBACK);
          if (
            request.outcome === undefined &&
            typeof result.code === "string" &&
            ASSERT_INSTEAD_CODES.has(result.code)
          ) {
            // The venue record does not settle this order: ask the operator.
            setAssertOnly((prev) => new Set(prev).add(request.client_order_id));
          }
        }
      } catch (err) {
        setResolveError(
          `${RESOLVE_FAILED_PREFIX} ${err instanceof Error ? err.message : String(err)}`,
        );
      } finally {
        setPending(false);
      }
    },
    [client, load],
  );

  const renderResolveForm = (row: UnresolvedOrderRow): JSX.Element => {
    const coid = row.client_order_id;
    const assertReady =
      assertion !== null &&
      (assertion === "not_executed" || executedQty.trim() !== "") &&
      confirmation === coid;
    return (
      <div className="kbot-ops__unresolved-resolve" data-testid="ops-unresolved-resolve-form">
        {settlesFromLookup(row) && !assertOnly.has(coid) ? (
          <>
            <p className="kbot-ops__unresolved-resolve-help">
              {EVIDENCE_HELP_PREFIX} <strong>{lookupLabel(row.last_lookup_result)}</strong>.{" "}
              {EVIDENCE_HELP_SUFFIX}
            </p>
            <div className="kbot-ops__unresolved-resolve-actions">
              <button
                type="button"
                className="kbot-ops__unresolved-resolve-submit"
                data-testid="ops-unresolved-accept-evidence"
                disabled={pending}
                aria-busy={pending || undefined}
                onClick={() => {
                  void submitResolve({ client_order_id: coid });
                }}
              >
                {pending ? SUBMITTING_LABEL : ACCEPT_EVIDENCE_LABEL}
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="kbot-ops__unresolved-resolve-help">
              {row.last_lookup_result === "none" ? ASSERT_HELP : WORKING_HELP}
            </p>
            <fieldset className="kbot-ops__unresolved-resolve-outcome" disabled={pending}>
              <legend>{ASSERT_LEGEND}</legend>
              <label className="kbot-ops__unresolved-resolve-choice">
                <input
                  type="radio"
                  name="ops-unresolved-outcome"
                  value="not_executed"
                  checked={assertion === "not_executed"}
                  onChange={() => setAssertion("not_executed")}
                />
                {NOT_EXECUTED_LABEL}
              </label>
              <label className="kbot-ops__unresolved-resolve-choice">
                <input
                  type="radio"
                  name="ops-unresolved-outcome"
                  value="executed"
                  checked={assertion === "executed"}
                  onChange={() => setAssertion("executed")}
                />
                {EXECUTED_LABEL}
              </label>
              <div className="kbot-ops__unresolved-resolve-field">
                <label htmlFor="ops-unresolved-executed-qty">{QTY_LABEL}</label>
                <input
                  id="ops-unresolved-executed-qty"
                  data-testid="ops-unresolved-executed-qty"
                  type="text"
                  inputMode="decimal"
                  autoComplete="off"
                  spellCheck={false}
                  value={executedQty}
                  disabled={assertion !== "executed"}
                  onChange={(event) => setExecutedQty(event.target.value)}
                />
              </div>
              <div className="kbot-ops__unresolved-resolve-field">
                <label htmlFor="ops-unresolved-confirm">
                  {CONFIRM_LABEL} <code>{coid}</code>
                </label>
                <input
                  id="ops-unresolved-confirm"
                  data-testid="ops-unresolved-confirm"
                  className="kbot-ops__unresolved-resolve-confirm"
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                />
              </div>
            </fieldset>
            <div className="kbot-ops__unresolved-resolve-actions">
              <button
                type="button"
                className="kbot-ops__unresolved-resolve-submit"
                data-testid="ops-unresolved-submit"
                disabled={pending || !assertReady}
                aria-busy={pending || undefined}
                onClick={() => {
                  if (assertion === null) {
                    return;
                  }
                  void submitResolve(
                    assertion === "executed"
                      ? {
                          client_order_id: coid,
                          outcome: "executed",
                          executed_qty: executedQty.trim(),
                          confirmation,
                        }
                      : { client_order_id: coid, outcome: "not_executed", confirmation },
                  );
                }}
              >
                {pending ? SUBMITTING_LABEL : SUBMIT_LABEL}
              </button>
            </div>
          </>
        )}
        {resolveError !== null ? (
          <p
            className="kbot-ops__row-result"
            role="alert"
            data-testid="ops-unresolved-resolve-error"
          >
            {resolveError}
          </p>
        ) : null}
      </div>
    );
  };

  const showEmpty = !loading && error === null && rows !== null && rows.length === 0;
  const showRows = rows !== null && rows.length > 0;

  return (
    <div className="kbot-ops__unresolved" data-testid="ops-unresolved-orders">
      <div className="kbot-ops__unresolved-head">
        <h3 className="kbot-ops__panel-title">{PANEL_TITLE}</h3>
        <button
          type="button"
          className="kbot-ops__unresolved-refresh"
          data-testid="ops-unresolved-refresh"
          disabled={loading}
          aria-busy={loading || undefined}
          onClick={() => {
            void load();
          }}
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      <p className="kbot-ops__unresolved-helper">{HELPER_TEXT}</p>
      {feedback !== null ? (
        <p
          className="kbot-ops__unresolved-feedback"
          role="status"
          data-testid="ops-unresolved-feedback"
        >
          {feedback}
        </p>
      ) : null}
      {loading && rows === null ? (
        <p className="kbot-ops__empty" data-testid="ops-unresolved-loading">
          {LOADING_TEXT}
        </p>
      ) : null}
      {error !== null ? (
        <p className="kbot-ops__row-result" role="alert" data-testid="ops-unresolved-error">
          {LIST_ERROR_PREFIX} {error}{" "}
          <button
            type="button"
            data-testid="ops-unresolved-retry"
            onClick={() => {
              void load();
            }}
          >
            {RETRY_TEXT}
          </button>
        </p>
      ) : null}
      {showEmpty ? (
        <p className="kbot-ops__empty" data-testid="ops-unresolved-empty">
          {EMPTY_TEXT}
        </p>
      ) : null}
      {showRows ? (
        <div className="kbot-ops__unresolved-scroll" data-testid="ops-unresolved-scroll">
          <table className="kbot-ops__unresolved-table">
            <thead>
              <tr>
                <th scope="col">{COL_COID}</th>
                <th scope="col">{COL_AGE}</th>
                <th scope="col">{COL_LOOKUP}</th>
                {canResolve ? <th scope="col">{COL_RESOLVE}</th> : null}
              </tr>
            </thead>
            <tbody>
              {rows!.map((row) => {
                const open = canResolve && openCoid === row.client_order_id;
                return (
                  <Fragment key={row.client_order_id}>
                    <tr data-testid={rowTestId(row.client_order_id)}>
                      <td className="kbot-ops__unresolved-coid">{row.client_order_id}</td>
                      <td className="kbot-ops__unresolved-age">{formatAge(row.age_seconds)}</td>
                      <td className="kbot-ops__unresolved-lookup">
                        {lookupLabel(row.last_lookup_result)}
                      </td>
                      {canResolve ? (
                        <td className="kbot-ops__unresolved-action">
                          <button
                            type="button"
                            className="kbot-ops__unresolved-resolve-toggle"
                            data-testid={`ops-unresolved-resolve-${row.client_order_id}`}
                            aria-expanded={open}
                            disabled={pending}
                            onClick={() => toggleResolve(row.client_order_id)}
                          >
                            {open ? RESOLVE_CLOSE_LABEL : RESOLVE_OPEN_LABEL}
                          </button>
                        </td>
                      ) : null}
                    </tr>
                    {open ? (
                      <tr className="kbot-ops__unresolved-resolve-row">
                        <td colSpan={4}>{renderResolveForm(row)}</td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
