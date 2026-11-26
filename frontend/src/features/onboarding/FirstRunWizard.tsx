/**
 * Full-screen Krell first-run wizard (KRELL-SIGNAL-20261010).
 *
 * ≤5 steps; one visual; ≤1 short sentence; one big sage CTA; quiet Skip
 * setup after welcome; persist complete/skip so users are never re-trapped.
 * When strategy and paper clients are passed (the App gate does), step 2
 * saves a free starter, step 3 arms paper only, and step 4 quotes real
 * paper status. Without those clients the shell still walks the steps.
 * The win screen is the celebration: completing it (or skipping from
 * that screen) consumes the shell toast so the workspace stays quiet.
 *
 * Recovery (KQ-04): after a lost arm response, Check status advances only
 * when the chosen venue and pair show this revision's paper arm, and a
 * Start that follows it checks first. A refused repeat of that revision
 * reads as already running. Neither path sends a second arm for a run
 * that exists. A failed loopback session is retried on the next action.
 *
 * Assumptions (F11): the status step's card states what the paper run
 * assumes, each value read from `GET /api/v1/paper/assumptions` for the
 * chosen venue and pair: the taker fee with the research fee-model label,
 * the slippage, the decision timeframe, the stop rule and the pretend
 * balance. A value the backend does not give reads 'unavailable'.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type JSX,
} from "react";
import { getCsrf } from "../../session";
import type {
  PaperClient,
  PaperCommandResult,
  PaperStatus,
  PaperStatusSelect,
} from "../paper/client";
import { paperMarketAvailability } from "../paper/marketAvailability";
import { paperHealthSentence, usePaperHealth } from "../paper/paperHealth";
import type { StrategyClient } from "../strategies/client";
import {
  CONSUMER_STARTER_CARD_ORDER,
  CONSUMER_STARTER_CARDS,
} from "../strategies/consumerStarterCopy";
import {
  fetchStarters,
  type StarterCatalogue,
  type StarterExample,
} from "../strategies/StarterPicker.client";
import {
  FIRST_RUN_WIZARD_STORAGE_KEY,
  isFirstRunWizardDismissed,
  readFirstRunWizardState,
  writeFirstRunWizardState,
  type FirstRunWizardPersisted,
  type FirstRunWizardStepId,
} from "./firstRunWizardPersistence";
import { markFirstWinCelebrateShown } from "./firstWinCelebratePersistence";
import { WIZARD_STEP_VISUALS } from "./wizardAssets";
import "./FirstRunWizard.signal.css";

export type FirstRunWizardStep = {
  id: FirstRunWizardStepId;
  sentence: string;
  primaryLabel: string;
};

export const DEFAULT_FIRST_RUN_WIZARD_STEPS: ReadonlyArray<FirstRunWizardStep> =
  [
    {
      id: "welcome",
      sentence:
        "Let’s set up a pretend trading bot on this computer — no real money.",
      primaryLabel: "Let’s go",
    },
    {
      id: "choose",
      sentence: "Pick one free strategy to try.",
      primaryLabel: "Use",
    },
    {
      id: "start_paper",
      sentence:
        "Start it with pretend money so you can see how it behaves.",
      primaryLabel: "Start paper run",
    },
    {
      id: "status",
      sentence:
        "It’s running on this computer (paper only — not live trading).",
      primaryLabel: "Looks good",
    },
    {
      id: "win",
      sentence: "You’re set. Here’s your workspace.",
      primaryLabel: "Open Krellbot",
    },
  ];

/** Same initial paper-arm defaults as the Run panel. Paper mode only. */
const WIZARD_MARKETS = { kraken: "XXBTZUSD", coinbase: "BTC-USD" } as const;
const WIZARD_PAPER_BALANCE = "1000";

export const WIZARD_LIVE_TRADING_UNAVAILABLE = "Live trading unavailable";

/** A stop rule as `GET /api/v1/paper/assumptions` serves it. */
export type PaperAssumptionsStop =
  | { type: "pct"; pct: string }
  | { type: "atr"; len: number; mult: string };

/**
 * What the paper engine assumes for one venue and pair, as
 * `GET /api/v1/paper/assumptions` serves it. Decimal values stay exact
 * strings. `null` is a value the backend does not give.
 */
export type PaperAssumptions = {
  fee_model: string | null;
  fee_bps: number | null;
  slippage_bps: number | null;
  timeframe: string | null;
  stop: PaperAssumptionsStop | null;
  starting_cash: string | null;
  quote: string | null;
};

const DECIMAL_TEXT = /^\d+(\.\d+)?$/;
const ASSET_TEXT = /^[A-Za-z0-9]{1,12}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textOrNull(value: unknown, pattern?: RegExp): string | null {
  if (typeof value !== "string" || value.length === 0) {
    return null;
  }
  return pattern === undefined || pattern.test(value) ? value : null;
}

function bpsOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}

function stopOrNull(value: unknown): PaperAssumptionsStop | null {
  if (!isRecord(value)) {
    return null;
  }
  if (value.type === "pct") {
    const pct = textOrNull(value.pct, DECIMAL_TEXT);
    return pct === null ? null : { type: "pct", pct };
  }
  if (value.type === "atr") {
    const mult = textOrNull(value.mult, DECIMAL_TEXT);
    const len = value.len;
    return mult !== null &&
      typeof len === "number" &&
      Number.isSafeInteger(len) &&
      len > 0
      ? { type: "atr", len, mult }
      : null;
  }
  return null;
}

/** Keep only well-formed values; anything else becomes `null`. */
export function adaptPaperAssumptions(raw: unknown): PaperAssumptions {
  const body = isRecord(raw) ? raw : {};
  return {
    fee_model: textOrNull(body.fee_model),
    fee_bps: bpsOrNull(body.fee_bps),
    slippage_bps: bpsOrNull(body.slippage_bps),
    timeframe: textOrNull(body.timeframe),
    stop: stopOrNull(body.stop),
    starting_cash: textOrNull(body.starting_cash, DECIMAL_TEXT),
    quote: textOrNull(body.quote, ASSET_TEXT),
  };
}

/**
 * Read `GET /api/v1/paper/assumptions` for one venue and pair with the
 * loopback session (cookie + CSRF header). Throws on any non-2xx answer.
 */
export async function fetchPaperAssumptions(
  select: PaperStatusSelect,
): Promise<PaperAssumptions> {
  const params = new URLSearchParams({ venue: select.venue, pair: select.pair });
  const response = await fetch(`/api/v1/paper/assumptions?${params.toString()}`, {
    method: "GET",
    credentials: "include",
    headers: { "X-Krellbot-CSRF": getCsrf() },
  });
  if (!response.ok) {
    throw new Error(`paper assumptions failed: ${response.status}`);
  }
  return adaptPaperAssumptions(await response.json());
}

type WizardAssumptionsPhase =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "ready"; assumptions: PaperAssumptions };

const ASSUMPTION_UNAVAILABLE = "unavailable";

export const WIZARD_PAPER_HONESTY =
  "Paper results are not evidence of live returns.";

/** Where to go next. Each names a real workspace view or control. */
export const WIZARD_NEXT_ACTIONS: ReadonlyArray<string> = [
  "Open Run to see each market check and any simulated fill.",
  "Use Pause new buys in Run to stop new entries. Exits and the stop keep running.",
  "Try the strategy on past candles in Research.",
];

const DECISION_CADENCE: ReadonlyMap<string, string> = new Map([
  ["1h", "Hourly"],
  ["4h", "Every 4 hours"],
  ["1d", "Daily"],
]);

const STOP_TAIL =
  "below the close of the candle that signals the buy, then only raised, never lowered";

/** Whole basis points as exact text, with the same figure in percent. */
function bpsText(bps: number): string {
  const percent = `${Math.floor(bps / 100)}.${String(bps % 100).padStart(2, "0")}`;
  return `${bps} bps (${percent}%)`;
}

function groupedDecimal(text: string): string {
  const [whole = "", fraction] = text.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}

type AssumptionRow = { id: string; label: string; value: string };

function assumptionRows(phase: WizardAssumptionsPhase): AssumptionRow[] {
  const labels: Array<[string, string]> = [
    ["fee", "Fee"],
    ["slippage", "Slippage"],
    ["decisions", "Decisions"],
    ["stop", "Stop"],
    ["balance", "Pretend balance"],
  ];
  if (phase.kind !== "ready") {
    const value = phase.kind === "loading" ? "Checking…" : ASSUMPTION_UNAVAILABLE;
    return labels.map(([id, label]) => ({ id, label, value }));
  }
  const a = phase.assumptions;
  const cadence = a.timeframe === null ? undefined : DECISION_CADENCE.get(a.timeframe);
  const values: Record<string, string> = {
    fee:
      a.fee_bps === null
        ? ASSUMPTION_UNAVAILABLE
        : a.fee_model === null
          ? bpsText(a.fee_bps)
          : `${bpsText(a.fee_bps)} · ${a.fee_model}`,
    slippage:
      a.slippage_bps === null
        ? ASSUMPTION_UNAVAILABLE
        : `${bpsText(a.slippage_bps)} against each simulated fill`,
    decisions:
      cadence === undefined
        ? ASSUMPTION_UNAVAILABLE
        : `${cadence}, on closed candles only`,
    stop:
      a.stop === null
        ? ASSUMPTION_UNAVAILABLE
        : a.stop.type === "pct"
          ? `${a.stop.pct}% ${STOP_TAIL}`
          : `${a.stop.mult} × the ${a.stop.len}-candle average true range ${STOP_TAIL}`,
    balance:
      a.starting_cash === null
        ? ASSUMPTION_UNAVAILABLE
        : a.quote === null
          ? groupedDecimal(a.starting_cash)
          : `${groupedDecimal(a.starting_cash)} ${a.quote}`,
  };
  return labels.map(([id, label]) => ({
    id,
    label,
    value: values[id] ?? ASSUMPTION_UNAVAILABLE,
  }));
}

export type WizardStatusPhase =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "ready"; status: PaperStatus };

export type WizardStatusChipKind =
  | "checking"
  | "running"
  | "waiting"
  | "not-running"
  | "unavailable"
  | "live-blocked";

export type FirstRunWizardProps = {
  storageKey?: string;
  steps?: ReadonlyArray<FirstRunWizardStep>;
  /**
   * When true, show even if persisted as skipped (tests / explicit resume).
   * Completed stays hidden unless forceShow is set.
   */
  forceShow?: boolean;
  onStepChange?: (stepId: FirstRunWizardStepId) => void;
  onSkip?: () => void;
  onComplete?: () => void;
  /**
   * Fired when the step primary is pressed. Return false to keep the step.
   */
  onPrimaryAction?: (stepId: FirstRunWizardStepId) => boolean | void;
  /**
   * Fired when a starter tile Use is pressed on the choose step.
   * Return false to keep the choose step.
   */
  onSelectStarter?: (starterId: string) => boolean | void;
  /**
   * When this value changes, re-read persisted wizard state.
   */
  syncKey?: number | string;
  /** Existing strategy draft client. Omit to keep the choose step local. */
  strategyClient?: StrategyClient;
  /** Catalogue loader. Defaults to the session starter fetch when saving. */
  loadStarters?: () => Promise<StarterCatalogue>;
  /** Existing paper client. Omit to advance Start paper run without arming. */
  paperClient?: PaperClient;
  /**
   * Loopback session started by App. Real starter and paper calls wait
   * for it so a fast Use click is not refused with 403.
   */
  sessionReady?: Promise<boolean>;
  /**
   * Starts the loopback session again after `sessionReady` resolved
   * false. The next action calls it before any request, keeping the
   * chosen starter and venue. Omit to keep the reopen instruction.
   */
  retrySession?: () => Promise<boolean>;
  /**
   * Reads what the paper run assumes for the chosen venue and pair.
   * Defaults to `GET /api/v1/paper/assumptions` when a paper client is
   * passed.
   */
  loadPaperAssumptions?: (select: PaperStatusSelect) => Promise<PaperAssumptions>;
};

function stepIndexOf(
  steps: ReadonlyArray<FirstRunWizardStep>,
  stepId: FirstRunWizardStepId,
): number {
  const index = steps.findIndex((step) => step.id === stepId);
  return index < 0 ? 0 : index;
}

function persisted(
  status: FirstRunWizardPersisted["status"],
  stepId: FirstRunWizardStepId,
  selectedStarterId: string | null,
  revisionId?: string,
): FirstRunWizardPersisted {
  const next: FirstRunWizardPersisted = {
    status,
    stepId,
    selectedStarterId,
  };
  if (typeof revisionId === "string" && revisionId.length > 0) {
    next.revisionId = revisionId;
  }
  return next;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * One honest sentence from a real paper status read. Never invents P&L
 * and never calls an unconfirmed or non-paper arm "running".
 */
export function honestWizardStatusSentence(phase: WizardStatusPhase): string {
  if (phase.kind === "loading") {
    return "Checking this computer…";
  }
  if (phase.kind === "unavailable") {
    return "Paper status unavailable.";
  }
  const status = phase.status;
  if (status.armed === true && status.mode === "paper") {
    if (status.entries_paused === true) {
      return "Waiting on this computer (paper only — not live trading).";
    }
    return "It’s running on this computer (paper only — not live trading).";
  }
  if (status.armed === true) {
    return "Live trading unavailable.";
  }
  return "Not running on this computer yet.";
}

export function wizardStatusChipKind(
  phase: WizardStatusPhase,
): WizardStatusChipKind {
  if (phase.kind === "loading") {
    return "checking";
  }
  if (phase.kind === "unavailable") {
    return "unavailable";
  }
  const status = phase.status;
  if (status.armed === true && status.mode === "paper") {
    return status.entries_paused === true ? "waiting" : "running";
  }
  if (status.armed === true) {
    return "live-blocked";
  }
  return "not-running";
}

async function validateStarter(
  strategyClient: StrategyClient,
  loadStarters: () => Promise<StarterCatalogue>,
  starterId: string,
  venue: keyof typeof WIZARD_MARKETS,
): Promise<{ revisionId: string } | { message: string }> {
  let catalogue: StarterCatalogue;
  try {
    catalogue = await loadStarters();
  } catch (error) {
    return { message: `Could not save that strategy: ${errorText(error)}` };
  }
  const example: StarterExample | undefined = catalogue.examples.find(
    (row) => row.id === starterId,
  );
  if (!example) {
    return { message: "That free strategy is not available." };
  }
  try {
    const created = await strategyClient.create({
      ...example.pack,
      markets: [{ venue, pair: WIZARD_MARKETS[venue] }],
    });
    const validated = await strategyClient.validate(created.revision_id);
    if (validated.state !== "validated" && validated.state !== "deployed") {
      const detail =
        validated.errors
          .map((error) => error.message)
          .filter((message) => message.length > 0)
          .join("; ") || `validation left state ${validated.state}`;
      return { message: `Could not save that strategy: ${detail}` };
    }
    return { revisionId: validated.revision_id };
  } catch (error) {
    return { message: `Could not save that strategy: ${errorText(error)}` };
  }
}

function armRefusal(result: PaperCommandResult | null | undefined): string {
  const message = result?.message;
  if (typeof message === "string" && message.length > 0) {
    return message;
  }
  return "Start paper run refused";
}

type WizardVenue = keyof typeof WIZARD_MARKETS;

const WIZARD_VENUE_LABELS: Record<WizardVenue, string> = {
  kraken: "Kraken",
  coinbase: "Coinbase",
};

function marketLabel(venue: WizardVenue): string {
  return `${WIZARD_VENUE_LABELS[venue]} · BTC/USD`;
}

/**
 * Refusals a repeat Start of an armed revision gets: the reservation check
 * runs first (`over_reserved`), the engine's duplicate check second.
 */
const REPEAT_ARM_CODES: ReadonlySet<string> = new Set([
  "over_reserved",
  "already_armed",
]);

/**
 * Reads only. True when paper status for the wizard's venue and pair is a
 * paper arm of this revision's strategy (its draft `strategy_id` is the
 * armed `pack_id`). Throws when either read fails, so a caller never
 * mistakes an unknown answer for "not armed".
 */
async function wizardArmExists(
  paperClient: PaperClient,
  revisionId: string | undefined,
  venue: WizardVenue,
): Promise<boolean> {
  const pair = WIZARD_MARKETS[venue];
  if (!revisionId || paperClient.listOwnedDrafts === undefined) {
    throw new Error("paper status unavailable");
  }
  const status = await paperClient.getStatus({ venue, pair });
  if (
    status.armed !== true ||
    status.mode !== "paper" ||
    status.venue !== venue ||
    status.pair !== pair ||
    typeof status.pack_id !== "string" ||
    status.pack_id.length === 0
  ) {
    return false;
  }
  const drafts = await paperClient.listOwnedDrafts();
  const draft = drafts.find((row) => row.revision_id === revisionId);
  return draft !== undefined && draft.strategy_id === status.pack_id;
}

export function FirstRunWizard({
  storageKey = FIRST_RUN_WIZARD_STORAGE_KEY,
  steps = DEFAULT_FIRST_RUN_WIZARD_STEPS,
  forceShow = false,
  onStepChange,
  onSkip,
  onComplete,
  onPrimaryAction,
  onSelectStarter,
  syncKey = 0,
  strategyClient,
  loadStarters,
  paperClient,
  sessionReady,
  retrySession,
  loadPaperAssumptions,
}: FirstRunWizardProps): JSX.Element | null {
  const [state, setState] = useState<FirstRunWizardPersisted>(() =>
    readFirstRunWizardState(storageKey),
  );
  const [enterKey, setEnterKey] = useState(0);
  const [chooseBusyId, setChooseBusyId] = useState<string | null>(null);
  const [chooseError, setChooseError] = useState<string | null>(null);
  const [armBusy, setArmBusy] = useState(false);
  const [armError, setArmError] = useState<string | null>(null);
  // The last arm's outcome is unknown: offer Check status, and check
  // before any further arm.
  const [armUnknown, setArmUnknown] = useState(false);
  const [checkBusy, setCheckBusy] = useState(false);
  const [checkResult, setCheckResult] = useState<string | null>(null);
  const [alreadyRunning, setAlreadyRunning] = useState<string | null>(null);
  const [statusPhase, setStatusPhase] = useState<WizardStatusPhase>({
    kind: "loading",
  });
  const [assumptionsPhase, setAssumptionsPhase] =
    useState<WizardAssumptionsPhase>({ kind: "loading" });
  const inFlight = useRef(false);
  const stepPrompt = useRef<HTMLParagraphElement>(null);
  const session = useRef(sessionReady);
  const assumptionsLoader = useRef(loadPaperAssumptions ?? fetchPaperAssumptions);

  useEffect(() => {
    session.current = sessionReady;
  }, [sessionReady]);

  useEffect(() => {
    assumptionsLoader.current = loadPaperAssumptions ?? fetchPaperAssumptions;
  }, [loadPaperAssumptions]);

  useEffect(() => {
    setState(readFirstRunWizardState(storageKey));
  }, [storageKey, syncKey]);

  const persist = useCallback(
    (next: FirstRunWizardPersisted) => {
      const saved = { ...next, marketVenue: next.marketVenue ?? state.marketVenue };
      writeFirstRunWizardState(saved, storageKey);
      setState(saved);
      setEnterKey((key) => key + 1);
    },
    [storageKey, state.marketVenue],
  );

  const dismissed = isFirstRunWizardDismissed(state);
  const visible = forceShow || !dismissed;

  const activeStep = useMemo(() => {
    const index = stepIndexOf(steps, state.stepId);
    return steps[index] ?? steps[0]!;
  }, [steps, state.stepId]);
  const health = usePaperHealth(activeStep.id === "status" ? paperClient : undefined);

  useEffect(() => {
    if (visible) stepPrompt.current?.focus({ preventScroll: true });
  }, [activeStep.id, visible]);

  useEffect(() => {
    if (!visible) {
      return;
    }
    onStepChange?.(activeStep.id);
  }, [activeStep.id, onStepChange, visible]);

  useEffect(() => {
    if (!visible || activeStep.id !== "status" || !paperClient) {
      return;
    }
    let cancelled = false;
    setStatusPhase({ kind: "loading" });
    paperClient
      .getStatus()
      .then((status) => {
        if (!cancelled) {
          setStatusPhase({ kind: "ready", status });
        }
      })
      .catch(() => {
        if (!cancelled) {
          setStatusPhase({ kind: "unavailable" });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [activeStep.id, paperClient, visible]);

  const assumptionsVenue: WizardVenue = state.marketVenue ?? "kraken";
  useEffect(() => {
    if (!visible || activeStep.id !== "status" || !paperClient) {
      return;
    }
    let cancelled = false;
    const load = assumptionsLoader.current;
    const select = {
      venue: assumptionsVenue,
      pair: WIZARD_MARKETS[assumptionsVenue],
    };
    setAssumptionsPhase({ kind: "loading" });
    void (async () => {
      try {
        // Wait for the loopback session so a reload here is not refused.
        if (session.current !== undefined) {
          await session.current;
        }
        const assumptions = adaptPaperAssumptions(await load(select));
        if (!cancelled) {
          setAssumptionsPhase({ kind: "ready", assumptions });
        }
      } catch {
        if (!cancelled) {
          setAssumptionsPhase({ kind: "unavailable" });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeStep.id, assumptionsVenue, paperClient, visible]);

  if (!visible) {
    return null;
  }

  const showSkip = state.status === "active" || activeStep.id !== "welcome";
  const visual = WIZARD_STEP_VISUALS[activeStep.id];
  const stepNumber = stepIndexOf(steps, activeStep.id) + 1;
  const isWin = activeStep.id === "win";
  const statusWired = activeStep.id === "status" && paperClient !== undefined;
  const sentence = statusWired
    ? paperClient?.getHealth ? paperHealthSentence(health) : honestWizardStatusSentence(statusPhase)
    : activeStep.sentence;
  const statusLoading = statusWired && statusPhase.kind === "loading";
  const catalogueLoader = loadStarters ?? fetchStarters;

  function dismissedNow(): boolean {
    const latest = readFirstRunWizardState(storageKey);
    return latest.status === "skipped" || latest.status === "completed";
  }

  function advanceTo(nextId: FirstRunWizardStepId, revisionId?: string): void {
    persist(
      persisted(
        "active",
        nextId,
        state.selectedStarterId ?? null,
        revisionId ?? state.revisionId,
      ),
    );
  }

  async function requireSession(kind: "choose" | "arm" | "check"): Promise<boolean> {
    const current = session.current;
    if (current === undefined) {
      return true;
    }
    if (await current) {
      return true;
    }
    if (retrySession !== undefined) {
      const retried = retrySession();
      session.current = retried;
      if (await retried) {
        return true;
      }
    }
    const control =
      kind === "choose" ? "Use" : kind === "arm" ? "Start paper run" : "Check status";
    const message =
      retrySession === undefined
        ? "The local workstation session is unavailable. Reopen the workstation and try again."
        : `The local workstation session is unavailable. Press ${control} again to retry.`;
    if (kind === "choose") {
      setChooseError(`Could not save that strategy: ${message}`);
    } else {
      setArmError(message);
    }
    return false;
  }

  async function saveStarter(starterId: string): Promise<void> {
    if (!strategyClient) {
      persist(persisted("active", "start_paper", starterId));
      return;
    }
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setChooseError(null);
    setChooseBusyId(starterId);
    try {
      if (!(await requireSession("choose"))) {
        return;
      }
      const saved = await validateStarter(
        strategyClient,
        catalogueLoader,
        starterId,
        state.marketVenue ?? "kraken",
      );
      if (dismissedNow()) {
        return;
      }
      if ("message" in saved) {
        setChooseError(saved.message);
        return;
      }
      persist({ ...persisted("active", "start_paper", starterId, saved.revisionId), marketVenue: state.marketVenue ?? "kraken" });
    } finally {
      inFlight.current = false;
      setChooseBusyId(null);
    }
  }

  function advancePastStart(revisionId: string, recovered: boolean): void {
    const index = stepIndexOf(steps, "start_paper");
    const nextStep = steps[index + 1];
    if (!nextStep) {
      return;
    }
    const venue = state.marketVenue ?? "kraken";
    setArmUnknown(false);
    setCheckResult(null);
    setAlreadyRunning(
      recovered
        ? `Already running on ${marketLabel(venue)}. Nothing was started twice.`
        : null,
    );
    persist(
      persisted(
        "active",
        nextStep.id,
        state.selectedStarterId ?? null,
        revisionId,
      ),
    );
  }

  async function checkStatus(): Promise<void> {
    if (!paperClient || inFlight.current) {
      return;
    }
    inFlight.current = true;
    setArmError(null);
    setCheckResult(null);
    setCheckBusy(true);
    try {
      if (!(await requireSession("check"))) {
        return;
      }
      const venue = state.marketVenue ?? "kraken";
      let found: boolean;
      try {
        found = await wizardArmExists(paperClient, state.revisionId, venue);
      } catch (reason) {
        if (!dismissedNow()) {
          setArmError(
            `Could not check status: ${errorText(reason)}. Press Check status again.`,
          );
        }
        return;
      }
      if (dismissedNow()) {
        return;
      }
      if (found && state.revisionId) {
        advancePastStart(state.revisionId, true);
        return;
      }
      // Confirmed absent: a new Start is a safe retry, not a duplicate.
      setArmUnknown(false);
      setCheckResult(
        `This strategy is not running on ${marketLabel(venue)} yet. Press Start paper run to try again.`,
      );
    } finally {
      inFlight.current = false;
      setCheckBusy(false);
    }
  }

  async function armAndAdvance(): Promise<void> {
    if (!paperClient || inFlight.current) {
      return;
    }
    inFlight.current = true;
    setArmError(null);
    setCheckResult(null);
    setArmBusy(true);
    try {
      if (!(await requireSession("arm"))) {
        return;
      }
      let revisionId = state.revisionId;
      if (!revisionId) {
        const starterId = state.selectedStarterId;
        if (!starterId || !strategyClient) {
          setArmError("Pick a strategy first.");
          return;
        }
        const saved = await validateStarter(
          strategyClient,
          catalogueLoader,
          starterId,
          state.marketVenue ?? "kraken",
        );
        if (dismissedNow()) {
          return;
        }
        if ("message" in saved) {
          setArmError(saved.message);
          return;
        }
        revisionId = saved.revisionId;
        persist(
          persisted(
            "active",
            "start_paper",
            starterId,
            revisionId,
          ),
        );
      }
      const arm = paperClient.arm;
      if (arm === undefined) {
        setArmError("Could not start the simulation. Reopen the workstation and try again.");
        return;
      }
      const venue = state.marketVenue ?? "kraken";
      if (armUnknown) {
        // The last arm may have landed: never send another until a read
        // says this revision is not armed on the chosen market.
        let found: boolean;
        try {
          found = await wizardArmExists(paperClient, revisionId, venue);
        } catch (reason) {
          if (!dismissedNow()) {
            setArmError(
              `Could not check status: ${errorText(reason)}. Press Check status again.`,
            );
          }
          return;
        }
        if (dismissedNow()) {
          return;
        }
        if (found) {
          advancePastStart(revisionId, true);
          return;
        }
        setArmUnknown(false);
      }
      let result: PaperCommandResult;
      try {
        result = await arm.call(paperClient, {
          revision_id: revisionId,
          venue,
          mode: "paper",
          paper_balance: WIZARD_PAPER_BALANCE,
        });
      } catch (reason) {
        if (!dismissedNow()) {
          setArmUnknown(true);
          setArmError(
            `Start paper run outcome unknown: ${errorText(reason)}. Check status to see whether it started before trying again.`,
          );
        }
        return;
      }
      if (dismissedNow()) {
        return;
      }
      if (result?.ok !== true) {
        if (typeof result?.code === "string" && REPEAT_ARM_CODES.has(result.code)) {
          // A refused repeat of this revision means its earlier Start went
          // through. Any other arm on the market keeps the refusal.
          let found = false;
          try {
            found = await wizardArmExists(paperClient, revisionId, venue);
          } catch {
            found = false;
          }
          if (dismissedNow()) {
            return;
          }
          if (found) {
            advancePastStart(revisionId, true);
            return;
          }
        }
        setArmError(armRefusal(result));
        return;
      }
      advancePastStart(revisionId, false);
    } finally {
      inFlight.current = false;
      setArmBusy(false);
    }
  }

  function handlePrimary(): void {
    const gate = onPrimaryAction?.(activeStep.id);
    if (gate === false) {
      return;
    }
    if (activeStep.id === "welcome") {
      persist(persisted("active", "choose", null));
      return;
    }
    if (activeStep.id === "win") {
      // Win screen already said "you're set". Do not toast again in the shell.
      markFirstWinCelebrateShown();
      persist(
        persisted(
          "completed",
          "win",
          state.selectedStarterId ?? null,
          state.revisionId,
        ),
      );
      onComplete?.();
      return;
    }
    if (activeStep.id === "choose") {
      return;
    }
    if (activeStep.id === "start_paper" && paperClient) {
      void armAndAdvance();
      return;
    }
    if (statusLoading) {
      return;
    }
    const index = stepIndexOf(steps, activeStep.id);
    const nextStep = steps[index + 1];
    if (!nextStep) {
      markFirstWinCelebrateShown();
      persist(
        persisted(
          "completed",
          "win",
          state.selectedStarterId ?? null,
          state.revisionId,
        ),
      );
      onComplete?.();
      return;
    }
    advanceTo(nextStep.id);
  }

  function handleSelectStarter(starterId: string): void {
    const gate = onSelectStarter?.(starterId);
    if (gate === false) {
      return;
    }
    void saveStarter(starterId);
  }

  function handleSkip(): void {
    // Skipping from the win screen has already shown the celebration.
    if (activeStep.id === "win") {
      markFirstWinCelebrateShown();
    }
    persist(
      persisted(
        "skipped",
        activeStep.id,
        state.selectedStarterId ?? null,
        state.revisionId,
      ),
    );
    onSkip?.();
  }

  const primaryLabel =
    activeStep.id === "start_paper" && armBusy
      ? "Starting…"
      : activeStep.primaryLabel;

  const isChoose = activeStep.id === "choose";
  const wizardClassName = isWin
    ? "kbot-first-run-wizard kbot-first-run-wizard--win"
    : isChoose
      ? "kbot-first-run-wizard kbot-first-run-wizard--choose"
      : "kbot-first-run-wizard";

  return (
    <div
      className={wizardClassName}
      data-testid="first-run-wizard"
      data-wizard-status={state.status}
      data-wizard-step={activeStep.id}
      role="dialog"
      aria-modal="true"
      aria-labelledby="kbot-first-run-wizard-sentence"
    >
      <div className="kbot-first-run-wizard__chrome">
        <div
          className="kbot-first-run-wizard__progress"
          data-testid="first-run-wizard-progress"
          aria-hidden="true"
        >
          {steps.map((step, index) => (
            <span
              key={step.id}
              className={
                index < stepNumber
                  ? "kbot-first-run-wizard__dot kbot-first-run-wizard__dot--on"
                  : "kbot-first-run-wizard__dot"
              }
              data-testid={`first-run-wizard-dot-${step.id}`}
            />
          ))}
        </div>

        <div
          className="kbot-first-run-wizard__stage"
          key={`${activeStep.id}-${enterKey}`}
          data-testid="first-run-wizard-stage"
        >
          <img
            className="kbot-first-run-wizard__visual"
            data-testid="first-run-wizard-visual"
            data-wizard-visual={visual.name}
            src={visual.src}
            alt=""
          />

          <p
            id="kbot-first-run-wizard-sentence"
            ref={stepPrompt}
            tabIndex={-1}
            className="kbot-first-run-wizard__sentence"
            data-testid="first-run-wizard-sentence"
          >
            {sentence}
          </p>

          {statusWired ? (
            <p
              className="kbot-first-run-wizard__chip"
              data-testid="first-run-wizard-status-chip"
              data-paper-status={paperClient?.getHealth ? (health?.state === "checked" || health?.state === "filled" ? "running" : "waiting") : wizardStatusChipKind(statusPhase)}
            >
              {WIZARD_LIVE_TRADING_UNAVAILABLE}
            </p>
          ) : null}

          {activeStep.id === "status" && alreadyRunning !== null ? (
            <p data-testid="first-run-wizard-already-running" role="status">
              {alreadyRunning}
            </p>
          ) : null}

          {statusWired ? (
            <section
              className="kbot-first-run-wizard__assumptions"
              data-testid="first-run-wizard-assumptions"
              aria-labelledby="kbot-first-run-wizard-assumptions-title"
              aria-busy={assumptionsPhase.kind === "loading"}
            >
              <h2
                className="kbot-first-run-wizard__assumptions-title"
                id="kbot-first-run-wizard-assumptions-title"
              >
                What this paper run assumes
              </h2>
              <dl className="kbot-first-run-wizard__assumptions-list">
                {assumptionRows(assumptionsPhase).map((row) => (
                  <div
                    key={row.id}
                    className="kbot-first-run-wizard__assumption"
                    data-testid={`first-run-wizard-assumption-${row.id}`}
                  >
                    <dt>{row.label}</dt>
                    <dd data-testid={`first-run-wizard-assumption-${row.id}-value`}>
                      {row.value}
                    </dd>
                  </div>
                ))}
              </dl>
              <p
                className="kbot-first-run-wizard__assumptions-honesty"
                data-testid="first-run-wizard-assumptions-honesty"
              >
                {WIZARD_PAPER_HONESTY}
              </p>
              <h3 className="kbot-first-run-wizard__assumptions-next">Next</h3>
              <ul
                className="kbot-first-run-wizard__next-actions"
                data-testid="first-run-wizard-next-actions"
              >
                {WIZARD_NEXT_ACTIONS.map((action) => (
                  <li key={action}>{action}</li>
                ))}
              </ul>
            </section>
          ) : null}

          {activeStep.id === "choose" ? (
            <>
              <label className="kbot-first-run-wizard__market" htmlFor="wizard-market">
                Exchange for simulated BTC/USD trades
                <select id="wizard-market" data-testid="first-run-wizard-market"
                  value={state.marketVenue ?? "kraken"} disabled={chooseBusyId !== null}
                  onChange={(event) => {
                    const marketVenue = event.target.value === "coinbase" ? "coinbase" : "kraken";
                    const next: FirstRunWizardPersisted = { ...state, marketVenue };
                    writeFirstRunWizardState(next, storageKey);
                    setState(next);
                  }}>
                  <option value="kraken">Kraken · BTC/USD</option>
                  <option value="coinbase">Coinbase · BTC/USD</option>
                </select>
              </label>
              <ul
                className="kbot-first-run-wizard__tiles"
                data-testid="first-run-wizard-tiles"
              >
                {CONSUMER_STARTER_CARD_ORDER.map((id) => {
                  const card = CONSUMER_STARTER_CARDS[id]!;
                  const busy = chooseBusyId === id;
                  return (
                    <li key={id} className="kbot-first-run-wizard__tile">
                      <div className="kbot-first-run-wizard__tile-copy">
                        <p className="kbot-first-run-wizard__tile-title">
                          {card.title}
                        </p>
                        <p
                          className="kbot-first-run-wizard__tile-blurb"
                          data-testid={`first-run-wizard-blurb-${id}`}
                        >
                          {card.description}
                        </p>
                      </div>
                      <button
                        type="button"
                        className="kbot-first-run-wizard__primary"
                        data-testid={`first-run-wizard-use-${id}`}
                        aria-label={`Use ${card.title}`}
                        disabled={chooseBusyId !== null}
                        onClick={() => handleSelectStarter(id)}
                      >
                        {busy ? "Saving…" : "Use"}
                      </button>
                    </li>
                  );
                })}
              </ul>
              {chooseError !== null ? (
                <p
                  className="kbot-first-run-wizard__alert"
                  data-testid="first-run-wizard-choose-error"
                  role="alert"
                >
                  {chooseError}
                </p>
              ) : null}
            </>
          ) : (
            <button
              type="button"
              className="kbot-first-run-wizard__primary"
              data-testid="first-run-wizard-primary"
              disabled={armBusy || checkBusy || statusLoading}
              onClick={handlePrimary}
            >
              {primaryLabel}
            </button>
          )}

          {activeStep.id === "start_paper" && !armBusy && !checkBusy ? (
            <button type="button" className="kbot-first-run-wizard__skip"
              onClick={() => {
                setArmError(null);
                setCheckResult(null);
                persist(persisted("active", "choose", state.selectedStarterId ?? null));
              }}>Back to strategies</button>
          ) : null}
          {activeStep.id === "start_paper" && state.marketVenue ? (
            <p data-testid="first-run-wizard-market-summary">
              {state.marketVenue === "kraken" ? "Kraken" : "Coinbase"} · BTC/USD · 1 hour · 1,000 USD paper balance.
              {" "}{paperMarketAvailability(state.marketVenue, WIZARD_MARKETS[state.marketVenue], "1h")}
            </p>
          ) : null}
          {activeStep.id === "start_paper" && armError !== null ? (
            <p
              className="kbot-first-run-wizard__alert"
              data-testid="first-run-wizard-arm-error"
              role="alert"
            >
              {armError}
            </p>
          ) : null}
          {activeStep.id === "start_paper" && armUnknown && paperClient ? (
            <button
              type="button"
              className="kbot-first-run-wizard__skip"
              data-testid="first-run-wizard-check-status"
              disabled={armBusy || checkBusy}
              onClick={() => void checkStatus()}
            >
              {checkBusy ? "Checking…" : "Check status"}
            </button>
          ) : null}
          {activeStep.id === "start_paper" && checkResult !== null ? (
            <p data-testid="first-run-wizard-check-result" role="status">
              {checkResult}
            </p>
          ) : null}

          {showSkip ? (
            <button
              type="button"
              className="kbot-first-run-wizard__skip"
              data-testid="first-run-wizard-skip"
              disabled={chooseBusyId !== null || armBusy || checkBusy}
              onClick={handleSkip}
            >
              Skip setup
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export {
  DEFAULT_FIRST_RUN_WIZARD_STATE,
  FIRST_RUN_WIZARD_STORAGE_KEY,
  isFirstRunWizardDismissed,
  readFirstRunWizardState,
  writeFirstRunWizardState,
} from "./firstRunWizardPersistence";
