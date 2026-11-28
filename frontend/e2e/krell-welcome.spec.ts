/**
 * M39.5 — the welcome on the Krell tab.
 *
 * Real guarded workstation; Krell's and the AI routes are mocked with
 * page.route, so no model or provider is ever contacted.
 * Contract: KRELL-SIGNAL-20261010.
 */
import type { Page, Route } from "@playwright/test";
import { test, expect } from "./journey-harness/journey-harness";
import { clickNav } from "../journeys/helpers/journey-steps";

const CONNECTION = {
  id: "c_aaaaaaaaaaaa",
  label: "Anthropic",
  host: "api.anthropic.com",
  model: "claude-test",
  capability: "tools",
};
const DISCLOSURE =
  "Your conversation with Krell and the Krellbot data she reads are sent to Anthropic (api.anthropic.com). Nothing is sent to krellbot.dev.";
const OPENING = "Hi, I'm Krell. I help you find your way around Krellbot.";
const WELCOME_BODY =
  "There are a few ways to get started. Krell, our AI assistant, can help you below. Or take the guided tour. If you're all set, hit Skip and dive in on your own, with no more guided instructions.";
const PREFERENCE_KEY = "krellbot.guidedPrompts.v1";

type Mock = { connected: boolean };

function sse(events: Array<Record<string, unknown>>): string {
  return events
    .map((e, i) => `id: ${i + 1}\r\nevent: ${String(e.type)}\r\ndata: ${JSON.stringify(e)}\r\n\r\n\r\n`)
    .join("");
}

async function mockKrell(page: Page, mock: Mock): Promise<void> {
  const json = (route: Route, body: unknown) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  const stream = (route: Route, events: Array<Record<string, unknown>>) =>
    route.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body: sse(events) });

  await page.route("**/api/v1/krell/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/krell/conversation" && request.method() === "GET") {
      return json(route, {
        schema_version: "1",
        ok: true,
        effect: "unchanged",
        messages: mock.connected
          ? [
              { id: 1, role: "notice", payload: { kind: "disclosure", text: DISCLOSURE, connection: CONNECTION }, ts: 1 },
              { id: 2, role: "notice", payload: { kind: "opening", text: OPENING, question: "", quick_replies: [] }, ts: 2 },
            ]
          : [],
        profile: { mode: null, answers: {} },
        budget: null,
        connection: mock.connected ? CONNECTION : null,
        disclosure: mock.connected ? DISCLOSURE : null,
      });
    }
    if (path === "/api/v1/krell/messages") {
      return stream(route, [
        { type: "text_delta", text: "Paper runs trade simulated funds only." },
        { type: "message_done", text: "Paper runs trade simulated funds only." },
        { type: "done" },
      ]);
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });

  await page.route("**/api/v1/ai/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/ai/providers") {
      return json(route, {
        ok: true,
        providers: [
          {
            id: "anthropic",
            label: "Anthropic",
            format: "anthropic_messages",
            base_url: "https://api.anthropic.com",
            custom: false,
            credential: "required",
          },
        ],
      });
    }
    if (path === "/api/v1/ai/connections") {
      return json(route, {
        ok: true,
        connections: mock.connected ? [{ ...CONNECTION, preset: "anthropic", active: true }] : [],
        active: mock.connected ? CONNECTION.id : null,
      });
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
}

async function openKrell(page: Page, journey: Parameters<Parameters<typeof test>[2]>[0]["journey"], mock: Mock) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockKrell(page, mock);
  await page.goto(journey.workstation.baseURL);
  await journey.waitForSessionReady(page);
  await clickNav(page, "Krell");
  return page.getByTestId("krell-view");
}

test("a fresh Krell conversation opens with the welcome, then the disclosure sits under the composer", async ({
  journey,
}) => {
  const { page } = journey;
  const view = await openKrell(page, journey, { connected: true });

  const welcome = view.getByTestId("krell-welcome");
  await expect(welcome.getByRole("heading", { name: "Welcome to Krellbot!" })).toBeVisible();
  await expect(welcome.getByText(WELCOME_BODY, { exact: true })).toBeVisible();
  await expect(welcome.getByRole("button", { name: "Take the tour", exact: true })).toBeVisible();
  await expect(welcome.getByRole("button", { name: "Skip", exact: true })).toBeVisible();
  await expect(view.getByText("Krellbot's guide", { exact: false })).toHaveCount(0);

  const composer = await view.getByTestId("krell-input").boundingBox();
  const disclosure = view.getByTestId("krell-disclosure");
  await expect(disclosure).toHaveText(DISCLOSURE);
  const line = await disclosure.boundingBox();
  expect(line!.y).toBeGreaterThan(composer!.y + composer!.height - 1);
  journey.assertGuards();
});

test("the welcome sits above the connect form when no model is connected", async ({ journey }) => {
  const { page } = journey;
  const view = await openKrell(page, journey, { connected: false });
  await expect(view.getByTestId("krell-welcome")).toBeVisible();
  await expect(view.getByTestId("ai-connect-form")).toBeVisible();
  journey.assertGuards();
});

test("Take the tour starts the guided tour", async ({ journey }) => {
  const { page } = journey;
  const view = await openKrell(page, journey, { connected: true });
  await expect(page.getByTestId("workspace-setup-tour")).toHaveCount(0);
  await view.getByRole("button", { name: "Take the tour", exact: true }).click();
  await expect(page.getByTestId("workspace-setup-tour")).toBeVisible();
  journey.assertGuards();
});

test("Skip hides the welcome for good, and Settings brings it back", async ({ journey }) => {
  const { page } = journey;
  const view = await openKrell(page, journey, { connected: true });
  await view.getByRole("button", { name: "Skip", exact: true }).click();
  await expect(view.getByTestId("krell-welcome")).toHaveCount(0);
  await expect(view.getByRole("button", { name: "Tour", exact: true })).toHaveCount(0);
  // Krell stays fully usable, and the disclosure stays.
  await expect(view.getByTestId("krell-input")).toBeVisible();
  await expect(view.getByTestId("krell-disclosure")).toHaveText(DISCLOSURE);

  await page.reload();
  await journey.waitForSessionReady(page);
  await expect(page.getByTestId("krell-view")).toBeVisible();
  await expect(page.getByTestId("krell-welcome")).toHaveCount(0);
  expect(await page.evaluate((key) => window.localStorage.getItem(key), PREFERENCE_KEY)).toContain('"enabled":false');

  await clickNav(page, "Settings");
  await page.getByRole("button", { name: /AI model/ }).click();
  const toggle = page.getByRole("checkbox", { name: "Show the welcome, tour offers and tips" });
  await expect(toggle).not.toBeChecked();
  await toggle.check();

  await clickNav(page, "Krell");
  await expect(page.getByTestId("krell-welcome")).toBeVisible();
  journey.assertGuards();
});

test("after the first message only the compact header shows", async ({ journey }) => {
  const { page } = journey;
  const view = await openKrell(page, journey, { connected: true });
  await expect(view.getByTestId("krell-welcome")).toBeVisible();

  await view.getByTestId("krell-input").fill("What is a paper run?");
  await view.getByTestId("krell-send").click();
  await expect(view.getByText("Paper runs trade simulated funds only.")).toBeVisible();

  await expect(view.getByTestId("krell-welcome")).toHaveCount(0);
  await expect(view.getByRole("heading", { name: "Krell", exact: true })).toBeVisible();
  await expect(view.getByRole("button", { name: "Tour", exact: true })).toBeVisible();
  await expect(view.getByTestId("krell-disclosure")).toHaveText(DISCLOSURE);
  journey.assertGuards();
});
