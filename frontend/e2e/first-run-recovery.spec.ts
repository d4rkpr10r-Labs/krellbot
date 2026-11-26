/**
 * First-run recovery in a real browser against a guarded workstation (KQ-04).
 *
 * - The server arms, the browser loses the response, and Check status finds
 *   that arm and continues without a second arm.
 * - After a reload the same Start is refused as a duplicate; the wizard
 *   reads that as already running and the server still holds one arm.
 * - The first session start fails; the next action retries it and keeps
 *   the chosen starter and venue.
 */
import type { Page, Route } from "@playwright/test";
import { expect, test } from "./journey-harness/journey-harness";

test.use({ wizardColdOpen: "fresh" });

type CommandBody = { command?: string; payload?: Record<string, unknown> };

function commandOf(route: Route): CommandBody {
  try {
    return (route.request().postDataJSON() ?? {}) as CommandBody;
  } catch {
    return {};
  }
}

/** Server-side armed fleet, read through the same loopback session. */
async function fleet(page: Page): Promise<Array<{ venue: string; pair: string; pack_id: string; mode: string }>> {
  return page.evaluate(async () => {
    const response = await fetch("/api/v1/paper/fleet", { credentials: "include" });
    const body = (await response.json()) as {
      deployments?: Array<{ venue: string; pair: string; pack_id: string; mode: string }>;
    };
    return body.deployments ?? [];
  });
}

async function chooseStarter(page: Page, venue: "kraken" | "coinbase", starterId: string): Promise<void> {
  await expect(page.getByTestId("first-run-wizard")).toHaveAttribute("data-wizard-step", "welcome");
  await page.getByTestId("first-run-wizard-primary").click();
  await page.getByTestId("first-run-wizard-market").selectOption(venue);
  await page.getByTestId(`first-run-wizard-use-${starterId}`).click();
  await expect(page.getByTestId("first-run-wizard")).toHaveAttribute("data-wizard-step", "start_paper");
}

/** Let the server arm, then drop the 200 before the page sees it. */
async function loseFirstArmResponse(page: Page): Promise<{ arms: () => number }> {
  let arms = 0;
  await page.route("**/api/v1/commands", async (route) => {
    if (route.request().method() !== "POST" || commandOf(route).command !== "paper.arm") {
      await route.fallback();
      return;
    }
    arms += 1;
    if (arms === 1) {
      const served = await route.fetch();
      expect(served.status()).toBe(200);
      expect((await served.json()) as { ok?: boolean }).toMatchObject({ ok: true });
      await route.abort("connectionreset");
      return;
    }
    await route.fallback();
  });
  return { arms: () => arms };
}

test("Check status finds an arm whose response was lost and continues without arming twice", async ({ journey }) => {
  const page = journey.page;
  await page.goto(journey.workstation.baseURL);
  const lost = await loseFirstArmResponse(page);
  await chooseStarter(page, "kraken", "starter-dual-ma-crossover");

  await page.getByTestId("first-run-wizard-primary").click();
  const alert = page.getByTestId("first-run-wizard-arm-error");
  await expect(alert).toContainText("Start paper run outcome unknown");
  await expect(alert).toContainText("Check status");
  await expect(page.getByTestId("first-run-wizard")).toHaveAttribute("data-wizard-step", "start_paper");

  await page.getByTestId("first-run-wizard-check-status").click();
  await expect(page.getByTestId("first-run-wizard")).toHaveAttribute("data-wizard-step", "status");
  await expect(page.getByTestId("first-run-wizard-already-running")).toHaveText(
    "Already running on Kraken · BTC/USD. Nothing was started twice.",
  );

  expect(lost.arms()).toBe(1);
  expect(await fleet(page)).toEqual([
    expect.objectContaining({ venue: "kraken", pair: "XXBTZUSD", pack_id: "starter-dual-ma-crossover", mode: "paper" }),
  ]);
});

test("Start after a reload reads the refused duplicate as already running", async ({ journey }) => {
  const page = journey.page;
  await page.goto(journey.workstation.baseURL);
  const lost = await loseFirstArmResponse(page);
  await chooseStarter(page, "kraken", "starter-dual-ma-crossover");

  await page.getByTestId("first-run-wizard-primary").click();
  await expect(page.getByTestId("first-run-wizard-arm-error")).toContainText("outcome unknown");

  await page.reload();
  await expect(page.getByTestId("first-run-wizard")).toHaveAttribute("data-wizard-step", "start_paper");
  const repeat = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/commands") &&
      response.request().postDataJSON()?.command === "paper.arm",
  );
  await page.getByTestId("first-run-wizard-primary").click();
  const refused = (await (await repeat).json()) as { ok?: boolean; code?: string };
  expect(refused.ok).toBe(false);
  expect(["already_armed", "over_reserved"]).toContain(refused.code);

  await expect(page.getByTestId("first-run-wizard")).toHaveAttribute("data-wizard-step", "status");
  await expect(page.getByTestId("first-run-wizard-already-running")).toContainText("Already running");
  expect(lost.arms()).toBe(2);
  expect(await fleet(page)).toHaveLength(1);
});

test("a failed session start is retried and keeps the chosen starter and venue", async ({ journey }) => {
  const page = journey.page;
  let bootstraps = 0;
  await page.route("**/api/v1/session/bootstrap", async (route) => {
    bootstraps += 1;
    if (bootstraps <= 2) {
      await route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"starting"}' });
      return;
    }
    await route.fallback();
  });
  await page.goto(journey.workstation.baseURL);

  await expect(page.getByTestId("first-run-wizard")).toHaveAttribute("data-wizard-step", "welcome");
  await page.getByTestId("first-run-wizard-primary").click();
  await page.getByTestId("first-run-wizard-market").selectOption("coinbase");
  const use = page.getByTestId("first-run-wizard-use-starter-mean-reversion-band");
  await use.click();
  await expect(page.getByTestId("first-run-wizard-choose-error")).toContainText("Press Use again to retry");
  await expect(page.getByTestId("first-run-wizard-market")).toHaveValue("coinbase");
  expect(bootstraps).toBe(2);

  await use.click();
  await expect(page.getByTestId("first-run-wizard")).toHaveAttribute("data-wizard-step", "start_paper");
  await expect(page.getByTestId("first-run-wizard-market-summary")).toContainText("Coinbase · BTC/USD");
  expect(bootstraps).toBe(3);

  await page.getByTestId("first-run-wizard-primary").click();
  await expect(page.getByTestId("first-run-wizard")).toHaveAttribute("data-wizard-step", "status");
  expect(await fleet(page)).toEqual([
    expect.objectContaining({ venue: "coinbase", pair: "BTC-USD", pack_id: "starter-mean-reversion-band", mode: "paper" }),
  ]);
});
