/**
 * M38.19 — First run, "Meet Krell (connect your AI)".
 *
 * Real guarded workstation on a fresh home, so the first-run wizard opens.
 * The AI and Krell routes are mocked with page.route: no model or provider
 * is ever contacted. Contract: KRELL-SIGNAL-20261010.
 */
import type { Page, Route } from "@playwright/test";
import { test, expect } from "./journey-harness/journey-harness";

test.use({ wizardColdOpen: "fresh" });

const WIZARD_KEY = "krellbot.home.firstRunWizard";
const MEET_KRELL_LABEL = "Meet Krell (connect your AI)";
const FAILED_MESSAGE = "The model did not answer the check.";
const CONNECTION = {
  id: "c_aaaaaaaaaaaa",
  label: "Anthropic",
  host: "api.anthropic.com",
  model: "claude-test",
  capability: "tools",
};
const DISCLOSURE =
  "Your conversation with Krell and the Krellbot data she reads are sent to Anthropic (api.anthropic.com). Nothing is sent to krellbot.dev.";
const OPENING = "Hey there. I'm Krell! Welcome to Krellbot.";
const QUESTION = "Are you new to algo trading, or experienced?";

type Mock = {
  probe: "tools" | "failed";
  active: string | null;
  started: boolean;
  starts: number;
  /** Every non-GET request to the AI routes, as "METHOD path". */
  aiWrites: string[];
};

function newMock(probe: Mock["probe"]): Mock {
  return { probe, active: null, started: false, starts: 0, aiWrites: [] };
}

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

async function mockAiAndKrell(page: Page, mock: Mock): Promise<void> {
  const json = (route: Route, body: unknown) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

  await page.route("**/api/v1/krell/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/krell/conversation" && request.method() === "GET") {
      const connected = mock.active === CONNECTION.id;
      return json(route, {
        schema_version: "1",
        ok: true,
        effect: "unchanged",
        messages: mock.started ? greeted() : [],
        profile: { mode: null, answers: {} },
        budget: null,
        connection: connected ? CONNECTION : null,
        disclosure: connected ? DISCLOSURE : null,
      });
    }
    if (path === "/api/v1/krell/start") {
      mock.starts += 1;
      mock.started = true;
      return route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
        body: sse([
          { type: "notice", kind: "disclosure", text: DISCLOSURE, connection: CONNECTION },
          { type: "opening", text: OPENING, question: QUESTION, quick_replies: ["Standard", "Advanced"] },
          { type: "done" },
        ]),
      });
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });

  await page.route("**/api/v1/ai/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET") {
      mock.aiWrites.push(`${request.method()} ${path}`);
    }
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
        connection: { ...CONNECTION, capability: mock.probe, preset: "anthropic", active: false },
        probe: { capability: mock.probe, message: mock.probe === "failed" ? FAILED_MESSAGE : "" },
      });
    }
    if (path === "/api/v1/ai/connections") {
      return json(route, { ok: true, connections: [], active: mock.active });
    }
    if (path === `/api/v1/ai/connections/${CONNECTION.id}/activate`) {
      mock.active = CONNECTION.id;
      return json(route, { ok: true, connection: { ...CONNECTION, active: true } });
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
}

async function wizardState(page: Page): Promise<string | null> {
  return page.evaluate((key) => window.localStorage.getItem(key), WIZARD_KEY);
}

async function expectWelcome(page: Page): Promise<void> {
  const wizard = page.getByTestId("first-run-wizard");
  await expect(wizard).toBeVisible();
  await expect(wizard).toHaveAttribute("data-wizard-step", "welcome");
  await expect(page.getByTestId("ai-connect-form")).toHaveCount(0);
  await expect(page.getByTestId("first-run-wizard-primary")).toHaveText("Let’s go");
  await expect(page.getByTestId("first-run-wizard-meet-krell")).toHaveText(MEET_KRELL_LABEL);
  await expect(page.getByTestId("kbot-shell")).toHaveCount(0);
}

async function fillAndConnect(page: Page): Promise<void> {
  const form = page.getByTestId("ai-connect-form");
  await expect(form).toBeVisible();
  await form.getByLabel("API key").fill("sk-test-not-a-real-key");
  await form.getByLabel("Model", { exact: true }).fill("claude-test");
  await form.getByRole("button", { name: "Connect", exact: true }).click();
}

test("Meet Krell connects a model, completes the wizard and opens Krell's opening in the Krell tab", async ({
  journey,
}) => {
  const { page } = journey;
  const mock = newMock("tools");
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockAiAndKrell(page, mock);
  await page.goto(journey.workstation.baseURL);

  await expectWelcome(page);
  const wizard = page.getByTestId("first-run-wizard");
  // One sage primary; Meet Krell is the secondary beside it.
  await expect(wizard.locator(".kbot-first-run-wizard__primary")).toHaveCount(1);
  await expect(wizard.locator("button.kbot-first-run-wizard__secondary")).toHaveCount(1);
  await expect(page.getByTestId("first-run-wizard-skip")).toHaveCount(0);

  await page.getByTestId("first-run-wizard-meet-krell").click();
  await expect(wizard).toHaveAttribute("data-wizard-step", "welcome");
  await fillAndConnect(page);

  await expect(page.getByTestId("first-run-wizard")).toHaveCount(0);
  await journey.waitForSessionReady(page);
  await expect(page).toHaveURL(/[?&]view=krell(?:&|$)/);
  await expect(
    page
      .getByRole("navigation", { name: "Workstation navigation" })
      .getByRole("button", { name: "Krell", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  const view = page.getByTestId("krell-view");
  await expect(view.getByText(OPENING)).toBeVisible();
  await expect(view.getByTestId("krell-input")).toBeVisible();
  // Krell takes over the first run; the workspace tour stays closed.
  await expect(page.getByTestId("workspace-setup-tour")).toHaveCount(0);
  expect(mock.starts, "the opening is asked for once, by the Krell tab").toBe(1);
  expect(JSON.parse((await wizardState(page)) ?? "{}").status).toBe("completed");

  await page.reload();
  await journey.waitForSessionReady(page);
  await expect(page.getByTestId("first-run-wizard")).toHaveCount(0);
  await expect(page.getByTestId("krell-view").getByText(OPENING)).toBeVisible();
  expect(mock.starts).toBe(1);
  journey.assertGuards();
});

test("cancelling Meet Krell returns to welcome with nothing persisted", async ({ journey }) => {
  const { page } = journey;
  const mock = newMock("tools");
  await page.setViewportSize({ width: 390, height: 844 });
  await mockAiAndKrell(page, mock);
  await page.goto(journey.workstation.baseURL);

  await expectWelcome(page);
  const meet = page.getByTestId("first-run-wizard-meet-krell");
  const box = await meet.boundingBox();
  expect(box, "Meet Krell is on screen at phone width").not.toBeNull();
  expect(box!.height).toBeGreaterThanOrEqual(44);
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390 + 1);
  expect(await wizardState(page)).toBeNull();

  await meet.click();
  const form = page.getByTestId("ai-connect-form");
  await expect(form).toBeVisible();
  // The form's fields stay inside its card at phone width.
  const width = await form.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
  expect(width.scroll, "connect form does not overflow at 390px").toBeLessThanOrEqual(width.client);
  await form.getByRole("button", { name: "Cancel", exact: true }).click();

  await expectWelcome(page);
  expect(await wizardState(page)).toBeNull();
  expect(mock.aiWrites).toEqual([]);

  await page.reload();
  await expectWelcome(page);
  journey.assertGuards();
});

test("an unusable model stays in the wizard and Back returns to welcome", async ({ journey }) => {
  const { page } = journey;
  const mock = newMock("failed");
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockAiAndKrell(page, mock);
  await page.goto(journey.workstation.baseURL);

  await expectWelcome(page);
  await page.getByTestId("first-run-wizard-meet-krell").click();
  await fillAndConnect(page);

  await expect(page.getByTestId("ai-capability-failed")).toHaveText(FAILED_MESSAGE);
  const wizard = page.getByTestId("first-run-wizard");
  await expect(wizard).toHaveAttribute("data-wizard-step", "welcome");
  await expect(page.getByTestId("ai-connect-form")).toBeVisible();
  await expect(page.getByTestId("kbot-shell")).toHaveCount(0);
  await expect(page).not.toHaveURL(/[?&]view=krell(?:&|$)/);
  expect(await wizardState(page)).toBeNull();

  await page.getByTestId("first-run-wizard-meet-krell-back").click();
  await expectWelcome(page);
  expect(await wizardState(page)).toBeNull();
  expect(mock.aiWrites).toEqual(["POST /api/v1/ai/connections"]);
  expect(mock.starts).toBe(0);
  journey.assertGuards();
});
