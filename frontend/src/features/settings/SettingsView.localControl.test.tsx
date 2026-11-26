/**
 * Settings → This computer: where credentials and data live (KRELL-SIGNAL-20261010).
 *
 * Pins the local-control card: per-venue credential source from
 * ``/api/v1/trust`` (keychain, env override, key file fallback) with an
 * override warning, the data folder and its mode, the keychain backend,
 * telemetry, the measured bind, Tailscale remote access, and the statement
 * that keys go only to the exchange. Missing evidence reads 'unavailable'.
 * Fake clients only; no key values or key file paths ever reach this card.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { KeysClient } from "../keys/client";
import {
  createTrustHttpClient,
  type LiveWizardHonesty,
  type TrustClient,
} from "../onboarding/client";
import type { PacksClient, TelemetryConsent } from "../packs/client";
import type { ReferralClient } from "../referral/client";
import type { VersionClient } from "../../api/version";
import { SettingsView } from "./SettingsView";

vi.mock("../../session", () => ({
  redeemBootstrap: (): Promise<void> => Promise.resolve(),
  recoverCsrf: (): Promise<boolean> => Promise.resolve(true),
  getCsrf: (): string => "test-csrf",
  issueReconnectToken: (): Promise<never> =>
    Promise.reject(new Error("not stubbed")),
  redeemReconnectToken: (): Promise<void> => Promise.resolve(),
}));

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const ALL_KEYCHAIN = {
  binance: "keychain",
  coinbase: "keychain",
  kraken: "keychain",
  okx: "keychain",
} as const;

function trustView(over: Partial<LiveWizardHonesty> = {}): LiveWizardHonesty {
  return {
    schema_version: "1",
    trust: {
      home: "/home/me/.krellbot",
      home_mode: "0o700",
      home_mode_ok: true,
      keychain_backend: "macOS Keyring",
      keychain_ok: true,
      posture_ok: true,
      posture_warning: null,
      bind: "127.0.0.1",
      live_arm_ui_allowed: false,
      trade_only_required: true,
      credential_source: { ...ALL_KEYCHAIN },
    },
    paper_only_release: true,
    release_policy: "paper_only",
    release_policy_code: "live_disabled_paper_only_release",
    release_policy_message: "paper-only release",
    live_grant_active: false,
    live_grant: { active: false, expires_at: null, grants: [] },
    live_ready: false,
    live_arm_ui_allowed: false,
    trade_only_required: true,
    license_required_for_exchange: false,
    keys_leave_machine: false,
    bind_measured: "127.0.0.1",
    remote_access: { mode: "off", origin: null },
    ...over,
  };
}

function consent(enabled: boolean): TelemetryConsent {
  return {
    schema_version: "1",
    enabled,
    default_off: true,
    funnel_events: [],
    disclosure: "Telemetry is opt-in and off by default.",
  };
}

function telemetryClient(
  result: Promise<TelemetryConsent>,
): Pick<PacksClient, "getTelemetryConsent" | "setTelemetryConsent"> {
  return {
    getTelemetryConsent: vi.fn().mockReturnValue(result),
    setTelemetryConsent: vi.fn(),
  };
}

function keysClient(): KeysClient {
  return {
    getStatus: vi.fn(),
    probeAndStore: vi.fn(),
    remove: vi.fn(),
  };
}

function referralClient(): ReferralClient {
  return { getStatus: vi.fn(), attribute: vi.fn() } as unknown as ReferralClient;
}

function versionClient(): VersionClient {
  return { getVersion: vi.fn() } as unknown as VersionClient;
}

function renderComputer(
  trust: TrustClient,
  telemetry = telemetryClient(Promise.resolve(consent(false))),
  extra: { sessionPending?: boolean } = {},
) {
  return render(
    <SettingsView
      section="computer"
      onSectionChange={() => {}}
      onNavigate={() => {}}
      sessionPending={extra.sessionPending}
      keysClient={keysClient()}
      trustClient={trust}
      referralClient={referralClient()}
      versionClient={versionClient()}
      packsClient={telemetry}
    />,
  );
}

function text(testId: string): string {
  return screen.getByTestId(testId).textContent ?? "";
}

describe("SettingsView — where credentials and data live", () => {
  it("shows the data folder, keychain, sources, telemetry, bind and remote access", async () => {
    const trust: TrustClient = { getTrust: vi.fn().mockResolvedValue(trustView()) };
    renderComputer(trust);

    const card = await screen.findByTestId("settings-local-control");
    await waitFor(() => {
      expect(text("settings-local-control-home")).toContain("/home/me/.krellbot");
    });
    expect(text("settings-local-control-data")).toMatch(/journal/);
    expect(text("settings-local-control-data")).toMatch(/ops\.sqlite/);
    expect(text("settings-local-control-home-mode")).toContain("0o700");
    expect(text("settings-local-control-keychain")).toContain("macOS Keyring");
    expect(text("settings-local-control-source-kraken")).toMatch(/Kraken.*OS keychain/);
    expect(text("settings-local-control-source-coinbase")).toMatch(/Coinbase.*OS keychain/);
    // Venues without an override and without a Settings form stay out of the list.
    expect(screen.queryByTestId("settings-local-control-source-binance")).toBeNull();
    expect(screen.queryByTestId("settings-local-control-override-warning")).toBeNull();
    await waitFor(() => {
      expect(text("settings-local-control-telemetry")).toMatch(/^Telemetry:\s*Off/);
    });
    expect(text("settings-local-control-bind")).toContain("127.0.0.1");
    expect(text("settings-local-control-bind")).toMatch(/measured/i);
    expect(text("settings-local-control-remote")).toMatch(/Off/);
    expect(text("settings-local-control-keys-destination")).toBe(
      "Keys are sent only to the exchange, never to Krellbot.",
    );
    expect(card.textContent ?? "").not.toMatch(/never leave/i);
    expect(trust.getTrust).toHaveBeenCalledTimes(1);
  });

  it("warns when an environment override or a plaintext key file is in play", async () => {
    const view = trustView();
    view.trust.credential_source = {
      ...ALL_KEYCHAIN,
      kraken: "env_override",
      coinbase: "keyfile_fallback",
      binance: "env_override",
    };
    renderComputer({ getTrust: vi.fn().mockResolvedValue(view) });

    await waitFor(() => {
      expect(text("settings-local-control-source-kraken")).toMatch(
        /Environment variables/,
      );
    });
    expect(text("settings-local-control-source-coinbase")).toMatch(
      /OS keychain, then a plaintext key file/,
    );
    // An override for a venue without a Settings form is still disclosed.
    expect(text("settings-local-control-source-binance")).toMatch(
      /Environment variables/,
    );
    expect(screen.queryByTestId("settings-local-control-source-okx")).toBeNull();

    const warning = text("settings-local-control-override-warning");
    expect(warning).toContain("KRELLBOT_KRAKEN_KEY");
    expect(warning).toContain("KRELLBOT_KRAKEN_SECRET");
    expect(warning).toContain("KRELLBOT_BINANCE_KEY");
    expect(warning).toContain("KRELLBOT_COINBASE_KEYFILE");
    expect(warning).toMatch(/plaintext/);
    expect(warning).toMatch(/scheduled tick/);
  });

  it("discloses Tailscale remote access and a non-loopback measured bind", async () => {
    const view = trustView({
      bind_measured: "192.0.2.10",
      remote_access: {
        mode: "tailscale_serve",
        origin: "https://box.example.ts.net",
      },
    });
    renderComputer({ getTrust: vi.fn().mockResolvedValue(view) });

    await waitFor(() => {
      expect(text("settings-local-control-remote")).toContain(
        "https://box.example.ts.net",
      );
    });
    expect(text("settings-local-control-remote")).toMatch(/Tailscale Serve/);
    expect(text("settings-local-control-remote")).toMatch(
      /same controls as a local one/,
    );
    expect(text("settings-local-control-bind")).toContain("192.0.2.10");
    expect(text("settings-local-control-bind")).toMatch(/not a loopback address/);
  });

  it("names a permissive home mode and a non-persistent keychain", async () => {
    const view = trustView();
    view.trust = {
      ...view.trust,
      home_mode: "0o755",
      home_mode_ok: false,
      keychain_backend: "FakeKeyring",
      keychain_ok: false,
      posture_ok: false,
    };
    renderComputer({ getTrust: vi.fn().mockResolvedValue(view) });

    await waitFor(() => {
      expect(text("settings-local-control-home-mode")).toContain("0o755");
    });
    expect(text("settings-local-control-home-mode")).toMatch(
      /other local users/i,
    );
    expect(text("settings-local-control-keychain")).toMatch(
      /FakeKeyring.*not persistent/,
    );
  });

  it("reads 'unavailable' when the trust read fails or omits the new fields", async () => {
    const older = trustView({ bind_measured: undefined, remote_access: undefined });
    delete older.trust.credential_source;
    const first = renderComputer({ getTrust: vi.fn().mockResolvedValue(older) });
    await waitFor(() => {
      expect(text("settings-local-control-source-kraken")).toMatch(/unavailable/);
    });
    expect(text("settings-local-control-bind")).toMatch(/unavailable/);
    expect(text("settings-local-control-remote")).toMatch(/unavailable/);
    first.unmount();

    renderComputer(
      { getTrust: vi.fn().mockRejectedValue(new Error("trust read refused")) },
      telemetryClient(Promise.reject(new Error("telemetry consent failed: 500"))),
    );
    await waitFor(() => {
      expect(text("settings-local-control-home")).toMatch(/unavailable/);
    });
    expect(text("settings-local-control-source-kraken")).toMatch(/unavailable/);
    expect(text("settings-local-control-bind")).toMatch(/unavailable/);
    expect(text("settings-local-control-remote")).toMatch(/unavailable/);
    await waitFor(() => {
      expect(text("settings-local-control-telemetry")).toMatch(/unavailable/);
    });
    expect(text("settings-local-control-keys-destination")).toBe(
      "Keys are sent only to the exchange, never to Krellbot.",
    );
  });

  it("shows opted-in telemetry", async () => {
    renderComputer(
      { getTrust: vi.fn().mockResolvedValue(trustView()) },
      telemetryClient(Promise.resolve(consent(true))),
    );
    await waitFor(() => {
      expect(text("settings-local-control-telemetry")).toMatch(/On/);
    });
  });

  it("waits for the local session before reading trust", () => {
    const trust: TrustClient = { getTrust: vi.fn().mockResolvedValue(trustView()) };
    renderComputer(trust, undefined, { sessionPending: true });
    expect(trust.getTrust).not.toHaveBeenCalled();
    expect(text("settings-local-control-source-kraken")).toMatch(/unavailable/);
  });

  it("parses the /api/v1/trust body without exposing more than the source kind", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          schema_version: "1",
          trust: {
            home: "/home/me/.krellbot",
            home_mode: "0o700",
            home_mode_ok: true,
            keychain_backend: "SecretService Keyring",
            keychain_ok: true,
            posture_ok: true,
            posture_warning: null,
            bind: "127.0.0.1",
            live_arm_ui_allowed: false,
            trade_only_required: true,
            credential_source: {
              kraken: "env_override",
              coinbase: "keychain",
              okx: "not-a-kind",
            },
          },
          paper_only_release: true,
          release_policy: "paper_only",
          release_policy_code: "live_disabled_paper_only_release",
          release_policy_message: "paper-only release",
          live_grant_active: false,
          live_grant: { active: false, expires_at: null, grants: [] },
          live_ready: false,
          live_arm_ui_allowed: false,
          trade_only_required: true,
          license_required_for_exchange: false,
          keys_leave_machine: false,
          bind_measured: "127.0.0.1",
          remote_access: { mode: "tailscale_serve", origin: "https://box.example.ts.net" },
        }),
      }),
    );
    const client = createTrustHttpClient();
    const parsed = await client.getTrust();
    expect(parsed.trust.credential_source).toEqual({
      kraken: "env_override",
      coinbase: "keychain",
    });
    expect(parsed.bind_measured).toBe("127.0.0.1");
    expect(parsed.remote_access).toEqual({
      mode: "tailscale_serve",
      origin: "https://box.example.ts.net",
    });

    renderComputer(client);
    await waitFor(() => {
      expect(text("settings-local-control-source-kraken")).toMatch(
        /Environment variables/,
      );
    });
    expect(text("settings-local-control-remote")).toContain(
      "https://box.example.ts.net",
    );
  });
});
