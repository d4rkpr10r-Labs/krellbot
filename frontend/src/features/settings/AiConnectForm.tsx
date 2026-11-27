/**
 * Connect an AI model for Krell (M38.13). Reused by the Krell tab and the
 * first-run wizard.
 *
 * The key lives only in the password input's DOM value. It is read once on
 * submit, sent in the add request, and cleared whether the request succeeds
 * or fails. It is never held in React state, rendered or logged.
 */
import { useEffect, useId, useRef, useState, type JSX } from "react";
import {
  activateConnection,
  addConnection,
  listConnections,
  listProviders,
  type AiCapability,
  type AiConnection,
  type AiProvider,
} from "./aiClient";

export const CLAUDE_NOTE =
  "Use an Anthropic API key, or use Krellbot from Claude Desktop or Claude Code through MCP (Settings → Agents & limits).";

const LOOPBACK_HINT =
  "Plain http:// works only for this computer (127.0.0.1, localhost or ::1). Other endpoints need https://.";

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
  onConnected(conn: AiConnection): void;
  onCancel?(): void;
};

export function AiConnectForm(props: AiConnectFormProps): JSX.Element {
  const { onConnected, onCancel } = props;
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

  async function submit(event: React.FormEvent): Promise<void> {
    event.preventDefault();
    if (!provider || busy) return;
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
      setProbe(added.probe);
      let conn = added.connection;
      if (conn.capability !== "failed") {
        const current = await listConnections();
        if (current.ok && current.active === null) {
          const activated = await activateConnection(conn.id);
          if (activated.ok) {
            conn = activated.connection;
          } else {
            setError(`${activated.message} (${activated.code})`);
          }
        }
      }
      onConnected(conn);
    } finally {
      if (keyRef.current) keyRef.current.value = "";
      setBusy(false);
    }
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
        disabled={busy}
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
        disabled={busy}
        required
      />

      <p
        className="kbot-settings__card-copy"
        data-testid="ai-egress-disclosure"
      >
        {disclosureFor(provider, baseUrl)}
      </p>

      {error ? (
        <p className="kbot-settings__error" role="alert">
          {error}
        </p>
      ) : null}
      {probe ? (
        <AiCapabilityStatus
          capability={probe.capability}
          message={probe.message}
        />
      ) : null}

      <div className="kbot-settings__inline-actions">
        <button type="submit" className="kbot-settings__btn" disabled={busy}>
          {busy ? "Connecting…" : "Connect"}
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
    </form>
  );
}
