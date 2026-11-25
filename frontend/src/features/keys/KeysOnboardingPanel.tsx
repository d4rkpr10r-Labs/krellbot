import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import "./KeysOnboardingPanel.signal.css";
import type { KeysClient, KeysStatus, KeysVenue } from "./client";
import { KEYS_VENUE } from "./client";

type VenueCopy = {
  title: string;
  helper: string;
  loading: string;
  loadErrorPrefix: string;
  keyLabel: string;
  secretLabel: string;
  absent: string;
  secretMultiline: boolean;
};

const VENUE_COPY: Record<KeysVenue, VenueCopy> = {
  kraken: {
    title: "Kraken exchange key",
    helper:
      "Enter the API key and private key from Kraken. Allow trading and disable withdrawals. Krellbot checks these permissions before saving the key in your operating system’s keychain.",
    loading: "Reading Kraken key status…",
    loadErrorPrefix:
      "Could not read Kraken key status — check the workstation is running.",
    keyLabel: "Public key",
    secretLabel: "Private key",
    absent:
      "No Kraken key is stored on this machine yet. Enter trade-only key material below.",
    secretMultiline: false,
  },
  coinbase: {
    title: "Coinbase exchange key",
    helper:
      "Enter your Coinbase Advanced Trade API key name and PEM private key. Allow trading and disable withdrawals and transfers. Krellbot checks these permissions before saving the key in your operating system’s keychain.",
    loading: "Reading Coinbase key status…",
    loadErrorPrefix:
      "Could not read Coinbase key status — check the workstation is running.",
    keyLabel: "API key name",
    secretLabel: "Private key (PEM)",
    absent:
      "No Coinbase key is stored on this machine yet. Enter trade-only key material below.",
    secretMultiline: true,
  },
};

const STORE_LABEL = "Probe and store";
const REMOVE_LABEL = "Remove key";
const REFRESH_LABEL = "Refresh";
const PAPER_ONLY_NOTE =
  "Paper trading needs no exchange key. Storing a key does not enable live trading in this paper-only release.";

const RECOVERY: Record<string, string> = {
  refused_withdraw: "Turn off withdrawals and transfers for this key at your exchange, then try again.",
  refused_unknown_permission: "Use a key whose permissions can be verified, then try again.",
  refused_trade_off: "Enable trading permission at your exchange, then try again.",
  refused_invalid: "Check the key and its permissions at your exchange, then try again.",
  refused_malformed: "Check the key format and try again. If this continues, check exchange availability.",
  refused_unreachable: "Check your connection and exchange availability, then try again.",
  unsupported_backend: "Unlock or set up your operating system’s keychain, then try again.",
  invalid_argument: "Check both key fields and try again.",
};

const SAFE_FEEDBACK = new Set([
  "stored in native OS keychain",
  "key has withdraw rights; refused",
  "key grants unrecognized permission; refused",
  "key lacks required trade permission; refused",
  "key cannot be verified (invalid or permission denied); refused",
  "venue response shape unknown; refused",
  "venue unreachable; cannot verify",
  "keyring backend is not a native OS keychain; refused",
  "input rejected; refused",
  "keyring write failed and rollback is uncertain",
  "keyring write failed; prior pair restored",
  "keyring write failed; no prior pair was present",
  "prior keyring state could not be read; nothing was written",
  "removed from keychain",
  "no key was stored",
  "keyring remove failed and outcome is uncertain",
]);

export type KeysOnboardingPanelProps = {
  client: KeysClient;
  /** Venue this panel manages. Defaults to Kraken for single-panel callers. */
  venue?: KeysVenue;
  /** When false, skip network reads (session still starting or failed). */
  enabled?: boolean;
};

function describeStatus(status: KeysStatus): string {
  if (status.corrupt) {
    return status.message || "keyring pair is corrupt; refused";
  }
  if (!status.present) {
    return status.message || "no key stored";
  }
  if (status.balances_available) {
    return "key present · exchange holdings available";
  }
  return "key present · exchange holdings unavailable";
}

export function KeysOnboardingPanel({
  client,
  venue = KEYS_VENUE,
  enabled = true,
}: KeysOnboardingPanelProps): JSX.Element {
  const copy = VENUE_COPY[venue];
  const [status, setStatus] = useState<KeysStatus | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [secret, setSecret] = useState("");
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [replacing, setReplacing] = useState(false);
  const requestRef = useRef(0);
  const inFlight = useRef(false);

  const publicId = `keys-public-${venue}`;
  const privateId = `keys-private-${venue}`;
  const titleId = `keys-onboarding-title-${venue}`;

  const load = useCallback(async (): Promise<void> => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const next = await client.getStatus(venue);
      if (requestId !== requestRef.current) {
        return;
      }
      setStatus(next);
    } catch {
      if (requestId !== requestRef.current) {
        return;
      }
      setStatus(null);
      setError("Unable to read key status. Check the local session and try again.");
    } finally {
      if (requestId === requestRef.current) {
        setLoading(false);
      }
    }
  }, [client, enabled, venue]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleStore = useCallback(async (): Promise<void> => {
    if (inFlight.current || !enabled) {
      return;
    }
    const trimmedKey = key.trim();
    const trimmedSecret = secret.trim();
    if (trimmedKey === "" || trimmedSecret === "") {
      setFeedback(
        venue === "coinbase"
          ? "Enter both the API key name and the private key."
          : "Enter both the public key and the private key.",
      );
      return;
    }
    inFlight.current = true;
    setPending(true);
    setFeedback(null);
    try {
      const result = await client.probeAndStore(trimmedKey, trimmedSecret, venue);
      const message = SAFE_FEEDBACK.has(result.message)
        ? result.message
        : "Key operation could not be completed.";
      setFeedback([message, RECOVERY[result.status]].filter(Boolean).join(" "));
      if (result.ok) {
        setKey("");
        setSecret("");
        setReplacing(false);
      }
      await load();
    } catch {
      setFeedback("Could not check and save the key. Check the local session and try again.");
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }, [client, enabled, key, load, secret, venue]);

  const handleRemove = useCallback(async (): Promise<void> => {
    if (inFlight.current || !enabled) {
      return;
    }
    inFlight.current = true;
    setPending(true);
    setFeedback(null);
    try {
      const result = await client.remove(venue);
      setFeedback(
        SAFE_FEEDBACK.has(result.message) ? result.message : "Key operation could not be completed.",
      );
      if (result.ok) {
        setKey("");
        setSecret("");
        setReplacing(false);
      }
      await load();
    } catch {
      setFeedback("Could not remove the key. Refresh its status before trying again.");
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }, [client, enabled, load, venue]);

  const controlsDisabled = !enabled || pending || loading;
  const present = status?.present === true && status.corrupt !== true;

  return (
    <section
      className="kbot-keys"
      data-testid={`keys-onboarding-${venue}`}
      data-venue={venue}
      aria-labelledby={titleId}
    >
      <div className="kbot-keys__head">
        <h2 className="kbot-keys__title" id={titleId}>
          {copy.title}
        </h2>
        <button
          type="button"
          className="kbot-keys__refresh"
          data-testid={`keys-refresh-${venue}`}
          disabled={controlsDisabled}
          aria-busy={loading || undefined}
          onClick={() => {
            void load();
          }}
        >
          {loading ? "Loading…" : REFRESH_LABEL}
        </button>
      </div>

      <p className="kbot-keys__helper">{copy.helper}</p>
      <p className="kbot-keys__paper-only" data-testid={`keys-paper-only-${venue}`}>
        {PAPER_ONLY_NOTE}
      </p>

      {!enabled ? (
        <p
          className="kbot-keys__status"
          data-testid={`keys-waiting-session-${venue}`}
          role="status"
        >
          Waiting for the local session before reading key status.
        </p>
      ) : null}

      {enabled && loading && status === null ? (
        <p className="kbot-keys__status" data-testid={`keys-loading-${venue}`} role="status">
          {copy.loading}
        </p>
      ) : null}

      {error !== null ? (
        <p className="kbot-keys__error" role="alert" data-testid={`keys-error-${venue}`}>
          {copy.loadErrorPrefix} ({error})
        </p>
      ) : null}

      {status !== null ? (
        <>
          <p className="kbot-keys__status" data-testid={`keys-status-${venue}`} role="status">
            {describeStatus(status)}
            {status.backend_label ? ` · ${status.backend_label}` : ""}
          </p>

          {present ? (
            <div className="kbot-keys__stored" data-testid={`keys-stored-${venue}`}>
              {status.balances_available && status.balances !== null ? (
                <div data-testid={`keys-balances-${venue}`}>
                  <h3 className="kbot-keys__balances-title">Exchange account free holdings</h3>
                  <p>These are exchange balances. Your paper trading balance is separate.</p>
                  {status.balances.length === 0 ? (
                    <p
                      className="kbot-keys__status"
                      data-testid={`keys-balances-empty-${venue}`}
                    >
                      No free holdings reported.
                    </p>
                  ) : (
                    <ul className="kbot-keys__balances-list">
                      {status.balances.map((row) => (
                        <li
                          key={row.asset}
                          className="kbot-keys__balance-row"
                          data-testid={`keys-balance-${venue}-${row.asset}`}
                        >
                          <span className="kbot-keys__balance-asset">{row.asset}</span>
                          <span className="kbot-keys__balance-free">{row.free}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ) : (
                <p
                  className="kbot-keys__status"
                  data-testid={`keys-balances-unavailable-${venue}`}
                >
                  Key stored, but exchange holdings could not be checked. The exchange may be unavailable or the key may have been revoked. Check the key and refresh.
                </p>
              )}
              <button
                type="button"
                className="kbot-keys__remove"
                data-testid={`keys-remove-${venue}`}
                disabled={controlsDisabled}
                aria-busy={pending || undefined}
                onClick={() => {
                  void handleRemove();
                }}
              >
                {REMOVE_LABEL}
              </button>
              {!replacing ? (
                <button
                  type="button"
                  className="kbot-keys__remove"
                  disabled={controlsDisabled}
                  data-testid={`keys-replace-${venue}`}
                  onClick={() => setReplacing(true)}
                >
                  Replace key
                </button>
              ) : null}
            </div>
          ) : null}
          {!present || replacing ? (
            <div className="kbot-keys__form" data-testid={`keys-form-${venue}`}>
              <p className="kbot-keys__absent" data-testid={`keys-absent-${venue}`}>
                {replacing
                  ? "Krellbot checks the new key before replacing the existing key."
                  : status.message === "keyring state could not be read"
                    ? "Unlock your keychain and refresh before adding a key."
                    : status.corrupt
                      ? "The stored key is incomplete. Check your keychain before adding a key."
                      : copy.absent}
              </p>
              <div className="kbot-keys__row">
                <label className="kbot-keys__label" htmlFor={publicId}>
                  {copy.keyLabel}
                </label>
                <input
                  id={publicId}
                  data-testid={`keys-public-${venue}`}
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  value={key}
                  disabled={controlsDisabled}
                  onChange={(event) => setKey(event.target.value)}
                />
              </div>
              <div className="kbot-keys__row">
                <label className="kbot-keys__label" htmlFor={privateId}>
                  {copy.secretLabel}
                </label>
                {copy.secretMultiline ? (
                  <textarea
                    id={privateId}
                    data-testid={`keys-private-${venue}`}
                    className="kbot-keys__secret-area"
                    autoComplete="off"
                    spellCheck={false}
                    rows={4}
                    value={secret}
                    disabled={controlsDisabled}
                    onChange={(event) => setSecret(event.target.value)}
                  />
                ) : (
                  <input
                    id={privateId}
                    data-testid={`keys-private-${venue}`}
                    type="password"
                    autoComplete="off"
                    spellCheck={false}
                    value={secret}
                    disabled={controlsDisabled}
                    onChange={(event) => setSecret(event.target.value)}
                  />
                )}
              </div>
              <button
                type="button"
                className="kbot-keys__store"
                data-testid={`keys-store-${venue}`}
                disabled={controlsDisabled || key.trim() === "" || secret.trim() === ""}
                aria-busy={pending || undefined}
                onClick={() => {
                  void handleStore();
                }}
              >
                {STORE_LABEL}
              </button>
              {replacing ? (
                <button
                  type="button"
                  className="kbot-keys__remove"
                  disabled={controlsDisabled}
                  data-testid={`keys-cancel-replace-${venue}`}
                  onClick={() => {
                    setKey("");
                    setSecret("");
                    setReplacing(false);
                    setFeedback(null);
                  }}
                >
                  Cancel
                </button>
              ) : null}
            </div>
          ) : null}
        </>
      ) : null}

      {feedback !== null ? (
        <p
          className="kbot-keys__feedback"
          role="status"
          data-testid={`keys-feedback-${venue}`}
        >
          {feedback}
        </p>
      ) : null}
    </section>
  );
}
