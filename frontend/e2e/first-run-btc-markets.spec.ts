import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect, runGuardedCli } from "./journey-harness/journey-harness";

test.use({ wizardColdOpen: "fresh" });

for (const [venue, pair] of [["kraken", "XXBTZUSD"], ["coinbase", "BTC-USD"]] as const) {
  test(`first run on ${venue} BTC/USD reads a simulated fill`, async ({ journey }) => {
    const page = journey.page;
    const home = journey.workstation.homeDir;
    await page.goto(journey.workstation.baseURL);
    await page.getByTestId("first-run-wizard-primary").click();
    await page.getByTestId("first-run-wizard-market").selectOption(venue);
    await page.getByTestId("first-run-wizard-use-starter-dual-ma-crossover").click();
    await expect(page.getByTestId("first-run-wizard-market-summary")).toContainText("BTC/USD · 1 hour · 1,000 USD");
    await page.getByTestId("first-run-wizard-primary").click();
    await expect(page.getByTestId("first-run-wizard")).toHaveAttribute("data-wizard-step", "status");
    await page.getByTestId("first-run-wizard-primary").click();
    await page.getByTestId("first-run-wizard-primary").click();
    await journey.waitForSessionReady(page);

    const source = {endpoint: "fixture://wave", retrieved_at: "2020-01-01T00:00:00Z", source_version: "test", payload_sha256: "0".repeat(64)};
    const rules = {schema_version: "1", venue, canonical_pair: pair, venue_symbol: pair, base_asset: "BTC", quote_asset: "USD", min_quantity: "0.000001", min_notional: "1", quantity_increment: "0.000001", price_increment: "0.01", source, usable_until: "2099-01-01T00:00:00Z"};
    writeFileSync(join(home, "run", "instrument-rules.json"), JSON.stringify({schema_version: "1", records: {[venue]: {[pair]: rules}}}));
    const csv = join(home, "data", "btc-wave.csv");
    const rows = Array.from({length: 30}, (_, i) => `${i * 3600000},10,11,9,10,100`);
    rows.push(`${30 * 3600000},20,21,19,20,100`);
    writeFileSync(csv, `ts_ms,open,high,low,close,volume\n${rows.join("\n")}\n`);
    const tick = runGuardedCli(["-m", "krellbot", "tick", "--venue", venue, "--offline-candles", csv], { home, commandLog: journey.workstation.commandLog });
    expect(tick.status, tick.stderr + tick.stdout).toBe(0);
    const [position, history] = await page.evaluate(async () => Promise.all([
      fetch("/api/v1/paper/position").then((response) => response.json()),
      fetch("/api/v1/paper/history").then((response) => response.json()),
    ]));
    expect(position.venue).toBe(venue);
    expect(position.pair).toBe(pair);
    expect(position.holdings[0].symbol).toBe("BTC");
    expect(Number(position.holdings[0].qty)).toBeGreaterThan(0);
    expect(Number(position.free_cash)).toBeGreaterThan(0);
    expect(Number(position.free_cash)).toBeLessThan(1000);
    expect(history.runs[0].recent_fills[0].side).toBe("buy");
  });
}
