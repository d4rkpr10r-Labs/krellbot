import { getCsrf } from "../../session";

export type NotificationEmailSettings = {
  to: string;
  from_addr: string;
  smtp_host: string;
  smtp_port: number;
};

export type NotificationSettings = {
  schema_version: string;
  desktop_enabled: boolean;
  email_enabled: boolean;
  email_configured: boolean;
  email: NotificationEmailSettings;
  smtp_password_env: string;
  telemetry_separate: boolean;
};

export type NotificationSettingsPatch = {
  desktop_enabled?: boolean;
  email_enabled?: boolean;
  email?: Partial<NotificationEmailSettings>;
};

export type NotificationChannelFailure = {
  ts: number;
  code: string;
  message: string;
  kind: string | null;
};

export type NotificationChannelDelivery = {
  enabled: boolean;
  last_sent_ts: number | null;
  pending: number;
  last_failure: NotificationChannelFailure | null;
};

export type NotificationDelivery = {
  schema_version: string;
  retry_seconds: number;
  channels: {
    desktop: NotificationChannelDelivery;
    email: NotificationChannelDelivery;
  };
};

export type NotificationsClient = {
  getNotificationSettings(): Promise<NotificationSettings>;
  updateNotificationSettings(
    patch: NotificationSettingsPatch,
  ): Promise<NotificationSettings>;
  /** Real per-channel delivery record. Optional so older clients still type. */
  getNotificationDelivery?(): Promise<NotificationDelivery>;
};

const DEFAULT_RETRY_SECONDS = 60;

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function adaptFailure(raw: unknown): NotificationChannelFailure | null {
  if (raw === null || typeof raw !== "object") {
    return null;
  }
  const obj = raw as Record<string, unknown>;
  const ts = finiteNumber(obj.ts);
  if (ts === null || typeof obj.code !== "string" || obj.code === "") {
    return null;
  }
  return {
    ts,
    code: obj.code,
    message:
      typeof obj.message === "string" && obj.message !== ""
        ? obj.message
        : "The notification was not delivered.",
    kind: typeof obj.kind === "string" && obj.kind !== "" ? obj.kind : null,
  };
}

function adaptChannelDelivery(raw: unknown): NotificationChannelDelivery {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const pending = finiteNumber(obj.pending);
  return {
    enabled: obj.enabled === true,
    last_sent_ts: finiteNumber(obj.last_sent_ts),
    pending: pending !== null && pending > 0 ? Math.floor(pending) : 0,
    last_failure: adaptFailure(obj.last_failure),
  };
}

export function adaptNotificationDelivery(raw: unknown): NotificationDelivery {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const channels = (obj.channels ?? {}) as Record<string, unknown>;
  const retry = finiteNumber(obj.retry_seconds);
  return {
    schema_version:
      typeof obj.schema_version === "string" ? obj.schema_version : "1",
    retry_seconds: retry !== null && retry > 0 ? retry : DEFAULT_RETRY_SECONDS,
    channels: {
      desktop: adaptChannelDelivery(channels.desktop),
      email: adaptChannelDelivery(channels.email),
    },
  };
}

function adaptEmail(raw: unknown): NotificationEmailSettings {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const portRaw = obj.smtp_port;
  const port =
    typeof portRaw === "number" && Number.isFinite(portRaw)
      ? portRaw
      : typeof portRaw === "string"
        ? Number.parseInt(portRaw, 10)
        : 587;
  return {
    to: typeof obj.to === "string" ? obj.to : "",
    from_addr: typeof obj.from_addr === "string" ? obj.from_addr : "",
    smtp_host: typeof obj.smtp_host === "string" ? obj.smtp_host : "",
    smtp_port: Number.isFinite(port) && port > 0 ? port : 587,
  };
}

export function adaptNotificationSettings(raw: unknown): NotificationSettings {
  const obj = (raw ?? {}) as Record<string, unknown>;
  return {
    schema_version:
      typeof obj.schema_version === "string" ? obj.schema_version : "1",
    desktop_enabled: obj.desktop_enabled === true,
    email_enabled: obj.email_enabled === true,
    email_configured: obj.email_configured === true,
    email: adaptEmail(obj.email),
    smtp_password_env:
      typeof obj.smtp_password_env === "string"
        ? obj.smtp_password_env
        : "KRELLBOT_SMTP_PASSWORD",
    telemetry_separate: obj.telemetry_separate !== false,
  };
}

export function createNotificationsHttpClient(): NotificationsClient {
  return {
    async getNotificationSettings(): Promise<NotificationSettings> {
      const response = await fetch("/api/v1/notifications", {
        method: "GET",
        credentials: "include",
        headers: {
          "X-Krellbot-CSRF": getCsrf(),
        },
      });
      if (!response.ok) {
        throw new Error(`notification settings failed: ${response.status}`);
      }
      return adaptNotificationSettings(await response.json());
    },
    async updateNotificationSettings(
      patch: NotificationSettingsPatch,
    ): Promise<NotificationSettings> {
      const response = await fetch("/api/v1/notifications", {
        method: "PUT",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-Krellbot-CSRF": getCsrf(),
        },
        body: JSON.stringify(patch),
      });
      if (!response.ok) {
        let detail = "";
        try {
          const errBody = (await response.json()) as { message?: unknown };
          if (typeof errBody.message === "string") {
            detail = `: ${errBody.message}`;
          }
        } catch {
          // optional
        }
        throw new Error(
          `notification settings update failed: ${response.status}${detail}`,
        );
      }
      return adaptNotificationSettings(await response.json());
    },
    async getNotificationDelivery(): Promise<NotificationDelivery> {
      const response = await fetch("/api/v1/notifications/delivery", {
        method: "GET",
        credentials: "include",
        headers: {
          "X-Krellbot-CSRF": getCsrf(),
        },
      });
      if (!response.ok) {
        throw new Error(`notification delivery status failed: ${response.status}`);
      }
      return adaptNotificationDelivery(await response.json());
    },
  };
}
