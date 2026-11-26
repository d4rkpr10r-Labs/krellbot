import { useCallback, useEffect, useId, useReducer, useRef, useState, type JSX } from "react";
import { DisclosureSection } from "../../components/disclosure/DisclosureSection";
import { DiagnosticReport } from "../diagnostics/DiagnosticReport";
import { getCsrf } from "../../session";
import { paperMarketAvailability } from "./marketAvailability";
import type {
  PaperArmDraftSummary,
  PaperClient,
  PaperCommandResult,
  PaperPosition,
  PaperStatus,
  PaperStatusSelect,
} from "./client";
import { PaperStatusCsvExport } from "./PaperStatusCsvExport";
import { PaperPnlPositionChart } from "./PaperPnlPositionChart";
import { RehearsalPanel } from "./RehearsalPanel";
import { TradingViewAlertPreview } from "./TradingViewAlertPreview";
import { PaperHealthDetails, usePaperHealth } from "./paperHealth";
import type { RehearsalClient } from "./rehearsalClient";
import { summarizePaperCommandRefusal } from "./paperCommandRefusalSummary";
import { PAUSE_NEW_BUYS_DISCLOSURE, STOP_RUN_DISCLOSURE } from "./stopRunDisclosure";
import "./StatusPanel.position.css";
import "./StatusPanel.signal.css";

export type {
  PaperArmDraftSummary,
  PaperClient,
  PaperCommandResult,
  PaperPosition,
  PaperStatus,
  PaperStatusSelect,
} from "./client";

export type StatusPanelProps = {
  client: PaperClient;
  /** Reports a position click to the shell inspector (toggle). */
  onPositionSelect?: (position: PaperPosition | null) => void;
  /** When true, the owned-position control shows as the inspector subject. */
  positionSelected?: boolean;
  /** Optional market selector from a fleet deep link. */
  select?: PaperStatusSelect | null;
  /** Prefer this runnable revision in the arm form (home starter handoff). */
  preferredRevisionId?: string | null;
  /** Fired after a backend-confirmed paper arm. */
  onPaperArmed?: () => void;
  /** Fired after any backend-confirmed paper state mutation (arm, pause, resume, stop). */
  onStatusMutated?: () => void;
  /** Reports confirmed armed/unarmed so the shell can celebrate a first win. */
  onArmedChange?: (armed: boolean) => void;
};

type EntriesSentence = "Entries active" | "Entries paused";

type CommandName = "pause" | "resume" | "disarm" | "raise_stop";

/** Arm venues offered by the paper surface (paper mode only). */
const ARM_VENUES: readonly string[] = ["kraken", "coinbase", "binance", "gate", "pionex", "okx", "bybit", "kucoin", "ourbit", "toobit", "xt", "mexc", "deepcoin", "lbank", "bitget", "htx", "btcc", "cryptocom"];

const DEFAULT_PAPER_BALANCE = "1000";

/**
 * A plain decimal string, the shape the backend parses with ``Decimal``.
 * ``1``, ``1.5``, ``0.25`` — but not ``1e3``, ``.5`` or an empty string.
 */
const DECIMAL_PATTERN = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;

export function isValidPaperBalance(value: string): boolean {
  return DECIMAL_PATTERN.test(value);
}

/** Remembers the last successfully armed balance across mounts. */
const LAST_ARMED_BALANCE_STORAGE_KEY = "krellbot.paper.lastArmedBalance";

function readLastArmedBalance(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    // Acquiring the storage capability itself may throw (e.g. a
    // SecurityError when browser policy denies storage access), so the
    // acquisition and the read are guarded together.
    return window.localStorage.getItem(LAST_ARMED_BALANCE_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeLastArmedBalance(value: string): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(LAST_ARMED_BALANCE_STORAGE_KEY, value);
  } catch {
    // Storage may be unavailable (denied access, quota, privacy mode):
    // the affordance is best-effort and never blocks arming.
  }
}

/** A stored value is offered again only as a usable decimal balance. */
function initialArmBalance(): string {
  const stored = readLastArmedBalance();
  if (
    stored !== null &&
    isValidPaperBalance(stored) &&
    Number(stored) > 0
  ) {
    return stored;
  }
  return DEFAULT_PAPER_BALANCE;
}

/**
 * The resting stop in force for one confirmed market, read from
 * `GET /api/v1/paper/details?venue=&pair=`. `stop` is the server's decimal
 * string or null when it could not read one; `readAtMs` is when this panel
 * received it, so a retained value always says how old it is.
 */
type RestingStopRead =
  | { kind: "read"; identity: string; stop: string | null; readAtMs: number }
  | { kind: "unavailable"; identity: string };

type PanelState = {
  status: PaperStatus | null;
  sentence: EntriesSentence;
  errorMessage: string | null;
  unavailable: boolean;
  pending: CommandName | null;
  unknownOutcome: string | null;
  /** Backend-confirmed raise-stop echo, mirrored verbatim from `message`. */
  raiseStopEcho: string | null;
  /** Owned position read from `GET /api/v1/paper/position`; null = none read. */
  position: PaperPosition | null;
  /** Honest failure surface for a position read that did not decode. */
  positionError: string | null;
  /** Resting stop read for the confirmed market; null = none read yet. */
  restingStop: RestingStopRead | null;
};

const INITIAL_STATE: PanelState = {
  status: null,
  sentence: "Entries active",
  errorMessage: null,
  unavailable: false,
  pending: null,
  unknownOutcome: null,
  raiseStopEcho: null,
  position: null,
  positionError: null,
  restingStop: null,
};

type PanelAction =
  | { type: "status"; status: PaperStatus }
  | { type: "unavailable" }
  | { type: "command-start"; command: CommandName }
  | { type: "command-refused"; message: string }
  | { type: "unknown-outcome"; message: string }
  | { type: "position"; position: PaperPosition }
  | { type: "position-failed"; message: string }
  | { type: "resting-stop"; read: RestingStopRead }
  | {
      type: "command-ok";
      status: PaperStatus | null;
      sentence?: EntriesSentence;
      raiseStopEcho?: string | null;
    };

function sentenceFor(entries_paused: boolean | undefined): EntriesSentence {
  return entries_paused ? "Entries paused" : "Entries active";
}

/**
 * Confirmed market identity for an armed status or position body.
 * Venue, pair and mode must all agree before a position may be shown
 * under a status heading — never another market's cached holdings.
 */
function marketIdentity(
  row: Pick<PaperStatus, "venue" | "pair" | "mode"> | Pick<PaperPosition, "venue" | "pair" | "mode"> | null | undefined,
): string | null {
  if (
    row === null ||
    row === undefined ||
    typeof row.venue !== "string" ||
    row.venue === "" ||
    typeof row.pair !== "string" ||
    row.pair === "" ||
    typeof row.mode !== "string" ||
    row.mode === ""
  ) {
    return null;
  }
  return `${row.venue}\0${row.pair}\0${row.mode}`;
}

function sameArmedMarket(
  status: PaperStatus | null | undefined,
  position: PaperPosition | null | undefined,
): boolean {
  if (status?.armed !== true || position?.armed !== true) {
    return false;
  }
  const left = marketIdentity(status);
  const right = marketIdentity(position);
  return left !== null && right !== null && left === right;
}

function commandLabel(command: CommandName | "arm"): string {
  if (command === "pause") return "Pause new buys";
  if (command === "resume") return "Resume";
  if (command === "arm") return "Start paper run";
  if (command === "raise_stop") return "Raise stop";
  return "Stop paper run";
}

/**
 * The status read is closed-shape (no `stop` on status reads), so the
 * backend's success message — ``stop set: <venue> <pair> -> <new>`` — is the
 * immediate confirmation of a raised stop; the follow-up status read then
 * re-reads the stop in force from `GET /api/v1/paper/details`. Mirror the
 * message verbatim; the visible sentence quotes it rather than recomputing.
 */
function raiseStopSentenceFromMessage(message: unknown): string | null {
  if (typeof message !== "string") {
    return null;
  }
  const arrow = message.lastIndexOf("-> ");
  if (arrow === -1) {
    return null;
  }
  const tail = message.slice(arrow + 3).trim();
  return tail.length > 0 ? `raised stop to ${tail}` : null;
}

function transportDetail(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function isConfirmedOk(result: PaperCommandResult | null | undefined): boolean {
  // Only an explicit backend `ok: true` counts as success; a malformed or
  // flag-less response is interpreted conservatively as not confirmed.
  return result?.ok === true;
}

function refusalMessage(
  command: CommandName | "arm",
  result: PaperCommandResult | null | undefined,
): string {
  return summarizePaperCommandRefusal(commandLabel(command), result).label;
}

function unknownMessage(command: CommandName, reason: unknown): string {
  return (
    `${commandLabel(command)} outcome unknown: ${transportDetail(reason)}. ` +
    "Refresh status to see the server state before retrying."
  );
}

/**
 * The owned-position sentence the armed paper branch renders. Every value
 * is quoted verbatim from the `GET /api/v1/paper/position` body — the
 * decimal strings are never re-formatted or zero-trimmed, so the sentence
 * is evidence about the persisted `paper-<venue>.json`, not a computation.
 */
/** Quote asset from a venue pair spelling (mirrors paper ``base_quote``). */
function quoteAssetFromPair(pair: string | undefined): string {
  if (pair === undefined || pair === "") {
    return "";
  }
  if (pair.includes("-")) {
    const parts = pair.split("-");
    if (parts.length === 2 && parts[0] !== "" && parts[1] !== "") {
      return parts[1];
    }
  }
  for (const quote of ["USDT", "USDC", "USD"] as const) {
    if (pair.endsWith(quote) && pair.length > quote.length) {
      return quote;
    }
  }
  return pair.length >= 3 ? pair.slice(-3) : "";
}

function positionSentence(position: PaperPosition): string {
  const parts = (position.holdings ?? []).map((row) => `${row.qty} ${row.symbol}`);
  const owned = parts.length > 0 ? parts.join(", ") : "no open position";
  const quote = quoteAssetFromPair(position.pair);
  const cash =
    position.free_cash !== undefined
      ? `${position.free_cash}${quote !== "" ? ` ${quote}` : ""}`
      : "";
  return cash !== "" ? `Owned position: ${owned} — free cash ${cash}` : `Owned position: ${owned}`;
}

/**
 * A confirmed status for the same market keeps the last stop read (it still
 * says when it was read) until the re-read lands; any other market, or no
 * armed market, drops it at once.
 */
function keptRestingStop(
  next: PaperStatus | null,
  current: RestingStopRead | null,
): RestingStopRead | null {
  if (current === null || next?.armed !== true) {
    return null;
  }
  return marketIdentity(next) === current.identity ? current : null;
}

/**
 * The resting stop line beside the Owned position sentence. The stop is
 * quoted as `/api/v1/paper/details` serves it, in the pair's quote asset,
 * with the time this panel read it. A missing value is 'unavailable'.
 */
function RestingStopLine({
  read,
  readable,
  pair,
}: {
  read: RestingStopRead | null;
  readable: boolean;
  pair: string | undefined;
}): JSX.Element {
  if (read === null && readable) {
    return (
      <p className="kbot-paper-status__empty" data-testid="paper-resting-stop">
        Reading resting stop…
      </p>
    );
  }
  if (read === null || read.kind === "unavailable") {
    return (
      <p className="kbot-paper-status__empty" data-testid="paper-resting-stop">
        Resting stop unavailable
      </p>
    );
  }
  const quote = quoteAssetFromPair(pair);
  const readAt = new Date(read.readAtMs);
  return (
    <p
      className={read.stop !== null ? "kbot-paper-status__pack" : "kbot-paper-status__empty"}
      data-testid="paper-resting-stop"
    >
      {read.stop !== null
        ? `Resting stop ${read.stop}${quote !== "" ? ` ${quote}` : ""}`
        : "Resting stop unavailable"}
      , as of <time dateTime={readAt.toISOString()}>{readAt.toLocaleTimeString()}</time>
    </p>
  );
}

/**
 * POST ``paper.raise_stop`` with the same command envelope the paper client
 * uses, dispatched directly from the panel: deliberately not a PaperClient
 * method so this slice touches no client surface. Refusals arrive as
 * HTTP 200 bodies with ``ok: false``; a non-2xx response is a transport
 * failure and maps to the unknown-outcome path, exactly like disarm.
 */
async function postRaiseStop(
  venue: string,
  pair: string,
  newStop: string,
): Promise<PaperCommandResult> {
  const response = await fetch("/api/v1/commands", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-Krellbot-CSRF": getCsrf(),
    },
    body: JSON.stringify({
      schema_version: "1",
      command: "paper.raise_stop",
      payload: { venue, pair, new_stop: newStop },
    }),
  });
  if (!response.ok) {
    throw new Error(`paper.raise_stop failed: ${response.status}`);
  }
  return (await response.json()) as PaperCommandResult;
}

/**
 * ``GET /api/v1/paper/daily-loss-limit``: the run's daily loss limit and the
 * entry gate as of its last tick. Values are decimal strings from the run's
 * recorded fills, quoted, never recomputed.
 */
type DailyLossLimitRead = {
  armed?: boolean;
  daily_loss_limit_pct?: string | null;
  limit_amount?: string | null;
  code?: string | null;
  reason?: string | null;
};

const DAILY_LOSS_LIMIT_COMMAND = "risk.set_daily_loss_limit";

/** A plain decimal percent above 0 and at most 100; the backend re-checks it exactly. */
export function isValidLossLimitPct(value: string): boolean {
  return DECIMAL_PATTERN.test(value) && Number(value) > 0 && Number(value) <= 100;
}

async function readDailyLossLimit(venue: string, pair: string): Promise<DailyLossLimitRead> {
  const params = new URLSearchParams({ venue, pair });
  const response = await fetch(`/api/v1/paper/daily-loss-limit?${params.toString()}`, {
    method: "GET",
    credentials: "include",
    headers: { "X-Krellbot-CSRF": getCsrf() },
  });
  if (!response.ok) {
    throw new Error(`daily loss limit read failed: ${response.status}`);
  }
  return (await response.json()) as DailyLossLimitRead;
}

/** POST ``risk.set_daily_loss_limit``; ``pct: null`` turns the limit off. */
async function postDailyLossLimit(
  venue: string,
  pair: string,
  pct: string | null,
): Promise<PaperCommandResult> {
  const response = await fetch("/api/v1/commands", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-Krellbot-CSRF": getCsrf(),
    },
    body: JSON.stringify({
      schema_version: "1",
      command: DAILY_LOSS_LIMIT_COMMAND,
      payload: { venue, pair, pct },
    }),
  });
  if (!response.ok) {
    throw new Error(`${DAILY_LOSS_LIMIT_COMMAND} failed: ${response.status}`);
  }
  return (await response.json()) as PaperCommandResult;
}

function dailyLossLimitSentence(read: DailyLossLimitRead): string {
  const pct = read.daily_loss_limit_pct ?? null;
  if (pct === null) {
    return "Off. New buys are never held for a daily loss.";
  }
  const limit =
    typeof read.limit_amount === "string" && read.limit_amount !== ""
      ? `${pct}% of starting cash (${read.limit_amount})`
      : `${pct}% of starting cash`;
  if (read.code === "daily_loss_limit") {
    return `New buys held until 00:00 UTC: today's loss reached the limit of ${limit}. Exits and stops keep running.`;
  }
  if (read.code === "loss_limit_unavailable") {
    const reason = typeof read.reason === "string" && read.reason !== "" ? ` (${read.reason})` : "";
    return `New buys held: today's loss can't be worked out${reason}. Exits and stops keep running.`;
  }
  return `Limit: ${limit}. When today's loss reaches it, new buys stop until 00:00 UTC; exits and stops keep running.`;
}

/**
 * The run's optional daily loss limit, collapsed by default. It reads the
 * limit only when opened (one read per open or confirmed change, never
 * polling), and changes it only through the authenticated command; the
 * backend is the single validator and its refusal message is shown as-is.
 */
function DailyLossLimitControl({ venue, pair }: { venue: string; pair: string }): JSX.Element {
  const bodyId = useId();
  const inputId = useId();
  const [open, setOpen] = useState(false);
  const [read, setRead] = useState<DailyLossLimitRead | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [pending, setPending] = useState(false);
  const [echo, setEcho] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [unknown, setUnknown] = useState<string | null>(null);
  const inFlightRef = useRef(false);
  // A late read for a superseded request or another market never lands.
  const readEpochRef = useRef(0);

  const load = useCallback(async (): Promise<void> => {
    const epoch = ++readEpochRef.current;
    setReadError(null);
    try {
      const next = await readDailyLossLimit(venue, pair);
      if (epoch !== readEpochRef.current) {
        return;
      }
      setRead(next);
      setValue(next.daily_loss_limit_pct ?? "");
    } catch (reason) {
      if (epoch !== readEpochRef.current) {
        return;
      }
      // A failed read is unknown, never an "off" claim.
      setRead(null);
      setReadError(transportDetail(reason));
    }
  }, [venue, pair]);

  useEffect(() => {
    if (open) {
      void load();
    }
    return () => {
      readEpochRef.current += 1;
    };
  }, [open, load]);

  const submit = async (pct: string | null): Promise<void> => {
    if (inFlightRef.current) {
      return;
    }
    inFlightRef.current = true;
    setPending(true);
    setEcho(null);
    setRefusal(null);
    setUnknown(null);
    try {
      let result: PaperCommandResult;
      try {
        result = await postDailyLossLimit(venue, pair, pct);
      } catch (reason) {
        // The command may or may not have been applied: no optimistic state, no retry.
        setUnknown(
          `Daily loss limit outcome unknown: ${transportDetail(reason)}. ` +
            "Read the limit again before retrying.",
        );
        return;
      }
      if (!isConfirmedOk(result)) {
        setRefusal(summarizePaperCommandRefusal("Daily loss limit", result).label);
        return;
      }
      setEcho(typeof result.message === "string" && result.message !== "" ? result.message : null);
      await load();
    } finally {
      setPending(false);
      inFlightRef.current = false;
    }
  };

  const valid = isValidLossLimitPct(value);
  const limitSet = typeof read?.daily_loss_limit_pct === "string";

  return (
    <div className="kbot-paper-status__loss-limit" data-testid="paper-daily-loss-limit">
      <button
        type="button"
        className="kbot-paper-status__action"
        aria-expanded={open}
        aria-controls={bodyId}
        data-testid="paper-daily-loss-limit-toggle"
        onClick={() => {
          setOpen((current) => !current);
        }}
      >
        Daily loss limit
      </button>
      {open ? (
        <div id={bodyId} role="region" aria-label="Daily loss limit" data-testid="paper-daily-loss-limit-body">
          {readError !== null ? (
            <p
              className="kbot-paper-status__empty"
              role="alert"
              data-testid="paper-daily-loss-limit-unavailable"
            >
              Daily loss limit unavailable: {readError}
            </p>
          ) : read === null ? (
            <p className="kbot-paper-status__empty" data-testid="paper-daily-loss-limit-loading">
              Reading daily loss limit…
            </p>
          ) : (
            <p className="kbot-paper-status__empty" data-testid="paper-daily-loss-limit-state">
              {dailyLossLimitSentence(read)}
            </p>
          )}
          {/* The same inline field + action layout as the stop control. */}
          <div className="kbot-paper-status__raise-stop">
            <label className="kbot-paper-status__field" htmlFor={inputId}>
              Daily loss limit, % of starting cash
              <input
                id={inputId}
                className="kbot-paper-status__input"
                data-testid="paper-daily-loss-limit-input"
                type="text"
                inputMode="decimal"
                value={value}
                onChange={(event) => {
                  setValue(event.target.value);
                }}
                disabled={pending}
              />
            </label>
            <button
              type="button"
              className="kbot-paper-status__action"
              data-testid="paper-daily-loss-limit-apply"
              disabled={pending || !valid}
              onClick={() => {
                void submit(value);
              }}
            >
              {pending ? "Saving…" : "Set limit"}
            </button>
            {limitSet ? (
              <button
                type="button"
                className="kbot-paper-status__action"
                data-testid="paper-daily-loss-limit-off"
                disabled={pending}
                onClick={() => {
                  void submit(null);
                }}
              >
                Turn off
              </button>
            ) : null}
            {value !== "" && !valid ? (
              <p className="kbot-paper-status__error" data-testid="paper-daily-loss-limit-error">
                Enter a percent above 0 and at most 100, e.g. 5 or 2.5
              </p>
            ) : null}
          </div>
          {echo !== null ? <p data-testid="paper-daily-loss-limit-ok">{echo}</p> : null}
          {unknown !== null ? (
            <p className="kbot-paper-status__unknown" role="alert" data-testid="paper-daily-loss-limit-unknown">
              {unknown}
            </p>
          ) : null}
          {refusal !== null ? (
            <p className="kbot-paper-status__error" role="alert" data-testid="paper-daily-loss-limit-refused">
              {refusal}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function reducer(state: PanelState, action: PanelAction): PanelState {
  switch (action.type) {
    case "status": {
      const next = action.status;
      // Keep a cached owned position only when the confirmed status still
      // names the same venue/pair/mode. A market change (or disarm)
      // invalidates the sentence immediately — never show another
      // market's holdings under the new heading while a fresh read is
      // in flight.
      const keepPosition =
        next.armed === true && sameArmedMarket(next, state.position);
      const keepError =
        next.armed === true &&
        state.positionError !== null &&
        marketIdentity(next) !== null &&
        marketIdentity(state.status) === marketIdentity(next);
      return {
        ...state,
        status: next,
        unavailable: false,
        unknownOutcome: null,
        sentence: next.armed ? sentenceFor(next.entries_paused) : state.sentence,
        position: keepPosition ? state.position : null,
        positionError: keepError ? state.positionError : null,
        restingStop: keptRestingStop(next, state.restingStop),
      };
    }
    case "unavailable": {
      // Availability is unknown: drop the cached status rather than keep
      // acting on it, and never manufacture an unarmed response.
      return {
        ...state,
        status: null,
        unavailable: true,
        unknownOutcome: null,
        position: null,
        positionError: null,
        restingStop: null,
      };
    }
    case "resting-stop": {
      // A read started for another market than the one now confirmed is
      // dropped: never another market's stop under this heading.
      if (marketIdentity(state.status) !== action.read.identity) {
        return state;
      }
      return { ...state, restingStop: action.read };
    }
    case "command-start": {
      return {
        ...state,
        pending: action.command,
        errorMessage: null,
        unknownOutcome: null,
        raiseStopEcho: null,
      };
    }
    case "command-refused": {
      return { ...state, pending: null, errorMessage: action.message };
    }
    case "unknown-outcome": {
      // Transport threw: the command may or may not have been applied.
      // Keep the last confirmed state, never claim success or disarm, and
      // never retry the command automatically.
      return { ...state, pending: null, unknownOutcome: action.message };
    }
    case "position": {
      // An independent position read is accepted only when its
      // venue/pair/mode agrees with the current confirmed status.
      // Otherwise surface a mismatch — never another market's cash or
      // holdings, and never a manufactured zero position.
      if (!sameArmedMarket(state.status, action.position)) {
        return {
          ...state,
          position: null,
          positionError:
            "Owned position does not match the current armed market",
        };
      }
      return { ...state, position: action.position, positionError: null };
    }
    case "position-failed": {
      // A refused wire variant or a failed read is surfaced, never
      // rendered as a zero position.
      return { ...state, position: null, positionError: action.message };
    }
    case "command-ok": {
      const nextStatus = action.status;
      const keepPosition =
        nextStatus?.armed === true &&
        sameArmedMarket(nextStatus, state.position);
      const keepError =
        nextStatus?.armed === true &&
        state.positionError !== null &&
        marketIdentity(nextStatus) !== null &&
        marketIdentity(state.status) === marketIdentity(nextStatus);
      return {
        ...state,
        pending: null,
        errorMessage: null,
        unknownOutcome: null,
        status: nextStatus,
        sentence: action.sentence ?? state.sentence,
        raiseStopEcho: action.raiseStopEcho ?? state.raiseStopEcho,
        position: keepPosition ? state.position : null,
        positionError: keepError ? state.positionError : null,
        restingStop: keptRestingStop(nextStatus, state.restingStop),
      };
    }
  }
}

const SHORT_REVISION_MAX = 16;

function formatRevisionOptionLabel(row: PaperArmDraftSummary): string {
  const shortId =
    row.revision_id.length > SHORT_REVISION_MAX
      ? `${row.revision_id.slice(0, 12)}…`
      : row.revision_id;
  const pairSuffix = row.pair !== "" ? ` (${row.pair})` : "";
  if (row.label !== "") {
    return `${row.label}${pairSuffix} — ${shortId}`;
  }
  return `${shortId}${pairSuffix}`;
}

/**
 * The paper arm form: pick an existing owned, runnable revision, enter a
 * paper balance and arm it through the authenticated ``paper.arm``
 * command. Only backend-confirmed outcomes change the visible state; a
 * refusal keeps the form, a transport failure is surfaced as unknown.
 */
function ArmForm({
  client,
  onArmConfirmed,
  preferredRevisionId = null,
}: {
  client: PaperClient;
  /**
   * Fired exactly once per backend-confirmed arm (never on a refusal or an
   * unknown transport outcome): the panel bumps its arm epoch and performs
   * one bounded status read so the armed controls appear immediately.
   */
  onArmConfirmed: () => void;
  preferredRevisionId?: string | null;
}): JSX.Element | null {
  const canArm =
    typeof client === "object" &&
    client !== null &&
    typeof client.arm === "function" &&
    typeof client.listOwnedDrafts === "function";

  const [drafts, setDrafts] = useState<PaperArmDraftSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [revisionId, setRevisionId] = useState("");
  const [venue, setVenue] = useState(ARM_VENUES[0]);
  const [balance, setBalance] = useState(initialArmBalance);
  const [pending, setPending] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [unknownOutcome, setUnknownOutcome] = useState<string | null>(null);
  const inFlightRef = useRef(false);

  useEffect(() => {
    if (!canArm) {
      return;
    }
    let cancelled = false;
    const list = client.listOwnedDrafts;
    if (list === undefined) {
      return;
    }
    list
      .call(client)
      .then((rows) => {
        if (cancelled) {
          return;
        }
        // Only runnable (validated/deployed) revisions may be armed;
        // the backend refuses the rest, so they are not offered.
        const armable = rows.filter((row) => row.runnable === true);
        setDrafts(armable);
        setLoadError(null);
        const initialDraft = armable.find((row) => row.revision_id === preferredRevisionId) ?? armable[0];
        const firstVenue = initialDraft?.markets?.[0]?.venue;
        if (firstVenue && ARM_VENUES.includes(firstVenue)) {
          setVenue(firstVenue);
        }
        setRevisionId((current) => {
          if (
            preferredRevisionId &&
            armable.some((row) => row.revision_id === preferredRevisionId)
          ) {
            return preferredRevisionId;
          }
          return current === "" && armable.length > 0
            ? armable[0].revision_id
            : current;
        });
      })
      .catch((reason: unknown) => {
        if (cancelled) {
          return;
        }
        // A failed listing is unknown, never an empty-list claim.
        setDrafts(null);
        setLoadError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      cancelled = true;
    };
  }, [client, canArm, preferredRevisionId]);

  if (!canArm) {
    // A legacy injected client without the arm surface: say so instead
    // of pretending arming is possible.
    return (
      <div
        className="kbot-paper-status__arm"
        data-testid="paper-arm-unavailable"
      >
        <h3 className="kbot-paper-status__arm-title">Start your paper run</h3>
        <p className="kbot-paper-status__empty">
          Start paper run controls unavailable
        </p>
      </div>
    );
  }

  const balanceValid = isValidPaperBalance(balance);
  const selectedDraft =
    drafts?.find((row) => row.revision_id === revisionId) ?? null;
  const selectedPair = selectedDraft?.markets?.find((market) => market.venue === venue)?.pair;
  const armDisabled = pending || !balanceValid || revisionId === "" ||
    (selectedDraft?.markets !== undefined && !selectedPair);

  const handleArm = async (): Promise<void> => {
    const arm = client.arm;
    if (arm === undefined || inFlightRef.current || revisionId === "") {
      return;
    }
    inFlightRef.current = true;
    setPending(true);
    setErrorMessage(null);
    setUnknownOutcome(null);
    try {
      let result: PaperCommandResult;
      try {
        result = await arm.call(client, {
          revision_id: revisionId,
          venue,
          mode: "paper",
          paper_balance: balance,
        });
      } catch (reason) {
        // Transport threw or the backend refused with an HTTP error
        // whose body the client kept on a typed error: the command may
        // or may not have been applied. No optimistic state, no retry.
        setUnknownOutcome(
          reason instanceof Error ? reason.message : String(reason),
        );
        return;
      }
      if (!isConfirmedOk(result)) {
        setErrorMessage(refusalMessage("arm", result));
        return;
      }
      // The arm is confirmed by the backend. The visible armed state
      // itself comes from the panel's status refresh, never from here.
      setUnknownOutcome(null);
      setErrorMessage(null);
      // Offer this balance again on the next arm attempt in this session.
      writeLastArmedBalance(balance);
      // Signal the panel: its post-arm read makes the armed controls render
      // without a manual refresh, navigation or remount.
      onArmConfirmed();
    } finally {
      setPending(false);
      inFlightRef.current = false;
    }
  };

  return (
    <div className="kbot-paper-status__arm" data-testid="paper-arm-form">
      <h3 className="kbot-paper-status__arm-title">Start your paper run</h3>
      {loadError !== null && drafts === null ? (
        <>
          <p
            className="kbot-paper-status__empty"
            role="alert"
            data-testid="paper-arm-load-failed"
          >
            Saved revisions unavailable
          </p>
          <p className="kbot-paper-status__error" data-testid="paper-arm-error">
            {loadError}
          </p>
          <button
            type="button"
            className="kbot-paper-status__action"
            onClick={() => {
              // An explicit retry is a new user action: reload the list.
              const list = client.listOwnedDrafts;
              if (list === undefined || inFlightRef.current) {
                return;
              }
              setLoadError(null);
              list
                .call(client)
                .then((rows) => {
                  const armable = rows.filter((row) => row.runnable === true);
                  setDrafts(armable);
                  setRevisionId((current) =>
                    current === "" && armable.length > 0
                      ? armable[0].revision_id
                      : current,
                  );
                })
                .catch((reason: unknown) => {
                  setDrafts(null);
                  setLoadError(
                    reason instanceof Error ? reason.message : String(reason),
                  );
                });
            }}
          >
            Retry revisions
          </button>
        </>
      ) : drafts === null ? (
        <p className="kbot-paper-status__empty" data-testid="paper-arm-loading">
          Loading saved revisions…
        </p>
      ) : drafts.length === 0 ? (
        <p className="kbot-paper-status__empty" data-testid="paper-arm-empty">
          No runnable saved revisions. Open Strategies, save a draft, then validate it.
        </p>
      ) : (
        <>
          <label
            className="kbot-paper-status__field"
            htmlFor="paper-arm-revision"
          >
            <span className="kbot-paper-status__field-label">Saved revision</span>
            <select
              id="paper-arm-revision"
              className="kbot-paper-status__select"
              data-testid="paper-arm-revision"
              value={revisionId}
              title={selectedDraft?.revision_id}
              onChange={(event) => {
                const nextRevision = event.target.value;
                setRevisionId(nextRevision);
                const firstMarket = drafts.find((row) => row.revision_id === nextRevision)?.markets?.[0];
                if (firstMarket && ARM_VENUES.includes(firstMarket.venue)) {
                  setVenue(firstMarket.venue);
                }
              }}
              disabled={pending}
            >
              {drafts.map((row) => (
                <option key={row.revision_id} value={row.revision_id}>
                  {formatRevisionOptionLabel(row)}
                </option>
              ))}
            </select>
            {selectedDraft && selectedDraft.revision_id.length > SHORT_REVISION_MAX ? (
              <span
                className="kbot-paper-status__revision-detail"
                data-testid="paper-arm-revision-id"
                title={selectedDraft.revision_id}
              >
                Revision {selectedDraft.revision_id}
              </span>
            ) : null}
          </label>
          <div className="kbot-paper-status__arm-row">
            <label className="kbot-paper-status__field" htmlFor="paper-arm-venue">
              <span className="kbot-paper-status__field-label">Venue</span>
              <select
                id="paper-arm-venue"
                className="kbot-paper-status__select"
                data-testid="paper-arm-venue"
                value={venue}
                onChange={(event) => {
                  setVenue(event.target.value);
                }}
                disabled={pending}
              >
                {ARM_VENUES.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label
              className="kbot-paper-status__field"
              htmlFor="paper-arm-balance"
            >
              <span className="kbot-paper-status__field-label">Paper balance</span>
              <input
                id="paper-arm-balance"
                className="kbot-paper-status__input"
                data-testid="paper-arm-balance"
                type="text"
                inputMode="decimal"
                value={balance}
                onChange={(event) => {
                  const next = event.target.value;
                  if (next === "") {
                    // An explicit clear is remembered: never silently refill
                    // the field with a stale balance next time.
                    writeLastArmedBalance("");
                  }
                  setBalance(next);
                }}
                disabled={pending}
              />
            </label>
          </div>
          {selectedDraft ? (
            <p data-testid="paper-market-availability">
              {selectedPair
                ? paperMarketAvailability(venue, selectedPair, selectedDraft.timeframe ?? "")
                : selectedDraft.markets
                  ? "This strategy has no market on the selected exchange."
                  : "Check that the strategy has a market on the selected exchange."}
              {selectedPair && paperMarketAvailability(venue, selectedPair, selectedDraft.timeframe ?? "").includes("offline candle replay") ? (
                <> <a href="https://github.com/d4rkpr10r-Labs/krellbot/blob/main/docs/paper.md#run-a-tick" target="_blank" rel="noreferrer">Offline replay instructions</a></>
              ) : null}
            </p>
          ) : null}
          {!balanceValid ? (
            <p
              className="kbot-paper-status__error"
              data-testid="paper-arm-balance-error"
            >
              Enter the paper balance as a plain decimal, e.g. 1000 or 250.50
            </p>
          ) : null}
          <button
            type="button"
            className="kbot-paper-status__action"
            data-testid="paper-arm-submit"
            disabled={armDisabled}
            onClick={() => {
              void handleArm();
            }}
          >
            {pending ? "Starting…" : "Start paper run"}
          </button>
          {unknownOutcome !== null ? (
            <p
              className="kbot-paper-status__unknown"
              role="alert"
              data-testid="paper-arm-unknown"
            >
              Start paper run outcome unknown: {unknownOutcome}. Refresh status
              to see the server state before retrying.
            </p>
          ) : null}
          {errorMessage !== null ? (
            <p
              className="kbot-paper-status__error"
              role="alert"
              data-testid="paper-arm-error"
            >
              {errorMessage}
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}

export function StatusPanel({
  client,
  onPositionSelect,
  positionSelected = false,
  select = null,
  preferredRevisionId = null,
  onPaperArmed,
  onStatusMutated,
  onArmedChange,
}: StatusPanelProps): JSX.Element {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  // Synchronous guard so a rapid double click cannot dispatch a duplicate
  // command before React re-renders the disabled buttons.
  const inFlightRef = useRef(false);
  // Bumped on every request start and on unmount so a late response from a
  // superseded request never updates the panel.
  const epochRef = useRef(0);
  // Same discipline for the owned-position read, which is a separate
  // request from the status read.
  const positionEpochRef = useRef(0);
  const statusSelect =
    select !== null &&
    select !== undefined &&
    typeof select.venue === "string" &&
    select.venue !== "" &&
    typeof select.pair === "string" &&
    select.pair !== ""
      ? select
      : undefined;
  const health = usePaperHealth(client, statusSelect, state.status);

  // The rehearsal surface is a separate client behind the same paper
  // panel: a legacy injected client without the factory renders the
  // honest unavailable state instead of crashing.
  const canRehearse =
    typeof client === "object" &&
    client !== null &&
    typeof client.createRehearsalClient === "function";
  const [rehearsalClient] = useState<RehearsalClient | null>(() =>
    canRehearse
      ? (client.createRehearsalClient as () => RehearsalClient)()
      : null,
  );

  useEffect(() => {
    const epoch = ++epochRef.current;
    client
      .getStatus(statusSelect)
      .then((next) => {
        if (epoch !== epochRef.current) {
          return;
        }
        dispatch({ type: "status", status: next });
      })
      .catch(() => {
        if (epoch !== epochRef.current) {
          return;
        }
        // A failed lookup means availability is unknown; never poll
        // automatically and never claim an unarmed state.
        dispatch({ type: "unavailable" });
      });
    return () => {
      epochRef.current += 1;
    };
  }, [client, statusSelect?.venue, statusSelect?.pair]);

  // Bumped exactly once per backend-confirmed arm (never on refusal or
  // unknown outcome): mirrors the epoch-guarded refreshSignal shape the
  // Editor inline control uses. Zero until the first confirmed arm.
  const [armEpoch, setArmEpoch] = useState(0);

  useEffect(() => {
    if (armEpoch === 0) {
      return;
    }
    const epoch = ++epochRef.current;
    let cancelled = false;
    client
      .getStatus(statusSelect)
      .then((next) => {
        if (cancelled || epoch !== epochRef.current) {
          return;
        }
        dispatch({ type: "status", status: next });
      })
      .catch(() => {
        if (cancelled || epoch !== epochRef.current) {
          return;
        }
        // The post-arm read failed: availability is unknown, never an
        // unarmed claim and never a second automatic read.
        dispatch({ type: "unavailable" });
      });
    return () => {
      cancelled = true;
      epochRef.current += 1;
    };
    // `armEpoch` is the only automatic re-read trigger besides the client
    // itself: one bounded read per confirmed arm, no timer, no polling.
  }, [client, armEpoch, statusSelect?.venue, statusSelect?.pair]);

  const [lastArmedPreferredId, setLastArmedPreferredId] = useState<string | null>(
    null,
  );
  const handleArmConfirmed = useCallback((): void => {
    setLastArmedPreferredId(preferredRevisionId ?? null);
    setArmEpoch((current) => current + 1);
    onPaperArmed?.();
    onStatusMutated?.();
  }, [onPaperArmed, onStatusMutated, preferredRevisionId]);

  useEffect(() => {
    if (state.status === null) {
      return;
    }
    onArmedChange?.(state.status.armed === true);
  }, [state.status, onArmedChange]);


  // The owned position is a separate read from the status: the status body
  // is closed-shape and never carries cash or quantity, so the armed branch
  // reconciles the position sentence from `GET /api/v1/paper/position`
  // whenever a confirmed status says a pack is armed. One read per confirmed
  // armed status, never polling; a legacy client without the method keeps
  // the honest unavailable line instead of a manufactured position.
  const positionReadable =
    typeof client === "object" &&
    client !== null &&
    typeof client.getPosition === "function";
  useEffect(() => {
    if (!positionReadable || state.status?.armed !== true) {
      return;
    }
    const epoch = ++positionEpochRef.current;
    const read = client.getPosition;
    if (read === undefined) {
      return;
    }
    // Capture the status identity this read was started for. A late
    // response that names a different market must not update the panel
    // even if the request epoch is still current.
    const expectedIdentity = marketIdentity(state.status);
    read
      .call(client, statusSelect)
      .then((position) => {
        if (epoch !== positionEpochRef.current) {
          return;
        }
        if (
          expectedIdentity === null ||
          marketIdentity(position) !== expectedIdentity
        ) {
          dispatch({
            type: "position-failed",
            message:
              "Owned position does not match the current armed market",
          });
          return;
        }
        dispatch({ type: "position", position });
      })
      .catch((reason: unknown) => {
        if (epoch !== positionEpochRef.current) {
          return;
        }
        // A refused wire variant or a failed transport is never rendered
        // as a zero position: say the position is unavailable.
        dispatch({
          type: "position-failed",
          message: transportDetail(reason),
        });
      });
    return () => {
      positionEpochRef.current += 1;
    };
  }, [client, positionReadable, state.status, statusSelect]);

  // The resting stop in force: one `GET /api/v1/paper/details` read for the
  // confirmed venue and pair per confirmed armed status, never polling. The
  // status and position bodies stay closed; a legacy client without the
  // method keeps the honest unavailable line.
  const detailsReadable =
    typeof client === "object" &&
    client !== null &&
    typeof client.getDetails === "function";
  const detailsEpochRef = useRef(0);
  useEffect(() => {
    const confirmed = state.status;
    const read = client.getDetails;
    if (!detailsReadable || read === undefined || confirmed?.armed !== true) {
      return;
    }
    const identity = marketIdentity(confirmed);
    const venue = confirmed.venue;
    const pair = confirmed.pair;
    if (identity === null || venue === undefined || pair === undefined) {
      return;
    }
    const epoch = ++detailsEpochRef.current;
    read
      .call(client, { venue, pair })
      .then((details) => {
        if (epoch !== detailsEpochRef.current) {
          return;
        }
        if (details.armed !== true || details.venue !== venue || details.pair !== pair) {
          dispatch({ type: "resting-stop", read: { kind: "unavailable", identity } });
          return;
        }
        dispatch({
          type: "resting-stop",
          read: {
            kind: "read",
            identity,
            stop: typeof details.resting_stop === "string" ? details.resting_stop : null,
            readAtMs: Date.now(),
          },
        });
      })
      .catch(() => {
        if (epoch !== detailsEpochRef.current) {
          return;
        }
        dispatch({ type: "resting-stop", read: { kind: "unavailable", identity } });
      });
    return () => {
      detailsEpochRef.current += 1;
    };
  }, [client, detailsReadable, state.status]);
  const refreshStatus = async (): Promise<void> => {
    if (inFlightRef.current) {
      return;
    }
    inFlightRef.current = true;
    const epoch = ++epochRef.current;
    try {
      const next = await client.getStatus(statusSelect);
      if (epoch !== epochRef.current) {
        return;
      }
      dispatch({ type: "status", status: next });
    } catch (reason) {
      if (epoch !== epochRef.current) {
        return;
      }
      // A read failure must be surfaced, never swallowed: the panel keeps
      // its last confirmed state and stays explicitly refreshable.
      dispatch({
        type: "unknown-outcome",
        message:
          `Status refresh failed: ${transportDetail(reason)}. ` +
          "The last confirmed state is shown; refresh to reconcile.",
      });
    } finally {
      inFlightRef.current = false;
    }
  };

  const runCommand = async (
    command: CommandName,
    action: () => Promise<PaperCommandResult>,
    onConfirmed: () => { status: PaperStatus | null; sentence?: EntriesSentence },
  ): Promise<void> => {
    if (inFlightRef.current) {
      return;
    }
    const current = state.status;
    if (!current?.armed || !current.venue || !current.pair) {
      return;
    }
    inFlightRef.current = true;
    const epoch = ++epochRef.current;
    dispatch({ type: "command-start", command });
    try {
      let result: PaperCommandResult;
      try {
        result = await action();
      } catch (reason) {
        // Transport threw: the outcome is unknown. No optimistic state, no
        // automatic retry; the user reconciles with an explicit refresh.
        if (epoch === epochRef.current) {
          dispatch({
            type: "unknown-outcome",
            message: unknownMessage(command, reason),
          });
        }
        return;
      }
      if (epoch !== epochRef.current) {
        return;
      }
      if (!isConfirmedOk(result)) {
        // Refused, or the response was malformed/ambiguous: keep the
        // confirmed state and surface the backend's own refusal message.
        dispatch({
          type: "command-refused",
          message: refusalMessage(command, result),
        });
        return;
      }
      const applied = onConfirmed();
      dispatch({
        type: "command-ok",
        status: applied.status,
        sentence: applied.sentence,
        raiseStopEcho:
          command === "raise_stop"
            ? (raiseStopSentenceFromMessage(result.message) ??
              (typeof result.message === "string" && result.message.length > 0
                ? result.message
                : null))
            : undefined,
      });
      onStatusMutated?.();
      if (command !== "disarm") {
        // Reconcile the sentence with the server after a confirmed command:
        // a single follow-up read, not polling.
        try {
          const next = await client.getStatus(statusSelect);
          if (epoch === epochRef.current) {
            dispatch({ type: "status", status: next });
          }
        } catch (reason) {
          // The sentence already reflects the backend-confirmed command;
          // only the reconciliation read failed, so say so truthfully.
          if (epoch === epochRef.current) {
            dispatch({
              type: "unknown-outcome",
              message:
                `Status refresh failed: ${transportDetail(reason)}. ` +
                "The command was confirmed; refresh to reconcile.",
            });
          }
        }
      }
    } finally {
      inFlightRef.current = false;
    }
  };

  const handlePause = (): Promise<void> => {
    const current = state.status;
    return runCommand(
      "pause",
      () => client.pauseEntries(current?.venue ?? "", current?.pair ?? ""),
      () => ({ status: current, sentence: "Entries paused" as EntriesSentence }),
    );
  };

  const handleResume = (): Promise<void> => {
    const current = state.status;
    return runCommand(
      "resume",
      () => client.resumeEntries(current?.venue ?? "", current?.pair ?? ""),
      () => ({ status: current, sentence: "Entries active" as EntriesSentence }),
    );
  };

  const handleDisarm = (): Promise<void> => {
    const current = state.status;
    return runCommand(
      "disarm",
      () => client.disarm(current?.venue ?? "", current?.pair ?? ""),
      () => ({
        // The backend confirmed the disarm: the workstation has no armed
        // pack anymore, so the controls disappear.
        status: { schema_version: "1", armed: false },
        sentence: "Entries active" as EntriesSentence,
      }),
    );
  };

  const [raiseStopValue, setRaiseStopValue] = useState("");
  const raiseStopValid = isValidPaperBalance(raiseStopValue);
  const runControlsDisclosureId = useId();

  const handleRaiseStop = (): Promise<void> => {
    const current = state.status;
    return runCommand(
      "raise_stop",
      () =>
        postRaiseStop(
          current?.venue ?? "",
          current?.pair ?? "",
          raiseStopValue,
        ),
      // The read surface never carries the stop, so the confirmed state
      // stays as-is; the visible truth is the backend echo plus the
      // follow-up reconciliation read inside runCommand.
      () => ({ status: current }),
    );
  };

  const paperControls =
    state.status?.armed === true &&
    state.status.venue &&
    state.status.pair &&
    state.status.mode === "paper";

  const busy = state.pending !== null;
  const unknownAlert =
    state.unknownOutcome !== null ? (
      <p
        className="kbot-paper-status__unknown"
        role="alert"
        data-testid="paper-status-unknown"
      >
        {state.unknownOutcome}
      </p>
    ) : null;

  const nextActionLabel = paperControls
    ? "Next: pause new buys, resume, or stop"
    : state.status === null
      ? "Next: waiting for paper status"
      : state.unavailable
        ? "Next: retry paper status"
        : state.status?.armed
          ? "Next: controls unavailable"
          : "Next: start your paper run";

  return (
    <section
      className="kbot-paper-status"
      role="region"
      aria-label="Paper controls"
      data-testid="paper-status-panel"
      data-pending={state.pending ?? ""}
    >
      <header className="kbot-paper-status__head">
        <h2 className="kbot-paper-status__title">Paper status</h2>
        <p
          className="kbot-paper-status__next"
          data-testid="paper-status-next-action"
        >
          {nextActionLabel}
        </p>
      </header>
      <div className="kbot-paper-status__body">
        <div
          className="kbot-paper-status__first-viewport"
          data-testid="paper-workstation-first-viewport"
        >
        {paperControls ? (
          <>
            {client.getHealth ? <PaperHealthDetails health={health} /> : null}
            <p
              className="kbot-paper-status__entries"
              data-testid="paper-status-entries"
            >
              {state.sentence}
            </p>
            <p
              className="kbot-paper-status__pack"
              data-testid="paper-status-pack"
            >
              {state.status?.pack_id} on {state.status?.venue}{" "}
              {state.status?.pair}
            </p>
            {typeof state.status?.armed_count === "number" &&
            state.status.armed_count > 1 ? (
              <p
                className="kbot-paper-status__multi"
                data-testid="paper-status-multi-deploy"
              >
                Showing 1 of {state.status.armed_count} armed deployments —
                see Armed paper runs for the full list.
              </p>
            ) : null}
            {/* The owned position + free cash, read verbatim from the
                persisted paper state via `GET /api/v1/paper/position`.
                Quoted, never computed: a refused wire variant or a failed
                read renders the honest unavailable line instead of a
                zero position. */}
            {state.positionError !== null ? (
              <p
                className="kbot-paper-status__empty"
                role="alert"
                data-testid="paper-position-unavailable"
              >
                Owned position unavailable: {state.positionError}
              </p>
            ) : state.position !== null ? (
              onPositionSelect ? (
                <button
                  type="button"
                  className={`kbot-paper-status__position${
                    positionSelected
                      ? " kbot-paper-status__position--selected"
                      : ""
                  }`}
                  data-testid="paper-position"
                  aria-pressed={positionSelected}
                  onClick={() => {
                    onPositionSelect(
                      positionSelected ? null : state.position,
                    );
                  }}
                >
                  {positionSentence(state.position)}
                </button>
              ) : (
                <p
                  className="kbot-paper-status__position"
                  data-testid="paper-position"
                >
                  {positionSentence(state.position)}
                </p>
              )
            ) : positionReadable ? (
              <p
                className="kbot-paper-status__empty"
                data-testid="paper-position-pending"
              >
                Reading owned position…
              </p>
            ) : (
              <p
                className="kbot-paper-status__empty"
                data-testid="paper-position-unavailable"
              >
                Owned position unavailable
              </p>
            )}
            {/* The resting stop in force, beside (never inside) the Owned
                position sentence, read from `GET /api/v1/paper/details`. */}
            <RestingStopLine
              read={state.restingStop}
              readable={detailsReadable}
              pair={state.status?.pair}
            />
            {/* P&L + position (or mark) series from journal/export APIs.
                One read per armed mount / armEpoch bump; fakes in tests. */}
            <PaperPnlPositionChart
              client={client}
              armed={true}
              refreshKey={armEpoch}
            />
            <p
              className="kbot-paper-status__empty"
              id={runControlsDisclosureId}
              data-testid="paper-stop-run-disclosure"
            >
              {PAUSE_NEW_BUYS_DISCLOSURE} {STOP_RUN_DISCLOSURE}
            </p>
            <div className="kbot-paper-status__actions">
              <button
                type="button"
                className="kbot-paper-status__action"
                disabled={busy}
                aria-describedby={runControlsDisclosureId}
                onClick={() => {
                  void handlePause();
                }}
              >
                {state.pending === "pause"
                  ? "Pausing…"
                  : "Pause new buys (doesn't sell)"}
              </button>
              <button
                type="button"
                className="kbot-paper-status__action"
                disabled={busy}
                onClick={() => {
                  void handleResume();
                }}
              >
                {state.pending === "resume" ? "Resuming…" : "Resume new buys"}
              </button>
              <button
                type="button"
                className="kbot-paper-status__action"
                disabled={busy}
                aria-describedby={runControlsDisclosureId}
                onClick={() => {
                  void handleDisarm();
                }}
              >
                {state.pending === "disarm" ? "Stopping…" : "Stop paper run"}
              </button>
              <PaperStatusCsvExport status={state.status} />
              <button
                type="button"
                className="kbot-paper-status__action"
                disabled={busy}
                onClick={() => {
                  void refreshStatus();
                }}
              >
                Refresh status
              </button>
            </div>
            {/* Raise the resting stop. Mounted only in the armed paper
                branch. The current stop is shown read-only above
                (paper-resting-stop); the input is never prefilled and
                there is no below-current check client-side — the
                backend's invalid_stop refusal is the single validator.
                Kept visible as contextual detail next to pause/disarm. */}
            <div
              className="kbot-paper-status__raise-stop"
              data-testid="paper-raise-stop"
            >
              <label
                className="kbot-paper-status__field"
                htmlFor="paper-raise-stop-input"
              >
                New resting stop
                <input
                  id="paper-raise-stop-input"
                  className="kbot-paper-status__input"
                  data-testid="paper-raise-stop-input"
                  type="text"
                  inputMode="decimal"
                  value={raiseStopValue}
                  onChange={(event) => {
                    setRaiseStopValue(event.target.value);
                  }}
                  disabled={busy}
                />
              </label>
              <button
                type="button"
                className="kbot-paper-status__action"
                data-testid="paper-raise-stop-apply"
                disabled={busy || !raiseStopValid}
                onClick={() => {
                  void handleRaiseStop();
                }}
              >
                {state.pending === "raise_stop"
                  ? "Raising…"
                  : "Apply new stop"}
              </button>
              {!raiseStopValid ? (
                <p
                  className="kbot-paper-status__error"
                  data-testid="paper-raise-stop-error"
                >
                  Enter the new stop as a plain decimal, e.g. 1050 or 250.50
                </p>
              ) : null}
            </div>
            {state.raiseStopEcho !== null ? (
              <p data-testid="paper-raise-stop-ok">{state.raiseStopEcho}</p>
            ) : null}
            <DailyLossLimitControl
              key={`${state.status?.venue ?? ""}\0${state.status?.pair ?? ""}`}
              venue={state.status?.venue ?? ""}
              pair={state.status?.pair ?? ""}
            />
            {unknownAlert}
            {state.errorMessage !== null ? (
              <div>
              <p
                className="kbot-paper-status__error"
                role="alert"
                data-testid="paper-status-error"
              >
                {state.errorMessage}
              </p>
              <DiagnosticReport />
              </div>
            ) : null}
            <DisclosureSection
              title="Start another paper run"
              summary="Arm another validated strategy on a different venue or pair"
              badge="Advanced"
              badgeTone="advanced"
              defaultOpen={false}
              forceOpen={
                preferredRevisionId !== null &&
                preferredRevisionId !== lastArmedPreferredId
              }
              storageKey="krellbot.disclosure.paper.arm-another"
              data-testid="paper-arm-another-disclosure"
            >
              <ArmForm
                client={client}
                onArmConfirmed={handleArmConfirmed}
                preferredRevisionId={preferredRevisionId}
              />
            </DisclosureSection>
          </>
        ) : state.status?.armed ? (
          <p className="kbot-paper-status__empty" data-testid="paper-status-empty">
            Controls unavailable
          </p>
        ) : state.unavailable ? (
          <>
            <p
              className="kbot-paper-status__empty"
              role="alert"
              data-testid="paper-status-unavailable"
            >
              Paper status unavailable
            </p>
            <div className="kbot-paper-status__actions">
              <button
                type="button"
                className="kbot-paper-status__action"
                onClick={() => {
                  void refreshStatus();
                }}
              >
                Retry status
              </button>
            </div>
            {unknownAlert}
          </>
        ) : state.status === null ? (
          // No response yet: availability is unknown, so no unarmed claim
          // and no command controls until a real status arrives.
          <p className="kbot-paper-status__empty" data-testid="paper-status-loading">
            Checking paper status…
          </p>
        ) : (
          // Confirmed unarmed (first session): one job panel — prompt + arm.
          // Empty chart / rehearsal stay demoted until armed (M25.6).
          <div
            className="kbot-paper-status__first-session"
            data-testid="paper-first-session-arm"
          >
            <p
              className="kbot-paper-status__prompt"
              data-testid="paper-status-empty"
            >
              Start a paper run with the strategy you picked
            </p>
            <ArmForm
              client={client}
              onArmConfirmed={handleArmConfirmed}
              preferredRevisionId={preferredRevisionId}
            />
            <DisclosureSection
              title="Paper chart"
              summary="Empty until a paper run records fills — never fabricated"
              badge="Advanced"
              badgeTone="advanced"
              defaultOpen={false}
              storageKey="krellbot.disclosure.paper.first-session-chart"
              data-testid="paper-first-session-chart-disclosure"
            >
              <div
                className="kbot-paper-chart kbot-paper-chart--honest-empty"
                data-testid="paper-chart-honest-empty"
                aria-label="Paper chart empty"
              >
                <p className="kbot-paper-chart__empty">
                  No journal series yet. Start a paper run to plot P&L from
                  recorded fills — never fabricated.
                </p>
              </div>
            </DisclosureSection>
          </div>
        )}
        </div>
        {/* Rehearsal stays collapsed behind Advanced even after arming so
            arming a paper run never opens a caution-badged fixture walkthrough. */}
        <DisclosureSection
          title="Synthetic paper rehearsal"
          summary="Fixture walkthrough — not market data"
          badge="Advanced"
          badgeTone="advanced"
          defaultOpen={false}
          storageKey="krellbot.disclosure.paper.rehearsal"
          data-testid="paper-rehearsal-disclosure"
        >
          <RehearsalPanel client={rehearsalClient} />
        </DisclosureSection>
        <DisclosureSection
          title="TradingView alert"
          summary="Validate an alert without placing an order"
          badge="Advanced"
          badgeTone="advanced"
          defaultOpen={false}
          storageKey="krellbot.disclosure.paper.tradingview"
        >
          <TradingViewAlertPreview />
        </DisclosureSection>
      </div>
    </section>
  );
}
