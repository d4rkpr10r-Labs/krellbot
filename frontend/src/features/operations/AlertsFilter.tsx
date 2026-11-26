import { useState, type ReactNode } from "react";
import type { AlertRow } from "./client";
import { summarizeAlertKind } from "./alertKindLabel";

const ALL_SEVERITIES = "all";
const ALL_KINDS = "all";
const ALL_SEVERITY_LABEL = "All severities";
const ALL_KIND_LABEL = "All kinds";
const CLEAR_TEXT = "Clear filters";
const EMPTY_TEXT = "No alerts match the current filters.";

/** Where Operations keeps the viewer's alert filters across reloads. */
export const ALERTS_FILTER_STORAGE_KEY = "krellbot.ops.alerts-filter";

// Severities and kinds are short backend codes; anything else read back
// from storage is treated as garbage rather than rendered as an option.
const STORED_CODE_PATTERN = /^[A-Za-z0-9_.:-]{1,64}$/;

type AlertsFilterValues = {
  severity: string;
  kind: string;
  query: string;
};

const DEFAULT_FILTERS: AlertsFilterValues = {
  severity: ALL_SEVERITIES,
  kind: ALL_KINDS,
  query: "",
};

export type AlertsFilterProps = {
  alerts: readonly AlertRow[];
  /**
   * Persist severity, kind and query across reloads in localStorage,
   * following DisclosureSection's storageKey. Best-effort only: a blocked,
   * throwing or garbage store falls back to the default filters.
   */
  storageKey?: string;
  children: (visible: AlertRow[], isEmpty: boolean) => ReactNode;
};

function isDefaultFilters(filters: AlertsFilterValues): boolean {
  return (
    filters.severity === DEFAULT_FILTERS.severity &&
    filters.kind === DEFAULT_FILTERS.kind &&
    filters.query === DEFAULT_FILTERS.query
  );
}

function readStoredFilters(storageKey: string | undefined): AlertsFilterValues {
  if (!storageKey || typeof window === "undefined") {
    return DEFAULT_FILTERS;
  }
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (raw === null) {
      return DEFAULT_FILTERS;
    }
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return DEFAULT_FILTERS;
    }
    const { severity, kind, query } = parsed as Record<string, unknown>;
    if (
      typeof severity !== "string" ||
      typeof kind !== "string" ||
      typeof query !== "string" ||
      !STORED_CODE_PATTERN.test(severity) ||
      !STORED_CODE_PATTERN.test(kind)
    ) {
      return DEFAULT_FILTERS;
    }
    return { severity, kind, query };
  } catch {
    return DEFAULT_FILTERS;
  }
}

function writeStoredFilters(
  storageKey: string | undefined,
  filters: AlertsFilterValues,
): void {
  if (!storageKey || typeof window === "undefined") {
    return;
  }
  try {
    if (isDefaultFilters(filters)) {
      window.localStorage.removeItem(storageKey);
      return;
    }
    // Only the three filter values; never alert rows or anything else.
    const stored: AlertsFilterValues = {
      severity: filters.severity,
      kind: filters.kind,
      query: filters.query,
    };
    window.localStorage.setItem(storageKey, JSON.stringify(stored));
  } catch {
    // Best-effort only.
  }
}

export function severityOptions(alerts: readonly AlertRow[]): string[] {
  return [ALL_SEVERITIES, ...Array.from(new Set(alerts.map((a) => a.severity)))];
}

export function kindOptions(alerts: readonly AlertRow[]): string[] {
  return [ALL_KINDS, ...Array.from(new Set(alerts.map((a) => a.kind)))];
}

/** Unacknowledged alerts — the honest unread count for Operations. */
export function unreadAlertCount(alerts: readonly AlertRow[]): number {
  return alerts.reduce(
    (count, alert) => count + (alert.acknowledged === true ? 0 : 1),
    0,
  );
}

export function unreadAlertLabel(count: number): string {
  return count === 1 ? "1 unread" : `${count} unread`;
}

export function AlertsFilter({ alerts, storageKey, children }: AlertsFilterProps) {
  const [filters, setFilters] = useState<AlertsFilterValues>(() =>
    readStoredFilters(storageKey),
  );
  const { severity, kind, query } = filters;
  const applyFilters = (next: AlertsFilterValues) => {
    setFilters(next);
    writeStoredFilters(storageKey, next);
  };

  // Keep an explicitly selected severity/kind represented even after a
  // refresh drops the last row of that value, so the select always
  // describes the filter actually applied.
  const severities = severityOptions(alerts);
  if (severity !== ALL_SEVERITIES && !severities.includes(severity)) {
    severities.push(severity);
  }
  const kinds = kindOptions(alerts);
  if (kind !== ALL_KINDS && !kinds.includes(kind)) {
    kinds.push(kind);
  }

  const needle = query.trim().toLowerCase();
  const visible = alerts.filter((alert) => {
    if (severity !== ALL_SEVERITIES && alert.severity !== severity) {
      return false;
    }
    if (kind !== ALL_KINDS && alert.kind !== kind) {
      return false;
    }
    if (needle === "") {
      return true;
    }
    const code = (alert.code ?? "").toLowerCase();
    const venue = (alert.venue ?? "").toLowerCase();
    const pair = (alert.pair ?? "").toLowerCase();
    const kindNeedle = alert.kind.toLowerCase();
    return (
      code.includes(needle) ||
      venue.includes(needle) ||
      pair.includes(needle) ||
      kindNeedle.includes(needle)
    );
  });
  const isEmpty = alerts.length > 0 && visible.length === 0;
  const filtersActive =
    severity !== ALL_SEVERITIES || kind !== ALL_KINDS || query !== "";
  const unread = unreadAlertCount(alerts);

  return (
    <div className="kbot-ops__alerts-filter" data-testid="ops-alerts-filter">
      <p
        className="kbot-ops__alerts-unread"
        data-testid="ops-alerts-unread-count"
        aria-live="polite"
      >
        {unreadAlertLabel(unread)}
      </p>
      <label id="ops-alerts-filter-severity-label" htmlFor="ops-alerts-filter-severity-input">
        Severity
        <select
          id="ops-alerts-filter-severity-input"
          aria-label="Filter alerts by severity"
          aria-describedby={isEmpty ? "ops-alerts-empty-id" : undefined}
          data-testid="ops-alerts-filter-severity"
          value={severity}
          onChange={(event) =>
            applyFilters({ ...filters, severity: event.target.value })
          }
        >
          {severities.map((option) => (
            <option key={option} value={option}>
              {option === ALL_SEVERITIES ? ALL_SEVERITY_LABEL : option}
            </option>
          ))}
        </select>
      </label>
      <label id="ops-alerts-filter-kind-label" htmlFor="ops-alerts-filter-kind-input">
        Kind
        <select
          id="ops-alerts-filter-kind-input"
          aria-label="Filter alerts by kind"
          aria-describedby={isEmpty ? "ops-alerts-empty-id" : undefined}
          data-testid="ops-alerts-filter-kind"
          value={kind}
          onChange={(event) =>
            applyFilters({ ...filters, kind: event.target.value })
          }
        >
          {kinds.map((option) => (
            <option key={option} value={option}>
              {option === ALL_KINDS
                ? ALL_KIND_LABEL
                : summarizeAlertKind(option).label}
            </option>
          ))}
        </select>
      </label>
      <label id="ops-alerts-filter-query-label" htmlFor="ops-alerts-filter-query-input">
        Search
        <input
          id="ops-alerts-filter-query-input"
          aria-label="Search alerts by code, venue, or kind"
          aria-describedby={isEmpty ? "ops-alerts-empty-id" : undefined}
          data-testid="ops-alerts-filter-query"
          type="text"
          value={query}
          placeholder="Filter by code, venue, pair, or kind"
          onChange={(event) =>
            applyFilters({ ...filters, query: event.target.value })
          }
        />
      </label>
      {filtersActive ? (
        <button
          type="button"
          data-testid="ops-alerts-filter-clear"
          onClick={() => applyFilters(DEFAULT_FILTERS)}
        >
          {CLEAR_TEXT}
        </button>
      ) : null}
      {isEmpty ? (
        <p id="ops-alerts-empty-id" className="kbot-ops__alerts-empty" data-testid="ops-alerts-empty">
          {EMPTY_TEXT}
        </p>
      ) : null}
      {children(visible, isEmpty)}
    </div>
  );
}
