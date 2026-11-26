/**
 * Classified feed failures show one recovery step (M28.5, F12).
 *
 * `/api/v1/paper/health` serves a closed `failure_code` on a feed error.
 * Each code maps to exactly one recovery action. Unknown or missing codes
 * fall back to the generic sentence and are never echoed. The live wizard
 * keeps 'Run krellbot doctor.' and gains an in-app Diagnostic report link.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { KeysClient } from "../keys/client";
import { LiveWizardPanel } from "../onboarding/LiveWizardPanel";
import type { LiveWizardHonesty, TrustClient } from "../onboarding/client";
import {
  DIAGNOSTIC_REPORT_HREF,
  FEED_FAILURE_CODES,
  PaperHealthDetails,
  paperHealthSentence,
  type PaperHealth,
} from "./paperHealth";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const GENERIC_FEED_SENTENCE = "Market data could not be fetched. Check your connection.";

const EXPECTED_STEP: Record<string, RegExp> = {
  timeout: /wait for the next automatic check/i,
  dns_or_network: /this computer is online/i,
  tls: /date and time/i,
  region_or_permission: /exchange that serves your region/i,
  rate_limited: /wait a few minutes/i,
  venue_error: /diagnostic report/i,
};

function feedError(failure_code: unknown): PaperHealth {
  return { armed: true, state: "feed_error", failure_code } as PaperHealth;
}

describe("feed failure codes", () => {
  it("serves the six closed codes the engine derives", () => {
    expect([...FEED_FAILURE_CODES]).toEqual([
      "timeout",
      "dns_or_network",
      "tls",
      "region_or_permission",
      "rate_limited",
      "venue_error",
    ]);
  });

  it("maps each code to exactly one recovery step", () => {
    const steps = new Set<string>();
    for (const code of FEED_FAILURE_CODES) {
      const { unmount } = render(<PaperHealthDetails health={feedError(code)} />);
      const recovery = screen.getByTestId("paper-feed-recovery");
      expect(within(recovery).getByTestId("paper-feed-failure-code").textContent).toBe(code);
      const step = within(recovery).getByTestId("paper-feed-recovery-step");
      expect(step.textContent).toMatch(EXPECTED_STEP[code]);
      expect(screen.getAllByTestId("paper-feed-recovery-step")).toHaveLength(1);
      steps.add(step.textContent ?? "");
      unmount();
    }
    expect(steps.size).toBe(FEED_FAILURE_CODES.length);
  });

  it("names the cause in the status sentence", () => {
    expect(paperHealthSentence(feedError("rate_limited"))).toMatch(/could not be fetched/i);
    expect(paperHealthSentence(feedError("rate_limited"))).toMatch(/limited requests/i);
    expect(paperHealthSentence(feedError("tls"))).toMatch(/secure connection/i);
    expect(paperHealthSentence(feedError("region_or_permission"))).toMatch(/refused/i);
  });

  it("links a venue error to the in-app diagnostic report", () => {
    render(<PaperHealthDetails health={feedError("venue_error")} />);
    const link = screen.getByRole("link", { name: "prepare a diagnostic report" });
    expect(link.getAttribute("href")).toBe(DIAGNOSTIC_REPORT_HREF);
    expect(screen.getByTestId("paper-feed-recovery-step").textContent).toBe(
      "If this repeats, prepare a diagnostic report.",
    );
    expect(DIAGNOSTIC_REPORT_HREF).toBe("/?view=settings&section=computer");
  });

  it.each([null, undefined, "TIMEOUT", "<b>timeout</b>", "Connection refused by 10.0.0.1", 429, ["timeout"]])(
    "keeps the generic sentence and shows no step for %j",
    (code) => {
      expect(paperHealthSentence(feedError(code))).toBe(GENERIC_FEED_SENTENCE);
      const { container } = render(<PaperHealthDetails health={feedError(code)} />);
      expect(screen.queryByTestId("paper-feed-recovery")).toBeNull();
      expect(container.textContent).toContain(GENERIC_FEED_SENTENCE);
      if (typeof code === "string") expect(container.innerHTML).not.toContain(code);
    },
  );

  it("shows no recovery step outside a feed error", () => {
    render(<PaperHealthDetails health={{ armed: true, state: "checked", failure_code: "timeout" }} />);
    expect(screen.queryByTestId("paper-feed-recovery")).toBeNull();
    expect(paperHealthSentence({ armed: true, state: "checked", failure_code: "timeout" })).toMatch(/market checked/i);
  });
});

function wizardHonesty(): LiveWizardHonesty {
  return {
    schema_version: "1",
    trust: {
      home: "/tmp/krellbot-home",
      home_mode: "0o755",
      home_mode_ok: false,
      keychain_backend: "FakeKeyring",
      keychain_ok: false,
      posture_ok: false,
      posture_warning: "keychain backend is FakeKeyring (not persistent)",
      bind: "127.0.0.1",
      live_arm_ui_allowed: false,
      trade_only_required: true,
    },
    paper_only_release: true,
    release_policy: "paper_only",
    release_policy_code: "live_disabled_paper_only_release",
    release_policy_message: "This is a paper-only release.",
    live_grant_active: false,
    live_grant: { active: false, expires_at: null, grants: [] },
    live_ready: false,
    live_arm_ui_allowed: false,
    trade_only_required: true,
    license_required_for_exchange: false,
    keys_leave_machine: false,
  };
}

function renderWizard(view: LiveWizardHonesty): void {
  const trustClient: TrustClient = { getTrust: vi.fn().mockResolvedValue(view) };
  const keysClient: KeysClient = {
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
  render(<LiveWizardPanel trustClient={trustClient} keysClient={keysClient} />);
  fireEvent.click(screen.getByTestId("live-wizard-step-security"));
}

describe("diagnostic report link beside 'Run krellbot doctor.'", () => {
  it("keeps the doctor line and adds the in-app link", async () => {
    renderWizard(wizardHonesty());
    await waitFor(() => {
      expect(screen.getByTestId("live-wizard-posture").textContent).toContain("Run krellbot doctor.");
    });
    const posture = screen.getByTestId("live-wizard-posture");
    const link = within(posture).getByRole("link", { name: "Diagnostic report" });
    expect(link.getAttribute("href")).toBe(DIAGNOSTIC_REPORT_HREF);
  });

  it("shows no link while the storage posture is fine", async () => {
    const view = wizardHonesty();
    renderWizard({ ...view, trust: { ...view.trust, posture_ok: true, posture_warning: null } });
    await waitFor(() => {
      expect(screen.getByTestId("live-wizard-posture").textContent).toMatch(/persistent keychain is available/i);
    });
    expect(screen.queryByRole("link", { name: "Diagnostic report" })).toBeNull();
  });
});
