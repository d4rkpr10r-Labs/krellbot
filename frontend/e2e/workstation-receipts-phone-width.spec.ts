import { test, expect, type Locator, type Page } from "@playwright/test";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import {
  startWorkstationForSpec,
  stopWorkstationForSpec,
  type WorkstationHandle,
} from "./workstation-harness";
import {
  clickNav,
  seedDismissedFirstRunWizard,
  skipFirstRunWizard,
} from "../journeys/helpers/journey-steps";

/**
 * Phone-width parity for the paper run's health, positions and receipts.
 *
 * The production bundle runs against its own workstation (real session and
 * CSRF, ``KRELLBOT_ENABLE_LIVE=0``, fake keyring, isolated home). The home
 * is seeded through ``PaperService.arm`` plus journal ticks and one paper
 * fill, so every panel renders real rows: StatusPanel's run health and
 * owned position, FleetArmedList with its candle age, the forward paper
 * ledger and portfolio history. No order is sent and nothing is fetched.
 *
 * At 390x844 (docs/ui-density.md) the page never scrolls sideways, and
 * every control in those panels is at least 44x44px and fits fully on
 * screen once scrolled to (docs/design-system.md, KRELL-SIGNAL-20261010).
 * Dense tables may still scroll inside their own panel. At 1440x900 the
 * compact desktop density stays.
 */

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const REPO_ROOT = resolve(__dirname, "..", "..");

const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1440, height: 900 };
const MIN_TARGET_PX = 44;

const PACK_ID = "phone-receipts-pack";
const VENUE = "kraken";
const PAIR = "SUIUSD";

/**
 * Arms one paper pack the way the workstation does, then records two ticks
 * that each read a closed 1h candle 65 minutes old, and the entry's paper
 * fill. Timestamps are relative to now so the run reads as current.
 */
const SEED_PY = String.raw`
import datetime as dt
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

from krellbot import config as kb_config
from krellbot.application.paper import PaperService

home = Path(sys.argv[1])
pack_id, venue, pair = sys.argv[2], sys.argv[3], sys.argv[4]
now = int(time.time())

packs = home / "packs"
packs.mkdir(parents=True, exist_ok=True)
pack_path = packs / f"{pack_id}.json"
pack_path.write_text(
    json.dumps(
        {
            "schema_version": 1,
            "id": pack_id,
            "version": "1.0.0",
            "label": "Phone width receipts",
            "author": "phone-width e2e",
            "timeframe": "1h",
            "indicators": {"sma20": {"fn": "sma", "src": "close", "len": 20}},
            "entry": ["close", ">", "sma20"],
            "exit": ["close", "<", "sma20"],
            "risk": {"max_account_pct": 25, "stop": {"type": "pct", "pct": 5}},
            "markets": [{"venue": venue, "pair": pair}],
        }
    ),
    encoding="utf-8",
)
result = PaperService(home=home).arm(
    pack_path,
    venue=venue,
    mode="paper",
    paper_balance=Decimal(1000),
    correlation_id="seed-phone-width",
)
if not result.ok:
    raise SystemExit(f"arm refused: {result.code} {result.message}")

# The ticks below belong to this arm only when they follow it.
config = kb_config.load_config(home)
for record in config.armed:
    record.armed_at_ts = now - 3 * 3600
kb_config.save_config(home, config)

entry_ts = now - 2 * 3600
check_ts = now - 3600


def tick(ts, reason, entry_qty):
    return {
        "ts": ts,
        "kind": "tick",
        "venue": venue,
        "pack": pack_id,
        "bar_ts": (ts - 65 * 60) * 1000,
        "detail": {
            "pair": pair,
            "reason": reason,
            "entry_qty": entry_qty,
            "exit_qty": "0",
            "stop_qty": "0",
            "owned_qty_after": "2.5",
        },
    }


journal = home / "journal"
journal.mkdir(parents=True, exist_ok=True)
for record in (tick(entry_ts, "entry", "2.5"), tick(check_ts, "flat", "0")):
    month = dt.datetime.fromtimestamp(record["ts"], tz=dt.timezone.utc)
    with open(journal / f"{month:%Y-%m}.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")

run = home / "run"
run.mkdir(parents=True, exist_ok=True)
(run / f"paper-{venue}.json").write_text(
    json.dumps(
        {
            "venue": venue,
            "balances": {"USD": "989.96", "SUI": "2.5"},
            "open_orders": [],
            "recent_fills": [
                {
                    "id": "phone-width-entry-1",
                    "coid": "phone-width-entry-1",
                    "pair": pair,
                    "side": "buy",
                    "qty": "2.5",
                    "price": "4.0000",
                    "ts_ms": entry_ts * 1000,
                }
            ],
            "seeded": True,
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
`;

function seedHome(home: string): void {
  const result = spawnSync(
    "uv",
    [
      "run",
      "--project",
      REPO_ROOT,
      "python",
      "-c",
      SEED_PY,
      home,
      PACK_ID,
      VENUE,
      PAIR,
    ],
    { cwd: REPO_ROOT, encoding: "utf8" },
  );
  if (result.status !== 0) {
    throw new Error(
      `phone-width seed failed (${result.status}): ${result.stderr ?? ""}`,
    );
  }
}

async function waitForSessionReady(page: Page): Promise<void> {
  await skipFirstRunWizard(page);
  await page.waitForFunction(
    () => {
      const root = document.querySelector('[data-testid="kbot-shell"]');
      return root?.getAttribute("data-bootstrap-done") === "1";
    },
    undefined,
    { timeout: 15_000 },
  );
}

async function settle(page: Page): Promise<void> {
  await page.evaluate(
    () =>
      new Promise((done) => {
        requestAnimationFrame(() => requestAnimationFrame(done));
      }),
  );
}

async function openDisclosure(page: Page, testId: string): Promise<void> {
  const toggle = page.getByTestId(`${testId}-toggle`);
  await expect(toggle).toBeVisible();
  if ((await toggle.getAttribute("aria-expanded")) !== "true") {
    await toggle.click();
  }
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
}

type PageWidth = { innerWidth: number; docScrollWidth: number; bodyScrollWidth: number };

async function measurePageWidth(page: Page): Promise<PageWidth> {
  return page.evaluate(() => ({
    innerWidth: window.innerWidth,
    docScrollWidth: document.documentElement.scrollWidth,
    bodyScrollWidth: document.body.scrollWidth,
  }));
}

/**
 * Every control inside ``region`` that a phone user can tap. Each one is
 * scrolled to (scrolling its own panel if it has one) and must then be at
 * least 44x44px and wholly visible: inside the viewport and not cut off by
 * any overflow-clipping ancestor. Returns one line per failure.
 */
async function auditControls(region: Locator, name: string): Promise<string[]> {
  return region.evaluate(
    (root, args) => {
      const selector = [
        "button",
        "a[href]",
        'input:not([type="hidden"])',
        "select",
        "textarea",
        "summary",
        '[role="button"]',
        '[tabindex]:not([tabindex="-1"])',
      ].join(",");
      const describe = (el: Element): string => {
        const testId = el.getAttribute("data-testid");
        const label =
          el.getAttribute("aria-label") ?? (el.textContent ?? "").trim().replace(/\s+/g, " ");
        return `${el.tagName.toLowerCase()}${testId ? `[${testId}]` : ""} "${label.slice(0, 48)}"`;
      };
      const isRendered = (el: Element): boolean => {
        if (el.getClientRects().length === 0) return false;
        const style = getComputedStyle(el);
        return style.visibility !== "hidden" && style.display !== "none";
      };
      const failures: string[] = [];
      const controls = [root, ...Array.from(root.querySelectorAll(selector))]
        .filter((el) => el.matches(selector))
        .filter(isRendered);
      if (controls.length === 0) {
        failures.push(`${args.name}: no rendered controls found`);
      }
      for (const el of controls) {
        el.scrollIntoView({ block: "center", inline: "nearest" });
        const rect = el.getBoundingClientRect();
        const what = `${args.name}: ${describe(el)}`;
        if (rect.width < args.min || rect.height < args.min) {
          failures.push(
            `${what} is ${rect.width.toFixed(1)}x${rect.height.toFixed(1)}px (< ${args.min}px)`,
          );
        }
        let left = Math.max(rect.left, 0);
        let right = Math.min(rect.right, window.innerWidth);
        let top = Math.max(rect.top, 0);
        let bottom = Math.min(rect.bottom, window.innerHeight);
        for (let node = el.parentElement; node; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (style.overflowX === "visible" && style.overflowY === "visible") continue;
          const box = node.getBoundingClientRect();
          const clipLeft = box.left + node.clientLeft;
          const clipTop = box.top + node.clientTop;
          left = Math.max(left, clipLeft);
          right = Math.min(right, clipLeft + node.clientWidth);
          top = Math.max(top, clipTop);
          bottom = Math.min(bottom, clipTop + node.clientHeight);
        }
        const shownWidth = Math.max(0, right - left);
        const shownHeight = Math.max(0, bottom - top);
        if (shownWidth + 0.5 < rect.width || shownHeight + 0.5 < rect.height) {
          failures.push(
            `${what} is clipped: ${shownWidth.toFixed(1)}x${shownHeight.toFixed(1)}px of ` +
              `${rect.width.toFixed(1)}x${rect.height.toFixed(1)}px on screen`,
          );
        }
      }
      return failures;
    },
    { name, min: MIN_TARGET_PX },
  );
}

/** The region's own box stays inside the viewport's width. */
async function auditRegionBox(region: Locator, name: string): Promise<string[]> {
  return region.evaluate(
    (el, regionName) => {
      const rect = el.getBoundingClientRect();
      if (rect.left < -0.5 || rect.right > window.innerWidth + 0.5) {
        return [
          `${regionName}: spans ${rect.left.toFixed(1)}..${rect.right.toFixed(1)}px ` +
            `in a ${window.innerWidth}px viewport`,
        ];
      }
      return [];
    },
    name,
  );
}

test("health, positions and receipts read at 390x844 with no sideways page scroll and 44px controls", async ({
  page,
}) => {
  test.setTimeout(150_000);
  const consoleErrors: string[] = [];
  page.on("pageerror", (err) => {
    consoleErrors.push(err.message);
  });
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });

  await page.setViewportSize(PHONE);
  await seedDismissedFirstRunWizard(page);
  await page.goto(workstation!.baseURL);
  await waitForSessionReady(page);
  await clickNav(page, "Workstation");

  // StatusPanel: run health and the owned position.
  const status = page.getByTestId("paper-status-panel");
  const health = page.getByTestId("paper-run-health");
  const position = page.getByTestId("paper-position");
  await expect(health).toBeVisible();
  await expect(health).toContainText("Market checked.");
  await expect(health.getByRole("link", { name: "Review automatic checks" })).toBeVisible();
  await expect(position).toBeVisible();
  await expect(position).toContainText("SUI");

  // FleetArmedList with M28.4's candle age.
  const fleet = page.getByTestId("fleet-armed-list");
  const fleetRow = fleet.getByTestId("fleet-armed-row");
  await expect(fleetRow).toHaveCount(1);
  const candleAge = fleetRow.getByTestId("fleet-armed-candle-age");
  await expect(candleAge).toHaveText("1h 5m old at last check");
  await expect(fleetRow.getByTestId("fleet-armed-candle-state")).toHaveText("Fresh");

  // Receipts: forward paper ledger and portfolio history.
  await openDisclosure(page, "paper-forward-ledger-disclosure");
  const ledger = page.getByTestId("paper-forward-ledger");
  await expect(ledger.getByTestId("paper-forward-ledger-row-0")).toBeVisible();
  await openDisclosure(page, "paper-portfolio-disclosure");
  const portfolio = page.getByTestId("portfolio-history-panel");
  await expect(portfolio.getByTestId("portfolio-deployment-card")).toHaveCount(1);
  await expect(portfolio.getByTestId("portfolio-equity-chart-svg")).toBeVisible();
  await settle(page);

  const failures: string[] = [];

  const width = await measurePageWidth(page);
  console.log(
    `[phone-width] ${PHONE.width}x${PHONE.height}: document ${width.docScrollWidth}px, ` +
      `body ${width.bodyScrollWidth}px, viewport ${width.innerWidth}px`,
  );
  if (width.docScrollWidth > width.innerWidth || width.bodyScrollWidth > width.innerWidth) {
    failures.push(
      `page scrolls sideways: document ${width.docScrollWidth}px, body ` +
        `${width.bodyScrollWidth}px, viewport ${width.innerWidth}px`,
    );
  }

  // StatusPanel's first viewport holds the run health, the owned position
  // and its run controls; each receipt panel is audited with its toggle.
  const regions: { name: string; locator: Locator }[] = [
    { name: "StatusPanel", locator: status.getByTestId("paper-workstation-first-viewport") },
    { name: "FleetArmedList", locator: fleet },
    { name: "ForwardPaperLedgerPanel", locator: page.getByTestId("paper-forward-ledger-disclosure") },
    { name: "PortfolioHistoryPanel", locator: page.getByTestId("paper-portfolio-disclosure") },
  ];
  for (const { name, locator } of regions) {
    failures.push(...(await auditRegionBox(locator, name)));
    failures.push(...(await auditControls(locator, name)));
  }

  // The candle age reads on screen without scrolling any panel sideways.
  await candleAge.scrollIntoViewIfNeeded();
  const ageBox = await candleAge.evaluate((el) => {
    const scroller = el.closest('[data-testid="fleet-armed-scroll"]') as HTMLElement | null;
    const r = el.getBoundingClientRect();
    return {
      left: r.left,
      right: r.right,
      scrollLeft: scroller?.scrollLeft ?? 0,
      innerWidth: window.innerWidth,
    };
  });
  if (ageBox.left < 0 || ageBox.right > ageBox.innerWidth || ageBox.scrollLeft !== 0) {
    failures.push(
      `FleetArmedList: candle age at ${ageBox.left.toFixed(1)}..${ageBox.right.toFixed(1)}px ` +
        `needs the fleet table scrolled ${ageBox.scrollLeft}px sideways`,
    );
  }

  for (const line of failures) {
    console.log(`[phone-width] FAIL ${line}`);
  }
  expect(failures, failures.join("\n")).toEqual([]);

  // Desktop keeps its compact density: the same page at 1440x900 neither
  // scrolls sideways nor inflates the ledger's compact Refresh control.
  await page.setViewportSize(DESKTOP);
  await settle(page);
  const desktop = await measurePageWidth(page);
  expect(desktop.docScrollWidth).toBeLessThanOrEqual(desktop.innerWidth);
  const ledgerRefresh = await ledger
    .getByTestId("paper-forward-ledger-refresh")
    .evaluate((el) => el.getBoundingClientRect().height);
  console.log(`[phone-width] ${DESKTOP.width}x${DESKTOP.height}: ledger refresh ${ledgerRefresh}px`);
  expect(ledgerRefresh).toBeGreaterThanOrEqual(34);
  expect(ledgerRefresh).toBeLessThan(MIN_TARGET_PX);
  const fleetHeaderVisible = await fleet
    .getByTestId("fleet-armed-table")
    .locator("thead")
    .evaluate((el) => el.getBoundingClientRect().height > 0);
  expect(fleetHeaderVisible).toBe(true);

  expect(consoleErrors).toEqual([]);
});

let workstation: WorkstationHandle | null = null;

test.beforeAll(async () => {
  workstation = await startWorkstationForSpec();
  seedHome(workstation.homeDir);
});

test.afterAll(async () => {
  if (workstation) {
    await stopWorkstationForSpec(workstation);
    workstation = null;
  }
});
