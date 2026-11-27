/**
 * Connect an AI model for Krell (M38.13). Reused by the Krell tab and the
 * first-run wizard.
 *
 * The key lives only in the password input's DOM value. It is read once on
 * submit, sent in the add request, and cleared whether the request succeeds
 * or fails. It is never held in React state, rendered or logged.
 *
 * Sign in with OpenRouter (M38.20) needs no key in the browser at all: the
 * engine returns an OpenRouter page to open, OpenRouter hands the key to the
 * engine's one-shot loopback listener, and this form only polls the
 * sign-in's state. A finished sign-in takes the same probe, activate and
 * onConnected path as a key add. Stopping, leaving the form and giving up
 * all cancel the sign-in on the engine, so an abandoned sign-in can never add
 * a connection later.
 *
 * Continue with ChatGPT (M38.23) follows the same start, waiting, cancel and
 * done pattern for the OpenAI provider. The engine returns its own loopback
 * launch address (which redirects to OpenAI), so no OpenAI URL or token is
 * ever held here. A sign-in that did not allow ChatGPT plan use is kept but
 * not probed, and the form explains the API-key alternative. The first
 * plan-enabled sign-in of a connection shows "You're using your ChatGPT
 * plan" once; the engine remembers that it was shown. With `reauth`, the form
 * signs an existing ChatGPT connection in again. The button is a text label:
 * no OpenAI logo asset ships with Krellbot (owner branding review).
 */
import { useEffect, useId, useRef, useState, type JSX } from "react";
import {
  activateConnection,
  addConnection,
  cancelSignin,
  getSigninStatus,
  listConnections,
  listProviders,
  markChatgptWelcome,
  probeConnection,
  startChatgptSignin,
  startOpenRouterSignin,
  type AiCapability,
  type AiConnection,
  type AiProbe,
  type AiProvider,
} from "./aiClient";

export const CLAUDE_NOTE =
  "Use an Anthropic API key, or use Krellbot from Claude Desktop or Claude Code through MCP (Settings → Agents & limits).";

const LOOPBACK_HINT =
  "Plain http:// works only for this computer (127.0.0.1, localhost or ::1). Other endpoints need https://.";

/** Only OpenRouter's own sign-in page is ever opened. */
const OPENROUTER_AUTHORIZE_PREFIX = "https://openrouter.ai/auth?";
export const SIGNIN_POLL_MS = 2000;
// The engine expires a sign-in after 300 s; stop asking a little later regardless.
const SIGNIN_MAX_POLLS = 165;
const SIGNIN_NOTE =
  "Or sign in with OpenRouter: Krellbot opens OpenRouter in a new tab, and the key OpenRouter returns goes straight to your keychain.";
const SIGNIN_BAD_URL =
  "Krellbot received a sign-in address that is not OpenRouter's, so it did not open it.";
const SIGNIN_GAVE_UP = "The sign-in did not finish. Start it again.";
const SIGNIN_LOCAL_ONLY = "ai_signin_local_only";

/** ChatGPT's sign-in starts at the engine's own one-shot launch address, which redirects to OpenAI. */
const CHATGPT_LAUNCH_URL = /^http:\/\/127\.0\.0\.1:\d{1,5}\/auth\/start\/[A-Za-z0-9_-]+$/;
const CHATGPT_LOCAL_ONLY = "ai_chatgpt_local_only";
export const CHATGPT_NOTE =
  "Or continue with ChatGPT to use your ChatGPT plan instead of an API key. Krellbot opens OpenAI's sign-in in a new tab, and the sign-in is kept in your keychain.";
const CHATGPT_BAD_URL =
  "Krellbot received a sign-in address that is not its own ChatGPT sign-in address, so it did not open it.";
export const CHATGPT_PLAN_DISABLED =
  "You signed in with ChatGPT, but ChatGPT plan use was not allowed, so Krell cannot use this connection. Choose Continue with ChatGPT again and allow plan use, or connect an OpenAI API key instead: enter the key and choose Connect.";
const CHATGPT_REAUTH_SIGNIN =
  "This ChatGPT sign-in has ended. Continue with ChatGPT to sign in again with the same account.";
const CHATGPT_REAUTH_PLAN =
  "ChatGPT plan use is not allowed for this sign-in. Continue with ChatGPT and allow it, or connect an OpenAI API key instead.";
export const CHATGPT_WELCOME_TITLE = "You're using your ChatGPT plan";
const CHATGPT_WELCOME_BODY =
  "Krell's requests on this connection use your ChatGPT plan. You can review and manage that usage in ChatGPT settings.";

type SigninKind = "openrouter" | "chatgpt";

const SIGNIN_COPY: Record<SigninKind, { waiting: string; open: string }> = {
  openrouter: {
    waiting: "Waiting for OpenRouter. Finish signing in on the tab that opened.",
    open: "Open the OpenRouter sign-in page",
  },
  chatgpt: {
    waiting: "Waiting for ChatGPT. Finish signing in on the tab that opened.",
    open: "Open the ChatGPT sign-in page",
  },
};

const COPY_TOOLS = "Ready for Krell";
const COPY_CHAT_ONLY = "Chat only: Krell can explain but not use tools";
const COPY_UNPROBED = "Not known yet: re-check";
const COPY_FAILED_FALLBACK = "The last check failed. Re-check to try again.";

/** Status line for a probe result. `failed` shows the server's message. */
export function capabilityCopy(
  capability: AiCapability,
  message?: string,
): string {
  switch (capability) {
    case "tools":
      return COPY_TOOLS;
    case "chat_only":
      return COPY_CHAT_ONLY;
    case "failed":
      return message || COPY_FAILED_FALLBACK;
    default:
      return COPY_UNPROBED;
  }
}

export function AiCapabilityStatus(props: {
  capability: AiCapability;
  message?: string;
}): JSX.Element {
  return (
    <p
      className={
        props.capability === "failed"
          ? "kbot-settings__error"
          : "kbot-settings__card-copy"
      }
      role="status"
      data-testid={`ai-capability-${props.capability}`}
    >
      {capabilityCopy(props.capability, props.message)}
    </p>
  );
}

function hostOf(url: string): string | null {
  try {
    const host = new URL(url).hostname;
    return host ? host.replace(/^\[|\]$/g, "") : null;
  } catch {
    return null;
  }
}

function disclosureFor(provider: AiProvider, baseUrl: string): string {
  const host = provider.custom ? hostOf(baseUrl.trim()) : hostOf(provider.baseUrl ?? "");
  const where = host
    ? `${provider.label} (${host})`
    : "the endpoint you enter below";
  return `Your conversation with Krell and the Krellbot data she reads are sent to ${where}. Nothing is sent to krellbot.dev.`;
}

export type AiConnectFormProps = {
  /**
   * Fires only when Krell is ready on this connection: its capability is
   * `tools` or `chat_only` AND it is the active connection (just
   * auto-activated, or already active). Never fires for a `failed` or
   * `unprobed` probe, when another connection stays active, or when the
   * activate call fails.
   */
  onConnected(conn: AiConnection): void;
  /**
   * Fires after any successful add, including `failed` probes and
   * connections that were not activated. Use it to refresh lists.
   */
  onAdded?(
    conn: AiConnection,
    probe: { capability: AiCapability; message: string },
  ): void;
  onCancel?(): void;
  /** Sign this existing ChatGPT connection in again (its saved account and registration). */
  reauth?: AiConnection;
};

export function AiConnectForm(props: AiConnectFormProps): JSX.Element {
  const { onConnected, onAdded, onCancel, reauth } = props;
  const ids = useId();
  const keyRef = useRef<HTMLInputElement>(null);
  const [providers, setProviders] = useState<AiProvider[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [presetId, setPresetId] = useState<string>("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<{
    capability: AiCapability;
    message: string;
  } | null>(null);
  const [signin, setSignin] = useState<{
    flowId: string;
    authorizeUrl: string;
    kind: SigninKind;
  } | null>(null);
  // Set when the engine refuses a sign-in from this browser (a remote session).
  const [unavailable, setUnavailable] = useState<Partial<Record<SigninKind, string>>>({});
  // A ChatGPT sign-in that did not allow plan use.
  const [planNote, setPlanNote] = useState<string | null>(null);
  // "You're using your ChatGPT plan", waiting for Got it.
  const [welcome, setWelcome] = useState<{ connectionId: string; done(): void } | null>(null);
  // Sign-ins this form is done with; any other is cancelled when its polling ends.
  const settledFlows = useRef(new Set<string>());

  useEffect(() => {
    let live = true;
    void listProviders().then((result) => {
      if (!live) return;
      if (!result.ok) {
        setLoadError(`${result.message} (${result.code})`);
        return;
      }
      setProviders(result.providers);
      setPresetId((current) => current || result.providers[0]?.id || "");
    });
    return () => {
      live = false;
    };
  }, []);

  const provider = providers?.find((p) => p.id === presetId) ?? null;
  const customProviders = (providers ?? []).filter((p) => p.custom);

  /**
   * After an add or a sign-in: show the probe, auto-activate a usable
   * connection when none is active, then report it per the props contract.
   */
  async function finishAdded(added: {
    connection: AiConnection;
    probe: AiProbe;
  }): Promise<void> {
    setProbe(added.probe);
    let conn = added.connection;
    let ready = false;
    if (conn.capability === "tools" || conn.capability === "chat_only") {
      const current = await listConnections();
      if (!current.ok) {
        setError(`${current.message} (${current.code})`);
      } else if (current.active === null) {
        const activated = await activateConnection(conn.id);
        if (activated.ok) {
          conn = activated.connection;
          ready = true;
        } else {
          setError(`${activated.message} (${activated.code})`);
        }
      } else if (current.active === conn.id) {
        conn = { ...conn, active: true };
        ready = true;
      }
    }
    onAdded?.(conn, added.probe);
    if (ready) onConnected(conn);
  }

  async function storedConnection(connectionId: string): Promise<AiConnection | undefined> {
    const listed = await listConnections();
    return listed.ok ? listed.connections.find((c) => c.id === connectionId) : undefined;
  }

  /** Show the ChatGPT plan welcome and wait for Got it. */
  function showWelcome(connectionId: string): Promise<void> {
    return new Promise((done) => setWelcome({ connectionId, done }));
  }

  /**
   * A sign-in stored a connection: probe it and finish like a key add. If
   * the probe fails, the connection is still stored, so the parent hears
   * about it through onAdded as unprobed. A ChatGPT sign-in without plan use
   * is not probed (the engine would refuse it); a connection's first
   * plan-enabled sign-in shows the welcome before the parent hears about it.
   */
  async function finishSignin(
    connectionId: string,
    plan: "enabled" | "disabled" | null,
    isLive: () => boolean,
  ): Promise<void> {
    if (plan === "disabled") {
      setPlanNote(CHATGPT_PLAN_DISABLED);
      const stored = await storedConnection(connectionId);
      if (stored && isLive()) {
        onAdded?.(stored, { capability: stored.capability, message: CHATGPT_PLAN_DISABLED });
      }
      return;
    }
    const probed = await probeConnection(connectionId);
    if (!isLive()) return;
    if (!probed.ok) setError(`${probed.message} (${probed.code})`);
    const stored = probed.ok
      ? probed.connection
      : plan === "enabled"
        ? await storedConnection(connectionId)
        : undefined;
    if (stored?.chatgpt?.welcome && isLive()) {
      await showWelcome(connectionId);
      if (!isLive()) return;
    }
    if (probed.ok) {
      const chatgpt = probed.connection.chatgpt;
      await finishAdded({
        ...probed,
        connection: { ...probed.connection, chatgpt: chatgpt && { ...chatgpt, welcome: false } },
      });
      return;
    }
    const known = stored ?? (await storedConnection(connectionId));
    if (known && isLive()) {
      onAdded?.(known, { capability: "unprobed", message: probed.message });
    }
  }

  async function gotIt(): Promise<void> {
    if (!welcome) return;
    const { connectionId, done } = welcome;
    const marked = await markChatgptWelcome(connectionId);
    if (!marked.ok) setError(`${marked.message} (${marked.code})`);
    setWelcome(null);
    done();
  }

  // The polling effect outlives renders; it always calls the latest finisher.
  const finishSigninRef = useRef(finishSignin);
  finishSigninRef.current = finishSignin;

  async function submit(event: React.FormEvent): Promise<void> {
    event.preventDefault();
    if (!provider || busy || signin) return;
    const input = keyRef.current;
    const credential = input?.value ?? "";
    if (input) input.value = "";
    setBusy(true);
    setError(null);
    setProbe(null);
    try {
      const added = await addConnection({
        preset: provider.id,
        model: model.trim(),
        credential: credential || undefined,
        baseUrl: provider.custom ? baseUrl.trim() || undefined : undefined,
      });
      if (!added.ok) {
        setError(`${added.message} (${added.code})`);
        return;
      }
      await finishAdded(added);
    } finally {
      if (keyRef.current) keyRef.current.value = "";
      setBusy(false);
    }
  }

  async function startSignin(kind: SigninKind): Promise<void> {
    if ((!provider && !reauth) || busy || signin) return;
    setBusy(true);
    setError(null);
    setProbe(null);
    setPlanNote(null);
    try {
      const started =
        kind === "chatgpt"
          ? await startChatgptSignin(
              reauth ? { connectionId: reauth.id } : { model: model.trim() },
            )
          : await startOpenRouterSignin(model.trim());
      if (!started.ok) {
        if (started.code === SIGNIN_LOCAL_ONLY || started.code === CHATGPT_LOCAL_ONLY) {
          setUnavailable((current) => ({ ...current, [kind]: started.message }));
        } else setError(`${started.message} (${started.code})`);
        return;
      }
      const trusted =
        kind === "chatgpt"
          ? CHATGPT_LAUNCH_URL.test(started.authorizeUrl)
          : started.authorizeUrl.startsWith(OPENROUTER_AUTHORIZE_PREFIX);
      if (!trusted) {
        setError(kind === "chatgpt" ? CHATGPT_BAD_URL : SIGNIN_BAD_URL);
        return;
      }
      window.open(started.authorizeUrl, "_blank", "noopener,noreferrer");
      setSignin({ flowId: started.flowId, authorizeUrl: started.authorizeUrl, kind });
    } finally {
      setBusy(false);
    }
  }

  /**
   * "Stop waiting": cancel on the engine. A sign-in whose callback already
   * arrived cannot be cancelled; keep following it so the connection it
   * stores is reported like any other.
   */
  async function stopWaiting(): Promise<void> {
    if (!signin || busy) return;
    const { flowId } = signin;
    const result = await cancelSignin(flowId);
    if (
      result.ok &&
      !result.cancelled &&
      (result.status.state === "waiting" || result.status.state === "connected")
    ) {
      return;
    }
    // Cancelled, or already over. On a refusal the effect cleanup tries once more.
    if (result.ok) settledFlows.current.add(flowId);
    setSignin((current) => (current?.flowId === flowId ? null : current));
  }

  useEffect(() => {
    if (!signin) return;
    const { flowId } = signin;
    let live = true;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const isLive = (): boolean => live;
    // Leave the sign-in; unless it settled, the cleanup below cancels it on the engine.
    const leave = (message: string | null): void => {
      if (!live) return;
      if (message) setError(message);
      setSignin(null);
    };
    const settle = (message: string | null): void => {
      settledFlows.current.add(flowId);
      leave(message);
    };
    const poll = async (): Promise<void> => {
      polls += 1;
      const result = await getSigninStatus(flowId);
      if (!live) return;
      if (!result.ok) {
        leave(`${result.message} (${result.code})`);
        return;
      }
      const { status } = result;
      if (status.state === "waiting") {
        if (polls >= SIGNIN_MAX_POLLS) leave(SIGNIN_GAVE_UP);
        else timer = setTimeout(() => void poll(), SIGNIN_POLL_MS);
        return;
      }
      if (status.state !== "connected" || !status.connectionId) {
        settle(`${status.message} (${status.code || status.state})`);
        return;
      }
      settledFlows.current.add(flowId);
      setBusy(true);
      try {
        await finishSigninRef.current(status.connectionId, status.plan, isLive);
      } finally {
        setBusy(false);
        settle(null);
      }
    };
    timer = setTimeout(() => void poll(), SIGNIN_POLL_MS);
    return () => {
      live = false;
      if (timer !== undefined) clearTimeout(timer);
      if (!settledFlows.current.has(flowId)) {
        settledFlows.current.add(flowId);
        void cancelSignin(flowId);
      }
    };
  }, [signin]);

  const locked = busy || signin !== null;

  const welcomeDialog = welcome ? (
    <div
      className="kbot-settings__welcome"
      role="dialog"
      aria-modal="true"
      aria-labelledby={`${ids}-welcome-title`}
      data-testid="ai-chatgpt-welcome"
    >
      <h3 className="kbot-settings__welcome-title" id={`${ids}-welcome-title`}>
        {CHATGPT_WELCOME_TITLE}
      </h3>
      <p className="kbot-settings__card-copy">{CHATGPT_WELCOME_BODY}</p>
      <button
        type="button"
        className="kbot-settings__btn kbot-settings__btn--primary"
        onClick={() => void gotIt()}
        autoFocus
      >
        Got it
      </button>
    </div>
  ) : null;

  const waiting = signin ? (
    <div data-testid="ai-signin-waiting">
      <p className="kbot-settings__card-copy" role="status">
        {SIGNIN_COPY[signin.kind].waiting}
      </p>
      <div className="kbot-settings__inline-actions">
        <a
          className="kbot-settings__link-btn"
          href={signin.authorizeUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          {SIGNIN_COPY[signin.kind].open}
        </a>
        {busy ? null : (
          <button
            type="button"
            className="kbot-settings__link-btn"
            onClick={() => void stopWaiting()}
          >
            Stop waiting
          </button>
        )}
      </div>
    </div>
  ) : null;

  const feedback = (
    <>
      {error ? (
        <p className="kbot-settings__error" role="alert">
          {error}
        </p>
      ) : null}
      {planNote ? (
        <p className="kbot-settings__card-copy" role="status" data-testid="ai-chatgpt-plan-disabled">
          {planNote}
        </p>
      ) : null}
      {probe ? (
        <AiCapabilityStatus
          capability={probe.capability}
          message={probe.message}
        />
      ) : null}
    </>
  );

  if (reauth) {
    return (
      <div className="kbot-settings__card" data-testid="ai-chatgpt-reauth">
        {welcomeDialog ?? (
          <>
            <p className="kbot-settings__card-copy">
              {reauth.chatgpt?.plan === "disabled" ? CHATGPT_REAUTH_PLAN : CHATGPT_REAUTH_SIGNIN}
            </p>
            {unavailable.chatgpt ? (
              <p className="kbot-settings__card-copy" data-testid="ai-signin-unavailable">
                {unavailable.chatgpt}
              </p>
            ) : null}
            {waiting}
            {feedback}
            <div className="kbot-settings__inline-actions">
              <button
                type="button"
                className="kbot-settings__btn"
                data-oai-siwc-button=""
                onClick={() => void startSignin("chatgpt")}
                disabled={locked || unavailable.chatgpt !== undefined}
              >
                Continue with ChatGPT
              </button>
              {onCancel ? (
                <button
                  type="button"
                  className="kbot-settings__btn"
                  onClick={onCancel}
                  disabled={busy}
                >
                  Cancel
                </button>
              ) : null}
            </div>
          </>
        )}
      </div>
    );
  }

  if (loadError) {
    return (
      <p className="kbot-settings__error" role="alert">
        {loadError}
      </p>
    );
  }
  if (!providers || !provider) {
    return (
      <p className="kbot-settings__card-copy" role="status">
        Loading AI providers…
      </p>
    );
  }

  const keyOptional = provider.credential === "optional";
  const offersSignin = provider.signin === "openrouter";
  const offersChatgpt = provider.signin === "chatgpt";

  if (welcomeDialog) {
    return <div className="kbot-settings__card">{welcomeDialog}</div>;
  }

  return (
    <form
      className="kbot-settings__card"
      onSubmit={(e) => void submit(e)}
      data-testid="ai-connect-form"
    >
      <label className="kbot-settings__field-label" htmlFor={`${ids}-provider`}>
        Provider
      </label>
      <select
        id={`${ids}-provider`}
        className="kbot-settings__input"
        value={presetId}
        onChange={(e) => setPresetId(e.target.value)}
        disabled={locked}
      >
        {providers.map((p) => (
          <option key={p.id} value={p.id}>
            {p.label}
          </option>
        ))}
      </select>

      {provider.id === "anthropic" ? (
        <p className="kbot-settings__card-copy" data-testid="ai-claude-note">
          {CLAUDE_NOTE}
        </p>
      ) : null}

      {provider.custom ? (
        <>
          <label
            className="kbot-settings__field-label"
            htmlFor={`${ids}-format`}
          >
            Format
          </label>
          <select
            id={`${ids}-format`}
            className="kbot-settings__input"
            value={provider.id}
            onChange={(e) => setPresetId(e.target.value)}
            disabled={busy}
          >
            {customProviders.map((p) => (
              <option key={p.id} value={p.id}>
                {p.format === "anthropic_messages"
                  ? "Anthropic Messages"
                  : "OpenAI-compatible"}
              </option>
            ))}
          </select>
          <label
            className="kbot-settings__field-label"
            htmlFor={`${ids}-base-url`}
          >
            Base URL
          </label>
          <input
            id={`${ids}-base-url`}
            className="kbot-settings__input"
            type="url"
            autoComplete="off"
            spellCheck={false}
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            disabled={busy}
            required
          />
          <p className="kbot-settings__card-copy" data-testid="ai-base-url-hint">
            {LOOPBACK_HINT}
          </p>
        </>
      ) : null}

      <label className="kbot-settings__field-label" htmlFor={`${ids}-key`}>
        {keyOptional ? "API key (optional)" : "API key"}
      </label>
      <input
        id={`${ids}-key`}
        ref={keyRef}
        className="kbot-settings__input"
        type="password"
        autoComplete="off"
        spellCheck={false}
        disabled={busy}
        required={!keyOptional}
      />

      <label className="kbot-settings__field-label" htmlFor={`${ids}-model`}>
        Model
      </label>
      <input
        id={`${ids}-model`}
        className="kbot-settings__input"
        type="text"
        autoComplete="off"
        spellCheck={false}
        value={model}
        onChange={(e) => setModel(e.target.value)}
        disabled={locked}
        required
      />

      <p
        className="kbot-settings__card-copy"
        data-testid="ai-egress-disclosure"
      >
        {disclosureFor(provider, baseUrl)}
      </p>

      {offersSignin && unavailable.openrouter ? (
        <p
          className="kbot-settings__card-copy"
          data-testid="ai-signin-unavailable"
        >
          {unavailable.openrouter}
        </p>
      ) : offersSignin ? (
        <p className="kbot-settings__card-copy" data-testid="ai-signin-note">
          {SIGNIN_NOTE}
        </p>
      ) : null}
      {offersChatgpt && unavailable.chatgpt ? (
        <p
          className="kbot-settings__card-copy"
          data-testid="ai-signin-unavailable"
        >
          {unavailable.chatgpt}
        </p>
      ) : offersChatgpt ? (
        <p className="kbot-settings__card-copy" data-testid="ai-chatgpt-note">
          {CHATGPT_NOTE}
        </p>
      ) : null}

      {waiting}
      {feedback}

      <div className="kbot-settings__inline-actions">
        <button type="submit" className="kbot-settings__btn" disabled={locked}>
          {busy ? "Connecting…" : "Connect"}
        </button>
        {offersSignin ? (
          <button
            type="button"
            className="kbot-settings__btn"
            onClick={() => void startSignin("openrouter")}
            disabled={locked || unavailable.openrouter !== undefined}
          >
            Sign in with OpenRouter
          </button>
        ) : null}
        {offersChatgpt ? (
          <button
            type="button"
            className="kbot-settings__btn"
            data-oai-siwc-button=""
            onClick={() => void startSignin("chatgpt")}
            disabled={locked || unavailable.chatgpt !== undefined}
          >
            Continue with ChatGPT
          </button>
        ) : null}
        {onCancel ? (
          <button
            type="button"
            className="kbot-settings__btn"
            onClick={onCancel}
            disabled={busy}
          >
            Cancel
          </button>
        ) : null}
      </div>
    </form>
  );
}
