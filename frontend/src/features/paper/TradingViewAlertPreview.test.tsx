import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TradingViewAlertPreview } from "./TradingViewAlertPreview";

vi.mock("../../session", () => ({ getCsrf: () => "preview-csrf" }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("previews a TradingView alert with the local session and shows paper-only status", async () => {
  const fetchPreview = vi.fn().mockResolvedValue(Response.json({
    ok: true,
    alert: {
      mode: "paper",
      ticker: "KRAKEN:BTCUSD",
      signal: "entry",
      occurred_at: "2026-11-22T18:00:01Z",
    },
  }));
  vi.stubGlobal("fetch", fetchPreview);
  render(<TradingViewAlertPreview />);

  const alertJson = '{"schema_version":"1","rule_id":"btc-entry","ticker":"KRAKEN:BTCUSD","signal":"entry","occurred_at":"2026-11-22T18:00:01Z"}';
  fireEvent.change(screen.getByLabelText("TradingView alert JSON"), { target: { value: alertJson } });
  fireEvent.click(screen.getByRole("button", { name: "Preview alert" }));

  expect((await screen.findByRole("status")).textContent).toMatch(/Paper preview.*KRAKEN:BTCUSD.*No order sent/);
  expect(fetchPreview).toHaveBeenCalledExactlyOnceWith("/api/v1/paper/tradingview/preview", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-Krellbot-CSRF": "preview-csrf" },
    body: alertJson,
  });
});
