/**
 * Settings → AI model (M38.13): connect, check, activate and remove the model
 * Krell runs on. Keys are never shown; the engine never returns them.
 */
import { useCallback, useEffect, useState, type JSX } from "react";
import {
  activateConnection,
  listConnections,
  probeConnection,
  removeConnection,
  type AiConnection,
} from "./aiClient";
import { AiCapabilityStatus, AiConnectForm } from "./AiConnectForm";

export function AiModelSection(): JSX.Element {
  const [connections, setConnections] = useState<AiConnection[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  // The server's message from the latest check, by connection id.
  const [messages, setMessages] = useState<Record<string, string>>({});
  // null until the user chooses; then the form follows the list being empty.
  const [adding, setAdding] = useState<boolean | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    const result = await listConnections();
    if (!result.ok) {
      setLoadError(`${result.message} (${result.code})`);
      return;
    }
    setLoadError(null);
    setConnections(result.connections);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function act(
    id: string,
    run: () => Promise<{ ok: boolean; message?: string; code?: string }>,
  ): Promise<void> {
    setBusyId(id);
    setRowError(null);
    try {
      const result = await run();
      if (!result.ok) {
        setRowError(`${result.message ?? ""} (${result.code ?? "unknown"})`);
      }
    } finally {
      await refresh();
      setBusyId(null);
    }
  }

  const list = connections ?? [];
  // An empty list always shows the form, even after Cancel then Remove.
  const showForm = list.length === 0 || adding === true;

  return (
    <section
      className="kbot-settings__panel"
      aria-labelledby="kbot-settings-ai-heading"
      data-testid="settings-section-ai"
    >
      <header className="kbot-settings__panel-head">
        <h2
          className="kbot-settings__panel-title"
          id="kbot-settings-ai-heading"
        >
          AI model
        </h2>
        <p className="kbot-settings__panel-note">
          Connect your own AI model so Krell, the in-app guide, can run on it.
          Your key is stored in your OS keychain and is never shown again.
        </p>
      </header>

      {loadError ? (
        <p className="kbot-settings__error" role="alert">
          {loadError}
        </p>
      ) : null}
      {rowError ? (
        <p className="kbot-settings__error" role="alert">
          {rowError}
        </p>
      ) : null}

      {list.length > 0 ? (
        <ul className="kbot-settings__keys-grid" aria-label="AI connections">
          {list.map((c) => {
            const busy = busyId === c.id;
            return (
              <li
                key={c.id}
                className="kbot-settings__card"
                data-testid={`ai-connection-${c.id}`}
              >
                <h3 className="kbot-settings__field-label">
                  {c.label} · {c.model}
                  {c.active ? (
                    <>
                      {" "}
                      <span data-testid="ai-active-badge">Active</span>
                    </>
                  ) : null}
                </h3>
                <AiCapabilityStatus
                  capability={c.capability}
                  message={messages[c.id]}
                />
                <p className="kbot-settings__card-copy">{c.disclosure}</p>
                <div className="kbot-settings__inline-actions">
                  {!c.active ? (
                    <button
                      type="button"
                      className="kbot-settings__btn"
                      disabled={busy}
                      onClick={() =>
                        void act(c.id, () => activateConnection(c.id))
                      }
                    >
                      Activate
                    </button>
                  ) : null}
                  {c.capability === "unprobed" || c.capability === "failed" ? (
                    <button
                      type="button"
                      className="kbot-settings__btn"
                      disabled={busy}
                      onClick={() =>
                        void act(c.id, async () => {
                          const result = await probeConnection(c.id);
                          if (result.ok) {
                            setMessages((m) => ({
                              ...m,
                              [c.id]: result.probe.message,
                            }));
                          }
                          return result;
                        })
                      }
                    >
                      Re-check
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="kbot-settings__btn"
                    disabled={busy}
                    onClick={() =>
                      void act(c.id, () => removeConnection(c.id))
                    }
                  >
                    Remove
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}

      {showForm ? (
        <AiConnectForm
          onConnected={() => undefined}
          onAdded={() => {
            setAdding(true);
            void refresh();
          }}
          onCancel={list.length > 0 ? () => setAdding(false) : undefined}
        />
      ) : (
        <button
          type="button"
          className="kbot-settings__btn"
          onClick={() => setAdding(true)}
        >
          Add another model
        </button>
      )}
    </section>
  );
}
