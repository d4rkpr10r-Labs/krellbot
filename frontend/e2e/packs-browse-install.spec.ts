/**
 * Signed and community Packs browse + one-click install (KRELL-SIGNAL-20261010).
 *
 * Playwright route fakes for catalogue / install / library / community
 * index — no real signed envelopes, network catalogue or community-index
 * fetch, or exchange calls. Session/CSRF still come from the guarded
 * workstation. Community index must be stubbed: PacksView mounts the
 * honesty panel which GETs /api/v1/community/index, and the real handler
 * would DNS raw.githubusercontent.com (journey net-guard refuses).
 */
import { test, expect } from "./journey-harness/journey-harness";
import { clickNav } from "../journeys/helpers/journey-steps";

const CATALOGUE = {
  schema_version: "1",
  catalogue_version: "1",
  updated: "2026-11-16T00:00:00Z",
  packs: [
    {
      id: "free-dual-ma",
      label: "Free Dual MA",
      description: "Educational free pack",
      version: "1.0.0",
      tier: "free",
      permissions: [],
    },
    {
      id: "paid-triple-stack",
      label: "Triple Stack",
      description: "Paid catalog pack",
      version: "2.1.0",
      tier: "paid",
      permissions: ["catalog"],
    },
  ],
};

const COMMUNITY_PACK = {
  id: "trend-follow",
  label: "Trend follow",
  author: "community-example",
  url: "https://raw.githubusercontent.com/d4rk-pri0r/krellbot-community-packs/main/packs/trend-follow.json",
};

async function stubPackApis(
  page: import("@playwright/test").Page,
  communityPacks: ReadonlyArray<typeof COMMUNITY_PACK> = [],
): Promise<void> {
  let library: Array<Record<string, unknown>> = [];

  await page.route("**/api/v1/catalogue", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(CATALOGUE),
    });
  });

  // M17.4 honesty panel auto-loads community index; keep it loopback-only.
  await page.route("**/api/v1/community/index", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "1",
        index_url: "https://raw.githubusercontent.com/d4rk-pri0r/krellbot-community-packs/main/index.json",
        packs: communityPacks,
      }),
    });
  });

  await page.route("**/api/v1/packs/install", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    const raw = route.request().postDataJSON() as { pack_id?: string };
    const packId = typeof raw?.pack_id === "string" ? raw.pack_id : "";
    if (packId === "paid-triple-stack") {
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "1",
          ok: false,
          code: "activation_required",
          message: "paid pack requires activation redeem before install",
        }),
      });
      return;
    }
    if (packId !== "free-dual-ma") {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "1",
          ok: false,
          code: "pack_not_found",
          message: "catalogue entry not found",
        }),
      });
      return;
    }
    const pack = {
      bucket: "download_pending",
      pack_id: "free-dual-ma",
      version: "1.0.0",
      permissions: [],
      rollback_ref: null,
    };
    library = [pack];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ schema_version: "1", ok: true, pack }),
    });
  });

  await page.route("**/api/v1/packs", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    // Do not intercept /api/v1/packs/starters or /install.
    const url = route.request().url();
    if (url.includes("/packs/")) {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(library),
    });
  });
}

test("packs browse: first-class Packs nav shows signed catalogue rows", async ({
  journey,
}) => {
  const page = journey.page;
  await stubPackApis(page);
  await page.goto(journey.workstation.baseURL);
  await journey.waitForSessionReady(page);
  await clickNav(page, "Packs");
  await expect(page.getByTestId("packs-view")).toBeVisible();
  await expect(page.getByTestId("pack-catalogue-list")).toBeVisible();
  await expect(page.getByTestId("pack-catalogue-id-free-dual-ma")).toHaveText(
    "free-dual-ma",
  );
  await expect(page.getByTestId("pack-catalogue-tier-free-dual-ma")).toHaveText(
    "free",
  );
  await expect(page.getByTestId("pack-catalogue-version-free-dual-ma")).toHaveText(
    "1.0.0",
  );
  await expect(page.getByTestId("pack-catalogue-tier-paid-triple-stack")).toHaveText(
    "paid",
  );
  journey.assertGuards();
});

test("community install keeps provenance and uses the session-protected community route", async ({
  journey,
}) => {
  const page = journey.page;
  await stubPackApis(page, [COMMUNITY_PACK]);
  await page.route("**/api/v1/community/install", async (route) => {
    await route.fulfill({
      json: {
        schema_version: "1",
        ok: true,
        pack: { ...COMMUNITY_PACK, version: "1.0.0" },
        path: "packs/community/trend-follow.json",
      },
    });
  });
  await page.goto(journey.workstation.baseURL);
  await journey.waitForSessionReady(page);
  await clickNav(page, "Packs");

  const row = page.getByTestId("community-index-row-trend-follow");
  await expect(row).toHaveAttribute("data-pack-source", "community");
  await expect(row).toContainText(/unverified/i);
  await expect(row).toContainText(COMMUNITY_PACK.author);
  await expect(page.getByTestId("pack-catalogue-id-free-dual-ma")).toBeVisible();

  const installRequest = page.waitForRequest(
    (request) => new URL(request.url()).pathname === "/api/v1/community/install",
  );
  await row.getByRole("button", { name: "Install" }).click();
  const request = await installRequest;
  expect(request.method()).toBe("POST");
  expect(request.postDataJSON()).toEqual({ pack_id: COMMUNITY_PACK.id });
  expect((await request.allHeaders())["x-krellbot-csrf"]).toBeTruthy();
  await expect(row.getByRole("status")).toHaveText(
    "Installed · trend-follow · 1.0.0 · author community-example · packs/community/trend-follow.json",
  );
  journey.assertGuards();
});

test("packs one-click install: free pack lands in library with bucket and version", async ({
  journey,
}) => {
  const page = journey.page;
  await stubPackApis(page);
  await page.goto(journey.workstation.baseURL);
  await journey.waitForSessionReady(page);
  await clickNav(page, "Packs");
  await expect(page.getByTestId("pack-catalogue-list")).toBeVisible();
  await expect(page.getByTestId("pack-library-empty")).toBeVisible();

  await page.getByTestId("pack-catalogue-install-free-dual-ma").click();
  await expect(
    page.getByTestId("pack-catalogue-install-status-free-dual-ma"),
  ).toContainText(/Installed · download_pending · 1\.0\.0/);

  await expect(page.getByTestId("pack-library-row-free-dual-ma")).toBeVisible();
  await expect(page.getByTestId("pack-library-bucket-free-dual-ma")).toContainText(
    "download_pending",
  );
  await expect(page.getByTestId("pack-library-version-free-dual-ma")).toHaveText(
    "1.0.0",
  );
  journey.assertGuards();
});

test("community catalogue can recover after a temporary outage", async ({ journey }) => {
  const page = journey.page;
  await stubPackApis(page);
  let requests = 0;
  await page.route("**/api/v1/community/index", async (route) => {
    requests += 1;
    if (requests === 1) {
      await route.fulfill({ status: 503, json: {} });
      return;
    }
    await route.fulfill({ json: { schema_version: "1", packs: [COMMUNITY_PACK] } });
  });
  await page.goto(journey.workstation.baseURL);
  await journey.waitForSessionReady(page);
  await clickNav(page, "Packs");
  const panel = page.getByTestId("community-index-panel");
  await expect(panel.getByText("Community packs are unavailable.", { exact: false })).toBeVisible();
  await panel.getByRole("button", { name: "Try again" }).click();
  await expect(panel.getByTestId("community-index-row-trend-follow")).toBeVisible();
  await expect(panel.getByRole("alert")).toHaveCount(0);
});
