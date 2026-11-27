/**
 * One shared Krell conversation for the Krell tab and the side panel
 * (M38.16).
 *
 * The provider sits at the shell root and reads nothing until `active`
 * first turns true (the tab or the panel is shown), so the rest of
 * Krellbot makes no Krell requests. It loads the stored thread once, asks
 * the engine for the disclosure and opening when they are missing, and
 * turns streamed events into display items.
 *
 * Confirm ticket ids never enter the display items: each one is kept in
 * a private map keyed by its tool call id, and `confirm(callId, …)` sends
 * it only in the confirm request body. Nothing here logs an event.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type JSX,
  type ReactNode,
} from "react";
import {
  createKrellClient,
  type KrellClient,
  type KrellConnection,
  type KrellDecision,
  type KrellEvent,
  type KrellEventHandler,
  type KrellMode,
  type KrellStoredMessage,
  type KrellToolResult,
} from "./client";

export type KrellItem =
  | { key: string; kind: "user"; text: string }
  | { key: string; kind: "krell"; text: string; question?: string; streaming: boolean }
  | { key: string; kind: "notice"; text: string }
  | { key: string; kind: "error"; code: string; text: string }
  | { key: string; kind: "tool"; callId: string; name: string; result: KrellToolResult | null }
  | { key: string; kind: "confirm"; callId: string; tool: string; confirm: Record<string, unknown> }
  | { key: string; kind: "decision"; decision: KrellDecision; tool: string };

export type KrellStatus = "idle" | "loading" | "ready" | "failed";

export type KrellContextValue = {
  messages: KrellItem[];
  /** A Krell stream (or a clear) is running; one at a time. */
  pending: boolean;
  /** The active model when Krell can use it (`tools` or `chat_only`), else null. */
  connection: KrellConnection | null;
  status: KrellStatus;
  loadError: string | null;
  send(text: string): void;
  confirm(callId: string, decision: KrellDecision): void;
  setMode(mode: KrellMode): void;
  clear(): void;
  /** Read the conversation again, e.g. after a model was connected. */
  reload(): void;
};

const CODE_NO_MODEL = "krell_no_model";
const CODE_BUSY = "krell_busy";
const CODE_CONFIRM_UNKNOWN = "agent_confirm_unknown";
const MESSAGE_BUSY = "Krell is already answering. Wait for that answer to finish.";
const MESSAGE_CONFIRM_UNKNOWN = "That card is no longer active, so nothing was sent.";
const MODE_REPLIES: Record<KrellMode, string> = { standard: "Standard", advanced: "Advanced" };
const TICKET_KEYS = new Set(["ticket_id", "confirm_ticket"]);

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A copy without any `ticket_id` or `confirm_ticket` key, at any depth. */
function withoutTickets<T>(value: T): T {
  if (Array.isArray(value)) return value.map(withoutTickets) as T;
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !TICKET_KEYS.has(key))
        .map(([key, item]) => [key, withoutTickets(item)]),
    ) as T;
  }
  return value;
}

function usable(conn: KrellConnection | null): KrellConnection | null {
  return conn && (conn.capability === "tools" || conn.capability === "chat_only") ? conn : null;
}

function asDecision(value: unknown): KrellDecision {
  return value === "confirm" ? "confirm" : "decline";
}

function confirmOf(result: unknown): Record<string, unknown> | null {
  if (!isRecord(result) || result.effect !== "needs_confirm") return null;
  return isRecord(result.confirm) ? result.confirm : null;
}

function toolItems(key: string, callId: string, name: string, result: unknown): KrellItem[] {
  const items: KrellItem[] = [
    { key, kind: "tool", callId, name, result: isRecord(result) ? withoutTickets(result) : null },
  ];
  const confirm = confirmOf(result);
  if (confirm) {
    items.push({
      key: `${key}-confirm`,
      kind: "confirm",
      callId,
      tool: str(confirm.tool, name),
      confirm: withoutTickets(confirm),
    });
  }
  return items;
}

/** The stored thread as display items (what a reload shows). */
function itemsFromHistory(messages: KrellStoredMessage[]): KrellItem[] {
  const items: KrellItem[] = [];
  for (const message of messages) {
    const p = message.payload;
    const key = `h${message.id}`;
    if (message.role === "user") {
      items.push({ key, kind: "user", text: str(p.text) });
    } else if (message.role === "assistant") {
      const text = str(p.text);
      if (text) items.push({ key, kind: "krell", text, streaming: false });
    } else if (message.role === "tool") {
      items.push(...toolItems(key, str(p.tool_call_id), str(p.name), p.result));
    } else if (message.role === "notice") {
      if (p.kind === "opening") {
        items.push({ key, kind: "krell", text: str(p.text), question: str(p.question), streaming: false });
      } else if (p.kind === "disclosure") {
        items.push({ key, kind: "notice", text: str(p.text) });
      } else if (p.kind === "error") {
        items.push({ key, kind: "error", code: str(p.code), text: str(p.message) });
      } else if (p.kind === "confirm_decision") {
        const tool = str(p.tool);
        items.push({ key, kind: "decision", decision: asDecision(p.decision), tool });
        if (isRecord(p.result)) items.push(...toolItems(`${key}-result`, str(p.call_id), tool, p.result));
      }
    }
  }
  return items;
}

/** Every confirm ticket a stored card carries, keyed by its tool call id. */
function ticketsFromHistory(messages: KrellStoredMessage[]): Map<string, string> {
  const tickets = new Map<string, string>();
  for (const message of messages) {
    if (message.role !== "tool") continue;
    const ticket = confirmOf(message.payload.result)?.ticket_id;
    if (typeof ticket === "string" && ticket) tickets.set(str(message.payload.tool_call_id), ticket);
  }
  return tickets;
}

function needsStart(messages: KrellStoredMessage[], connectionId: string): boolean {
  const notices = messages.filter((m) => m.role === "notice").map((m) => m.payload);
  const opened = notices.some((p) => p.kind === "opening");
  const disclosed = notices.some(
    (p) => p.kind === "disclosure" && isRecord(p.connection) && p.connection.id === connectionId,
  );
  return !opened || !disclosed;
}

/** Close a streaming Krell message; drop it if nothing arrived. */
function settle(items: KrellItem[], finalText?: string): KrellItem[] {
  const last = items[items.length - 1];
  if (last?.kind === "krell" && last.streaming) {
    const text = finalText || last.text;
    const rest = items.slice(0, -1);
    return text ? [...rest, { ...last, text, streaming: false }] : rest;
  }
  return items;
}

function applyEvent(items: KrellItem[], event: KrellEvent, key: string): KrellItem[] {
  switch (event.type) {
    case "notice":
      if (event.kind === "disclosure") return [...items, { key, kind: "notice", text: str(event.text) }];
      if (event.kind === "confirm_decision") {
        return [...items, { key, kind: "decision", decision: asDecision(event.decision), tool: str(event.tool) }];
      }
      return items;
    case "opening":
      return [...items, { key, kind: "krell", text: event.text, question: event.question, streaming: false }];
    case "text_delta": {
      const last = items[items.length - 1];
      if (last?.kind === "krell" && last.streaming) {
        return [...items.slice(0, -1), { ...last, text: last.text + event.text }];
      }
      return [...items, { key, kind: "krell", text: event.text, streaming: true }];
    }
    case "message_done": {
      const last = items[items.length - 1];
      if (last?.kind === "krell" && last.streaming) return settle(items, event.text);
      return event.text ? [...items, { key, kind: "krell", text: event.text, streaming: false }] : items;
    }
    case "tool_start":
      return [...settle(items), { key, kind: "tool", callId: event.id, name: event.name, result: null }];
    case "tool_result": {
      let index = items.length - 1;
      while (index >= 0) {
        const item = items[index];
        if (item.kind === "tool" && item.callId === event.id && item.result === null) break;
        index -= 1;
      }
      const added = toolItems(key, event.id, event.name, event.result);
      if (index < 0) return [...items, added[0]];
      return items.map((item, i) => (i === index ? { ...added[0], key: item.key } : item));
    }
    case "confirm_request":
      return [
        ...items,
        {
          key,
          kind: "confirm",
          callId: event.id,
          tool: str(event.confirm.tool),
          confirm: withoutTickets(event.confirm),
        },
      ];
    case "error":
      return [...settle(items), { key, kind: "error", code: event.code, text: event.message }];
    case "done":
      return settle(items);
    default:
      // budget, usage, highlight: the budget meter and highlights come in M38.17.
      return items;
  }
}

type State = {
  status: KrellStatus;
  loadError: string | null;
  connection: KrellConnection | null;
  messages: KrellItem[];
  pending: boolean;
};

type Action =
  | { type: "loading" }
  | { type: "loaded"; messages: KrellItem[]; connection: KrellConnection | null }
  | { type: "load_failed"; message: string }
  | { type: "pending"; value: boolean }
  | { type: "add"; item: KrellItem }
  | { type: "event"; event: KrellEvent; key: string }
  | { type: "cleared" };

const INITIAL: State = { status: "idle", loadError: null, connection: null, messages: [], pending: false };

function reduce(state: State, action: Action): State {
  switch (action.type) {
    case "loading":
      return { ...state, status: "loading", loadError: null };
    case "loaded":
      return { ...state, status: "ready", messages: action.messages, connection: usable(action.connection) };
    case "load_failed":
      return { ...state, status: "failed", loadError: action.message };
    case "pending":
      return { ...state, pending: action.value };
    case "add":
      return { ...state, messages: [...state.messages, action.item] };
    case "event": {
      const messages = applyEvent(state.messages, action.event, action.key);
      const noModel = action.event.type === "error" && action.event.code === CODE_NO_MODEL;
      return { ...state, messages, connection: noModel ? null : state.connection };
    }
    case "cleared":
      return { ...state, messages: [] };
  }
}

const KrellContext = createContext<KrellContextValue | null>(null);

export type KrellConversationProviderProps = {
  /** True while the Krell tab or the side panel is shown; the first true loads. */
  active: boolean;
  children: ReactNode;
  client?: KrellClient;
};

export function KrellConversationProvider({
  active,
  children,
  client,
}: KrellConversationProviderProps): JSX.Element {
  const api = useMemo(() => client ?? createKrellClient(), [client]);
  const [state, dispatch] = useReducer(reduce, INITIAL);
  const tickets = useRef(new Map<string, string>());
  const busy = useRef(false);
  const requested = useRef(false);
  const keys = useRef(0);
  const nextKey = useCallback((): string => {
    keys.current += 1;
    return `k${keys.current}`;
  }, []);

  const onEvent = useCallback<KrellEventHandler>(
    (event) => {
      if (event.type === "confirm_request") {
        const ticket = event.confirm.ticket_id;
        if (typeof ticket === "string" && ticket) tickets.current.set(event.id, ticket);
      }
      dispatch({ type: "event", event, key: nextKey() });
    },
    [nextKey],
  );

  /** Run one Krell stream; refused locally while another runs. */
  const run = useCallback(
    async (open: (handler: KrellEventHandler) => Promise<void>, before?: () => void): Promise<void> => {
      if (busy.current) {
        dispatch({ type: "add", item: { key: nextKey(), kind: "error", code: CODE_BUSY, text: MESSAGE_BUSY } });
        return;
      }
      busy.current = true;
      before?.();
      dispatch({ type: "pending", value: true });
      try {
        await open(onEvent);
      } finally {
        busy.current = false;
        dispatch({ type: "pending", value: false });
      }
    },
    [nextKey, onEvent],
  );

  const load = useCallback(async (): Promise<void> => {
    dispatch({ type: "loading" });
    const result = await api.conversation();
    if (!result.ok) {
      dispatch({ type: "load_failed", message: `${result.message} (${result.code})` });
      return;
    }
    tickets.current = ticketsFromHistory(result.messages);
    dispatch({ type: "loaded", messages: itemsFromHistory(result.messages), connection: result.connection });
    const conn = usable(result.connection);
    if (conn && needsStart(result.messages, conn.id)) await run((h) => api.start(h));
  }, [api, run]);

  useEffect(() => {
    if (active && !requested.current) {
      requested.current = true;
      void load();
    }
  }, [active, load]);

  const send = useCallback(
    (text: string): void => {
      const trimmed = text.trim();
      if (!trimmed) return;
      void run(
        (h) => api.send(trimmed, h),
        () => dispatch({ type: "add", item: { key: nextKey(), kind: "user", text: trimmed } }),
      );
    },
    [api, nextKey, run],
  );

  const confirm = useCallback(
    (callId: string, decision: KrellDecision): void => {
      const ticket = tickets.current.get(callId);
      if (!ticket) {
        dispatch({
          type: "add",
          item: { key: nextKey(), kind: "error", code: CODE_CONFIRM_UNKNOWN, text: MESSAGE_CONFIRM_UNKNOWN },
        });
        return;
      }
      void run((h) => api.confirm(ticket, decision, h));
    },
    [api, nextKey, run],
  );

  const setMode = useCallback(
    (mode: KrellMode): void => {
      void run(
        (h) => api.setMode(mode, h),
        () => dispatch({ type: "add", item: { key: nextKey(), kind: "user", text: MODE_REPLIES[mode] } }),
      );
    },
    [api, nextKey, run],
  );

  const clear = useCallback((): void => {
    void (async () => {
      let cleared = false;
      await run(async (handler) => {
        const result = await api.clear();
        if (!result.ok) {
          handler({ type: "error", code: result.code, message: result.message });
          return;
        }
        tickets.current.clear();
        dispatch({ type: "cleared" });
        cleared = true;
      });
      // A fresh thread: the engine shows the disclosure and opening again.
      if (cleared && state.connection) await run((h) => api.start(h));
    })();
  }, [api, run, state.connection]);

  const reload = useCallback((): void => {
    requested.current = true;
    void load();
  }, [load]);

  const value = useMemo<KrellContextValue>(
    () => ({
      messages: state.messages,
      pending: state.pending,
      connection: state.connection,
      status: state.status,
      loadError: state.loadError,
      send,
      confirm,
      setMode,
      clear,
      reload,
    }),
    [state, send, confirm, setMode, clear, reload],
  );

  return <KrellContext.Provider value={value}>{children}</KrellContext.Provider>;
}

export function useKrell(): KrellContextValue {
  const value = useContext(KrellContext);
  if (!value) throw new Error("useKrell needs a KrellConversationProvider");
  return value;
}
