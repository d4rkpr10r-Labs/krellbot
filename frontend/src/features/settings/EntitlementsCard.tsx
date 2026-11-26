/**
 * Settings → Trust & billing: what entitlements gate and what continues on
 * lapse (M28.14, decision D10). Contract: KRELL-SIGNAL-20261010.
 *
 * Read-only. The rule copy and the license state both come from
 * `GET /api/v1/entitlements`; the backend test checks every rule key against
 * the tick gate, so this card renders the text as served and never invents a
 * license status. No price is shown (M28.34 is undecided).
 */
import { useEffect, useState, type JSX } from "react";
import "./EntitlementsCard.signal.css";

export type EntitlementRule = {
  key: string;
  group: string;
  text: string;
};

export type EntitlementLicense = {
  /** missing | active | past_due | lapsed | dead | revoked | other */
  status: string;
  period_end: number | null;
  grace_until: number | null;
  paid_catalogue_entries_allowed: boolean;
};

export type EntitlementDisclosure = {
  schema_version: string;
  license: EntitlementLicense;
  license_gated_packs: string[];
  sku_entitlements: string[];
  records_can_disagree: boolean;
  rules: EntitlementRule[];
};

export type EntitlementsClient = {
  getEntitlements(): Promise<EntitlementDisclosure>;
};

function asNumberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item !== "")
    : [];
}

export function parseEntitlementDisclosure(
  body: Record<string, unknown>,
): EntitlementDisclosure {
  const license =
    body.license && typeof body.license === "object"
      ? (body.license as Record<string, unknown>)
      : {};
  const rulesRaw = Array.isArray(body.rules) ? body.rules : [];
  const rules = rulesRaw
    .filter((r): r is Record<string, unknown> => !!r && typeof r === "object")
    .map((r) => ({
      key: typeof r.key === "string" ? r.key : "",
      group: typeof r.group === "string" ? r.group : "",
      text: typeof r.text === "string" ? r.text : "",
    }))
    .filter((r) => r.key !== "" && r.text !== "");
  return {
    schema_version:
      typeof body.schema_version === "string" ? body.schema_version : "1",
    license: {
      status: typeof license.status === "string" ? license.status : "other",
      period_end: asNumberOrNull(license.period_end),
      grace_until: asNumberOrNull(license.grace_until),
      // Fail closed: only an explicit true reads as allowed.
      paid_catalogue_entries_allowed:
        license.paid_catalogue_entries_allowed === true,
    },
    license_gated_packs: asStringList(body.license_gated_packs),
    sku_entitlements: asStringList(body.sku_entitlements),
    records_can_disagree: body.records_can_disagree !== false,
    rules,
  };
}

export function createEntitlementsHttpClient(): EntitlementsClient {
  return {
    async getEntitlements(): Promise<EntitlementDisclosure> {
      const response = await fetch("/api/v1/entitlements", {
        method: "GET",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      let body: Record<string, unknown> = {};
      try {
        body = (await response.json()) as Record<string, unknown>;
      } catch {
        body = {};
      }
      if (!response.ok) {
        const detail =
          typeof body.detail === "string"
            ? body.detail
            : "entitlements read refused";
        throw new Error(detail);
      }
      return parseEntitlementDisclosure(body);
    },
  };
}

const GROUPS: ReadonlyArray<{ id: string; title: string }> = [
  { id: "gated", title: "What the license gates" },
  { id: "continues", title: "What continues after a lapse" },
  { id: "never_gated", title: "Never gated" },
  { id: "records", title: "Entitlement records" },
];

const LICENSE_STATUS_LABELS: Readonly<Record<string, string>> = {
  missing: "none verified",
  active: "active",
  past_due: "past due",
  lapsed: "lapsed",
  dead: "not accepted",
  revoked: "revoked",
};

function formatUtc(seconds: number): string {
  const iso = new Date(seconds * 1000).toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

function listOrNone(items: string[], lead: string, none: string): string {
  return items.length > 0 ? `${lead}: ${items.join(", ")}.` : none;
}

export type EntitlementsCardProps = {
  client: EntitlementsClient;
  /** When false, skip the read (session still starting or failed). */
  enabled?: boolean;
};

export function EntitlementsCard({
  client,
  enabled = true,
}: EntitlementsCardProps): JSX.Element {
  const [state, setState] = useState<EntitlementDisclosure | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    void client
      .getEntitlements()
      .then((next) => {
        if (!cancelled) {
          setState(next);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState(null);
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client, enabled]);

  const license = state?.license;
  const knownGroups = new Set(GROUPS.map((g) => g.id));
  const groups = state
    ? [
        ...GROUPS,
        ...(state.rules.some((r) => !knownGroups.has(r.group))
          ? [{ id: "other", title: "Other" }]
          : []),
      ]
        .map((group) => ({
          ...group,
          rules: state.rules.filter((r) =>
            group.id === "other" ? !knownGroups.has(r.group) : r.group === group.id,
          ),
        }))
        .filter((group) => group.rules.length > 0)
    : [];

  return (
    <section
      className="kbot-settings__card kbot-entitlements"
      aria-labelledby="kbot-entitlements-title"
      data-testid="settings-entitlements"
    >
      <h3 className="kbot-settings__subhead" id="kbot-entitlements-title">
        What a license gates
      </h3>

      {!enabled ? (
        <p
          className="kbot-settings__status-line"
          data-testid="entitlements-loading"
          role="status"
        >
          Local session unavailable — entitlement status cannot be read right
          now.
        </p>
      ) : loading && state === null ? (
        <p
          className="kbot-settings__status-line"
          data-testid="entitlements-loading"
          role="status"
        >
          Reading entitlement status…
        </p>
      ) : null}

      {error !== null ? (
        <p
          className="kbot-settings__error"
          data-testid="entitlements-error"
          role="alert"
        >
          Entitlement status unavailable: {error}
        </p>
      ) : null}

      {state !== null && license !== undefined ? (
        <>
          <div className="kbot-entitlements__facts" data-testid="entitlements-state">
            <p data-testid="entitlements-license-status">
              {`License on this computer: ${
                LICENSE_STATUS_LABELS[license.status] ?? "unrecognized status"
              }.`}
            </p>
            <p data-testid="entitlements-paid-entries">
              {license.paid_catalogue_entries_allowed
                ? license.grace_until !== null
                  ? `New positions from paid catalogue packs: allowed through ${formatUtc(
                      license.grace_until,
                    )}.`
                  : "New positions from paid catalogue packs: allowed."
                : "New positions from paid catalogue packs: off."}
            </p>
            {license.grace_until !== null &&
            !license.paid_catalogue_entries_allowed ? (
              <p data-testid="entitlements-grace-until">
                {`Grace date: ${formatUtc(license.grace_until)}.`}
              </p>
            ) : null}
            <p data-testid="entitlements-gated-packs">
              {listOrNone(
                state.license_gated_packs,
                "Paid catalogue packs installed here",
                "No paid catalogue packs are installed here.",
              )}
            </p>
            <p data-testid="entitlements-sku">
              {listOrNone(
                state.sku_entitlements,
                "SKU entitlements recorded here",
                "No SKU entitlements are recorded here.",
              )}
            </p>
          </div>

          <div className="kbot-entitlements__groups" data-testid="entitlements-rules">
            {groups.map((group) => (
              <div
                key={group.id}
                className="kbot-entitlements__group"
                data-testid={`entitlements-group-${group.id}`}
              >
                <h4 className="kbot-entitlements__group-title">{group.title}</h4>
                <ul className="kbot-entitlements__list">
                  {group.rules.map((rule) => (
                    <li
                      key={rule.key}
                      data-testid={`entitlements-rule-${rule.key}`}
                    >
                      {rule.text}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </>
      ) : null}
    </section>
  );
}
