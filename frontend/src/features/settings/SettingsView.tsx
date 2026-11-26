/**
 * Dedicated Settings view — Signal workstation (KRELL-SIGNAL-20261010).
 *
 * Consolidates local configuration, exchange keys, paper/safety posture,
 * trust disclosures, opt-in telemetry, local session/reconnect/demo fixture,
 * and About into a spacious two-column destination (`?view=settings`).
 */
import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import type { VersionClient } from "../../api/version";
import { DisclosureSection } from "../../components/disclosure/DisclosureSection";
import type { ReconnectIssue } from "../../session";
import { AboutPanel } from "../../shell/AboutPanel";
import type { View } from "../../shell/Navigation";
import { ReconnectQr } from "../../shell/ReconnectQr";
import type { SettingsSection } from "../../shell/route";
import { DemoFixturePanel } from "../demo/DemoFixturePanel";
import { DiagnosticReport } from "../diagnostics/DiagnosticReport";
import { KeysOnboardingPanel } from "../keys/KeysOnboardingPanel";
import { createKeysHttpClient, type KeysClient } from "../keys/client";
import { LiveWizardPanel } from "../onboarding/LiveWizardPanel";
import {
  createTrustHttpClient,
  type CredentialSource,
  type LiveWizardHonesty,
  type TrustClient,
} from "../onboarding/client";
import { PaidDisclosuresPanel } from "../packs/PaidDisclosuresPanel";
import { PackBillingForms, type PackBillingClient } from "../packs/PackBillingForms";
import {
  createPacksHttpClient,
  type PacksClient,
  type TelemetryConsent,
} from "../packs/client";
import { ReferralDisclosurePanel } from "../referral/ReferralDisclosurePanel";
import {
  createReferralHttpClient,
  type ReferralClient,
} from "../referral/client";
import "./SettingsView.signal.css";

type SectionMeta = {
  id: SettingsSection;
  label: string;
  shortNote: string;
};

type LocalTrustState = {
  status: "idle" | "loading" | "ready" | "failed";
  view: LiveWizardHonesty | null;
};

type TelemetryReading = "checking" | "on" | "off" | "unavailable";

/** Venues with an Exchange keys form; always listed on the local-control card. */
const KEY_FORM_VENUES: ReadonlyArray<string> = ["kraken", "coinbase"];

const VENUE_LABELS: Readonly<Record<string, string>> = {
  kraken: "Kraken",
  coinbase: "Coinbase",
};

const CREDENTIAL_SOURCE_COPY: Readonly<Record<CredentialSource, string>> = {
  keychain: "OS keychain",
  env_override: "Environment variables (override the OS keychain)",
  keyfile_fallback: "OS keychain, then a plaintext key file",
};

const KEYS_DESTINATION_COPY =
  "Keys are sent only to the exchange, never to Krellbot.";

export const SETTINGS_NAV_ITEMS: ReadonlyArray<SectionMeta> = [
  {
    id: "keys",
    label: "Exchange keys",
    shortNote: "Local OS keyring",
  },
  {
    id: "safety",
    label: "Paper & safety",
    shortNote: "Paper-only posture",
  },
  {
    id: "trust",
    label: "Trust & billing",
    shortNote: "Disclosures, keys & refunds",
  },
  {
    id: "privacy",
    label: "Privacy & telemetry",
    shortNote: "Opt-in only",
  },
  {
    id: "computer",
    label: "This computer",
    shortNote: "Session & fixtures",
  },
  {
    id: "about",
    label: "About",
    shortNote: "Build & version",
  },
];

export type SettingsViewProps = {
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
  onNavigate: (view: View) => void;
  sessionPending?: boolean;
  sessionFailed?: boolean;
  onRecoverSession?: () => Promise<{ ok: boolean; error?: string }>;
  onIssueReconnect?: () => Promise<ReconnectIssue>;
  onRedeemReconnect?: (
    token: string,
  ) => Promise<{ ok: boolean; error?: string }>;
  keysClient?: KeysClient;
  trustClient?: TrustClient;
  referralClient?: ReferralClient;
  versionClient?: VersionClient;
  packsClient?: Pick<PacksClient, "getTelemetryConsent" | "setTelemetryConsent"> &
    Partial<Pick<PacksClient, "redeemActivation" | "requestRefund">>;
};

export function SettingsView({
  section,
  onSectionChange,
  onNavigate,
  sessionPending = false,
  sessionFailed = false,
  onRecoverSession,
  onIssueReconnect,
  onRedeemReconnect,
  keysClient,
  trustClient,
  referralClient,
  versionClient,
  packsClient,
}: SettingsViewProps): JSX.Element {
  const sessionReady = !sessionPending && !sessionFailed;

  const fallbackKeysRef = useRef<KeysClient | null>(null);
  if (fallbackKeysRef.current === null && keysClient === undefined) {
    fallbackKeysRef.current = createKeysHttpClient();
  }
  const keys = keysClient ?? fallbackKeysRef.current!;

  const fallbackTrustRef = useRef<TrustClient | null>(null);
  if (fallbackTrustRef.current === null && trustClient === undefined) {
    fallbackTrustRef.current = createTrustHttpClient();
  }
  const trust = trustClient ?? fallbackTrustRef.current!;

  const fallbackReferralRef = useRef<ReferralClient | null>(null);
  if (fallbackReferralRef.current === null && referralClient === undefined) {
    fallbackReferralRef.current = createReferralHttpClient();
  }
  const referral = referralClient ?? fallbackReferralRef.current!;

  const fallbackPacksRef = useRef<PacksClient | null>(null);
  if (
    fallbackPacksRef.current === null &&
    (packsClient === undefined || typeof packsClient.redeemActivation !== "function")
  ) {
    fallbackPacksRef.current = createPacksHttpClient();
  }
  const telemetryApi = packsClient ?? fallbackPacksRef.current!;
  const billingApi: PackBillingClient = {
    redeemActivation: (key: string) =>
      typeof packsClient?.redeemActivation === "function"
        ? packsClient.redeemActivation(key)
        : fallbackPacksRef.current!.redeemActivation(key),
    requestRefund:
      typeof packsClient?.requestRefund === "function"
        ? (sku: string) => packsClient.requestRefund!(sku)
        : packsClient === undefined ||
            typeof packsClient.redeemActivation !== "function"
          ? (sku: string) => fallbackPacksRef.current!.requestRefund(sku)
          : undefined,
  };

  // Telemetry consent: the Privacy toggle and the This computer card read it.
  const [telemetry, setTelemetry] = useState<TelemetryConsent | null>(null);
  const [telemetryLoading, setTelemetryLoading] = useState(false);
  const [telemetryBusy, setTelemetryBusy] = useState(false);
  const [telemetryError, setTelemetryError] = useState<string | null>(null);

  useEffect(() => {
    if ((section !== "privacy" && section !== "computer") || !sessionReady) {
      return;
    }
    let cancelled = false;
    setTelemetryLoading(true);
    setTelemetryError(null);
    void telemetryApi
      .getTelemetryConsent()
      .then((next) => {
        if (!cancelled) {
          setTelemetry(next);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setTelemetryError(
            error instanceof Error ? error.message : String(error),
          );
        }
      })
      .finally(() => {
        if (!cancelled) {
          setTelemetryLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [section, sessionReady, telemetryApi]);

  // This computer: where credentials and data live (GET /api/v1/trust).
  const [localTrust, setLocalTrust] = useState<LocalTrustState>({
    status: "idle",
    view: null,
  });

  useEffect(() => {
    if (section !== "computer" || !sessionReady) {
      return;
    }
    let cancelled = false;
    setLocalTrust({ status: "loading", view: null });
    void trust
      .getTrust()
      .then((view) => {
        if (!cancelled) {
          setLocalTrust({ status: "ready", view });
        }
      })
      .catch(() => {
        if (!cancelled) {
          setLocalTrust({ status: "failed", view: null });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [section, sessionReady, trust]);

  const handleToggleTelemetry = useCallback(
    async (enabled: boolean): Promise<void> => {
      if (!sessionReady || telemetryBusy) {
        return;
      }
      setTelemetryBusy(true);
      setTelemetryError(null);
      try {
        const next = await telemetryApi.setTelemetryConsent(enabled);
        setTelemetry(next);
      } catch (error) {
        setTelemetryError(
          error instanceof Error ? error.message : String(error),
        );
      } finally {
        setTelemetryBusy(false);
      }
    },
    [sessionReady, telemetryApi, telemetryBusy],
  );

  // Local session recovery and multi-browser reconnect controls.
  const [recoverError, setRecoverError] = useState<string | null>(null);
  const recoveryAttempt = useRef(0);
  const [reconnectOffer, setReconnectOffer] = useState<ReconnectIssue | null>(
    null,
  );
  const [reconnectBusy, setReconnectBusy] = useState(false);
  const [reconnectError, setReconnectError] = useState<string | null>(null);
  const [pasteToken, setPasteToken] = useState("");
  const [pasteError, setPasteError] = useState<string | null>(null);
  const [pasteBusy, setPasteBusy] = useState(false);

  async function recoverSession(): Promise<void> {
    const attempt = ++recoveryAttempt.current;
    setRecoverError(null);
    try {
      const result = await onRecoverSession?.();
      if (attempt === recoveryAttempt.current) {
        setRecoverError(result?.ok ? null : result?.error || "recovery refused");
      }
    } catch (error) {
      if (attempt === recoveryAttempt.current) {
        setRecoverError(error instanceof Error ? error.message : String(error));
      }
    }
  }

  async function issueReconnect(): Promise<void> {
    if (!onIssueReconnect || reconnectBusy) {
      return;
    }
    setReconnectBusy(true);
    setReconnectError(null);
    try {
      const offer = await onIssueReconnect();
      setReconnectOffer(offer);
    } catch (error) {
      setReconnectOffer(null);
      setReconnectError(error instanceof Error ? error.message : String(error));
    } finally {
      setReconnectBusy(false);
    }
  }

  async function redeemPasted(): Promise<void> {
    if (!onRedeemReconnect || pasteBusy) {
      return;
    }
    const token = pasteToken.trim();
    if (!token) {
      setPasteError("Enter the reconnect token from the other browser");
      return;
    }
    setPasteBusy(true);
    setPasteError(null);
    try {
      const result = await onRedeemReconnect(token);
      if (!result.ok) {
        setPasteError(result.error || "reconnect refused");
      }
    } catch (error) {
      setPasteError(error instanceof Error ? error.message : String(error));
    } finally {
      setPasteBusy(false);
    }
  }

  const sessionLabel = sessionFailed
    ? "Local session failed"
    : sessionPending
      ? "Starting the local session"
      : "Local session ready";

  function renderSectionBody(activeSection: SettingsSection): JSX.Element {
    switch (activeSection) {
      case "keys":
        return (
          <section
            className="kbot-settings__panel"
            aria-labelledby="kbot-settings-keys-heading"
            data-testid="settings-section-keys"
          >
            <header className="kbot-settings__panel-head">
              <h2
                className="kbot-settings__panel-title"
                id="kbot-settings-keys-heading"
              >
                Exchange keys
              </h2>
              <p className="kbot-settings__panel-note">
                Paper trading needs no exchange key. If you add one, Krellbot checks its permissions with the exchange and stores it in your OS keychain. Storing a key does not enable live trading.
              </p>
            </header>
            <div className="kbot-settings__keys-grid">
              <KeysOnboardingPanel
                client={keys}
                venue="kraken"
                enabled={sessionReady}
              />
              <KeysOnboardingPanel
                client={keys}
                venue="coinbase"
                enabled={sessionReady}
              />
            </div>
          </section>
        );

      case "safety":
        return (
          <section
            className="kbot-settings__panel"
            aria-labelledby="kbot-settings-safety-heading"
            data-testid="settings-section-safety"
          >
            <header className="kbot-settings__panel-head">
              <h2
                className="kbot-settings__panel-title"
                id="kbot-settings-safety-heading"
              >
                Paper &amp; safety
              </h2>
              <p className="kbot-settings__panel-note">
                This release is paper-only. Strategies simulate on your computer
                with pretend funds and never submit live orders.
              </p>
            </header>
            <div className="kbot-settings__card">
              <p className="kbot-settings__card-copy">
                Manage running paper simulations in Run, or inspect live
                preflight requirements in Deploy.
              </p>
              <div className="kbot-settings__inline-actions">
                <button
                  type="button"
                  className="kbot-settings__link-btn"
                  data-testid="settings-link-run"
                  onClick={() => onNavigate("workstation")}
                >
                  Open Run →
                </button>
                <button
                  type="button"
                  className="kbot-settings__link-btn"
                  data-testid="settings-link-deploy"
                  onClick={() => onNavigate("deployments")}
                >
                  Open Deploy preflight →
                </button>
              </div>
            </div>
            <LiveWizardPanel
              trustClient={trust}
              keysClient={keys}
              enabled={sessionReady}
            />
          </section>
        );

      case "trust":
        return (
          <section
            className="kbot-settings__panel"
            aria-labelledby="kbot-settings-trust-heading"
            data-testid="settings-section-trust"
          >
            <header className="kbot-settings__panel-head">
              <h2
                className="kbot-settings__panel-title"
                id="kbot-settings-trust-heading"
              >
                Trust &amp; billing
              </h2>
              <p className="kbot-settings__panel-note">
                Honest disclosures, activation keys, refunds, and exchange
                referral attribution. The engine is free and open-source; packs
                are optional.
              </p>
            </header>
            <PaidDisclosuresPanel variant="settings" />
            <PackBillingForms client={billingApi} />
            <ReferralDisclosurePanel
              client={referral}
              enabled={sessionReady}
            />
          </section>
        );

      case "privacy":
        return (
          <section
            className="kbot-settings__panel"
            aria-labelledby="kbot-settings-privacy-heading"
            data-testid="settings-section-privacy"
          >
            <header className="kbot-settings__panel-head">
              <h2
                className="kbot-settings__panel-title"
                id="kbot-settings-privacy-heading"
              >
                Privacy &amp; telemetry
              </h2>
              <p className="kbot-settings__panel-note">
                Telemetry is opt-in and off by default. Exchange keys, order
                journals, and balances are never transmitted.
              </p>
            </header>
            <div className="kbot-settings__card">
              <label className="kbot-settings__toggle-row">
                <input
                  type="checkbox"
                  data-testid="settings-telemetry-opt-in"
                  checked={telemetry?.enabled ?? false}
                  disabled={!sessionReady || telemetryLoading || telemetryBusy}
                  onChange={(event) => {
                    void handleToggleTelemetry(event.target.checked);
                  }}
                />
                <span className="kbot-settings__toggle-copy">
                  <span className="kbot-settings__toggle-title">
                    Share anonymous opt-in usage telemetry
                  </span>
                  <span
                    className="kbot-settings__toggle-desc"
                    data-testid="settings-telemetry-disclosure"
                  >
                    {telemetry?.disclosure ??
                      "Telemetry is opt-in and off by default."}
                  </span>
                </span>
              </label>
              <p
                className="kbot-settings__status-line"
                data-testid="settings-telemetry-status"
                role="status"
              >
                {!sessionReady
                  ? "Local session unavailable — consent cannot be changed right now."
                  : telemetryLoading
                    ? "Loading telemetry preference…"
                    : telemetryBusy
                      ? "Saving telemetry preference…"
                      : telemetry?.enabled
                        ? "Status: Opted in."
                        : "Status: Off (default)."}
              </p>
              {telemetryError !== null ? (
                <p
                  className="kbot-settings__error"
                  data-testid="settings-telemetry-error"
                  role="alert"
                >
                  {telemetryError}
                </p>
              ) : null}
            </div>
          </section>
        );

      case "computer":
        return (
          <section
            className="kbot-settings__panel"
            aria-labelledby="kbot-settings-computer-heading"
            data-testid="settings-section-computer"
          >
            <header className="kbot-settings__panel-head">
              <h2
                className="kbot-settings__panel-title"
                id="kbot-settings-computer-heading"
              >
                This computer
              </h2>
              <p className="kbot-settings__panel-note">
                Where your keys and data live, loopback session controls,
                second-browser reconnect, and opt-in local demo fixtures.
              </p>
            </header>

            <div className="kbot-settings__card">
              <DiagnosticReport />
            </div>

            <div
              className="kbot-settings__card"
              data-testid="settings-local-session"
            >
              <p
                className="kbot-settings__session-lead"
                data-testid="settings-session-status"
              >
                <span
                  className={
                    sessionFailed
                      ? "kbot-settings__dot kbot-settings__dot--caution"
                      : "kbot-settings__dot"
                  }
                  aria-hidden="true"
                />
                {sessionLabel}
              </p>
              <p className="kbot-settings__card-copy">
                The workstation binds to localhost on this machine only.
              </p>

              {sessionFailed ? (
                <div
                  className="kbot-settings__session-failure"
                  data-testid="settings-session-failure"
                  role="alert"
                >
                  <p className="kbot-settings__card-copy">
                    The local session could not start. Recover the existing
                    session or paste a reconnect token from an active browser on
                    this computer.
                  </p>
                  {onRecoverSession ? (
                    <button
                      type="button"
                      className="kbot-settings__btn"
                      data-testid="settings-recover-session"
                      onClick={() => {
                        void recoverSession();
                      }}
                    >
                      Recover the local session
                    </button>
                  ) : null}
                  {recoverError !== null ? (
                    <p
                      className="kbot-settings__error"
                      data-testid="settings-recover-error"
                      role="alert"
                    >
                      Recovery failed: {recoverError}
                    </p>
                  ) : null}
                  {onRedeemReconnect ? (
                    <div
                      className="kbot-settings__reconnect-paste"
                      data-testid="settings-reconnect-paste"
                    >
                      <label
                        className="kbot-settings__field-label"
                        htmlFor="kbot-settings-reconnect-token"
                      >
                        Reconnect token
                      </label>
                      <input
                        id="kbot-settings-reconnect-token"
                        className="kbot-settings__input"
                        data-testid="settings-reconnect-token-input"
                        type="text"
                        autoComplete="off"
                        spellCheck={false}
                        value={pasteToken}
                        disabled={pasteBusy}
                        onChange={(event) => {
                          setPasteToken(event.target.value);
                        }}
                      />
                      <button
                        type="button"
                        className="kbot-settings__btn"
                        data-testid="settings-reconnect-redeem"
                        disabled={pasteBusy}
                        onClick={() => {
                          void redeemPasted();
                        }}
                      >
                        Join with reconnect token
                      </button>
                      {pasteError !== null ? (
                        <p
                          className="kbot-settings__error"
                          data-testid="settings-reconnect-paste-error"
                          role="alert"
                        >
                          {pasteError}
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : null}

              {sessionReady && onIssueReconnect ? (
                <div
                  className="kbot-settings__reconnect"
                  data-testid="settings-reconnect"
                >
                  <h3 className="kbot-settings__subhead">
                    Reconnect another browser
                  </h3>
                  <p className="kbot-settings__card-copy">
                    Mint a short-lived token or QR code for a second browser on
                    this machine. The one-time bootstrap token stays spent.
                  </p>
                  <button
                    type="button"
                    className="kbot-settings__btn"
                    data-testid="settings-reconnect-issue"
                    disabled={reconnectBusy}
                    onClick={() => {
                      void issueReconnect();
                    }}
                  >
                    {reconnectBusy
                      ? "Creating reconnect code…"
                      : "Show reconnect code →"}
                  </button>
                  {reconnectError !== null ? (
                    <p
                      className="kbot-settings__error"
                      data-testid="settings-reconnect-error"
                      role="alert"
                    >
                      {reconnectError}
                    </p>
                  ) : null}
                  {reconnectOffer !== null ? (
                    <div
                      className="kbot-settings__reconnect-offer"
                      data-testid="settings-reconnect-offer"
                    >
                      <ReconnectQr
                        payload={reconnectOffer.token}
                        label="Reconnect token QR code"
                      />
                      <p className="kbot-settings__field-label">Token</p>
                      <code
                        className="kbot-settings__code"
                        data-testid="settings-reconnect-token"
                      >
                        {reconnectOffer.token}
                      </code>
                      <p className="kbot-settings__card-copy">
                        Open in the other browser, or paste the token there:
                      </p>
                      <code
                        className="kbot-settings__code"
                        data-testid="settings-reconnect-url"
                      >
                        {reconnectOffer.qrPayload}
                      </code>
                      <p className="kbot-settings__card-copy">
                        Expires in {reconnectOffer.expiresIn}s.
                      </p>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>

            <LocalControlCard
              trust={localTrust}
              telemetry={
                !sessionReady || telemetryError !== null
                  ? "unavailable"
                  : telemetryLoading || telemetry === null
                    ? "checking"
                    : telemetry.enabled
                      ? "on"
                      : "off"
              }
            />

            <DisclosureSection
              title="Demo fixture seed"
              summary="Opt-in labeled fixture/demo rows for local preview — never live P&L or soak proof."
              badge="Fixture"
              badgeTone="advanced"
              defaultOpen={false}
              storageKey="krellbot.disclosure.settings.demo-fixture"
              data-testid="settings-demo-fixture"
            >
              <DemoFixturePanel enabled={sessionReady} />
            </DisclosureSection>
          </section>
        );

      case "about":
        return (
          <section
            className="kbot-settings__panel"
            aria-labelledby="kbot-settings-about-heading"
            data-testid="settings-section-about"
          >
            <header className="kbot-settings__panel-head">
              <h2
                className="kbot-settings__panel-title"
                id="kbot-settings-about-heading"
              >
                About this build
              </h2>
              <p className="kbot-settings__panel-note">
                Version, signing posture honesty, and how to reproduce a starter
                pack locally.
              </p>
            </header>
            <div className="kbot-settings__card">
              <AboutPanel client={versionClient} enabled={!sessionPending} />
            </div>
          </section>
        );

      default: {
        const exhaustive: never = activeSection;
        return exhaustive;
      }
    }
  }

  return (
    <div className="kbot-settings" data-testid="settings-view">
      <div className="kbot-shell__pagehead">
        <div className="kbot-shell__pagehead-copy">
          <h1 className="kbot-shell__heading">Settings</h1>
          <p className="kbot-shell__pagesub">
            Exchange keys, paper-only posture, disclosures, privacy, and local
            session controls — stored on this computer only.
          </p>
        </div>
      </div>

      <div className="kbot-settings__layout">
        <nav
          className="kbot-settings__nav"
          aria-label="Settings sections"
          data-testid="settings-section-nav"
        >
          {SETTINGS_NAV_ITEMS.map((item) => {
            const isCurrent = section === item.id;
            return (
              <button
                key={item.id}
                type="button"
                className={
                  "kbot-settings__nav-item" +
                  (isCurrent ? " kbot-settings__nav-item--active" : "")
                }
                data-testid={`settings-nav-${item.id}`}
                aria-current={isCurrent ? "true" : undefined}
                onClick={() => onSectionChange(item.id)}
              >
                <span className="kbot-settings__nav-label">{item.label}</span>
                <span className="kbot-settings__nav-note">{item.shortNote}</span>
              </button>
            );
          })}
        </nav>

        <div
          className="kbot-settings__content"
          data-testid="settings-content"
          data-settings-section={section}
        >
          {renderSectionBody(section)}
        </div>
      </div>
    </div>
  );
}

function isLoopbackAddress(address: string): boolean {
  return (
    address.startsWith("127.") ||
    address === "::1" ||
    address.startsWith("::ffff:127.")
  );
}

function venueLabel(venue: string): string {
  return VENUE_LABELS[venue] ?? venue;
}

function overrideWarnings(
  sources: Readonly<Record<string, CredentialSource>>,
): string[] {
  const lines: string[] = [];
  for (const venue of Object.keys(sources).sort()) {
    const label = venueLabel(venue);
    const ven = venue.toUpperCase();
    if (sources[venue] === "env_override") {
      lines.push(
        `${label}: KRELLBOT_${ven}_KEY or KRELLBOT_${ven}_SECRET is set, so the OS keychain is not used for this venue.`,
      );
    } else if (sources[venue] === "keyfile_fallback") {
      lines.push(
        `${label}: KRELLBOT_${ven}_KEYFILE is set. When the OS keychain has no ${label} key, Krellbot reads one from that plaintext file.`,
      );
    }
  }
  return lines;
}

/**
 * Settings → This computer: where credentials and data live (M28.13).
 *
 * Every line is read from ``/api/v1/trust`` or the telemetry consent; a
 * missing reading says 'unavailable'. Only the source kind of each venue's
 * keys is shown — never a key, a secret or a key file path.
 */
function LocalControlCard({
  trust,
  telemetry,
}: {
  trust: LocalTrustState;
  telemetry: TelemetryReading;
}): JSX.Element {
  const view = trust.status === "ready" ? trust.view : null;
  const missing = trust.status === "loading" ? "checking…" : "unavailable";
  const snap = view?.trust ?? null;
  const sources = snap?.credential_source;

  const listedVenues = [
    ...KEY_FORM_VENUES,
    ...Object.keys(sources ?? {})
      .filter(
        (venue) =>
          !KEY_FORM_VENUES.includes(venue) && sources?.[venue] !== "keychain",
      )
      .sort(),
  ];
  const warnings = sources ? overrideWarnings(sources) : [];

  let homeMode = missing;
  if (snap) {
    if (snap.home_mode === null) {
      homeMode = "unavailable";
    } else if (snap.home_mode_ok) {
      homeMode = `${snap.home_mode} (owner only)`;
    } else {
      homeMode = `${snap.home_mode}. Other local users may be able to read this folder.`;
    }
  }

  let keychain = missing;
  if (snap) {
    if (snap.keychain_backend === null) {
      keychain = "unavailable";
    } else {
      keychain = snap.keychain_ok
        ? snap.keychain_backend
        : `${snap.keychain_backend} (not persistent)`;
    }
  }

  let bind = missing;
  let bindCaution = false;
  if (view) {
    const measured = view.bind_measured;
    if (typeof measured === "string" && measured) {
      bindCaution = !isLoopbackAddress(measured);
      bind = bindCaution
        ? `${measured} (measured on this connection). This is not a loopback address.`
        : `${measured} (measured on this connection)`;
    } else {
      bind = "unavailable";
    }
  }

  let remote = missing;
  if (view) {
    const access = view.remote_access;
    if (!access) {
      remote = "unavailable";
    } else if (access.mode === "off") {
      remote = "Off. No Tailscale access is configured.";
    } else {
      const where = access.origin ? ` at ${access.origin}` : "";
      remote = `On through Tailscale Serve${where}, for the configured owner login only. A remote session has the same controls as a local one.`;
    }
  }

  const telemetryCopy =
    telemetry === "on"
      ? "On (you opted in)"
      : telemetry === "off"
        ? "Off"
        : telemetry === "checking"
          ? "checking…"
          : "unavailable";

  const homeModeCaution =
    snap !== null && snap.home_mode !== null && !snap.home_mode_ok;
  const keychainCaution = snap !== null && !snap.keychain_ok;

  return (
    <div className="kbot-settings__card" data-testid="settings-local-control">
      <h3 className="kbot-settings__subhead">Where your keys and data live</h3>
      <p
        className="kbot-settings__card-copy"
        data-testid="settings-local-control-keys-destination"
      >
        {KEYS_DESTINATION_COPY}
      </p>

      <p
        className="kbot-settings__card-copy"
        data-testid="settings-local-control-home"
      >
        <strong>Data folder:</strong>{" "}
        {snap && snap.home ? (
          <code className="kbot-settings__inline-code">{snap.home}</code>
        ) : (
          missing
        )}
      </p>
      <p
        className="kbot-settings__card-copy"
        data-testid="settings-local-control-data"
      >
        The journal (journal/), the operations store (ops.sqlite), receipts and
        packs are kept in this folder on this computer.
      </p>
      <p
        className={
          homeModeCaution
            ? "kbot-settings__card-copy kbot-settings__caution"
            : "kbot-settings__card-copy"
        }
        data-testid="settings-local-control-home-mode"
      >
        <strong>Folder access:</strong> {homeMode}
      </p>
      <p
        className={
          keychainCaution
            ? "kbot-settings__card-copy kbot-settings__caution"
            : "kbot-settings__card-copy"
        }
        data-testid="settings-local-control-keychain"
      >
        <strong>Keychain:</strong> {keychain}
      </p>

      {listedVenues.map((venue) => {
        const kind = sources?.[venue];
        return (
          <p
            key={venue}
            className="kbot-settings__card-copy"
            data-testid={`settings-local-control-source-${venue}`}
          >
            <strong>{venueLabel(venue)} keys:</strong>{" "}
            {kind ? CREDENTIAL_SOURCE_COPY[kind] : missing}
          </p>
        );
      })}

      {warnings.length > 0 ? (
        <div
          className="kbot-settings__caution-box"
          data-testid="settings-local-control-override-warning"
        >
          {warnings.map((line) => (
            <p key={line} className="kbot-settings__card-copy">
              {line}
            </p>
          ))}
          <p className="kbot-settings__card-copy">
            Checked in this workstation&apos;s environment. The scheduled tick
            starts with its own environment, which may differ.
          </p>
        </div>
      ) : null}

      <p
        className="kbot-settings__card-copy"
        data-testid="settings-local-control-telemetry"
      >
        <strong>Telemetry:</strong> {telemetryCopy}
      </p>
      <p
        className={
          bindCaution
            ? "kbot-settings__card-copy kbot-settings__caution"
            : "kbot-settings__card-copy"
        }
        data-testid="settings-local-control-bind"
      >
        <strong>Workstation address:</strong> {bind}
      </p>
      <p
        className="kbot-settings__card-copy"
        data-testid="settings-local-control-remote"
      >
        <strong>Remote access:</strong> {remote}
      </p>
    </div>
  );
}
