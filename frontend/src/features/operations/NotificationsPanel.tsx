import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import "./NotificationsPanel.signal.css";
import type {
  NotificationChannelDelivery,
  NotificationDelivery,
  NotificationsClient,
  NotificationSettings,
} from "./notificationsClient";

const PANEL_TITLE = "Notifications";
const LOADING_TEXT = "Loading notification settings…";
const LOAD_ERROR_PREFIX =
  "Could not load notification settings — check the workstation is running.";
const HELPER_TEXT =
  "Opt in to local desktop and/or email alerts. Acknowledgements stay in Operations. Telemetry is a separate opt-in and is not controlled here.";
const DESKTOP_LABEL = "Desktop notifications";
const EMAIL_LABEL = "Email notifications";
const DESKTOP_HINT = "Local only — no external credentials required.";
const EMAIL_HINT_PREFIX = "SMTP password is never stored here; set";
const SAVE_LABEL = "Save";
const SAVING_LABEL = "Saving…";
const CLIENT_UNAVAILABLE =
  "Notification settings are unavailable in this build.";
const DELIVERY_TITLE = "Delivery";
const DELIVERY_HELPER =
  "An alert counts as sent only when the channel accepted it. Each alert notifies once per occurrence.";
const DELIVERY_ERROR_PREFIX = "Could not load delivery status";
const CHANNEL_ORDER = [
  ["desktop", "Desktop"],
  ["email", "Email"],
] as const;
// Widest ECMAScript Date range in ms; a larger ts would make toISOString throw.
const MAX_DATE_MS = 8.64e15;

export type NotificationsPanelProps = {
  client: NotificationsClient;
};

function emptyEmail(): NotificationSettings["email"] {
  return { to: "", from_addr: "", smtp_host: "", smtp_port: 587 };
}

function formatDeliveryTs(ts: number): string {
  const ms = ts * 1000;
  if (!Number.isFinite(ms) || Math.abs(ms) > MAX_DATE_MS) {
    return "—";
  }
  return `${new Date(ms).toISOString().replace("T", " ").slice(0, 19)} UTC`;
}

function retryText(retrySeconds: number, pending: number): string {
  const cadence =
    retrySeconds === 60
      ? "Retrying at most once a minute"
      : `Retrying at most once every ${retrySeconds} seconds`;
  if (pending <= 0) {
    return `${cadence}.`;
  }
  return `${cadence} (${pending} ${pending === 1 ? "alert" : "alerts"} waiting).`;
}

function ChannelDelivery({
  name,
  label,
  state,
  retrySeconds,
}: {
  name: string;
  label: string;
  state: NotificationChannelDelivery;
  retrySeconds: number;
}): JSX.Element {
  const failure = state.last_failure;
  const failing =
    failure !== null &&
    (state.last_sent_ts === null || failure.ts > state.last_sent_ts);
  let detail: JSX.Element;
  if (!state.enabled) {
    detail = <span className="kbot-ops__notifications-delivery-text">Off.</span>;
  } else if (failing && failure !== null) {
    detail = (
      <>
        <span
          className="kbot-ops__notifications-delivery-failure"
          data-testid={`ops-notifications-delivery-${name}-failure`}
        >
          Not delivered. Last failure{" "}
          <time className="kbot-ops__notifications-delivery-ts">
            {formatDeliveryTs(failure.ts)}
          </time>
          : {failure.message}
        </span>
        <span className="kbot-ops__notifications-delivery-text">
          {retryText(retrySeconds, state.pending)}
        </span>
        {state.last_sent_ts !== null ? (
          <span className="kbot-ops__notifications-delivery-text">
            Last delivered{" "}
            <time className="kbot-ops__notifications-delivery-ts">
              {formatDeliveryTs(state.last_sent_ts)}
            </time>
            .
          </span>
        ) : null}
      </>
    );
  } else if (state.last_sent_ts !== null) {
    detail = (
      <>
        <span className="kbot-ops__notifications-delivery-text">
          Last delivered{" "}
          <time className="kbot-ops__notifications-delivery-ts">
            {formatDeliveryTs(state.last_sent_ts)}
          </time>
          .
        </span>
        {failure !== null ? (
          <span className="kbot-ops__notifications-delivery-text">
            Earlier failure{" "}
            <time className="kbot-ops__notifications-delivery-ts">
              {formatDeliveryTs(failure.ts)}
            </time>
            : {failure.message}
          </span>
        ) : null}
      </>
    );
  } else {
    detail = (
      <span className="kbot-ops__notifications-delivery-text">
        Nothing sent yet.
      </span>
    );
  }
  return (
    <li
      className="kbot-ops__notifications-delivery-row"
      data-testid={`ops-notifications-delivery-${name}`}
    >
      <span className="kbot-ops__notifications-delivery-channel">{label}</span>
      {detail}
    </li>
  );
}

export function NotificationsPanel({
  client,
}: NotificationsPanelProps): JSX.Element {
  const [settings, setSettings] = useState<NotificationSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [desktopEnabled, setDesktopEnabled] = useState(false);
  const [emailEnabled, setEmailEnabled] = useState(false);
  const [emailTo, setEmailTo] = useState("");
  const [emailFrom, setEmailFrom] = useState("");
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState("587");
  const [delivery, setDelivery] = useState<NotificationDelivery | null>(null);
  const [deliveryError, setDeliveryError] = useState<string | null>(null);
  const requestRef = useRef(0);
  const deliveryRequestRef = useRef(0);
  const inFlight = useRef(false);

  const hasClient = typeof client.getNotificationSettings === "function";
  const hasDelivery = typeof client.getNotificationDelivery === "function";

  const loadDelivery = useCallback(async (): Promise<void> => {
    if (typeof client.getNotificationDelivery !== "function") {
      return;
    }
    const requestId = ++deliveryRequestRef.current;
    try {
      const next = await client.getNotificationDelivery();
      if (requestId !== deliveryRequestRef.current) {
        return;
      }
      setDelivery(next);
      setDeliveryError(null);
    } catch (err) {
      if (requestId !== deliveryRequestRef.current) {
        return;
      }
      setDelivery(null);
      setDeliveryError(err instanceof Error ? err.message : String(err));
    }
  }, [client]);

  const applySettings = useCallback((next: NotificationSettings): void => {
    setSettings(next);
    setDesktopEnabled(next.desktop_enabled);
    setEmailEnabled(next.email_enabled);
    const email = next.email ?? emptyEmail();
    setEmailTo(email.to);
    setEmailFrom(email.from_addr);
    setSmtpHost(email.smtp_host);
    setSmtpPort(String(email.smtp_port || 587));
  }, []);

  const load = useCallback(async (): Promise<void> => {
    if (!hasClient) {
      setSettings(null);
      setError(CLIENT_UNAVAILABLE);
      setLoading(false);
      return;
    }
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    void loadDelivery();
    try {
      const next = await client.getNotificationSettings();
      if (requestId !== requestRef.current) {
        return;
      }
      applySettings(next);
    } catch (err) {
      if (requestId !== requestRef.current) {
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (requestId === requestRef.current) {
        setLoading(false);
      }
    }
  }, [applySettings, client, hasClient, loadDelivery]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSave = useCallback(async (): Promise<void> => {
    if (inFlight.current || !hasClient) {
      return;
    }
    inFlight.current = true;
    setPending(true);
    setFeedback(null);
    const portNum = Number.parseInt(smtpPort, 10);
    try {
      const next = await client.updateNotificationSettings({
        desktop_enabled: desktopEnabled,
        email_enabled: emailEnabled,
        email: {
          to: emailTo.trim(),
          from_addr: emailFrom.trim(),
          smtp_host: smtpHost.trim(),
          smtp_port: Number.isFinite(portNum) && portNum > 0 ? portNum : 587,
        },
      });
      applySettings(next);
      setFeedback("Notification settings saved.");
    } catch (err) {
      setFeedback(
        `Save failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }, [
    applySettings,
    client,
    desktopEnabled,
    emailEnabled,
    emailFrom,
    emailTo,
    hasClient,
    smtpHost,
    smtpPort,
  ]);

  const busy = pending || loading;
  const passwordEnv =
    settings?.smtp_password_env ?? "KRELLBOT_SMTP_PASSWORD";

  return (
    <div
      className="kbot-ops__notifications"
      data-testid="ops-notifications"
    >
      <div className="kbot-ops__notifications-head">
        <h2 className="kbot-ops__panel-title" id="ops-notifications-title">
          {PANEL_TITLE}
        </h2>
        <button
          type="button"
          className="kbot-ops__notifications-refresh"
          data-testid="ops-notifications-refresh"
          disabled={busy || !hasClient}
          aria-busy={loading || undefined}
          onClick={() => {
            void load();
          }}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      <p className="kbot-ops__notifications-helper">{HELPER_TEXT}</p>

      {!hasClient ? (
        <p
          className="kbot-ops__notifications-error"
          role="alert"
          data-testid="ops-notifications-unavailable"
        >
          {CLIENT_UNAVAILABLE}
        </p>
      ) : null}

      {hasClient && loading && settings === null ? (
        <p
          className="kbot-ops__notifications-status"
          role="status"
          data-testid="ops-notifications-loading"
        >
          {LOADING_TEXT}
        </p>
      ) : null}

      {error !== null ? (
        <p
          className="kbot-ops__notifications-error"
          role="alert"
          data-testid="ops-notifications-error"
        >
          {LOAD_ERROR_PREFIX} ({error})
        </p>
      ) : null}

      {settings !== null ? (
        <form
          className="kbot-ops__notifications-form"
          data-testid="ops-notifications-form"
          onSubmit={(event) => {
            event.preventDefault();
            void handleSave();
          }}
        >
          <label className="kbot-ops__notifications-toggle">
            <input
              type="checkbox"
              data-testid="ops-notifications-desktop"
              checked={desktopEnabled}
              disabled={busy}
              onChange={(event) => setDesktopEnabled(event.target.checked)}
            />
            <span>
              <span className="kbot-ops__notifications-toggle-label">
                {DESKTOP_LABEL}
              </span>
              <span className="kbot-ops__notifications-toggle-hint">
                {DESKTOP_HINT}
              </span>
            </span>
          </label>

          <label className="kbot-ops__notifications-toggle">
            <input
              type="checkbox"
              data-testid="ops-notifications-email"
              checked={emailEnabled}
              disabled={busy}
              onChange={(event) => setEmailEnabled(event.target.checked)}
            />
            <span>
              <span className="kbot-ops__notifications-toggle-label">
                {EMAIL_LABEL}
              </span>
              <span className="kbot-ops__notifications-toggle-hint">
                {EMAIL_HINT_PREFIX}{" "}
                <code className="kbot-ops__notifications-env">{passwordEnv}</code>.
              </span>
            </span>
          </label>

          {emailEnabled ? (
            <div
              className="kbot-ops__notifications-email-fields"
              data-testid="ops-notifications-email-fields"
            >
              <label className="kbot-ops__notifications-field">
                <span>To</span>
                <input
                  type="email"
                  data-testid="ops-notifications-email-to"
                  autoComplete="off"
                  value={emailTo}
                  disabled={busy}
                  onChange={(event) => setEmailTo(event.target.value)}
                />
              </label>
              <label className="kbot-ops__notifications-field">
                <span>From</span>
                <input
                  type="email"
                  data-testid="ops-notifications-email-from"
                  autoComplete="off"
                  value={emailFrom}
                  disabled={busy}
                  onChange={(event) => setEmailFrom(event.target.value)}
                />
              </label>
              <label className="kbot-ops__notifications-field">
                <span>SMTP host</span>
                <input
                  type="text"
                  data-testid="ops-notifications-smtp-host"
                  autoComplete="off"
                  spellCheck={false}
                  value={smtpHost}
                  disabled={busy}
                  onChange={(event) => setSmtpHost(event.target.value)}
                />
              </label>
              <label className="kbot-ops__notifications-field">
                <span>SMTP port</span>
                <input
                  type="number"
                  data-testid="ops-notifications-smtp-port"
                  autoComplete="off"
                  value={smtpPort}
                  disabled={busy}
                  onChange={(event) => setSmtpPort(event.target.value)}
                />
              </label>
            </div>
          ) : null}

          <div className="kbot-ops__notifications-actions">
            <button
              type="submit"
              className="kbot-ops__notifications-save"
              data-testid="ops-notifications-save"
              disabled={busy}
              aria-busy={pending || undefined}
            >
              {pending ? SAVING_LABEL : SAVE_LABEL}
            </button>
          </div>
        </form>
      ) : null}

      {feedback !== null ? (
        <p
          className="kbot-ops__notifications-feedback"
          role="status"
          data-testid="ops-notifications-feedback"
        >
          {feedback}
        </p>
      ) : null}

      {hasClient && hasDelivery && (delivery !== null || deliveryError !== null) ? (
        <section
          className="kbot-ops__notifications-delivery"
          data-testid="ops-notifications-delivery"
          aria-labelledby="ops-notifications-delivery-title"
        >
          <h3
            className="kbot-ops__notifications-delivery-title"
            id="ops-notifications-delivery-title"
          >
            {DELIVERY_TITLE}
          </h3>
          <p className="kbot-ops__notifications-helper">{DELIVERY_HELPER}</p>
          {deliveryError !== null ? (
            <p
              className="kbot-ops__notifications-error"
              role="alert"
              data-testid="ops-notifications-delivery-error"
            >
              {DELIVERY_ERROR_PREFIX} ({deliveryError}).
            </p>
          ) : null}
          {delivery !== null ? (
            <ul className="kbot-ops__notifications-delivery-list">
              {CHANNEL_ORDER.map(([name, label]) => (
                <ChannelDelivery
                  key={name}
                  name={name}
                  label={label}
                  state={delivery.channels[name]}
                  retrySeconds={delivery.retry_seconds}
                />
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
