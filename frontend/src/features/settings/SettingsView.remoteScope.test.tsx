/**
 * Settings → This computer: what a Tailscale session may do (D14, KRELL-SIGNAL-20261010).
 *
 * The local-control card shows the stored tailnet scope from
 * ``/api/v1/trust`` (``remote_scope``). Read-only is the default. The scope
 * changes only from a local session, through a CSRF-carrying PUT; a
 * Tailscale session sees the scope with no control. Fake clients only.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { KeysClient } from "../keys/client";
import {
  createTrustHttpClient,
  parseLiveWizardHonesty,
  type LiveWizardHonesty,
  type RemoteScope,
  type TrustClient,
} from "../onboarding/client";
import type { PacksClient, TelemetryConsent } from "../packs/client";
import type { ReferralClient } from "../referral/client";
import type { VersionClient } from "../../api/version";
import { SettingsView } from "./SettingsView";

vi.mock("../../session", () => ({
  redeemBootstrap: (): Promise<void> => Promise.resolve(),
  recoverCsrf: (): Promise<boolean> => Promise.resolve(true),
  getCsrf: (): string => "CANARY-CSRF",
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

function trustBody(over: Record<string, unknown> = {}): Record<string, unknown> {
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
      credential_source: { kraken: "keychain", coinbase: "keychain" },
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
    remote_scope: "read_only",
    ...over,
  };
}

function view(over: Record<string, unknown> = {}): LiveWizardHonesty {
  return parseLiveWizardHonesty(trustBody(over));
}

const REMOTE = { mode: "tailscale_serve", origin: "https://box.example.ts.net" };

function consent(): TelemetryConsent {
  return {
    schema_version: "1",
    enabled: false,
    default_off: true,
    funnel_events: [],
    disclosure: "Telemetry is opt-in and off by default.",
  };
}

function telemetryClient(): Pick<
  PacksClient,
  "getTelemetryConsent" | "setTelemetryConsent"
> {
  return {
    getTelemetryConsent: vi.fn().mockResolvedValue(consent()),
    setTelemetryConsent: vi.fn(),
  };
}

function renderComputer(trust: TrustClient) {
  return render(
    <SettingsView
      section="computer"
      onSectionChange={() => {}}
      onNavigate={() => {}}
      keysClient={
        { getStatus: vi.fn(), probeAndStore: vi.fn(), remove: vi.fn() } as KeysClient
      }
      trustClient={trust}
      referralClient={
        { getStatus: vi.fn(), attribute: vi.fn() } as unknown as ReferralClient
      }
      versionClient={{ getVersion: vi.fn() } as unknown as VersionClient}
      packsClient={telemetryClient()}
    />,
  );
}

function text(testId: string): string {
  return screen.getByTestId(testId).textContent ?? "";
}

describe("SettingsView — Tailscale session scope", () => {
  it("says a Tailscale session is read-only by default and offers no control there", async () => {
    const trust: TrustClient = {
      getTrust: vi.fn().mockResolvedValue(view({ remote_access: REMOTE })),
      setRemoteScope: vi.fn(),
    };
    renderComputer(trust);

    await waitFor(() => {
      expect(text("settings-local-control-remote-scope")).toMatch(/Read only/);
    });
    const remote = text("settings-local-control-remote");
    expect(remote).toContain("https://box.example.ts.net");
    expect(remote).toMatch(/can view but cannot change anything/);
    expect(remote).not.toMatch(/same controls as a local one/);
    expect(text("settings-local-control-remote-scope")).toMatch(
      /arming, the kill switch, pausing, live grants and settings changes are refused/i,
    );
    expect(text("settings-local-control-remote-scope-note")).toMatch(
      /only a local session on this computer can change this/i,
    );
    expect(
      screen.queryByTestId("settings-local-control-remote-scope-toggle"),
    ).toBeNull();
    expect(trust.setRemoteScope).not.toHaveBeenCalled();
  });

  it("says a full-control Tailscale session has the same controls as a local one", async () => {
    renderComputer({
      getTrust: vi
        .fn()
        .mockResolvedValue(view({ remote_access: REMOTE, remote_scope: "full" })),
      setRemoteScope: vi.fn(),
    });

    await waitFor(() => {
      expect(text("settings-local-control-remote-scope")).toMatch(/Full control/);
    });
    expect(text("settings-local-control-remote")).toMatch(
      /same controls as a local one/,
    );
    expect(
      screen.queryByTestId("settings-local-control-remote-scope-toggle"),
    ).toBeNull();
  });

  it("lets a local session switch the scope and shows the stored result", async () => {
    const setRemoteScope = vi
      .fn<(scope: RemoteScope) => Promise<RemoteScope>>()
      .mockResolvedValueOnce("full")
      .mockResolvedValueOnce("read_only");
    renderComputer({ getTrust: vi.fn().mockResolvedValue(view()), setRemoteScope });

    const toggle = await screen.findByTestId(
      "settings-local-control-remote-scope-toggle",
    );
    expect(text("settings-local-control-remote-scope")).toMatch(/Read only/);
    expect(toggle.textContent).toBe("Allow full control from Tailscale");
    expect(text("settings-local-control-remote")).toMatch(/Off/);

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(text("settings-local-control-remote-scope")).toMatch(/Full control/);
    });
    expect(setRemoteScope).toHaveBeenLastCalledWith("full");
    expect(text("settings-local-control-remote-scope-toggle")).toBe(
      "Make Tailscale sessions read-only",
    );

    fireEvent.click(screen.getByTestId("settings-local-control-remote-scope-toggle"));
    await waitFor(() => {
      expect(text("settings-local-control-remote-scope")).toMatch(/Read only/);
    });
    expect(setRemoteScope).toHaveBeenLastCalledWith("read_only");
  });

  it("keeps the stored scope and says so when the change is refused", async () => {
    renderComputer({
      getTrust: vi.fn().mockResolvedValue(view()),
      setRemoteScope: vi.fn().mockRejectedValue(new Error("csrf required")),
    });

    fireEvent.click(
      await screen.findByTestId("settings-local-control-remote-scope-toggle"),
    );
    await waitFor(() => {
      expect(text("settings-local-control-remote-scope-error")).toMatch(
        /csrf required/,
      );
    });
    expect(text("settings-local-control-remote-scope")).toMatch(/Read only/);
  });

  it("reads 'unavailable' when the server does not report a scope", async () => {
    const older = view();
    delete older.remote_scope;
    renderComputer({ getTrust: vi.fn().mockResolvedValue(older), setRemoteScope: vi.fn() });

    await waitFor(() => {
      expect(text("settings-local-control-remote-scope")).toMatch(/unavailable/);
    });
    expect(
      screen.queryByTestId("settings-local-control-remote-scope-toggle"),
    ).toBeNull();
  });

  it("parses remote_scope and PUTs a change with the session CSRF", async () => {
    expect(view().remote_scope).toBe("read_only");
    expect(view({ remote_scope: "full" }).remote_scope).toBe("full");
    expect(view({ remote_scope: "FULL" }).remote_scope).toBeNull();
    expect(view({ remote_scope: undefined }).remote_scope).toBeNull();

    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, code: "remote_scope_updated", remote_scope: "full" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = createTrustHttpClient();
    await expect(client.setRemoteScope!("full")).resolves.toBe("full");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/remote-access/scope");
    expect(init.method).toBe("PUT");
    expect(init.credentials).toBe("include");
    expect((init.headers as Record<string, string>)["X-Krellbot-CSRF"]).toBe(
      "CANARY-CSRF",
    );
    expect(JSON.parse(String(init.body))).toEqual({ scope: "full" });

    fetchMock.mockResolvedValueOnce({
      ok: false,
      json: async () => ({ code: "remote_read_only", detail: "Tailscale sessions are read-only." }),
    });
    await expect(client.setRemoteScope!("full")).rejects.toThrow(/read-only/);
  });
});
