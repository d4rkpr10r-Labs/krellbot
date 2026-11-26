/**
 * NotificationsPanel shows whether alert notifications were really delivered.
 *
 * The engine records a send only when the channel succeeded and keeps each
 * channel's last failure at GET /api/v1/notifications/delivery. The panel
 * must say when a channel is failing, with the time and the reason, and must
 * not call a channel healthy because a send was attempted.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NotificationsPanel } from "./NotificationsPanel";
import {
  adaptNotificationDelivery,
  createNotificationsHttpClient,
  type NotificationChannelDelivery,
  type NotificationDelivery,
  type NotificationSettings,
  type NotificationsClient,
} from "./notificationsClient";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// 2023-11-14 22:13:20 UTC and two minutes later.
const T_FAIL = 1_700_000_000;
const T_SENT = 1_700_000_120;

function settings(over: Partial<NotificationSettings> = {}): NotificationSettings {
  return {
    schema_version: "1",
    desktop_enabled: true,
    email_enabled: true,
    email_configured: true,
    email: {
      to: "ops@example.com",
      from_addr: "",
      smtp_host: "smtp.example.com",
      smtp_port: 587,
    },
    smtp_password_env: "KRELLBOT_SMTP_PASSWORD",
    telemetry_separate: true,
    ...over,
  };
}

function channel(over: Partial<NotificationChannelDelivery> = {}): NotificationChannelDelivery {
  return {
    enabled: true,
    last_sent_ts: null,
    pending: 0,
    last_failure: null,
    ...over,
  };
}

function delivery(
  desktop: NotificationChannelDelivery,
  email: NotificationChannelDelivery,
): NotificationDelivery {
  return {
    schema_version: "1",
    retry_seconds: 60,
    channels: { desktop, email },
  };
}

function makeClient(
  getNotificationDelivery: NotificationsClient["getNotificationDelivery"],
): NotificationsClient {
  return {
    getNotificationSettings: vi.fn().mockResolvedValue(settings()),
    updateNotificationSettings: vi.fn().mockResolvedValue(settings()),
    getNotificationDelivery,
  };
}

describe("NotificationsPanel delivery status", () => {
  it("names a failing channel with the failure time, reason and retry cadence", async () => {
    const client = makeClient(
      vi.fn().mockResolvedValue(
        delivery(
          channel({
            pending: 2,
            last_failure: {
              ts: T_FAIL,
              code: "desktop_command_missing",
              message: "The desktop notification command was not found.",
              kind: "needs_reconcile",
            },
          }),
          channel({ last_sent_ts: T_SENT }),
        ),
      ),
    );
    render(<NotificationsPanel client={client} />);

    const desktop = await screen.findByTestId("ops-notifications-delivery-desktop");
    const failure = within(desktop).getByTestId("ops-notifications-delivery-desktop-failure");
    expect(failure.textContent).toContain("Not delivered");
    expect(failure.textContent).toContain("2023-11-14 22:13:20 UTC");
    expect(failure.textContent).toContain("The desktop notification command was not found.");
    expect(desktop.textContent).toMatch(/at most once a minute/i);
    expect(desktop.textContent).toMatch(/2 alerts waiting/i);
    expect(desktop.textContent).not.toMatch(/last delivered/i);

    const email = screen.getByTestId("ops-notifications-delivery-email");
    expect(email.textContent).toContain("Last delivered 2023-11-14 22:15:20 UTC");
    expect(within(email).queryByTestId("ops-notifications-delivery-email-failure")).toBeNull();
  });

  it("keeps an earlier failure visible after a later delivery without calling it current", async () => {
    const client = makeClient(
      vi.fn().mockResolvedValue(
        delivery(
          channel({
            last_sent_ts: T_SENT,
            last_failure: {
              ts: T_FAIL,
              code: "desktop_timeout",
              message: "The desktop notification command timed out.",
              kind: "scheduler_stale",
            },
          }),
          channel({ enabled: false }),
        ),
      ),
    );
    render(<NotificationsPanel client={client} />);

    const desktop = await screen.findByTestId("ops-notifications-delivery-desktop");
    expect(desktop.textContent).toContain("Last delivered 2023-11-14 22:15:20 UTC");
    expect(desktop.textContent).toContain("Earlier failure 2023-11-14 22:13:20 UTC");
    expect(desktop.textContent).not.toContain("Not delivered");
    expect(within(desktop).queryByTestId("ops-notifications-delivery-desktop-failure")).toBeNull();
  });

  it("says when a channel is off or has sent nothing yet", async () => {
    const client = makeClient(
      vi.fn().mockResolvedValue(delivery(channel(), channel({ enabled: false }))),
    );
    render(<NotificationsPanel client={client} />);

    const desktop = await screen.findByTestId("ops-notifications-delivery-desktop");
    expect(desktop.textContent).toMatch(/nothing sent yet/i);
    expect(screen.getByTestId("ops-notifications-delivery-email").textContent).toMatch(/off/i);
  });

  it("reloads delivery status on Refresh", async () => {
    const getDelivery = vi.fn().mockResolvedValue(delivery(channel(), channel()));
    const client = makeClient(getDelivery);
    render(<NotificationsPanel client={client} />);
    await screen.findByTestId("ops-notifications-delivery-desktop");
    expect(getDelivery).toHaveBeenCalledTimes(1);

    const refresh = screen.getByTestId("ops-notifications-refresh") as HTMLButtonElement;
    await waitFor(() => expect(refresh.disabled).toBe(false));
    fireEvent.click(refresh);
    await waitFor(() => expect(getDelivery).toHaveBeenCalledTimes(2));
  });

  it("reports a delivery status read failure and keeps the settings usable", async () => {
    const client = makeClient(vi.fn().mockRejectedValue(new Error("delivery read failed: 503")));
    render(<NotificationsPanel client={client} />);

    const error = await screen.findByTestId("ops-notifications-delivery-error");
    expect(error.textContent).toContain("delivery read failed: 503");
    expect(screen.getByTestId("ops-notifications-form")).toBeTruthy();
    expect(screen.queryByTestId("ops-notifications-delivery-desktop")).toBeNull();
  });
});

describe("notifications delivery client", () => {
  it("GETs /api/v1/notifications/delivery with the session cookie", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        schema_version: "1",
        retry_seconds: 60,
        channels: {
          desktop: {
            enabled: true,
            last_sent_ts: null,
            pending: 1,
            last_failure: {
              ts: T_FAIL,
              code: "desktop_command_failed",
              message: "The desktop notification command exited with an error.",
              kind: "store_fault",
            },
          },
          email: { enabled: false, last_sent_ts: null, pending: 0, last_failure: null },
        },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = createNotificationsHttpClient();
    expect(typeof client.getNotificationDelivery).toBe("function");
    const got = await client.getNotificationDelivery!();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/notifications/delivery",
      expect.objectContaining({ method: "GET", credentials: "include" }),
    );
    expect(got.channels.desktop.last_failure?.code).toBe("desktop_command_failed");
    expect(got.channels.desktop.pending).toBe(1);
    expect(got.channels.email.last_failure).toBeNull();
  });

  it("treats a malformed body as no delivery evidence", () => {
    const adapted = adaptNotificationDelivery({
      channels: { desktop: { last_failure: { ts: "soon" } } },
    });
    expect(adapted.channels.desktop.last_failure).toBeNull();
    expect(adapted.channels.desktop.last_sent_ts).toBeNull();
    expect(adapted.channels.email.enabled).toBe(false);
    expect(adapted.retry_seconds).toBe(60);
  });
});
