/**
 * Client for Krell's loopback routes under `/api/v1/krell/` (M38.15).
 *
 * The streaming routes are POSTs behind the engine's state-change gate
 * (session cookie, `X-Krellbot-CSRF`, loopback origin) and answer with
 * server-sent events; every stream ends with `done`. A refusal before the
 * stream starts (gate, bad body) arrives as JSON and is turned into one
 * `error` event plus `done`, so callers handle a single event shape.
 *
 * A confirm card's ticket id travels only in the confirm request body.
 * This module never logs an event or puts one in an error message.
 */
import { getCsrf } from "../../session";

export type KrellMode = "standard" | "advanced";
export type KrellDecision = "confirm" | "decline";

/** The active model as the conversation route reports it (no secrets). */
export type KrellConnection = {
  id: string;
  label: string;
  host: string;
  model: string;
  capability: string;
};

export type KrellToolResult = Record<string, unknown>;

export type KrellEvent =
  | { type: "notice"; kind: string; [field: string]: unknown }
  | { type: "opening"; text: string; question: string; quick_replies: string[] }
  | { type: "budget"; [field: string]: unknown }
  | { type: "text_delta"; text: string }
  | { type: "message_done"; text: string }
  | { type: "usage"; input_tokens: number; output_tokens: number }
  | { type: "tool_start"; id: string; name: string; arguments: unknown }
  | { type: "tool_result"; id: string; name: string; result: KrellToolResult }
  | { type: "highlight"; test_id: string }
  | { type: "confirm_request"; id: string; confirm: Record<string, unknown> }
  | { type: "error"; code: string; message: string }
  | { type: "done" };

/** One stored conversation row: `role` is user, assistant, tool or notice. */
export type KrellStoredMessage = {
  id: number;
  role: string;
  payload: Record<string, unknown>;
};

export type KrellConversationBody = {
  messages: KrellStoredMessage[];
  connection: KrellConnection | null;
};

export type KrellRefusal = { ok: false; code: string; message: string };
export type KrellResult<T> = ({ ok: true } & T) | KrellRefusal;

export type KrellEventHandler = (event: KrellEvent) => void;

export type KrellClient = {
  conversation(): Promise<KrellResult<KrellConversationBody>>;
  clear(): Promise<KrellResult<object>>;
  start(onEvent: KrellEventHandler): Promise<void>;
  send(text: string, onEvent: KrellEventHandler): Promise<void>;
  confirm(ticketId: string, decision: KrellDecision, onEvent: KrellEventHandler): Promise<void>;
  setMode(mode: KrellMode, onEvent: KrellEventHandler): Promise<void>;
};

const BASE = "/api/v1/krell";
const GENERIC_MESSAGE = "Krellbot could not complete that request.";
const NETWORK_MESSAGE = "Krellbot could not reach the local engine.";
const CUT_MESSAGE = "The local engine stopped answering before Krell finished.";

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function dispatchFrame(frame: string, onEvent: KrellEventHandler): void {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  if (!data) return;
  let payload: unknown;
  try {
    payload = JSON.parse(data);
  } catch {
    return; // A malformed frame is skipped, never echoed.
  }
  if (isRecord(payload) && typeof payload.type === "string") {
    onEvent(payload as KrellEvent);
  }
}

const FRAME_END = /\r?\n\r?\n/;

/** Read a `text/event-stream` body frame by frame, calling `onEvent` per event. */
export async function readSse(response: Response, onEvent: KrellEventHandler): Promise<void> {
  if (!response.body) {
    for (const frame of (await response.text()).split(FRAME_END)) dispatchFrame(frame, onEvent);
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
    let match = FRAME_END.exec(buffer);
    while (match) {
      dispatchFrame(buffer.slice(0, match.index), onEvent);
      buffer = buffer.slice(match.index + match[0].length);
      match = FRAME_END.exec(buffer);
    }
    if (done) break;
  }
  dispatchFrame(buffer, onEvent);
}

/** A refusal built only from the server's code and detail. */
async function refusalOf(response: Response): Promise<KrellRefusal> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const record = isRecord(body) ? body : {};
  return {
    ok: false,
    code: str(record.code, `http_${response.status}`),
    message: str(record.detail, GENERIC_MESSAGE),
  };
}

function csrfHeaders(): Record<string, string> {
  return { "Content-Type": "application/json", "X-Krellbot-CSRF": getCsrf() };
}

async function stream(path: string, body: unknown, onEvent: KrellEventHandler): Promise<void> {
  let finished = false;
  const emit: KrellEventHandler = (event) => {
    if (finished) return;
    if (event.type === "done") finished = true;
    onEvent(event);
  };
  const fail = (code: string, message: string): void => {
    emit({ type: "error", code, message });
    emit({ type: "done" });
  };
  let response: Response;
  try {
    response = await fetch(`${BASE}/${path}`, {
      method: "POST",
      credentials: "include",
      headers: { ...csrfHeaders(), Accept: "text/event-stream" },
      body: JSON.stringify(body),
    });
  } catch {
    fail("network_error", NETWORK_MESSAGE);
    return;
  }
  const type = response.headers.get("Content-Type") ?? "";
  if (!response.ok || !type.startsWith("text/event-stream")) {
    const refusal = await refusalOf(response);
    fail(refusal.code, refusal.message);
    return;
  }
  try {
    await readSse(response, emit);
  } catch {
    // The connection dropped mid-stream; fall through to the cut notice.
  }
  if (!finished) fail("stream_interrupted", CUT_MESSAGE);
}

function toConnection(raw: unknown): KrellConnection | null {
  if (!isRecord(raw)) return null;
  return {
    id: str(raw.id),
    label: str(raw.label),
    host: str(raw.host),
    model: str(raw.model),
    capability: str(raw.capability),
  };
}

function toStored(raw: unknown): KrellStoredMessage | null {
  if (!isRecord(raw) || typeof raw.role !== "string" || !isRecord(raw.payload)) return null;
  return { id: typeof raw.id === "number" ? raw.id : 0, role: raw.role, payload: raw.payload };
}

export function createKrellClient(): KrellClient {
  return {
    async conversation() {
      try {
        const response = await fetch(`${BASE}/conversation`, { credentials: "include" });
        if (!response.ok) return refusalOf(response);
        const body: unknown = await response.json();
        const record = isRecord(body) ? body : {};
        if (record.ok === false) return refusalOf(response);
        const rows = Array.isArray(record.messages) ? record.messages : [];
        return {
          ok: true,
          messages: rows.map(toStored).filter((m): m is KrellStoredMessage => m !== null),
          connection: toConnection(record.connection),
        };
      } catch {
        return { ok: false, code: "network_error", message: NETWORK_MESSAGE };
      }
    },
    async clear() {
      try {
        const response = await fetch(`${BASE}/conversation`, {
          method: "DELETE",
          credentials: "include",
          headers: csrfHeaders(),
        });
        return response.ok ? { ok: true } : refusalOf(response);
      } catch {
        return { ok: false, code: "network_error", message: NETWORK_MESSAGE };
      }
    },
    start: (onEvent) => stream("start", {}, onEvent),
    send: (text, onEvent) => stream("messages", { text }, onEvent),
    confirm: (ticketId, decision, onEvent) =>
      stream("confirm", { ticket_id: ticketId, decision }, onEvent),
    setMode: (mode, onEvent) => stream("profile", { mode }, onEvent),
  };
}
