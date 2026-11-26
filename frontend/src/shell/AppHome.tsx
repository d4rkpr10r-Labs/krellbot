import { useRef, useState, type JSX } from "react";
import { BrandMark } from "../components/brand/BrandMark";
import type { View } from "./Navigation";
import "./AppHome.signal.css";

/**
 * Application Home — Signal composition (KRELL-SIGNAL-20261010).
 *
 * A welcoming application overview that routes a new user to the areas
 * this build actually supports. It is informational navigation only:
 * it renders before authenticated data loads, claims no connection,
 * account, license or market state, and never gates on engine
 * telemetry. Operational views keep their own bootstrap gates.
 */

type HomeCard = {
  view: View;
  action: string;
  detail: string;
  testid: string;
};

const HOME_CARDS: ReadonlyArray<HomeCard> = [
  {
    view: "strategies",
    action: "Create or open a strategy",
    detail:
      "Start with an example or import your own rules. Save the draft, then validate it.",
    testid: "app-home-card-strategies",
  },
  {
    view: "research",
    action: "Explore research",
    detail:
      "Replay a strategy on your candle CSV and inspect the signal on each bar.",
    testid: "app-home-card-research",
  },
  {
    view: "studio",
    action: "Open Studio",
    detail:
      "See a saved strategy as a graph of indicators, entries and exits.",
    testid: "app-home-card-studio",
  },
  {
    view: "workstation",
    action: "Paper workstation",
    detail:
      "See which packs are armed, review paper fills, or try the synthetic rehearsal.",
    testid: "app-home-card-workstation",
  },
  {
    view: "operations",
    action: "Operations",
    detail:
      "Review alerts and the kill switch for the local workstation.",
    testid: "app-home-card-operations",
  },
];

type AppHomeProps = {
  onNavigate: (view: View) => void;
  /** True while the local session bootstrap has not settled yet. */
  sessionPending?: boolean;
  /**
   * True when the one-time session bootstrap was rejected. Shown
   * honestly instead of presenting startup as complete.
   */
  sessionFailed?: boolean;
  /** Offered only on failure; re-reads the CSRF for the existing session. */
  onRecoverSession?: () => Promise<{ ok: boolean; error?: string }>;
};

export function AppHome({
  onNavigate,
  sessionPending = false,
  sessionFailed = false,
  onRecoverSession,
}: AppHomeProps): JSX.Element {
  const [recoverError, setRecoverError] = useState<string | null>(null);
  const recoveryAttempt = useRef(0);

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

  return (
    <section
      className="app-home"
      data-testid="app-home"
      aria-labelledby="app-home-title"
    >
      <div className="app-home__welcome">
        <BrandMark />
        <div>
          <h1 className="app-home__title" id="app-home-title">
            Your crypto workstation, local by design
          </h1>
          <p className="app-home__purpose">
            Build, inspect and paper-test strategy packs on this machine.
            Start in Strategies to try a moving-average example.
          </p>
        </div>
        <p
          className="app-home__availability"
          data-testid="app-home-availability"
        >
          <span className="app-home__availability-mode">Local preview</span>
          <span aria-hidden="true"> · </span>
          <span className="app-home__availability-live">
            Live trading unavailable
          </span>
        </p>
        {sessionPending && !sessionFailed ? (
          <p
            className="app-home__session"
            data-testid="app-home-session"
            role="status"
          >
            Starting the local session…
          </p>
        ) : null}
        {sessionFailed ? (
          <div
            className="app-home__session app-home__session--failed"
            data-testid="app-home-session-failure"
            role="alert"
          >
            <p className="app-home__note-copy">
              The local session could not start. Check that the workstation is
              running, then recover the session to load or save a strategy.
            </p>
            {onRecoverSession ? (
              <button
                type="button"
                className="app-home__recover"
                data-testid="app-home-recover-session"
                onClick={() => { void recoverSession(); }}
              >
                Recover the local session
              </button>
            ) : null}
            {recoverError !== null ? (
              <p data-testid="apphome-recover-error" role="alert">
                Recovery failed: {recoverError}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>

      <ul className="app-home__cards">
        {HOME_CARDS.map((card) => (
          <li key={card.view} className="app-home__card-item">
            <button
              type="button"
              className="app-home__card"
              data-testid={card.testid}
              onClick={() => {
                onNavigate(card.view);
              }}
            >
              <span className="app-home__card-action">{card.action}</span>
              <span className="app-home__card-detail">{card.detail}</span>
            </button>
          </li>
        ))}
      </ul>

      <div className="app-home__notes">
        <section
          className="app-home__note"
          aria-labelledby="app-home-works-title"
        >
          <h2 className="app-home__note-title" id="app-home-works-title">
            What works here
          </h2>
          <ul className="app-home__note-list">
            <li>Creating, editing and validating your own strategy packs.</li>
            <li>Reopening saved drafts and copying them as new drafts.</li>
            <li>Research runs over a dataset you provide.</li>
            <li>Graph editing for saved packs in Studio.</li>
            <li>Paper status, last runs and stop controls.</li>
          </ul>
        </section>
        <section
          className="app-home__note"
          aria-labelledby="app-home-unfinished-title"
        >
          <h2 className="app-home__note-title" id="app-home-unfinished-title">
            Not connected yet
          </h2>
          <p className="app-home__note-copy">
            No exchange credentials are needed for paper trading. Live orders
            are unavailable in this release.
          </p>
          <p className="app-home__note-copy">
            Keep the workstation running while you save drafts, run backtests,
            or check paper status.
          </p>
        </section>
      </div>
    </section>
  );
}
