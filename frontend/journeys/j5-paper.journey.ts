import {readFileSync} from "node:fs";
import {join} from "node:path";
import {test, expect} from "../e2e/journey-harness/journey-harness";
import {armPaper, tickPaper, paperRefresh, holdRehearsal, quantity, expandArmedRecords} from "./helpers/paper-steps";
import {clickNav, pickStarter, saveJson, validateRevision, SMA_CROSS_FIXTURE_JSON, readJournalTicks} from "./helpers/journey-steps";

test.beforeEach(async ({journey}) => {await journey.page.goto(journey.workstation.baseURL); await journey.waitForSessionReady(journey.page);});

test("J5.1 I can arm a validated strategy in paper mode with a starting balance. It shows as armed with its venue, pair, starting cash and risk cap", async ({journey}) => {
  const page = journey.page;
  await armPaper(page,"2500");
  await clickNav(page,"Workstation");
  await expect(page.getByTestId("paper-status-entries")).toContainText(/active/i);
  await expect(page.getByTestId("paper-run-health")).toContainText("Waiting for the first market check.");
  await expect(page.getByTestId("paper-run-health").getByRole("link", {name: "Review automatic checks"})).toBeVisible();
  await expandArmedRecords(page);
  const row = page.getByTestId("paper-armed-row").first();
  await expect(row).toBeVisible();
  expect(await row.getAttribute("data-venue")).toBe("kraken");
  expect(await row.getAttribute("data-pair")).toBe("SUIUSD");
  await expect(page.getByTestId("paper-position")).toBeVisible();
  const visible = (await row.innerText())+" "+(await page.getByTestId("paper-position").innerText());
  // Do not run an unauthorized tick or move this assertion post-tick.
  // This scenario promises the starting balance on the newly armed surface.
  expect.soft(visible,"starting cash must be visible immediately after arming").toContain("2500");
  expect.soft(visible,"the armed surface must label its risk cap").toMatch(/(?:risk|cap)[^\n]*100|100[^\n]*(?:risk|cap)/i);
  const config=JSON.parse(readFileSync(join(journey.workstation.homeDir,"config.json"),"utf8"));
  expect(config.armed[0].starting_cash).toBe("2500"); expect(config.armed[0].cap).toBe("100");
});

test("J5.2 Arming a second strategy on the same venue and pair is refused, and the message says why", async ({journey}) => {
  const page=journey.page;
  await armPaper(page);
  await clickNav(page, "Strategies");
  await pickStarter(page);
  const second=JSON.parse(SMA_CROSS_FIXTURE_JSON); second.id="second-sma-cross";
  await saveJson(page,JSON.stringify(second)); await validateRevision(page,"validated");
  await page.getByTestId("editor-open-in-run").click();
  await expect(page.getByTestId("paper-arm-form")).toBeVisible();
  await page.locator("#paper-arm-venue").selectOption("kraken");
  await page.locator("#paper-arm-balance").fill("1000");
  await page.getByTestId("paper-arm-submit").click();
  await expect(page.getByTestId("paper-arm-error")).toContainText(
    /over_reserved|already armed|exceed account cash/i,
  );
  await clickNav(page,"Workstation");
  await expandArmedRecords(page);
  await expect(page.getByTestId("paper-armed-row")).toHaveCount(1);
});

test("J5.3 As paper ticks run, the status shows position, cash, resting stop and trade history, and all of them match the paper journal", async ({journey}) => {
  const page=journey.page; await armPaper(page);
  const series=[100,90,95,96,90,95];
  for (let size=3;size<=series.length;size++) tickPaper(journey,series.slice(0,size));
  const ticks=readJournalTicks(journey.workstation.homeDir); expect(ticks).toHaveLength(4);
  const last=ticks.at(-1)!; expect(Number(last.detail.owned_qty_after)).toBeGreaterThan(0);
  const state=JSON.parse(readFileSync(join(journey.workstation.homeDir,"run/paper-kraken.json"),"utf8"));
  await clickNav(page,"Workstation"); await paperRefresh(page).click();
  const position=page.getByTestId("paper-position");
  await expect(position).toContainText(String(last.detail.owned_qty_after));
  await expect(position).toContainText(state.balances.USD);
  await page.getByTestId("paper-last-runs-refresh").click();
  const history=page.getByTestId("paper-last-runs-panel");
  await expect(history).toContainText("4 ticks");
  const fills=ticks.reduce((n,t)=>n+Number(Number(t.detail.entry_qty??0)>0)+Number(Number(t.detail.exit_qty??0)>0)+Number(Number(t.detail.stop_qty??0)>0),0);
  await expect(history).toContainText(`${fills} fills`);
  const stops=state.open_orders.filter((o: Record<string,unknown>)=>o.stop_price!==undefined);
  expect(stops.length).toBeGreaterThan(0);
  // A blank "New resting stop" input is NOT disclosure of the current stop.
  const displayed=page.getByTestId("paper-status-panel").getByTestId("paper-resting-stop");
  await expect(displayed,"current resting stop must be shown, not merely editable").toHaveText(/^Resting stop \d/);
  // Compare by value: run/ keeps the stop quantized ("47.5000"); the details route serves it canonical ("47.5").
  const shown=/^Resting stop (\d+(?:\.\d+)?)\b/.exec(await displayed.innerText())![1];
  expect(Number(shown),"current resting stop must be shown at its persisted value").toBe(Number(stops[0].stop_price));
});

test("J5.4 The status shows when it last updated. If a read fails or the data goes stale, it says so instead of showing old numbers as current", async ({journey}) => {
  const page=journey.page; await armPaper(page); await clickNav(page,"Workstation");
  await expect(page.getByTestId("paper-position")).toBeVisible();
  // Re-selecting Run re-reads status and position; let that settle before the read fails.
  await page.waitForLoadState("networkidle");
  await page.route("**/api/v1/paper/**",route=>route.abort("connectionreset"));
  await paperRefresh(page).click();
  const error=page.getByRole("alert").filter({hasText:/status refresh failed/i});
  await expect(error).toContainText(/refresh failed/i);
  const statusText=(await page.getByTestId("paper-status-panel").locator(".kbot-paper-status__body p").allInnerTexts()).join("\n");
  expect.soft(statusText,"ordinary paper status needs a visible last-updated/as-of time").toMatch(/(?:updated|as.of|checked).*(?:\d|just now)/i);
  // Retained values are acceptable only when explicitly labelled old.
  await expect(error).toContainText(/last confirmed state|stale|unavailable/i);
});

test("J5.5 On a fresh install, the built-in rehearsal walks a sample strategy through an entry, a held position and an exit on simulated data, and every screen labels it as simulated", async ({journey}) => {
  const page=journey.page;
  // The fixture is fresh for THIS test, not the fifth scenario's old home.
  await holdRehearsal(page);
  expect(quantity(await page.getByTestId("rehearsal-position").innerText())).toBeGreaterThan(0);
  expect(quantity(await page.getByTestId("rehearsal-stop").innerText())).toBeGreaterThan(0);
  await expect(page.getByTestId("rehearsal-fills-panel")).toHaveAttribute("aria-label",/simulated/i);
  await page.getByTestId("rehearsal-command-exit").click();
  await expect.poll(async()=>{await page.getByTestId("rehearsal-command-refresh").click(); return quantity(await page.getByTestId("rehearsal-position").innerText());},{timeout:30_000}).toBe(0);
  expect(quantity(await page.getByTestId("rehearsal-position").innerText())).toBe(0);
  const fills=(await page.getByTestId("rehearsal-fills-panel").getByRole("row").allInnerTexts()).join(" ").toLowerCase();
  expect(fills).toMatch(/buy/); expect(fills).toMatch(/sell/);
  await expect(page.getByTestId("rehearsal-label")).toContainText(/simulated data, not market/i);
});

test("J5.6 After the first paper arm, a brief celebrate shows once and the coach stays silent", async ({journey}) => {
  const page = journey.page;
  await armPaper(page, "1000");
  await clickNav(page, "Workstation");
  await expect(page.getByTestId("first-win-celebrate")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("first-win-celebrate-title")).toContainText(
    /Paper run is live on this computer/i,
  );
  // Coach dismissed; no soft Studio tip nag after the win.
  await expect(page.getByTestId("first-run-coach")).toHaveCount(0);
  await expect(page.locator("[data-testid='soft-studio-tip']")).toHaveCount(0);
  await page.getByTestId("first-win-celebrate-dismiss").click();
  await expect(page.getByTestId("first-win-celebrate")).toHaveCount(0);
  await page.reload();
  await journey.waitForSessionReady(page);
  await clickNav(page, "Workstation");
  await expect(page.getByTestId("first-win-celebrate")).toHaveCount(0);
  await expect(page.getByTestId("first-run-coach")).toHaveCount(0);
  // Power path still reachable via More / Advanced after silence.
  await clickNav(page, "Strategy studio");
  await expect(
    page.getByRole("navigation", { name: "Workstation navigation" }).getByTestId(
      "kbot-nav-more-toggle",
    ),
  ).toHaveAttribute("aria-expanded", "true");
});
