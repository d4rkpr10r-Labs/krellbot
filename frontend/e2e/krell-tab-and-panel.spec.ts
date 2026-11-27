/**
 * M38.16 — the Krell tab, the "Ask Krell" side panel and the avatar.
 *
 * Real guarded workstation for the shell; Krell's and the AI routes are
 * mocked with page.route, so no model or provider is ever contacted.
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
const QUESTION = "Are you new to algo trading, or experienced?";

type Mock = { connected: boolean; conversationReads: number; sent: unknown[] };

function sse(events: Array<Record<string, unknown>>): string {
  return events
    .map((e, i) => `id: ${i + 1}\r\nevent: ${String(e.type)}\r\ndata: ${JSON.stringify(e)}\r\n\r\n\r\n`)
    .join("");
}

function greeted(): unknown[] {
  return [
    { id: 1, role: "notice", payload: { kind: "disclosure", text: DISCLOSURE, connection: CONNECTION }, ts: 1 },
    {
      id: 2,
      role: "notice",
      payload: { kind: "opening", text: OPENING, question: QUESTION, quick_replies: ["Standard", "Advanced"] },
      ts: 2,
    },
  ];
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
      mock.conversationReads += 1;
      return json(route, {
        schema_version: "1",
        ok: true,
        effect: "unchanged",
        messages: mock.connected ? greeted() : [],
        profile: { mode: null, answers: {} },
        budget: null,
        connection: mock.connected ? CONNECTION : null,
        disclosure: mock.connected ? DISCLOSURE : null,
      });
    }
    if (path === "/api/v1/krell/start") {
      return stream(route, [
        { type: "notice", kind: "disclosure", text: DISCLOSURE, connection: CONNECTION },
        { type: "opening", text: OPENING, question: QUESTION, quick_replies: ["Standard", "Advanced"] },
        { type: "done" },
      ]);
    }
    if (path === "/api/v1/krell/messages") {
      mock.sent.push(request.postDataJSON());
      return stream(route, [
        { type: "text_delta", text: "Paper runs trade " },
        { type: "text_delta", text: "simulated funds only." },
        { type: "message_done", text: "Paper runs trade simulated funds only." },
        { type: "done" },
      ]);
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });

  await page.route("**/api/v1/ai/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
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
    if (path === "/api/v1/ai/connections" && request.method() === "POST") {
      return json(route, {
        ok: true,
        connection: { ...CONNECTION, preset: "anthropic", active: false },
        probe: { capability: "tools", message: "" },
      });
    }
    if (path === "/api/v1/ai/connections") {
      return json(route, { ok: true, connections: [], active: mock.connected ? CONNECTION.id : null });
    }
    if (path.endsWith("/activate")) {
      mock.connected = true;
      return json(route, { ok: true, connection: { ...CONNECTION, active: true } });
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
}

async function expectTextOnly(page: Page): Promise<void> {
  const tab = page
    .getByRole("navigation", { name: "Workstation navigation" })
    .getByRole("button", { name: "Krell", exact: true });
  const ask = page.getByTestId("ask-krell");
  await expect(tab).toHaveText("Krell");
  await expect(ask).toHaveText("Ask Krell");
  for (const control of [tab, ask]) {
    await expect(control.locator("img, svg, picture")).toHaveCount(0);
    expect(await control.evaluate((el) => getComputedStyle(el).backgroundImage)).toBe("none");
  }
}

test("Krell is the first tab and shows the connect form until a model connects", async ({ journey }) => {
  const { page } = journey;
  const mock: Mock = { connected: false, conversationReads: 0, sent: [] };
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockKrell(page, mock);
  await page.goto(journey.workstation.baseURL);
  await journey.waitForSessionReady(page);

  const primary = page.getByTestId("kbot-nav-primary").locator(".kbot-nav__item");
  await expect(primary.first()).toHaveText("Krell");
  await expect(primary.nth(1)).toHaveText("Workspace");
  await expectTextOnly(page);
  // Nothing about Krell is read until the tab or the panel is opened.
  expect(mock.conversationReads).toBe(0);

  await clickNav(page, "Krell");
  await expect(page).toHaveURL(/[?&]view=krell(?:&|$)/);
  await expect(page.getByTestId("ai-connect-form")).toBeVisible();
  await expect(page.getByTestId("krell-input")).toHaveCount(0);

  await page.getByLabel("API key").fill("sk-test-not-a-real-key");
  await page.getByLabel("Model", { exact: true }).fill("claude-test");
  await page.getByRole("button", { name: "Connect", exact: true }).click();

  const view = page.getByTestId("krell-view");
  await expect(view.getByTestId("krell-input")).toBeVisible();
  await expect(page.getByTestId("ai-connect-form")).toHaveCount(0);
  await expect(view.getByText(OPENING)).toBeVisible();
  await expect(view.getByText(DISCLOSURE)).toBeVisible();
  await expect(view.getByTestId("krell-avatar")).toHaveCount(1);
  await expectTextOnly(page);
  journey.assertGuards();
});

test("tab and panel share one conversation", async ({ journey }) => {
  const { page } = journey;
  const mock: Mock = { connected: true, conversationReads: 0, sent: [] };
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockKrell(page, mock);
  await page.goto(journey.workstation.baseURL);
  await journey.waitForSessionReady(page);

  await page.getByTestId("ask-krell").click();
  const panel = page.getByTestId("krell-panel");
  await expect(panel).toBeVisible();
  const docked = await panel.boundingBox();
  expect(docked).toEqual({ x: 1440 - 340, y: 0, width: 340, height: 900 });
  // The docked panel does not cover the header's Ask Krell button.
  const ask = await page.getByTestId("ask-krell").boundingBox();
  expect(ask!.x + ask!.width).toBeLessThanOrEqual(1440 - 340);

  await panel.getByTestId("krell-input").fill("What is a paper run?");
  await panel.getByTestId("krell-send").click();
  await expect(panel.getByText("Paper runs trade simulated funds only.")).toBeVisible();
  expect(mock.sent).toEqual([{ text: "What is a paper run?" }]);
  await expect(panel.getByTestId("krell-message-krell")).toHaveCount(2);
  // One avatar per run of Krell's messages: the opening, then the reply.
  await expect(panel.getByTestId("krell-avatar")).toHaveCount(2);

  await panel.getByRole("button", { name: "Open in Krell tab" }).click();
  await expect(page).toHaveURL(/[?&]view=krell(?:&|$)/);
  await expect(
    page
      .getByRole("navigation", { name: "Workstation navigation" })
      .getByRole("button", { name: "Krell", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(page.getByTestId("krell-panel")).toHaveCount(0);
  const view = page.getByTestId("krell-view");
  await expect(view.getByTestId("krell-message-user")).toHaveText(/What is a paper run\?/);
  await expect(view.getByText("Paper runs trade simulated funds only.")).toBeVisible();
  await expect(view.getByTestId("krell-avatar")).toHaveCount(2);
  // Moving to the tab carries focus to its composer.
  await expect(view.getByTestId("krell-input")).toBeFocused();
  expect(mock.conversationReads).toBe(1);
  await expectTextOnly(page);
  journey.assertGuards();
});

test("Escape and Close return focus to Ask Krell, which focuses the composer on the Krell tab", async ({ journey }) => {
  const { page } = journey;
  const mock: Mock = { connected: true, conversationReads: 0, sent: [] };
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockKrell(page, mock);
  await page.goto(journey.workstation.baseURL);
  await journey.waitForSessionReady(page);

  const ask = page.getByTestId("ask-krell");
  await ask.click();
  const panel = page.getByTestId("krell-panel");
  await expect(panel.getByTestId("krell-input")).toBeVisible();
  // Docked on desktop: not modal, and the workstation stays usable.
  await expect(panel).not.toHaveAttribute("role", "dialog");
  await expect(page.locator("main.kbot-shell__main")).not.toHaveAttribute("inert");
  await panel.getByTestId("krell-input").focus();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("krell-panel")).toHaveCount(0);
  await expect(ask).toBeFocused();
  await expect(ask).toHaveAttribute("aria-expanded", "false");

  await ask.click();
  await page.getByTestId("krell-panel").getByRole("button", { name: "Close" }).click();
  await expect(page.getByTestId("krell-panel")).toHaveCount(0);
  await expect(ask).toBeFocused();

  // On the Krell tab the button focuses the tab's composer; no second conversation opens.
  await clickNav(page, "Krell");
  const view = page.getByTestId("krell-view");
  await expect(view.getByTestId("krell-input")).toBeVisible();
  await ask.click();
  await expect(page.getByTestId("krell-panel")).toHaveCount(0);
  await expect(page.getByTestId("krell-input")).toHaveCount(1);
  await expect(view.getByTestId("krell-input")).toBeFocused();
  await expect(ask).toHaveAttribute("aria-expanded", "false");
  journey.assertGuards();
});

test("the setup tour waits behind the Krell sheet and keeps its own Escape", async ({ journey }) => {
  const { page } = journey;
  const mock: Mock = { connected: true, conversationReads: 0, sent: [] };
  await page.context().addInitScript(() => {
    try {
      window.localStorage.setItem("krellbot.home.setupTour", JSON.stringify({ status: "active", stepId: "workspace" }));
    } catch {
      // Best-effort seed only.
    }
  });
  const tourState = () => page.evaluate(() => window.localStorage.getItem("krellbot.home.setupTour"));
  await page.setViewportSize({ width: 390, height: 844 });
  await mockKrell(page, mock);
  await page.goto(journey.workstation.baseURL);
  await journey.waitForSessionReady(page);
  const tour = page.getByTestId("workspace-setup-tour");
  await expect(tour).toHaveCount(1);

  // Phone: nothing outside the modal sheet stays on top of it or reachable.
  await page.getByTestId("ask-krell").click();
  const panel = page.getByTestId("krell-panel");
  await expect(panel.getByTestId("krell-input")).toBeVisible();
  await expect(tour).toHaveCount(0);
  await panel.getByTestId("krell-input").focus();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("krell-panel")).toHaveCount(0);
  await expect(tour).toHaveCount(1);
  expect(JSON.parse((await tourState()) ?? "{}").status).toBe("active");

  // Desktop: the docked panel's Escape closes only the panel.
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByTestId("ask-krell").click();
  await expect(page.getByTestId("krell-panel").getByTestId("krell-input")).toBeVisible();
  await expect(tour).toHaveCount(1);
  await page.getByTestId("krell-panel").getByTestId("krell-input").focus();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("krell-panel")).toHaveCount(0);
  await expect(tour).toHaveCount(1);
  expect(JSON.parse((await tourState()) ?? "{}").status).toBe("active");
  journey.assertGuards();
});

test("panel becomes a full-screen sheet at phone width", async ({ journey }) => {
  const { page } = journey;
  const mock: Mock = { connected: true, conversationReads: 0, sent: [] };
  await page.setViewportSize({ width: 390, height: 844 });
  await mockKrell(page, mock);
  await page.goto(journey.workstation.baseURL);
  await journey.waitForSessionReady(page);

  const ask = page.getByTestId("ask-krell");
  await expect(ask).toHaveAccessibleName("Ask Krell");
  const askBox = await ask.boundingBox();
  expect(askBox!.height).toBeGreaterThanOrEqual(44);
  // The button and Search share the brand row, so the compact phone header keeps its rows.
  const brandBox = await page.getByTestId("kbot-shell-brand").boundingBox();
  const searchBox = await page.getByTestId("kbot-shell-palette-hint").boundingBox();
  expect(Math.abs(askBox!.y - brandBox!.y)).toBeLessThanOrEqual(2);
  expect(Math.abs(searchBox!.y - brandBox!.y)).toBeLessThanOrEqual(2);
  await ask.click();
  const panel = page.getByTestId("krell-panel");
  await expect(panel.getByText(OPENING)).toBeVisible();
  expect(await panel.boundingBox()).toEqual({ x: 0, y: 0, width: 390, height: 844 });
  const overflowX = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflowX).toBeLessThanOrEqual(1);

  // The sheet is modal: the workstation behind it is out of the tab order and the tree.
  await expect(panel).toHaveAttribute("role", "dialog");
  await expect(panel).toHaveAttribute("aria-modal", "true");
  await expect(page.locator("header.kbot-shell__header")).toHaveAttribute("inert", "");
  await expect(page.locator("main.kbot-shell__main")).toHaveAttribute("inert", "");
  for (let step = 0; step < 8; step += 1) {
    await page.keyboard.press("Tab");
    const inside = await page.evaluate(
      () =>
        document.activeElement === document.body ||
        document.activeElement?.closest('[data-testid="krell-panel"]') !== null,
    );
    expect(inside).toBe(true);
  }

  // >=44px targets, and Close (the way off the sheet) returns focus to Ask Krell.
  for (const name of ["Open in Krell tab", "Close"]) {
    const box = await panel.getByRole("button", { name }).boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }
  await panel.getByRole("button", { name: "Close" }).click();
  await expect(page.getByTestId("krell-panel")).toHaveCount(0);
  await expect(page.locator("main.kbot-shell__main")).not.toHaveAttribute("inert");
  await expect(ask).toHaveAttribute("aria-expanded", "false");
  await expect(ask).toBeFocused();
  journey.assertGuards();
});
