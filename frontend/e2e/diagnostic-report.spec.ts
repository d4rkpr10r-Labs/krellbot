import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect, runGuardedCli } from "./journey-harness/journey-harness";
import { armPaper } from "../journeys/helpers/paper-steps";
import { clickNav, COMMITTED_CANDLE_CSV, readJournalTicks } from "../journeys/helpers/journey-steps";

test("paper diagnostics preview and download use local journal evidence", async ({ journey }) => {
  const { page, workstation } = journey;
  await page.goto(workstation.baseURL);
  await journey.waitForSessionReady(page);
  await armPaper(page);
  const tick = runGuardedCli(
    ["-m", "krellbot", "tick", "--venue", "kraken", "--offline-candles", COMMITTED_CANDLE_CSV],
    { home: workstation.homeDir, commandLog: workstation.commandLog },
  );
  expect(tick.status, tick.stdout + tick.stderr).toBe(0);
  const ticks = readJournalTicks(workstation.homeDir);
  expect(ticks.length).toBeGreaterThan(0);
  const last = ticks.at(-1)!;
  const injected = { ...last, detail: { ...last.detail, metadata_refusal: "metadata_stale", private_field: "fake-secret-never-export" } };
  writeFileSync(join(workstation.homeDir, "journal", "diagnostic-test.jsonl"), JSON.stringify(injected) + "\n");

  await clickNav(page, "Settings");
  await page.getByTestId("settings-nav-computer").click();
  const panel = page.getByTestId("diagnostic-report");
  await panel.getByRole("button", { name: "Prepare report" }).click();
  await expect(panel.getByTestId("diagnostic-preview")).toContainText("metadata_stale");
  await panel.getByLabel("What happened? Add steps to reproduce it").fill("The paper check could not load rules.");
  const event = page.waitForEvent("download");
  await panel.getByRole("button", { name: "Download report" }).click();
  const download = await event;
  const downloadedPath = await download.path();
  const bytes = readFileSync(downloadedPath, "utf8");
  const report = JSON.parse(bytes);
  expect(report.build.version).toBeTruthy();
  expect(report.events.some((item: { ts: number; refusal_code: string }) => item.ts === last.ts && item.refusal_code === "metadata_stale")).toBe(true);
  expect(report.reproduction_steps).toBe("The paper check could not load rules.");
  expect(bytes).not.toContain("fake-secret-never-export");
});
