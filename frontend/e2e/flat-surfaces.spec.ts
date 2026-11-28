/**
 * Flat surfaces on Workspace home and Settings (docs/design-system.md,
 * "Shell composition", contract KRELL-SIGNAL-20261010).
 *
 * At 1440×900 and 390×844 this pins two things:
 *
 * 1. Surface depth. Walking the DOM under <main>, no ancestor chain holds
 *    more than two elements that each paint their own background, a
 *    visible border and a border-radius > 0 (the panel, then a control
 *    inside it). Status chips and pills, fully rounded ends, are not
 *    surfaces.
 * 2. The page gradient. Scrolled to the top and to the bottom of the page,
 *    no opaque surface sits between the bottom-centre of the viewport and
 *    the shell root (controls and read-only code fields keep their solid
 *    field colour; they are not surfaces), and the bottom rows of the
 *    viewport change when the root gradient is removed, so the gradient
 *    really shows at the bottom of the page.
 *
 * Fresh guarded workstation per test; the shell suites seed a dismissed
 * wizard, the armed home goes through the real first-run wizard.
 */
import type { Page } from "@playwright/test";
import { test, expect } from "./journey-harness/journey-harness";
import { clickNav } from "../journeys/helpers/journey-steps";

const VIEWPORTS = [
  { name: "1440×900", width: 1440, height: 900 },
  { name: "390×844", width: 390, height: 844 },
] as const;

/** The page, then one panel: at most a panel and a control inside it. */
const MAX_SURFACE_DEPTH = 2;

type SurfaceDepth = { depth: number; chain: string[] };

async function settle(page: Page): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      }),
  );
}

/** Deepest chain of bordered, backgrounded, rounded elements under <main>. */
async function deepestSurfaceChain(page: Page): Promise<SurfaceDepth> {
  return page.evaluate(() => {
    const main = document.querySelector("main.kbot-shell__main");
    if (!main) throw new Error("shell main content is not mounted");

    const alphaOf = (color: string): number => {
      if (color === "transparent") return 0;
      const slash = /\/\s*([\d.]+)(%?)\s*\)$/.exec(color);
      if (slash) return parseFloat(slash[1]) / (slash[2] ? 100 : 1);
      const rgba = /^rgba\(([^)]*)\)$/.exec(color);
      if (rgba) {
        const parts = rgba[1].split(",");
        return parts.length === 4 ? parseFloat(parts[3]) : 1;
      }
      return 1;
    };
    const px = (value: string, basis: number): number =>
      value.trim().endsWith("%") ? (parseFloat(value) / 100) * basis : parseFloat(value);
    const sides = ["top", "right", "bottom", "left"] as const;
    const corners = ["top-left", "top-right", "bottom-right", "bottom-left"] as const;

    const isSurface = (el: Element): boolean => {
      if (!el.checkVisibility({ visibilityProperty: true })) return false;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return false;
      const style = getComputedStyle(el);
      const background =
        alphaOf(style.backgroundColor) > 0 || style.backgroundImage !== "none";
      const border = sides.some((side) => {
        const width = parseFloat(style.getPropertyValue(`border-${side}-width`));
        const line = style.getPropertyValue(`border-${side}-style`);
        const color = style.getPropertyValue(`border-${side}-color`);
        return width > 0 && line !== "none" && line !== "hidden" && alphaOf(color) > 0;
      });
      const shortSide = Math.min(rect.width, rect.height);
      const radius = Math.max(
        ...corners.map((corner) =>
          px(style.getPropertyValue(`border-${corner}-radius`).split(" ")[0], shortSide),
        ),
      );
      // Chips and pills (fully rounded ends) are not surfaces.
      const pill = radius >= rect.height / 2 - 0.5;
      return background && border && radius > 0 && !pill;
    };

    const describe = (el: Element): string => {
      const testId = el.getAttribute("data-testid");
      const firstClass = (el.getAttribute("class") ?? "").trim().split(/\s+/)[0];
      return (
        el.tagName.toLowerCase() +
        (firstClass ? `.${firstClass}` : "") +
        (testId ? `[${testId}]` : "")
      );
    };

    let deepest: string[] = [];
    const walk = (el: Element, chain: string[]): void => {
      const next = isSurface(el) ? [...chain, describe(el)] : chain;
      if (next.length > deepest.length) deepest = next;
      for (const child of Array.from(el.children)) walk(child, next);
    };
    for (const child of Array.from(main.children)) walk(child, []);
    return { depth: deepest.length, chain: deepest };
  });
}

async function expectFlat(page: Page, where: string): Promise<void> {
  await settle(page);
  const { depth, chain } = await deepestSurfaceChain(page);
  expect(
    depth,
    `${where}: at most ${MAX_SURFACE_DEPTH} nested surfaces, got ${chain.join(" > ")}`,
  ).toBeLessThanOrEqual(MAX_SURFACE_DEPTH);
}

/** First opaque element between the bottom-centre point and the shell root. */
async function bottomCover(page: Page): Promise<{
  rootCoversViewport: boolean;
  rootHasGradient: boolean;
  opaque: string | null;
}> {
  return page.evaluate(() => {
    const root = document.querySelector<HTMLElement>('[data-testid="kbot-shell"]');
    if (!root) throw new Error("shell root is not mounted");
    const alphaOf = (color: string): number => {
      if (color === "transparent") return 0;
      const slash = /\/\s*([\d.]+)(%?)\s*\)$/.exec(color);
      if (slash) return parseFloat(slash[1]) / (slash[2] ? 100 : 1);
      const rgba = /^rgba\(([^)]*)\)$/.exec(color);
      if (rgba) {
        const parts = rgba[1].split(",");
        return parts.length === 4 ? parseFloat(parts[3]) : 1;
      }
      return 1;
    };
    // Controls and read-only code fields keep a solid field colour by design
    // (docs/design-system.md, Controls); they are not surfaces.
    const FIELD = "input, select, textarea, button, pre, code";
    const x = Math.round(window.innerWidth / 2);
    const y = window.innerHeight - 4;
    const hit = document.elementFromPoint(x, y);
    let opaque: string | null = null;
    for (let el = hit; el && el !== root; el = el.parentElement) {
      if (el.matches(FIELD) || el.closest(FIELD)) continue;
      if (alphaOf(getComputedStyle(el).backgroundColor) >= 0.98) {
        const testId = el.getAttribute("data-testid");
        opaque = `${el.tagName.toLowerCase()}.${el.className}${testId ? `[${testId}]` : ""}`;
        break;
      }
    }
    const rect = root.getBoundingClientRect();
    // One pixel of slack for a fractional root height at the scroll end.
    return {
      rootCoversViewport:
        hit !== null &&
        root.contains(hit) &&
        rect.top <= 1 &&
        rect.bottom >= window.innerHeight - 1,
      rootHasGradient: /gradient\(/.test(getComputedStyle(root).backgroundImage),
      opaque,
    };
  });
}

async function expectGradientAtBottom(page: Page, where: string): Promise<void> {
  for (const scroll of ["top", "bottom"] as const) {
    await page.evaluate((to) => {
      window.scrollTo(0, to === "top" ? 0 : document.documentElement.scrollHeight);
    }, scroll);
    await settle(page);
    const label = `${where}, scrolled to the ${scroll}`;

    const cover = await bottomCover(page);
    expect(cover.rootHasGradient, `${label}: the shell root paints the gradient`).toBe(true);
    expect(cover.rootCoversViewport, `${label}: the shell root reaches the viewport bottom`).toBe(
      true,
    );
    expect(cover.opaque, `${label}: no opaque surface hides the gradient`).toBeNull();

    // Same frame, root gradient on then off: the bottom rows must change.
    const viewport = page.viewportSize();
    if (!viewport) throw new Error("viewport size is not set");
    const clip = { x: 0, y: viewport.height - 8, width: viewport.width, height: 8 };
    const shot = () => page.screenshot({ clip, animations: "disabled", caret: "hide" });
    const withGradient = await shot();
    await page.evaluate(() => {
      document
        .querySelector<HTMLElement>('[data-testid="kbot-shell"]')
        ?.style.setProperty("background-image", "none", "important");
    });
    const withoutGradient = await shot();
    await page.evaluate(() => {
      document
        .querySelector<HTMLElement>('[data-testid="kbot-shell"]')
        ?.style.removeProperty("background-image");
    });
    expect(
      withGradient.equals(withoutGradient),
      `${label}: the page gradient shows at the bottom of the viewport`,
    ).toBe(false);
  }
}

for (const viewport of VIEWPORTS) {
  test(`Workspace home and its starter chooser stay two surfaces deep over a full-viewport gradient at ${viewport.name}`, async ({
    journey,
  }) => {
    const page = journey.page;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto(journey.workstation.baseURL);
    await journey.waitForSessionReady(page);
    await expect(page.getByTestId("app-home-first-viewport")).toBeVisible();

    await expectFlat(page, "Workspace home");
    await expectGradientAtBottom(page, "Workspace home");

    await page.getByTestId("app-home-cta-paper").click();
    await expect(page.getByTestId("paper-starter-chooser")).toBeVisible();
    await expect(page.locator('[data-testid^="starter-picker-item-"]').first()).toBeVisible();
    await expectFlat(page, "Workspace home starter chooser");
    await expectGradientAtBottom(page, "Workspace home starter chooser");
  });

  test(`every Settings section stays two surfaces deep over a full-viewport gradient at ${viewport.name}`, async ({
    journey,
  }) => {
    const page = journey.page;
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto(journey.workstation.baseURL);
    await journey.waitForSessionReady(page);
    await clickNav(page, "Settings");
    await expect(page.getByTestId("settings-view")).toBeVisible();

    const nav = page.getByTestId("settings-section-nav");
    const sections = await nav
      .locator('[data-testid^="settings-nav-"]')
      .evaluateAll((items) =>
        items.map((item) => item.getAttribute("data-testid")?.replace("settings-nav-", "") ?? ""),
      );
    expect(sections.length, "Settings lists its sections").toBeGreaterThan(0);

    for (const section of sections) {
      await nav.getByTestId(`settings-nav-${section}`).click();
      await expect(page.getByTestId("settings-content")).toHaveAttribute(
        "data-settings-section",
        section,
      );
      await page.waitForLoadState("networkidle");
      await expectFlat(page, `Settings → ${section}`);
      await expectGradientAtBottom(page, `Settings → ${section}`);
    }
  });
}

test.describe("after the first-run wizard arms a paper strategy", () => {
  test.use({ wizardColdOpen: "fresh" });

  for (const viewport of VIEWPORTS) {
    test(`the running-paper home stays two surfaces deep over a full-viewport gradient at ${viewport.name}`, async ({
      journey,
    }) => {
      const page = journey.page;
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.goto(journey.workstation.baseURL);

      const wizard = page.getByTestId("first-run-wizard");
      const primary = page.getByTestId("first-run-wizard-primary");
      await expect(wizard).toHaveAttribute("data-wizard-step", "welcome");
      await primary.click();
      await expect(wizard).toHaveAttribute("data-wizard-step", "choose");
      await page.getByTestId("first-run-wizard-use-starter-dual-ma-crossover").click();
      await expect(wizard).toHaveAttribute("data-wizard-step", "start_paper");
      await primary.click();
      await expect(wizard).toHaveAttribute("data-wizard-step", "status");
      await primary.click();
      await expect(wizard).toHaveAttribute("data-wizard-step", "win");
      await primary.click();
      await journey.waitForSessionReady(page);

      await expect(page.getByTestId("app-home-active-paper")).toBeVisible({ timeout: 15_000 });
      // The setup tour opens over Home after the wizard; it is not part of Home.
      await expect(page.getByTestId("workspace-setup-tour")).toBeVisible();
      await page.getByTestId("workspace-setup-tour-skip").click();
      await expect(page.getByTestId("workspace-setup-tour")).toHaveCount(0);

      await expectFlat(page, "Workspace home with a running paper strategy");
      await expectGradientAtBottom(page, "Workspace home with a running paper strategy");
    });
  }
});
