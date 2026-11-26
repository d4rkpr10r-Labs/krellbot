/**
 * Settings → Trust & billing states what entitlements gate and what continues
 * on lapse (M28.14, decision D10). Contract: KRELL-SIGNAL-20261010.
 *
 * The rule copy comes from `GET /api/v1/entitlements`, whose every key the
 * backend test checks against the tick gate. This suite pins how the card
 * renders it: the gated/continues/never-gated/records groups, the license
 * state read from this computer, an honest unavailable state, and no price.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { VersionClient } from "../../api/version";
import type { KeysClient } from "../keys/client";
import type { TrustClient } from "../onboarding/client";
import type { PacksClient } from "../packs/client";
import type { ReferralClient } from "../referral/client";
import type {
  EntitlementDisclosure,
  EntitlementsClient,
} from "./EntitlementsCard";
import { SettingsView } from "./SettingsView";

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

const RULES: EntitlementDisclosure["rules"] = [
  {
    key: "paid_catalogue_entries_need_license",
    group: "gated",
    text: "In the tick, the license decides one thing: whether packs installed from the paid catalogue (packs/catalog/) may open new positions.",
  },
  {
    key: "grace_keeps_entries",
    group: "gated",
    text: "While the license is active or past due, those packs keep opening positions until the grace date last verified on this computer. The tick does not go online to check.",
  },
  {
    key: "after_grace_entries_stop",
    group: "gated",
    text: "After that grace date, or if the license has any other status or was never verified on this computer, paid catalogue packs open no new positions.",
  },
  {
    key: "exits_and_stops_continue",
    group: "continues",
    text: "Exits and protective stops keep running for every pack, paid catalogue packs included.",
  },
  {
    key: "nothing_force_closed",
    group: "continues",
    text: "A lapse never closes a position. Positions are sold only by the pack's exit rule or its stop protection, as before the lapse.",
  },
  {
    key: "export_continues",
    group: "continues",
    text: "Export does not read the license. Home export and the journal keep working after a lapse.",
  },
  {
    key: "diagnostics_continue",
    group: "continues",
    text: "Diagnostics do not read the license. The diagnostic report keeps working after a lapse.",
  },
  {
    key: "own_and_community_packs_never_gated",
    group: "never_gated",
    text: "Packs you write and community packs never need a license. A lapse does not change how they run.",
  },
  {
    key: "paid_install_needs_sku_entitlement",
    group: "records",
    text: "Installing a paid catalogue entry from Packs needs its purchase recorded on this computer as a SKU entitlement. Checkout records it; a refund removes it.",
  },
  {
    key: "records_can_disagree",
    group: "records",
    text: "The license status (new entries) and the SKU entitlements (paid installs) are separate records on this computer and can disagree today. A refund removes the SKU entitlement but does not change the license status.",
  },
];

// 2027-01-15T08:00:00Z
const GRACE_UNTIL = 1_800_000_000;

function disclosure(
  license: Partial<EntitlementDisclosure["license"]>,
  extra: Partial<EntitlementDisclosure> = {},
): EntitlementDisclosure {
  return {
    schema_version: "1",
    license: {
      status: "missing",
      period_end: null,
      grace_until: null,
      paid_catalogue_entries_allowed: false,
      ...license,
    },
    license_gated_packs: [],
    sku_entitlements: [],
    records_can_disagree: true,
    rules: RULES,
    ...extra,
  };
}

function fakeEntitlements(
  body: EntitlementDisclosure | Error,
): EntitlementsClient & { getEntitlements: ReturnType<typeof vi.fn> } {
  return {
    getEntitlements:
      body instanceof Error
        ? vi.fn().mockRejectedValue(body)
        : vi.fn().mockResolvedValue(body),
  };
}

function quietClients() {
  const keys: KeysClient = {
    getStatus: vi.fn().mockResolvedValue({
      schema_version: "1",
      venue: "kraken",
      present: false,
      corrupt: false,
      message: "no key stored",
      backend_label: null,
      balances: null,
      balances_available: false,
    }),
    probeAndStore: vi.fn(),
    remove: vi.fn(),
  };
  const trust: TrustClient = { getTrust: vi.fn().mockRejectedValue(new Error("unused")) };
  const referral: ReferralClient = {
    getStatus: vi.fn().mockResolvedValue({
      schema_version: "1",
      headline: "Exchange fee share / referral",
      body: "Exchange referral disclosure.",
      packs_body: "Packs referral disclosure.",
      earn_rule: "Disclosed attribution.",
      source: "local",
      receipt_count: 0,
      receipts: [],
    }),
    attribute: vi.fn(),
  };
  const version: VersionClient = {
    getVersion: vi.fn().mockResolvedValue({
      schema_version: "1",
      version: "0.9.7",
      channel: "prerelease",
      artifacts_signed: false,
    }),
  };
  const packs: Pick<PacksClient, "getTelemetryConsent" | "setTelemetryConsent"> = {
    getTelemetryConsent: vi.fn(),
    setTelemetryConsent: vi.fn(),
  };
  return { keys, trust, referral, version, packs };
}

function renderTrust(
  entitlements: EntitlementsClient,
  opts: { sessionPending?: boolean } = {},
) {
  const c = quietClients();
  return render(
    <SettingsView
      section="trust"
      onSectionChange={() => {}}
      onNavigate={() => {}}
      sessionPending={opts.sessionPending}
      keysClient={c.keys}
      trustClient={c.trust}
      referralClient={c.referral}
      versionClient={c.version}
      packsClient={c.packs}
      entitlementsClient={entitlements}
    />,
  );
}

const PRICE_OR_TIER =
  /[$€£¥]|\bUSD\b|per month|\/month|a month|monthly|one-time|yearly|annual|price|\btier/i;

describe("Settings → Trust & billing — what entitlements gate", () => {
  it("states the lapse rules in their groups, and that own and community packs are never gated", async () => {
    const client = fakeEntitlements(
      disclosure(
        {
          status: "past_due",
          period_end: GRACE_UNTIL - 86_400,
          grace_until: GRACE_UNTIL,
          paid_catalogue_entries_allowed: false,
        },
        { license_gated_packs: ["paid-sma"], sku_entitlements: ["paid-sma"] },
      ),
    );
    renderTrust(client);

    const card = await screen.findByTestId("settings-entitlements");
    await waitFor(() => {
      expect(within(card).getByTestId("entitlements-rules")).toBeTruthy();
    });
    expect(client.getEntitlements).toHaveBeenCalledTimes(1);

    const gated = within(card).getByTestId("entitlements-group-gated");
    expect(gated.textContent).toMatch(/What the license gates/);
    expect(gated.textContent).toContain(RULES[0].text);
    expect(gated.textContent).toContain(RULES[1].text);
    expect(gated.textContent).toContain(RULES[2].text);

    const continues = within(card).getByTestId("entitlements-group-continues");
    expect(continues.textContent).toMatch(/What continues after a lapse/);
    for (const key of [
      "exits_and_stops_continue",
      "nothing_force_closed",
      "export_continues",
      "diagnostics_continue",
    ]) {
      const rule = RULES.find((r) => r.key === key)!;
      expect(
        within(continues).getByTestId(`entitlements-rule-${key}`).textContent,
      ).toBe(rule.text);
    }

    const never = within(card).getByTestId("entitlements-group-never_gated");
    expect(never.textContent).toMatch(/Never gated/);
    expect(never.textContent).toMatch(/Packs you write and community packs never need a license/);

    const records = within(card).getByTestId("entitlements-group-records");
    expect(
      within(records).getByTestId("entitlements-rule-records_can_disagree").textContent,
    ).toMatch(/separate records on this computer and can disagree/);

    // The state read from this computer, without inventing anything.
    expect(within(card).getByTestId("entitlements-license-status").textContent).toBe(
      "License on this computer: past due.",
    );
    expect(within(card).getByTestId("entitlements-paid-entries").textContent).toBe(
      "New positions from paid catalogue packs: off.",
    );
    expect(within(card).getByTestId("entitlements-grace-until").textContent).toBe(
      "Grace date: 2027-01-15 08:00 UTC.",
    );
    expect(within(card).getByTestId("entitlements-gated-packs").textContent).toBe(
      "Paid catalogue packs installed here: paid-sma.",
    );
    expect(within(card).getByTestId("entitlements-sku").textContent).toBe(
      "SKU entitlements recorded here: paid-sma.",
    );

    // No price and no plan names anywhere in the card (M28.34 is undecided).
    expect(card.textContent ?? "").not.toMatch(PRICE_OR_TIER);
  });

  it("shows entries allowed through the grace date while the license is active", async () => {
    renderTrust(
      fakeEntitlements(
        disclosure({
          status: "active",
          period_end: GRACE_UNTIL - 86_400,
          grace_until: GRACE_UNTIL,
          paid_catalogue_entries_allowed: true,
        }),
      ),
    );
    const card = await screen.findByTestId("settings-entitlements");
    await waitFor(() => {
      expect(within(card).getByTestId("entitlements-paid-entries").textContent).toBe(
        "New positions from paid catalogue packs: allowed through 2027-01-15 08:00 UTC.",
      );
    });
    expect(within(card).getByTestId("entitlements-license-status").textContent).toBe(
      "License on this computer: active.",
    );
    expect(within(card).getByTestId("entitlements-gated-packs").textContent).toBe(
      "No paid catalogue packs are installed here.",
    );
    expect(within(card).getByTestId("entitlements-sku").textContent).toBe(
      "No SKU entitlements are recorded here.",
    );
  });

  it("says no license is verified when the cache is missing", async () => {
    renderTrust(fakeEntitlements(disclosure({ status: "missing" })));
    const card = await screen.findByTestId("settings-entitlements");
    await waitFor(() => {
      expect(within(card).getByTestId("entitlements-license-status").textContent).toBe(
        "License on this computer: none verified.",
      );
    });
    expect(within(card).getByTestId("entitlements-paid-entries").textContent).toBe(
      "New positions from paid catalogue packs: off.",
    );
    expect(within(card).queryByTestId("entitlements-grace-until")).toBeNull();
  });

  it("shows an unavailable state and invents no license status when the read fails", async () => {
    renderTrust(fakeEntitlements(new Error("session required")));
    const card = await screen.findByTestId("settings-entitlements");
    const error = await within(card).findByTestId("entitlements-error");
    expect(error.getAttribute("role")).toBe("alert");
    expect(error.textContent).toBe(
      "Entitlement status unavailable: session required",
    );
    expect(within(card).queryByTestId("entitlements-license-status")).toBeNull();
    expect(within(card).queryByTestId("entitlements-rules")).toBeNull();
  });

  it("does not read entitlements while the local session is starting", async () => {
    const client = fakeEntitlements(disclosure({ status: "active" }));
    renderTrust(client, { sessionPending: true });
    const card = await screen.findByTestId("settings-entitlements");
    expect(within(card).getByTestId("entitlements-loading").textContent).toMatch(
      /Local session unavailable/,
    );
    expect(client.getEntitlements).not.toHaveBeenCalled();
  });
});
