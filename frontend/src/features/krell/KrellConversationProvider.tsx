/**
 * One shared Krell conversation for the Krell tab and the side panel
 * (M38.16).
 *
 * The provider sits at the shell root and reads nothing until a Krell
 * `surface` (the tab or the panel) is first shown, so the rest of
 * Krellbot makes no Krell requests. It loads the stored thread, asks
 * the engine for the disclosure and opening when they are missing, and
 * turns streamed events into display items.
 *
 * Confirm ticket ids never enter the display items: each one is kept in
 * a private map keyed by its tool call id, and `confirm(callId, …)` sends
 * it only in the confirm request body. Nothing here logs an event.
 *
 * M38.17: decisions (and an `agent_confirm_unknown` answer to a press)
 * carry their card's call id, so each confirm card can show what the engine
 * decided; the token budget and Krell's highlights are kept here too.
 *
 * M38.18: the stored mode is kept here as the engine reports it: from the
 * conversation read, a mode press the engine took, or a `set_profile` result
 * that ran. Krell's words alone never change it.
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
  MODE_REPLIES,
  createKrellClient,
  toBudget,
  toMode,
  type KrellBudget,
  type KrellClient,
  type KrellConnection,
  type KrellDecision,
  type KrellEvent,
  type KrellEventHandler,
  type KrellMode,
  type KrellStoredMessage,
  type KrellToolResult,
} from "./client";
import { useKrellHighlight } from "./useKrellHighlight";

export type KrellItem =
  /** `sending`: shown at once, not yet answered by the engine. */
  | { key: string; kind: "user"; text: string; sending?: boolean }
  | { key: string; kind: "krell"; text: string; question?: string; streaming: boolean }
  | { key: string; kind: "notice"; text: string }
  /** `callId`: the confirm card whose press this error answered. */
  | { key: string; kind: "error"; code: string; text: string; callId?: string }
  /** `ended`: the stream that started the call has ended (with no result, it may or may not have run). */
  | { key: string; kind: "tool"; callId: string; name: string; result: KrellToolResult | null; ended?: boolean }
  | { key: string; kind: "confirm"; callId: string; tool: string; confirm: Record<string, unknown> }
  /** `callId`: the confirm card decided on ("" when it cannot be told). `ended`: its stream has ended. */
  | { key: string; kind: "decision"; callId: string; decision: KrellDecision; tool: string; ended?: boolean };

export type KrellStatus = "idle" | "loading" | "ready" | "failed";

export type KrellContextValue = {
  messages: KrellItem[];
  /** A Krell stream (or a clear) is running; one at a time. */
  pending: boolean;
  /** The active model when Krell can use it (`tools` or `chat_only`), else null. */
  connection: KrellConnection | null;
  /** Krell's token budget, once the engine has reported it. */
  budget: KrellBudget | null;
  /** The stored mode; null until one is chosen (the quick replies show until then). */
  mode: KrellMode | null;
  status: KrellStatus;
  loadError: string | null;
  /** The composer's text, shared by the tab and the panel. */
  draft: string;
  setDraft(text: string): void;
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
  // Not now is stored with its ticket but no call id: find its card by ticket here.
  const cardOf = new Map(Array.from(ticketsFromHistory(messages), ([callId, ticket]) => [ticket, callId]));
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
        const callId = str(p.call_id) || cardOf.get(str(p.ticket_id)) || "";
        items.push({ key, kind: "decision", callId, decision: asDecision(p.decision), tool, ended: true });
        if (isRecord(p.result)) items.push(...toolItems(`${key}-result`, callId, tool, p.result));
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

type UserItem = Extract<KrellItem, { kind: "user" }>;

function isSending(item: KrellItem | undefined): item is UserItem {
  return item?.kind === "user" && item.sending === true;
}

/** `callId`: set for the events of a press on that confirm card. */
function applyEvent(items: KrellItem[], event: KrellEvent, key: string, callId?: string): KrellItem[] {
  const last = items[items.length - 1];
  if (isSending(last) && event.type !== "error") {
    // Any answer but an error means the engine took the message. A new
    // disclosure comes just before the engine stores it, so show the
    // disclosure first, as a reload does.
    const taken: KrellItem = { ...last, sending: false };
    if (event.type === "notice" && event.kind === "disclosure") {
      return [...items.slice(0, -1), { key, kind: "notice", text: str(event.text) }, taken];
    }
    items = [...items.slice(0, -1), taken];
  }
  switch (event.type) {
    case "notice":
      if (event.kind === "disclosure") return [...items, { key, kind: "notice", text: str(event.text) }];
      if (event.kind === "confirm_decision") {
        return [
          ...items,
          { key, kind: "decision", callId: callId ?? "", decision: asDecision(event.decision), tool: str(event.tool) },
        ];
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
      return [...settle(items), { key, kind: "error", code: event.code, text: event.message, callId }];
    case "done":
      return settle(items);
    default:
      // budget, usage, highlight: kept in the provider's state, not shown as items.
      return items;
  }
}

type State = {
  status: KrellStatus;
  loadError: string | null;
  connection: KrellConnection | null;
  budget: KrellBudget | null;
  mode: KrellMode | null;
  /** The latest highlight event; `nonce` tells a repeat of the same test id apart. */
  highlight: { testId: string; nonce: number } | null;
  messages: KrellItem[];
  pending: boolean;
  draft: string;
};

/** What a conversation read sets. */
type Thread = {
  messages: KrellItem[];
  connection: KrellConnection | null;
  budget: KrellBudget | null;
  mode: KrellMode | null;
};

type Action =
  | { type: "loading" }
  | ({ type: "loaded" } & Thread)
  | { type: "load_failed"; message: string }
  | { type: "pending"; value: boolean }
  | { type: "add"; item: KrellItem }
  | { type: "event"; event: KrellEvent; key: string; callId?: string }
  | { type: "cleared" }
  | { type: "draft"; value: string; ifEmpty?: boolean }
  /** The stored thread, read again after a turn whose first answer was an error. */
  | ({ type: "resync" } & Thread)
  /** Drop the unanswered message (the thread could not be read to check it). */
  | { type: "unsend" }
  /** The engine took a mode press, so it stored that mode. */
  | { type: "mode"; mode: KrellMode };

const INITIAL: State = {
  status: "idle",
  loadError: null,
  connection: null,
  budget: null,
  mode: null,
  highlight: null,
  messages: [],
  pending: false,
  draft: "",
};

/**
 * A stream has ended: no result can still arrive for its calls or its
 * Confirm, so cards stop saying they are running.
 */
function endStream(items: KrellItem[]): KrellItem[] {
  const open = (item: KrellItem): boolean =>
    (item.kind === "tool" && item.result === null && !item.ended) || (item.kind === "decision" && !item.ended);
  return items.some(open) ? items.map((item) => (open(item) ? { ...item, ended: true } : item)) : items;
}

/** The mode a `set_profile` result stored, when the call ran and names one. */
function storedMode(event: KrellEvent): KrellMode | null {
  if (event.type !== "tool_result" || event.name !== "set_profile" || event.result.ok !== true) return null;
  const profile = event.result.profile;
  return isRecord(profile) ? toMode(profile.mode) : null;
}

/** What an event changes besides the display items: the budget, the highlight and the mode. */
function sideState(state: State, event: KrellEvent): Partial<Pick<State, "budget" | "highlight" | "mode">> {
  if (event.type === "budget") return { budget: toBudget(event) ?? state.budget };
  if (event.type === "highlight" && typeof event.test_id === "string") {
    return { highlight: { testId: event.test_id, nonce: (state.highlight?.nonce ?? 0) + 1 } };
  }
  const mode = storedMode(event);
  return mode ? { mode } : {};
}

function reduce(state: State, action: Action): State {
  switch (action.type) {
    case "loading":
      // A re-read keeps what is shown (say the connect form and its check
      // result) until the new read lands.
      return state.status === "ready" ? state : { ...state, status: "loading", loadError: null };
    case "loaded":
      return {
        ...state,
        status: "ready",
        messages: action.messages,
        connection: usable(action.connection),
        budget: action.budget ?? state.budget,
        mode: action.mode,
      };
    case "load_failed":
      return { ...state, status: "failed", loadError: action.message };
    case "pending":
      return { ...state, pending: action.value, messages: action.value ? state.messages : endStream(state.messages) };
    case "add":
      return { ...state, messages: [...state.messages, action.item] };
    case "event": {
      const messages = applyEvent(state.messages, action.event, action.key, action.callId);
      const noModel = action.event.type === "error" && action.event.code === CODE_NO_MODEL;
      return { ...state, ...sideState(state, action.event), messages, connection: noModel ? null : state.connection };
    }
    case "cleared":
      // Clearing forgets the profile too, so the mode question is open again.
      return { ...state, messages: [], mode: null };
    case "draft":
      return { ...state, draft: action.ifEmpty && state.draft ? state.draft : action.value };
    case "resync":
      return {
        ...state,
        messages: action.messages,
        connection: usable(action.connection),
        budget: action.budget ?? state.budget,
        mode: action.mode,
      };
    case "unsend":
      return { ...state, messages: state.messages.filter((item) => !isSending(item)) };
    case "mode":
      return { ...state, mode: action.mode };
  }
}

const KrellContext = createContext<KrellContextValue | null>(null);

/** Where the conversation is shown right now, if anywhere. */
export type KrellSurface = "tab" | "panel" | null;

export type KrellConversationProviderProps = {
  /** The first surface shown loads the thread; each newly shown surface may re-read it. */
  surface: KrellSurface;
  children: ReactNode;
  client?: KrellClient;
};

export function KrellConversationProvider({
  surface,
  children,
  client,
}: KrellConversationProviderProps): JSX.Element {
  const api = useMemo(() => client ?? createKrellClient(), [client]);
  const [state, dispatch] = useReducer(reduce, INITIAL);
  const tickets = useRef(new Map<string, string>());
  const busy = useRef(false);
  const loading = useRef(false);
  const requested = useRef(false);
  const shown = useRef<KrellSurface>(null);
  const messagesRef = useRef<KrellItem[]>(state.messages);
  useEffect(() => {
    messagesRef.current = state.messages;
  }, [state.messages]);
  const keys = useRef(0);
  const nextKey = useCallback((): string => {
    keys.current += 1;
    return `k${keys.current}`;
  }, []);

  useKrellHighlight(state.highlight?.testId ?? null, state.highlight?.nonce);

  /** `callId`: the confirm card a press stream answers. */
  const onEvent = useCallback(
    (event: KrellEvent, callId?: string): void => {
      if (event.type === "confirm_request") {
        const ticket = event.confirm.ticket_id;
        if (typeof ticket === "string" && ticket) tickets.current.set(event.id, ticket);
      }
      dispatch({ type: "event", event, key: nextKey(), callId });
    },
    [nextKey],
  );

  /** Run one Krell stream; refused locally while another runs. */
  const run = useCallback(
    async (open: (handler: typeof onEvent) => Promise<void>, before?: () => void): Promise<void> => {
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

  /** One read at a time; a second request while one runs is the same read. */
  const load = useCallback(async (): Promise<void> => {
    if (loading.current) return;
    loading.current = true;
    try {
      dispatch({ type: "loading" });
      const result = await api.conversation();
      if (!result.ok) {
        dispatch({ type: "load_failed", message: `${result.message} (${result.code})` });
        return;
      }
      tickets.current = ticketsFromHistory(result.messages);
      dispatch({
        type: "loaded",
        messages: itemsFromHistory(result.messages),
        connection: result.connection,
        budget: result.budget,
        mode: result.mode,
      });
      const conn = usable(result.connection);
      if (conn && needsStart(result.messages, conn.id)) await run((h) => api.start(h));
    } finally {
      loading.current = false;
    }
  }, [api, run]);

  // The first surface shown reads the thread. Each newly shown surface (the
  // panel, then the tab; or either one again) reads it again when no usable
  // model was found or the read failed: a model may have been connected
  // meanwhile, e.g. in Settings.
  useEffect(() => {
    const appeared = surface !== null && surface !== shown.current;
    shown.current = surface;
    if (!appeared) return;
    const stale = (state.status === "ready" && state.connection === null) || state.status === "failed";
    if (!requested.current || (stale && !busy.current)) {
      requested.current = true;
      void load();
    }
  }, [surface, load, state.status, state.connection]);

  /**
   * After a turn whose first answer was an error. The engine may have refused
   * before storing the message (busy, invalid, no model, a session refusal)
   * or stored it and then failed (a provider error), so show the stored
   * thread as it is. The message was stored when the thread now holds more
   * of the person's messages than were shown before the send
   * (`sentBefore`); otherwise its text goes back in the composer.
   */
  const afterFirstError = useCallback(
    async (
      text: string,
      error: { code: string; message: string },
      sentBefore: number,
      restoreDraft: boolean,
    ): Promise<void> => {
      const result = await api.conversation();
      let stored = false;
      if (result.ok) {
        tickets.current = ticketsFromHistory(result.messages);
        const history = result.messages;
        const last = history[history.length - 1];
        const errorStored =
          last?.role === "notice" && last.payload.kind === "error" && last.payload.code === error.code;
        stored = history.filter((m) => m.role === "user").length > sentBefore;
        const items = itemsFromHistory(history);
        dispatch({
          type: "resync",
          messages: errorStored
            ? items
            : [...items, { key: nextKey(), kind: "error", code: error.code, text: error.message }],
          connection: result.connection,
          budget: result.budget,
          mode: result.mode,
        });
      } else {
        dispatch({ type: "unsend" });
      }
      if (!stored && restoreDraft) dispatch({ type: "draft", value: text, ifEmpty: true });
    },
    [api, nextKey],
  );

  /** A turn that starts with the person's own message, shown at once as sending. */
  const userTurn = useCallback(
    (text: string, open: (handler: KrellEventHandler) => Promise<void>, fromComposer: boolean): void => {
      // The person's messages on screen match their stored ones (a refused
      // message is removed again), so this counts what was stored before.
      const sentBefore = messagesRef.current.filter((item) => item.kind === "user" && !item.sending).length;
      void run(
        async (handler) => {
          const seen: { first: KrellEvent | null } = { first: null };
          await open((event) => {
            seen.first ??= event;
            handler(event);
          });
          const first = seen.first;
          if (first?.type === "error") await afterFirstError(text, first, sentBefore, fromComposer);
        },
        () => {
          dispatch({ type: "add", item: { key: nextKey(), kind: "user", text, sending: true } });
          if (fromComposer) dispatch({ type: "draft", value: "" });
        },
      );
    },
    [afterFirstError, nextKey, run],
  );

  const send = useCallback(
    (text: string): void => {
      const trimmed = text.trim();
      if (trimmed) userTurn(trimmed, (h) => api.send(trimmed, h), true);
    },
    [api, userTurn],
  );

  const setDraft = useCallback((text: string): void => {
    dispatch({ type: "draft", value: text });
  }, []);

  const confirm = useCallback(
    (callId: string, decision: KrellDecision): void => {
      const ticket = tickets.current.get(callId);
      if (!ticket) {
        dispatch({
          type: "add",
          item: { key: nextKey(), kind: "error", code: CODE_CONFIRM_UNKNOWN, text: MESSAGE_CONFIRM_UNKNOWN, callId },
        });
        return;
      }
      void run((h) => api.confirm(ticket, decision, (event) => h(event, callId)));
    },
    [api, nextKey, run],
  );

  /** A mode quick reply: `POST /api/v1/krell/profile`, then a turn in that mode. */
  const setMode = useCallback(
    (mode: KrellMode): void => {
      userTurn(
        MODE_REPLIES[mode],
        (h) => {
          let answered = false;
          return api.setMode(mode, (event) => {
            // As for a message: any first answer but an error means the engine took the press.
            if (!answered && event.type !== "error") dispatch({ type: "mode", mode });
            answered = true;
            h(event);
          });
        },
        false,
      );
    },
    [api, userTurn],
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
      budget: state.budget,
      mode: state.mode,
      status: state.status,
      loadError: state.loadError,
      draft: state.draft,
      setDraft,
      send,
      confirm,
      setMode,
      clear,
      reload,
    }),
    [state, setDraft, send, confirm, setMode, clear, reload],
  );

  return <KrellContext.Provider value={value}>{children}</KrellContext.Provider>;
}

export function useKrell(): KrellContextValue {
  const value = useContext(KrellContext);
  if (!value) throw new Error("useKrell needs a KrellConversationProvider");
  return value;
}
