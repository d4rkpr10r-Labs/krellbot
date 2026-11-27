/**
 * Settings → AI model (M38.13): connect, check, activate and remove the model
 * Krell runs on. Keys are never shown; the engine never returns them.
 *
 * A Sign in with ChatGPT connection (M38.23) also shows its plan state, can
 * be signed out (the engine revokes it at OpenAI) and signed in again with
 * Continue with ChatGPT. Removing or signing out says so when OpenAI did not
 * confirm the revocation.
 */
import { useCallback, useEffect, useState, type JSX } from "react";
import {
  activateConnection,
  listConnections,
  probeConnection,
  removeConnection,
  signOutConnection,
  type AiConnection,
} from "./aiClient";
import { AiCapabilityStatus, AiConnectForm } from "./AiConnectForm";

export const CHATGPT_USAGE_URL = "https://chatgpt.com/settings/usage";

/** What a ChatGPT connection's row says about its sign-in and plan. */
export function chatgptStateCopy(conn: AiConnection): string | null {
  const chatgpt = conn.chatgpt;
  if (!chatgpt) return null;
  if (!chatgpt.signedIn) return "Signed out of ChatGPT. Continue with ChatGPT to sign in again.";
  if (chatgpt.plan === "disabled") {
    return "Signed in with ChatGPT, but plan use is not allowed, so Krell cannot use it. Allow it with Continue with ChatGPT, or connect an OpenAI API key instead.";
  }
  const who = chatgpt.account ? ` as ${chatgpt.account}` : "";
  return `Signed in with ChatGPT${who}. Krell's requests use your ChatGPT plan.`;
}

export function AiModelSection(): JSX.Element {
  const [connections, setConnections] = useState<AiConnection[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  // The server's message from the latest check, by connection id.
  const [messages, setMessages] = useState<Record<string, string>>({});
  // null until the user chooses; then the form follows the list being empty.
  const [adding, setAdding] = useState<boolean | null>(null);
  // The ChatGPT connection being signed in again, if any.
  const [reauthId, setReauthId] = useState<string | null>(null);
  // An unconfirmed revocation's explanation, after a sign-out or removal.
  const [notice, setNotice] = useState<string | null>(null);

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
    run: () => Promise<{ ok: boolean; message?: string; code?: string; detail?: string }>,
  ): Promise<void> {
    setBusyId(id);
    setRowError(null);
    setNotice(null);
    try {
      const result = await run();
      if (!result.ok) {
        setRowError(`${result.message ?? ""} (${result.code ?? "unknown"})`);
      } else if (result.detail) {
        setNotice(result.detail);
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
      {notice ? (
        <p className="kbot-settings__card-copy" role="status" data-testid="ai-revocation-notice">
          {notice}
        </p>
      ) : null}

      {list.length > 0 ? (
        <ul className="kbot-settings__keys-grid" aria-label="AI connections">
          {list.map((c) => {
            const busy = busyId === c.id;
            const chatgptCopy = chatgptStateCopy(c);
            const signInAgain =
              c.chatgpt !== null && (!c.chatgpt.signedIn || c.chatgpt.plan === "disabled");
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
                {chatgptCopy ? (
                  <p className="kbot-settings__card-copy" data-testid="ai-chatgpt-state">
                    {chatgptCopy}
                    {c.chatgpt?.signedIn && c.chatgpt.plan === "enabled" ? (
                      <>
                        {" "}
                        <a
                          className="kbot-settings__link-btn"
                          href={CHATGPT_USAGE_URL}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          Manage usage
                        </a>
                      </>
                    ) : null}
                  </p>
                ) : null}
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
                  {signInAgain && reauthId !== c.id ? (
                    <button
                      type="button"
                      className="kbot-settings__btn"
                      disabled={busy}
                      onClick={() => setReauthId(c.id)}
                    >
                      Continue with ChatGPT
                    </button>
                  ) : null}
                  {c.chatgpt?.signedIn ? (
                    <button
                      type="button"
                      className="kbot-settings__btn"
                      disabled={busy}
                      onClick={() =>
                        void act(c.id, () => signOutConnection(c.id))
                      }
                    >
                      Sign out
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
                {reauthId === c.id ? (
                  <AiConnectForm
                    reauth={c}
                    onConnected={() => undefined}
                    onAdded={() => {
                      setReauthId(null);
                      void refresh();
                    }}
                    onCancel={() => setReauthId(null)}
                  />
                ) : null}
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
