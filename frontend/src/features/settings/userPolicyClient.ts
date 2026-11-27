/**
 * Client for the operator policy (`GET`/`PUT /api/v1/user-policy`) and the
 * MCP launch command (`GET /api/v1/agents/mcp-setup`) behind Settings →
 * Agents & limits (M38.10).
 *
 * The policy keeps its wire shape (snake_case keys, decimals as strings) so
 * the editor sends back exactly what it loaded apart from the user's edits.
 *
 * Each read carries the engine's `digest` of the policy file, and each save
 * or reset sends it back as `expected_digest`. The engine refuses the write
 * (`user_policy_changed`) if the file changed since that read, so a page
 * showing an old read can never write it back over a newer policy.
 */
import { getCsrf } from "../../session";

export type AgentLimits = {
  allowed_venues: string[] | null;
  allowed_pairs: string[] | null;
  max_paper_balance: string;
  max_risk_cap_pct: string;
  require_daily_loss_limit: boolean;
  max_agent_arms: number;
  krell_allow_pause_resume: boolean;
  krell_allow_disarm: boolean;
  mcp_allow_pause_resume: boolean;
  mcp_allow_disarm: boolean;
  token_budget_per_conversation: number;
  token_budget_per_month: number;
};

export type UserPolicy = {
  schema_version: "2";
  allow_paper_arm: boolean;
  allow_live: boolean;
  allow_key_export: boolean;
  agents: AgentLimits;
};

/** The engine reads the policy file as invalid and enforces its fail-closed snapshot. */
export type PolicyInvalid = { code: string; message: string };

export type McpSetup = {
  command: string;
  args: string[];
  env: Record<string, string>;
  /** The Claude Code command line, quoted by the engine for this computer's shell. */
  claudeCode: string;
};

export type PolicyRefusal = { ok: false; code: string; message: string };

/**
 * The engine's digest of the policy file a read or save saw, sent back as
 * `expected_digest`. `null` only when the reply carried none.
 */
export type PolicyDigest = string | null;

export type PolicyRead =
  | {
      ok: true;
      policy: UserPolicy;
      invalid: PolicyInvalid | null;
      digest: PolicyDigest;
    }
  | PolicyRefusal;

export type PolicySave =
  | { ok: true; policy: UserPolicy; changed: boolean; digest: PolicyDigest }
  | PolicyRefusal;

export type McpSetupRead = { ok: true; setup: McpSetup } | PolicyRefusal;

const POLICY_URL = "/api/v1/user-policy";
const MCP_SETUP_URL = "/api/v1/agents/mcp-setup";
const CODE_POLICY_INVALID = "user_policy_invalid";
const CODE_POLICY_UPDATED = "user_policy_updated";
/** The engine refused a save: the policy file changed since it was read. */
export const CODE_POLICY_CHANGED = "user_policy_changed";
/** A reply this screen cannot read (for example, a count above 2^53). */
export const CODE_BAD_RESPONSE = "bad_response";
const GENERIC_MESSAGE = "Krellbot could not complete that request.";
const UNREADABLE_MESSAGE = "Krellbot returned a reply this screen cannot read.";

type Body = Record<string, unknown>;

function isNames(value: unknown): value is string[] | null {
  return (
    value === null ||
    (Array.isArray(value) && value.every((item) => typeof item === "string"))
  );
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function toLimits(raw: unknown): AgentLimits | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Body;
  if (
    !isNames(r.allowed_venues) ||
    !isNames(r.allowed_pairs) ||
    typeof r.max_paper_balance !== "string" ||
    typeof r.max_risk_cap_pct !== "string" ||
    typeof r.require_daily_loss_limit !== "boolean" ||
    !isCount(r.max_agent_arms) ||
    typeof r.krell_allow_pause_resume !== "boolean" ||
    typeof r.krell_allow_disarm !== "boolean" ||
    typeof r.mcp_allow_pause_resume !== "boolean" ||
    typeof r.mcp_allow_disarm !== "boolean" ||
    !isCount(r.token_budget_per_conversation) ||
    !isCount(r.token_budget_per_month)
  ) {
    return null;
  }
  return {
    allowed_venues: r.allowed_venues,
    allowed_pairs: r.allowed_pairs,
    max_paper_balance: r.max_paper_balance,
    max_risk_cap_pct: r.max_risk_cap_pct,
    require_daily_loss_limit: r.require_daily_loss_limit,
    max_agent_arms: r.max_agent_arms,
    krell_allow_pause_resume: r.krell_allow_pause_resume,
    krell_allow_disarm: r.krell_allow_disarm,
    mcp_allow_pause_resume: r.mcp_allow_pause_resume,
    mcp_allow_disarm: r.mcp_allow_disarm,
    token_budget_per_conversation: r.token_budget_per_conversation,
    token_budget_per_month: r.token_budget_per_month,
  };
}

function toPolicy(raw: unknown): UserPolicy | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Body;
  const agents = toLimits(r.agents);
  if (
    r.schema_version !== "2" ||
    typeof r.allow_paper_arm !== "boolean" ||
    typeof r.allow_live !== "boolean" ||
    typeof r.allow_key_export !== "boolean" ||
    agents === null
  ) {
    return null;
  }
  return {
    schema_version: "2",
    allow_paper_arm: r.allow_paper_arm,
    allow_live: r.allow_live,
    allow_key_export: r.allow_key_export,
    agents,
  };
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/** A refusal built only from the server's code and message (or gate detail). */
function refusalOf(status: number, body: Body): PolicyRefusal {
  return {
    ok: false,
    code: str(body.code, `http_${status}`),
    message: str(body.message) || str(body.detail) || GENERIC_MESSAGE,
  };
}

function unreadable(): PolicyRefusal {
  return { ok: false, code: CODE_BAD_RESPONSE, message: UNREADABLE_MESSAGE };
}

/** The reply's digest: `null` when it has none, `undefined` for a bad one. */
function digestOf(body: Body): PolicyDigest | undefined {
  if (!("digest" in body) || body.digest === undefined) return null;
  return typeof body.digest === "string" && body.digest !== ""
    ? body.digest
    : undefined;
}

function networkFailure(): PolicyRefusal {
  return {
    ok: false,
    code: "network_error",
    message: "Krellbot could not reach the local engine.",
  };
}

async function request(
  url: string,
  init?: RequestInit,
): Promise<{ ok: true; body: Body } | PolicyRefusal> {
  let response: Response;
  try {
    response = await fetch(url, { credentials: "include", ...init });
  } catch {
    return networkFailure();
  }
  let parsed: unknown = null;
  try {
    parsed = await response.json();
  } catch {
    parsed = null;
  }
  const body = (parsed && typeof parsed === "object" ? parsed : {}) as Body;
  if (!response.ok || body.ok === false) {
    return refusalOf(response.status, body);
  }
  return { ok: true, body };
}

async function put(
  payload: unknown,
  expectedDigest: PolicyDigest,
): Promise<PolicySave> {
  const body =
    expectedDigest === null
      ? { policy: payload }
      : { policy: payload, expected_digest: expectedDigest };
  const result = await request(POLICY_URL, {
    method: "PUT",
    headers: { "Content-Type": "application/json", "X-Krellbot-CSRF": getCsrf() },
    body: JSON.stringify(body),
  });
  if (!result.ok) return result;
  const policy = toPolicy(result.body.policy);
  const digest = digestOf(result.body);
  // A save checked against a digest must name the file it wrote, or the
  // next save could not be checked.
  if (policy === null || digest === undefined) return unreadable();
  if (expectedDigest !== null && digest === null) return unreadable();
  return {
    ok: true,
    policy,
    changed: result.body.code === CODE_POLICY_UPDATED,
    digest,
  };
}

export async function loadUserPolicy(): Promise<PolicyRead> {
  const result = await request(POLICY_URL);
  if (!result.ok) return result;
  const policy = toPolicy(result.body.policy);
  const digest = digestOf(result.body);
  if (policy === null || digest === undefined) return unreadable();
  const invalid =
    result.body.code === CODE_POLICY_INVALID
      ? {
          code: CODE_POLICY_INVALID,
          message: str(result.body.message, "The policy file is invalid."),
        }
      : null;
  return { ok: true, policy, invalid, digest };
}

/** Save `policy` over the file `digest` names (from the read or last save). */
export function saveUserPolicy(
  policy: UserPolicy,
  digest: PolicyDigest,
): Promise<PolicySave> {
  return put(policy, digest);
}

/**
 * Replace an invalid policy file with the engine's default agent limits.
 *
 * A schema "1" body carries no agent limits, so the engine fills in its own
 * defaults (it does for a file it reads as invalid) and the defaults live in
 * one place. Paper arming goes back to its default (allowed). Live and key
 * export are sent exactly as the engine reported them: this screen never
 * changes either. Like a save, it is refused if the file changed since
 * `digest` was read.
 */
export function resetUserPolicy(
  current: UserPolicy,
  digest: PolicyDigest,
): Promise<PolicySave> {
  return put(
    {
      schema_version: "1",
      allow_paper_arm: true,
      allow_live: current.allow_live,
      allow_key_export: current.allow_key_export,
    },
    digest,
  );
}

export async function loadMcpSetup(): Promise<McpSetupRead> {
  const result = await request(MCP_SETUP_URL);
  if (!result.ok) return result;
  const { command, args, env, claude_code: claudeCode } = result.body;
  if (
    typeof command !== "string" ||
    !Array.isArray(args) ||
    !args.every((arg) => typeof arg === "string") ||
    !env ||
    typeof env !== "object" ||
    !Object.values(env).every((value) => typeof value === "string") ||
    typeof claudeCode !== "string"
  ) {
    return unreadable();
  }
  return {
    ok: true,
    setup: {
      command,
      args: args as string[],
      env: env as Record<string, string>,
      claudeCode,
    },
  };
}
