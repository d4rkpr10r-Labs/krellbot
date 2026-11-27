import { getCsrf } from "../../session";

/**
 * How the backtest engine charges fees: the same taker fee on every buy and
 * every sell fill. The costs side-car names it as `fee_model`.
 */
export const RESEARCH_FEE_MODEL_LABEL = "taker, per side";

/** Basis points of slippage the engine applies when a run names none. */
export const RESEARCH_DEFAULT_SLIPPAGE_BPS = 5;

export type RunRequest = {
  datasetPath: string;
  feeBps: number;
  /** Omitted: the engine default (`RESEARCH_DEFAULT_SLIPPAGE_BPS`). */
  slippageBps?: number;
  fromMs: number;
  toMs: number;
  packPath?: string;
  revisionId?: string;
  holdoutFromMs?: number;
  holdoutToMs?: number;
};

export type ResearchJobSummary = {
  id: string;
  state: string;
};

export type StoredResult = {
  legacy_receipt: Record<string, unknown>;
  trace: Array<Record<string, unknown>>;
};

export type ResearchJobSnapshot = {
  id: string;
  state: string;
  error?: { code: string; message: string };
};

/** One simulated backtest fill leg. Decimal values stay exact strings. */
export type FillLeg = {
  side: "buy" | "sell";
  price: string;
  qty: string;
  fee: string;
  is_stop: boolean;
};

/** Per-bar fills of one research run, keyed by the bar_ts they filled on. */
export type ResearchFills = {
  fills: Record<string, FillLeg[]>;
};

/**
 * The research costs side-car served at `GET /api/v1/jobs/{id}/costs`.
 * Money and returns are exact Decimal text; `null` means the engine could
 * not compute that figure (for example a rerun outside its numeric range,
 * or fills that did not reconcile with the ledger).
 */
export type ResearchCosts = {
  fee_model: string;
  fee_bps: number;
  slippage_bps: number;
  slippage_mult: number;
  starting_cash: string | null;
  fill_count: number | null;
  total_fees: string | null;
  net_return_pct: string | null;
  gross_return_pct: string | null;
  stressed: {
    factor: number;
    fee_bps: number;
    slippage_bps: number;
    slippage_mult: number;
    return_pct: string | null;
  };
};

/**
 * The run identity read from `GET /api/v1/jobs/{id}/run`. `null` means the
 * run never resolved that value (for example every input of the id); it is
 * never a guessed zero.
 */
export type ResearchRunIdentity = {
  /** Hex sha256 over the run's 13 canonical inputs. */
  run_id: string | null;
  /**
   * Distinct packs tried for this strategy on this dataset, failed and
   * cancelled runs included.
   */
  variants_tried: number | null;
};

export type ResearchClient = {
  submitRun(request: RunRequest): Promise<ResearchJobSummary>;
  cancelJob(jobId: string): Promise<void>;
  getResult(jobId: string): Promise<StoredResult | null>;
  getJob(jobId: string): Promise<ResearchJobSnapshot | null>;
  getResultDownload(jobId: string): Promise<Response>;
  /** Null when the run recorded no fills (404) or the body is malformed. */
  getFills?(jobId: string): Promise<ResearchFills | null>;
  /** `null` when the job has no cost side-car (404) or it is malformed. */
  getCosts?(jobId: string): Promise<ResearchCosts | null>;
  /** `null` when the job has no run record (404) or the body is malformed. */
  getRun?(jobId: string): Promise<ResearchRunIdentity | null>;
};

const DECIMAL_TEXT = /^-?\d+(\.\d+)?$/;
const SHA256_HEX = /^[0-9a-f]{64}$/;

/**
 * Validate a run-record body. A wrong-typed field makes the whole identity
 * unavailable rather than showing an invented id or count.
 */
export function readResearchRunIdentity(raw: unknown): ResearchRunIdentity | null {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const obj = raw as Record<string, unknown>;
  const run = obj.run;
  if (run === null || typeof run !== "object" || Array.isArray(run)) {
    return null;
  }
  const runId = (run as Record<string, unknown>).run_id;
  if (runId !== null && !(typeof runId === "string" && SHA256_HEX.test(runId))) {
    return null;
  }
  const variants = obj.variants_tried;
  if (
    variants !== null &&
    !(typeof variants === "number" && Number.isSafeInteger(variants) && variants >= 0)
  ) {
    return null;
  }
  return { run_id: runId, variants_tried: variants };
}

function readFiniteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Decimal text or `null`; anything else is malformed (`undefined`). */
function readDecimalText(value: unknown): string | null | undefined {
  if (value === null) {
    return null;
  }
  return typeof value === "string" && DECIMAL_TEXT.test(value)
    ? value
    : undefined;
}

/**
 * Validate a costs side-car body. A missing or wrong-typed field makes the
 * whole block unavailable rather than showing an invented number.
 */
export function readResearchCosts(raw: unknown): ResearchCosts | null {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const obj = raw as Record<string, unknown>;
  const stressedRaw = obj.stressed;
  if (
    stressedRaw === null ||
    typeof stressedRaw !== "object" ||
    Array.isArray(stressedRaw)
  ) {
    return null;
  }
  const stressedObj = stressedRaw as Record<string, unknown>;
  const feeBps = readFiniteNumber(obj.fee_bps);
  const slippageBps = readFiniteNumber(obj.slippage_bps);
  const slippageMult = readFiniteNumber(obj.slippage_mult);
  const fillCount = obj.fill_count === null ? null : readFiniteNumber(obj.fill_count);
  const factor = readFiniteNumber(stressedObj.factor);
  const stressedFeeBps = readFiniteNumber(stressedObj.fee_bps);
  const stressedSlippageBps = readFiniteNumber(stressedObj.slippage_bps);
  const stressedSlippageMult = readFiniteNumber(stressedObj.slippage_mult);
  const startingCash = readDecimalText(obj.starting_cash);
  const totalFees = readDecimalText(obj.total_fees);
  const netReturn = readDecimalText(obj.net_return_pct);
  const grossReturn = readDecimalText(obj.gross_return_pct);
  const stressedReturn = readDecimalText(stressedObj.return_pct);
  if (
    typeof obj.fee_model !== "string" ||
    feeBps === null ||
    slippageBps === null ||
    slippageMult === null ||
    (fillCount === null && obj.fill_count !== null) ||
    factor === null ||
    stressedFeeBps === null ||
    stressedSlippageBps === null ||
    stressedSlippageMult === null ||
    startingCash === undefined ||
    totalFees === undefined ||
    netReturn === undefined ||
    grossReturn === undefined ||
    stressedReturn === undefined
  ) {
    return null;
  }
  return {
    fee_model: obj.fee_model,
    fee_bps: feeBps,
    slippage_bps: slippageBps,
    slippage_mult: slippageMult,
    starting_cash: startingCash,
    fill_count: fillCount,
    total_fees: totalFees,
    net_return_pct: netReturn,
    gross_return_pct: grossReturn,
    stressed: {
      factor,
      fee_bps: stressedFeeBps,
      slippage_bps: stressedSlippageBps,
      slippage_mult: stressedSlippageMult,
      return_pct: stressedReturn,
    },
  };
}

function readFillLeg(raw: unknown): FillLeg | null {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const leg = raw as Record<string, unknown>;
  if (leg.side !== "buy" && leg.side !== "sell") {
    return null;
  }
  if (
    typeof leg.price !== "string" ||
    typeof leg.qty !== "string" ||
    typeof leg.fee !== "string" ||
    typeof leg.is_stop !== "boolean"
  ) {
    return null;
  }
  return {
    side: leg.side,
    price: leg.price,
    qty: leg.qty,
    fee: leg.fee,
    is_stop: leg.is_stop,
  };
}

function readFills(raw: unknown): ResearchFills | null {
  if (raw === null || typeof raw !== "object") {
    return null;
  }
  const fills = (raw as { fills?: unknown }).fills;
  if (fills === null || typeof fills !== "object" || Array.isArray(fills)) {
    return null;
  }
  const out: Record<string, FillLeg[]> = {};
  for (const [barTs, legs] of Object.entries(fills as Record<string, unknown>)) {
    if (!Array.isArray(legs)) {
      return null;
    }
    const parsed: FillLeg[] = [];
    for (const rawLeg of legs) {
      const leg = readFillLeg(rawLeg);
      if (leg === null) {
        return null;
      }
      parsed.push(leg);
    }
    out[barTs] = parsed;
  }
  return { fills: out };
}

async function postJson(url: string, body: unknown): Promise<unknown> {
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-Krellbot-CSRF": getCsrf(),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`request failed: ${response.status}`);
  }
  return await response.json();
}

const READ_DEADLINE_MS = 10000;

function readTimeoutError(): Error {
  return new Error(`read timed out after ${READ_DEADLINE_MS}ms`);
}

async function getJson(url: string): Promise<unknown | null> {
  const controller = new AbortController();
  let expired = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      expired = true;
      reject(readTimeoutError());
      controller.abort();
    }, READ_DEADLINE_MS);
  });
  try {
    const response = (await Promise.race([
      fetch(url, {
        method: "GET",
        credentials: "include",
        headers: {
          "X-Krellbot-CSRF": getCsrf(),
        },
        signal: controller.signal,
      }),
      deadline,
    ])) as Response;
    if (response.status === 404) {
      return null;
    }
    if (!response.ok) {
      throw new Error(`request failed: ${response.status}`);
    }
    return (await Promise.race([response.json(), deadline])) as unknown;
  } catch (err) {
    if (expired) {
      throw readTimeoutError();
    }
    throw err;
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  }
}

export function createHttpClient(): ResearchClient {
  return {
    async submitRun(request: RunRequest): Promise<ResearchJobSummary> {
      const body: Record<string, unknown> = {
        kind: "research.backtest",
        correlation_id: "research-ui",
        dataset_csv: request.datasetPath,
        fee_bps: request.feeBps,
        from_ms: request.fromMs,
        to_ms: request.toMs,
      };
      if (typeof request.slippageBps === "number") {
        body.slippage_bps = request.slippageBps;
      }
      if (request.packPath) {
        body.pack_path = request.packPath;
      }
      if (request.revisionId) {
        body.revision_id = request.revisionId;
      }
      if (typeof request.holdoutFromMs === "number") {
        body.holdout_from_ms = request.holdoutFromMs;
      }
      if (typeof request.holdoutToMs === "number") {
        body.holdout_to_ms = request.holdoutToMs;
      }
      const raw = (await postJson("/api/v1/research/jobs", body)) as {
        id?: string;
        state?: string;
      };
      return {
        id: String(raw.id ?? ""),
        state: String(raw.state ?? ""),
      };
    },
    async cancelJob(jobId: string): Promise<void> {
      await postJson(
        `/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`,
        {},
      );
    },
    async getResult(jobId: string): Promise<StoredResult | null> {
      const raw = await getJson(
        `/api/v1/jobs/${encodeURIComponent(jobId)}/result`,
      );
      if (raw === null) {
        return null;
      }
      if (raw === null || typeof raw !== "object") {
        return null;
      }
      const obj = raw as { legacy_receipt?: unknown; trace?: unknown };
      if (!obj.legacy_receipt || typeof obj.legacy_receipt !== "object") {
        return null;
      }
      const trace = Array.isArray(obj.trace)
        ? (obj.trace as Array<Record<string, unknown>>)
        : [];
      return {
        legacy_receipt: obj.legacy_receipt as Record<string, unknown>,
        trace,
      };
    },
    async getJob(jobId: string): Promise<ResearchJobSnapshot | null> {
      const raw = await getJson(
        `/api/v1/jobs/${encodeURIComponent(jobId)}`,
      );
      if (raw === null || typeof raw !== "object") {
        return null;
      }
      const obj = raw as {
        id?: unknown;
        state?: unknown;
        error?: unknown;
      };
      if (typeof obj.id !== "string" || typeof obj.state !== "string") {
        return null;
      }
      let error: { code: string; message: string } | undefined;
      if (obj.error && typeof obj.error === "object") {
        const e = obj.error as { code?: unknown; message?: unknown };
        if (typeof e.code === "string" && typeof e.message === "string") {
          error = { code: e.code, message: e.message };
        }
      }
      const snap: ResearchJobSnapshot = { id: obj.id, state: obj.state };
      if (error !== undefined) {
        snap.error = error;
      }
      return snap;
    },
    async getCosts(jobId: string): Promise<ResearchCosts | null> {
      const raw = await getJson(
        `/api/v1/jobs/${encodeURIComponent(jobId)}/costs`,
      );
      return readResearchCosts(raw);
    },
    async getRun(jobId: string): Promise<ResearchRunIdentity | null> {
      const raw = await getJson(
        `/api/v1/jobs/${encodeURIComponent(jobId)}/run`,
      );
      return readResearchRunIdentity(raw);
    },
    async getResultDownload(jobId: string): Promise<Response> {
      const response = await fetch(
        `/api/v1/jobs/${encodeURIComponent(jobId)}/result/download`,
        {
          method: "GET",
          credentials: "include",
          headers: {
            "X-Krellbot-CSRF": getCsrf(),
          },
        },
      );
      if (!response.ok) {
        throw new Error(`download failed: ${response.status}`);
      }
      return response;
    },
    async getFills(jobId: string): Promise<ResearchFills | null> {
      const raw = await getJson(
        `/api/v1/jobs/${encodeURIComponent(jobId)}/fills`,
      );
      return readFills(raw);
    },
  };
}
