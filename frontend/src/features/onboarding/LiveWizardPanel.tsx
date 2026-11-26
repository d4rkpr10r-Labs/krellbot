import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import { KeysOnboardingPanel } from "../keys/KeysOnboardingPanel";
import type { KeysClient } from "../keys/client";
import { DIAGNOSTIC_REPORT_HREF } from "../paper/paperHealth";
import type { LiveWizardHonesty, TrustClient } from "./client";
import "./LiveWizardPanel.signal.css";

type WizardStep = "welcome" | "security" | "exchange" | "live";

const STEPS: ReadonlyArray<{ id: WizardStep; label: string }> = [
  { id: "welcome", label: "Welcome" },
  { id: "security", label: "Security" },
  { id: "exchange", label: "Exchange" },
  { id: "live", label: "Live state" },
];

const PANEL_TITLE = "Exchange keys and trading permissions";
const HELPER =
  "Check key storage on this computer, test a Kraken key, or review an existing trading authorization.";

const WELCOME_TRUTHS: ReadonlyArray<string> = [
  "The engine is free and open-source.",
  "Exchange keys are stored locally in the OS keychain and are not sent to krellbot.dev.",
  "You can run a paper strategy without an exchange account or a paid pack.",
];

const KEY_PERM_REQUIREMENT =
  "Trade-only permission required, withdraw permission never granted. No exchange key is probed from the Security step.";

export type LiveWizardPanelProps = {
  trustClient: TrustClient;
  keysClient: KeysClient;
  enabled?: boolean;
};

function describeGrant(view: LiveWizardHonesty): string {
  if (!view.live_grant_active || view.live_grant.grants.length === 0) {
    return "No live grant on file.";
  }
  const grant = view.live_grant.grants[0];
  const until =
    view.live_grant.expires_at != null
      ? new Date(view.live_grant.expires_at * 1000).toISOString()
      : "unknown expiry";
  return `Grant on file: ${grant.venue} ${grant.pair} until ${until}. This records permission only. It cannot start a strategy or place an order.`;
}

export function LiveWizardPanel({
  trustClient,
  keysClient,
  enabled = true,
}: LiveWizardPanelProps): JSX.Element {
  const [step, setStep] = useState<WizardStep>("welcome");
  const [view, setView] = useState<LiveWizardHonesty | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef(0);

  const load = useCallback(async (): Promise<void> => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const next = await trustClient.getTrust();
      if (requestId !== requestRef.current) {
        return;
      }
      setView(next);
    } catch (err) {
      if (requestId !== requestRef.current) {
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (requestId === requestRef.current) {
        setLoading(false);
      }
    }
  }, [enabled, trustClient]);

  useEffect(() => {
    void load();
  }, [load]);

  const stepIndex = STEPS.findIndex((s) => s.id === step);
  const trust = view?.trust;

  return (
    <section
      className="kbot-live-wizard"
      aria-labelledby="live-wizard-title"
      data-testid="live-wizard"
    >
      <h3 className="kbot-live-wizard__title" id="live-wizard-title">
        {PANEL_TITLE}
      </h3>
      <p className="kbot-live-wizard__helper">{HELPER}</p>

      <ul className="kbot-live-wizard__steps" aria-label="Wizard steps">
        {STEPS.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              className="kbot-live-wizard__step-btn"
              data-testid={`live-wizard-step-${item.id}`}
              aria-current={step === item.id ? "step" : undefined}
              onClick={() => {
                setStep(item.id);
              }}
            >
              {item.label}
            </button>
          </li>
        ))}
      </ul>

      {step === "welcome" ? (
        <div
          className="kbot-live-wizard__panel"
          data-testid="live-wizard-welcome"
        >
          <h4 className="kbot-live-wizard__panel-title">Welcome</h4>
          <p className="kbot-live-wizard__panel-copy">
            Paper trading uses a simulated balance. Connecting an exchange key is optional.
          </p>
          <ul className="kbot-live-wizard__list">
            {WELCOME_TRUTHS.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <p
            className="kbot-live-wizard__panel-copy"
            data-testid="live-wizard-no-license"
          >
            No license token is collected when you check an exchange key.
          </p>
        </div>
      ) : null}

      {step === "security" ? (
        <div
          className="kbot-live-wizard__panel"
          data-testid="live-wizard-security"
        >
          <h4 className="kbot-live-wizard__panel-title">Local security checks</h4>
          <p className="kbot-live-wizard__panel-copy">
            Check whether the keychain can store keys and who can read the Krellbot data folder.
          </p>
          {loading ? (
            <p className="kbot-live-wizard__loading" data-testid="live-wizard-loading">
              Checking local storage…
            </p>
          ) : null}
          {error ? (
            <p className="kbot-live-wizard__error" data-testid="live-wizard-error">
              {error}
            </p>
          ) : null}
          {trust ? (
            <>
              <p
                className={
                  trust.posture_ok
                    ? "kbot-live-wizard__banner kbot-live-wizard__banner--ok"
                    : "kbot-live-wizard__banner"
                }
                data-testid="live-wizard-posture"
              >
                {trust.posture_ok
                  ? "A persistent keychain is available and the data folder is private to your user."
                  : `${trust.posture_warning ?? "Could not verify local key storage"}. Run krellbot doctor.`}
                {trust.posture_ok ? null : (
                  <>
                    {" "}
                    <a href={DIAGNOSTIC_REPORT_HREF} data-testid="live-wizard-diagnostic-report">
                      Diagnostic report
                    </a>
                  </>
                )}
              </p>
              <ul className="kbot-live-wizard__rows">
                <li className="kbot-live-wizard__row">
                  <span className="kbot-live-wizard__row-label">Keychain backend</span>
                  <span
                    className="kbot-live-wizard__row-value"
                    data-testid="live-wizard-keychain"
                  >
                    {trust.keychain_backend ?? "(none)"}
                    {trust.keychain_ok ? "" : " — not persistent"}
                  </span>
                </li>
                <li className="kbot-live-wizard__row">
                  <span className="kbot-live-wizard__row-label">Data folder</span>
                  <span
                    className="kbot-live-wizard__row-value"
                    data-testid="live-wizard-home"
                  >
                    {trust.home}
                  </span>
                </li>
                <li className="kbot-live-wizard__row">
                  <span className="kbot-live-wizard__row-label">Folder permissions</span>
                  <span
                    className="kbot-live-wizard__row-value"
                    data-testid="live-wizard-home-mode"
                  >
                    {trust.home_mode ?? "unknown / not readable"}
                    {trust.home_mode_ok ? "" : " (expected 0o700)"}
                  </span>
                </li>
                <li className="kbot-live-wizard__row">
                  <span className="kbot-live-wizard__row-label">Workstation address</span>
                  <span
                    className="kbot-live-wizard__row-value"
                    data-testid="live-wizard-bind"
                  >
                    {trust.bind}
                  </span>
                </li>
                <li className="kbot-live-wizard__row">
                  <span className="kbot-live-wizard__row-label">Live arm UI</span>
                  <span
                    className="kbot-live-wizard__row-value"
                    data-testid="live-wizard-arm-ui"
                  >
                    {trust.live_arm_ui_allowed ? "allowed" : "not allowed"}
                  </span>
                </li>
                <li className="kbot-live-wizard__row">
                  <span className="kbot-live-wizard__row-label">Key permissions</span>
                  <span
                    className="kbot-live-wizard__row-value"
                    data-testid="live-wizard-key-requirement"
                  >
                    {KEY_PERM_REQUIREMENT}
                  </span>
                </li>
              </ul>
            </>
          ) : null}
        </div>
      ) : null}

      {step === "exchange" ? (
        <div
          className="kbot-live-wizard__panel"
          data-testid="live-wizard-exchange"
        >
          <h4 className="kbot-live-wizard__panel-title">Exchange</h4>
          <p className="kbot-live-wizard__panel-copy">
            Krellbot sends an authenticated permission check to Kraken. It saves the key in your OS keychain only if the check passes. A saved key tells you the check passed when you added it.
          </p>
          <KeysOnboardingPanel
            client={keysClient}
            venue="kraken"
            enabled={enabled}
          />
        </div>
      ) : null}

      {step === "live" ? (
        <div className="kbot-live-wizard__panel" data-testid="live-wizard-live">
          <h4 className="kbot-live-wizard__panel-title">Live trading permissions</h4>
          <p className="kbot-live-wizard__panel-copy">
            A grant records permission for one exchange and pair. The paper-only release blocks live orders even when a key and a grant are both present.
          </p>
          {loading ? (
            <p className="kbot-live-wizard__loading">Reading live state…</p>
          ) : null}
          {error ? (
            <p className="kbot-live-wizard__error">{error}</p>
          ) : null}
          {view ? (
            <div className="kbot-live-wizard__state">
              <p
                className="kbot-live-wizard__state-chip"
                data-testid="live-wizard-paper-only"
              >
                <strong>Paper-only release:</strong>{" "}
                {view.paper_only_release
                  ? view.release_policy_message ||
                    "Live order send is disabled in this paper-only release."
                  : "This build permits live trading subject to its other checks."}
              </p>
              <p
                className="kbot-live-wizard__state-chip"
                data-testid="live-wizard-grant"
              >
                <strong>Live grant:</strong> {describeGrant(view)}
              </p>
              <p
                className="kbot-live-wizard__state-chip"
                data-testid="live-wizard-live-ready"
              >
                This screen cannot arm a strategy for live trading.
              </p>
              <p
                className="kbot-live-wizard__state-chip"
                data-testid="live-wizard-keys-local"
              >
                Keys sent to krellbot.dev:{" "}
                {view.keys_leave_machine ? "yes" : "no"}
              </p>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="kbot-live-wizard__nav">
        <button
          type="button"
          className="kbot-live-wizard__nav-btn"
          data-testid="live-wizard-back"
          disabled={stepIndex <= 0}
          onClick={() => {
            if (stepIndex > 0) {
              setStep(STEPS[stepIndex - 1].id);
            }
          }}
        >
          Back
        </button>
        <button
          type="button"
          className="kbot-live-wizard__nav-btn kbot-live-wizard__nav-btn--primary"
          data-testid="live-wizard-next"
          disabled={stepIndex >= STEPS.length - 1}
          onClick={() => {
            if (stepIndex < STEPS.length - 1) {
              setStep(STEPS[stepIndex + 1].id);
            }
          }}
        >
          Next
        </button>
      </div>
    </section>
  );
}
