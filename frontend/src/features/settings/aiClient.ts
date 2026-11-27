/**
 * Client for the loopback `/api/v1/ai/` routes (M38.12).
 *
 * The engine never returns a stored key. The key typed into the connect form
 * is sent once in the add request body; this module never logs it and never
 * puts it in an error message or a refusal.
 */
import { getCsrf } from "../../session";

export type AiCapability = "tools" | "chat_only" | "failed" | "unprobed";
export type AiFormat = "openai_compatible" | "anthropic_messages";
export type AiSignin = "none" | "openrouter" | "chatgpt";

/** A saved ChatGPT account with no connection now: "Continue as" reuses its registration. */
export type AiChatgptAccount = { id: string; email: string | null };

export type AiProvider = {
  id: string;
  label: string;
  format: AiFormat;
  baseUrl: string | null;
  custom: boolean;
  credential: "required" | "optional";
  /** A browser sign-in the engine offers for this provider, besides a key. */
  signin: AiSignin;
  /** ChatGPT sign-in only: saved accounts to continue as. */
  chatgptAccounts: AiChatgptAccount[];
};

/**
 * A ChatGPT sign-in's state (M38.23), from the engine's non-secret record:
 * never a token. `welcome` is true until "You're using your ChatGPT plan"
 * was shown once for this connection.
 */
export type AiChatgptState = {
  plan: "enabled" | "disabled";
  signedIn: boolean;
  welcome: boolean;
  account: string | null;
};

export type AiConnection = {
  id: string;
  preset: string;
  label: string;
  format: AiFormat;
  baseUrl: string;
  host: string;
  model: string;
  auth: string;
  capability: AiCapability;
  active: boolean;
  disclosure: string;
  /** Set only for a Sign in with ChatGPT connection. */
  chatgpt: AiChatgptState | null;
};

export type AiProbe = {
  capability: AiCapability;
  message: string;
};

export type AiRefusal = { ok: false; code: string; message: string };
export type AiResult<T> = ({ ok: true } & T) | AiRefusal;

export type AiSigninState = "waiting" | "connected" | "failed" | "expired";

/** A sign-in's state. `failed` and `expired` carry the server's code and fixed message. */
export type AiSigninStatus = {
  state: AiSigninState;
  connectionId: string | null;
  /** ChatGPT only: whether the sign-in allowed ChatGPT plan use. */
  plan: "enabled" | "disabled" | null;
  code: string;
  message: string;
};

/** How OpenAI answered a ChatGPT sign-out or removal's revocation. */
export type AiRevocation = "confirmed" | "not_confirmed" | "none";

export type AiAddInput = {
  preset: string;
  model: string;
  credential?: string;
  baseUrl?: string;
};

const JSON_HEADERS = { "Content-Type": "application/json" };
const GENERIC_MESSAGE = "Krellbot could not complete that request.";

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asCapability(value: unknown): AiCapability {
  return value === "tools" || value === "chat_only" || value === "failed"
    ? value
    : "unprobed";
}

function asFormat(value: unknown): AiFormat {
  return value === "anthropic_messages" ? value : "openai_compatible";
}

function asSignin(value: unknown): AiSignin {
  return value === "openrouter" || value === "chatgpt" ? value : "none";
}

function asSigninState(value: unknown): AiSigninState {
  return value === "waiting" || value === "connected" || value === "expired"
    ? value
    : "failed";
}

function toChatgpt(raw: unknown): AiChatgptState | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  return {
    plan: r.plan === "enabled" ? "enabled" : "disabled",
    signedIn: r.signed_in === true,
    welcome: r.welcome === true,
    account: typeof r.account === "string" ? r.account : null,
  };
}

function toAccounts(raw: unknown): AiChatgptAccount[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item) => (item ?? {}) as Record<string, unknown>)
    .filter((r) => typeof r.id === "string" && /^[0-9a-f]{16}$/.test(r.id))
    .map((r) => ({ id: String(r.id), email: typeof r.email === "string" ? r.email : null }));
}

function asPlan(value: unknown): "enabled" | "disabled" | null {
  return value === "enabled" || value === "disabled" ? value : null;
}

function asRevocation(value: unknown): AiRevocation {
  return value === "confirmed" || value === "not_confirmed" ? value : "none";
}

function toConnection(raw: unknown): AiConnection {
  const r = (raw ?? {}) as Record<string, unknown>;
  return {
    id: str(r.id),
    preset: str(r.preset),
    label: str(r.label),
    format: asFormat(r.format),
    baseUrl: str(r.base_url),
    host: str(r.host),
    model: str(r.model),
    auth: str(r.auth),
    capability: asCapability(r.capability),
    active: r.active === true,
    disclosure: str(r.disclosure),
    chatgpt: toChatgpt(r.chatgpt),
  };
}

function toProbe(raw: unknown): AiProbe {
  const r = (raw ?? {}) as Record<string, unknown>;
  return { capability: asCapability(r.capability), message: str(r.message) };
}

/** Parsed body, or a refusal built only from the server's code and detail. */
async function parse(response: Response): Promise<Record<string, unknown>> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const record = (body && typeof body === "object" ? body : {}) as Record<
    string,
    unknown
  >;
  if (!response.ok || record.ok === false) {
    return {
      ok: false,
      code: str(record.code, `http_${response.status}`),
      message: str(record.detail, GENERIC_MESSAGE),
    };
  }
  return record;
}

function refusal(parsed: Record<string, unknown>): AiRefusal {
  return {
    ok: false,
    code: str(parsed.code, "unknown"),
    message: str(parsed.message, GENERIC_MESSAGE),
  };
}

function networkFailure(): AiRefusal {
  return {
    ok: false,
    code: "network_error",
    message: "Krellbot could not reach the local engine.",
  };
}

async function getJson(url: string): Promise<Record<string, unknown>> {
  const response = await fetch(url, { credentials: "include" });
  return parse(response);
}

async function send(
  method: "POST" | "DELETE",
  url: string,
  body?: unknown,
): Promise<Record<string, unknown>> {
  const response = await fetch(url, {
    method,
    credentials: "include",
    headers: { ...JSON_HEADERS, "X-Krellbot-CSRF": getCsrf() },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return parse(response);
}

export async function listProviders(): Promise<
  AiResult<{ providers: AiProvider[] }>
> {
  try {
    const parsed = await getJson("/api/v1/ai/providers");
    if (parsed.ok === false) return refusal(parsed);
    const rows = Array.isArray(parsed.providers) ? parsed.providers : [];
    return {
      ok: true,
      providers: rows.map((raw) => {
        const r = raw as Record<string, unknown>;
        return {
          id: str(r.id),
          label: str(r.label),
          format: asFormat(r.format),
          baseUrl: typeof r.base_url === "string" ? r.base_url : null,
          custom: r.custom === true,
          credential: r.credential === "optional" ? "optional" : "required",
          signin: asSignin(r.signin),
          chatgptAccounts: toAccounts(r.chatgpt_accounts),
        };
      }),
    };
  } catch {
    return networkFailure();
  }
}

export async function listConnections(): Promise<
  AiResult<{ connections: AiConnection[]; active: string | null }>
> {
  try {
    const parsed = await getJson("/api/v1/ai/connections");
    if (parsed.ok === false) return refusal(parsed);
    const rows = Array.isArray(parsed.connections) ? parsed.connections : [];
    return {
      ok: true,
      connections: rows.map(toConnection),
      active: typeof parsed.active === "string" ? parsed.active : null,
    };
  } catch {
    return networkFailure();
  }
}

/** Adds and probes. Does not activate. */
export async function addConnection(
  input: AiAddInput,
): Promise<AiResult<{ connection: AiConnection; probe: AiProbe }>> {
  const body: Record<string, string> = {
    preset: input.preset,
    model: input.model,
  };
  if (input.credential) body.credential = input.credential;
  if (input.baseUrl) body.base_url = input.baseUrl;
  try {
    const parsed = await send("POST", "/api/v1/ai/connections", body);
    if (parsed.ok === false) return refusal(parsed);
    return {
      ok: true,
      connection: toConnection(parsed.connection),
      probe: toProbe(parsed.probe),
    };
  } catch {
    return networkFailure();
  }
}

/**
 * Removes a connection. A ChatGPT sign-in is revoked at OpenAI first;
 * `detail` explains a revocation OpenAI did not confirm.
 */
export async function removeConnection(
  id: string,
): Promise<AiResult<{ revocation: AiRevocation; detail: string }>> {
  try {
    const parsed = await send(
      "DELETE",
      `/api/v1/ai/connections/${encodeURIComponent(id)}`,
    );
    if (parsed.ok === false) return refusal(parsed);
    return {
      ok: true,
      revocation: asRevocation(parsed.revocation),
      detail: str(parsed.detail),
    };
  } catch {
    return networkFailure();
  }
}

/** Sign a ChatGPT connection out: the engine revokes it at OpenAI and clears its tokens. */
export async function signOutConnection(
  id: string,
): Promise<AiResult<{ revocation: AiRevocation; detail: string }>> {
  try {
    const parsed = await send(
      "POST",
      `/api/v1/ai/connections/${encodeURIComponent(id)}/signout`,
    );
    if (parsed.ok === false) return refusal(parsed);
    return {
      ok: true,
      revocation: asRevocation(parsed.revocation),
      detail: str(parsed.detail),
    };
  } catch {
    return networkFailure();
  }
}

/** Record in the engine that this connection's ChatGPT plan welcome was shown. */
export async function markChatgptWelcome(id: string): Promise<AiResult<unknown>> {
  try {
    const parsed = await send(
      "POST",
      `/api/v1/ai/connections/${encodeURIComponent(id)}/welcome`,
    );
    return parsed.ok === false ? refusal(parsed) : { ok: true };
  } catch {
    return networkFailure();
  }
}

export async function activateConnection(
  id: string,
): Promise<AiResult<{ connection: AiConnection }>> {
  try {
    const parsed = await send(
      "POST",
      `/api/v1/ai/connections/${encodeURIComponent(id)}/activate`,
    );
    if (parsed.ok === false) return refusal(parsed);
    return { ok: true, connection: toConnection(parsed.connection) };
  } catch {
    return networkFailure();
  }
}

export async function probeConnection(
  id: string,
): Promise<AiResult<{ connection: AiConnection; probe: AiProbe }>> {
  try {
    const parsed = await send(
      "POST",
      `/api/v1/ai/connections/${encodeURIComponent(id)}/probe`,
    );
    if (parsed.ok === false) return refusal(parsed);
    return {
      ok: true,
      connection: toConnection(parsed.connection),
      probe: toProbe(parsed.probe),
    };
  } catch {
    return networkFailure();
  }
}

/**
 * Start Sign in with OpenRouter. The engine opens a one-shot listener on
 * 127.0.0.1 and returns the OpenRouter page to open; the key it receives
 * goes straight to the keychain and never reaches the browser.
 */
export async function startOpenRouterSignin(
  model: string,
): Promise<AiResult<{ flowId: string; authorizeUrl: string }>> {
  try {
    const parsed = await send("POST", "/api/v1/ai/signin/openrouter", { model });
    if (parsed.ok === false) return refusal(parsed);
    return {
      ok: true,
      flowId: str(parsed.flow_id),
      authorizeUrl: str(parsed.authorize_url),
    };
  } catch {
    return networkFailure();
  }
}

/**
 * Start Continue with ChatGPT (M38.23): a new sign-in with `model`, or a
 * sign-in again for an existing ChatGPT connection. The engine returns its
 * own loopback launch address, which redirects to OpenAI; every token goes
 * straight to the keychain and never reaches the browser.
 */
export async function startChatgptSignin(
  input: { model: string; account?: string } | { connectionId: string },
): Promise<AiResult<{ flowId: string; authorizeUrl: string }>> {
  const body =
    "connectionId" in input
      ? { connection_id: input.connectionId }
      : input.account
        ? { model: input.model, account: input.account }
        : { model: input.model };
  try {
    const parsed = await send("POST", "/api/v1/ai/signin/chatgpt", body);
    if (parsed.ok === false) return refusal(parsed);
    return {
      ok: true,
      flowId: str(parsed.flow_id),
      authorizeUrl: str(parsed.authorize_url),
    };
  } catch {
    return networkFailure();
  }
}

export async function getSigninStatus(
  flowId: string,
): Promise<AiResult<{ status: AiSigninStatus }>> {
  try {
    const parsed = await getJson(
      `/api/v1/ai/signin/${encodeURIComponent(flowId)}`,
    );
    if (parsed.ok === false) return refusal(parsed);
    return { ok: true, status: toSigninStatus(parsed) };
  } catch {
    return networkFailure();
  }
}

/**
 * Cancel a sign-in still waiting for OpenRouter, so it can never add a
 * connection later. One whose callback already arrived is left to finish:
 * `cancelled` is false and `status` says where it is.
 */
export async function cancelSignin(
  flowId: string,
): Promise<AiResult<{ cancelled: boolean; status: AiSigninStatus }>> {
  try {
    const parsed = await send(
      "DELETE",
      `/api/v1/ai/signin/${encodeURIComponent(flowId)}`,
    );
    if (parsed.ok === false) return refusal(parsed);
    return {
      ok: true,
      cancelled: parsed.cancelled === true,
      status: toSigninStatus(parsed),
    };
  } catch {
    return networkFailure();
  }
}

function toSigninStatus(parsed: Record<string, unknown>): AiSigninStatus {
  return {
    state: asSigninState(parsed.state),
    connectionId:
      typeof parsed.connection_id === "string" ? parsed.connection_id : null,
    plan: asPlan(parsed.plan),
    code: str(parsed.code),
    message: str(parsed.detail, GENERIC_MESSAGE),
  };
}
