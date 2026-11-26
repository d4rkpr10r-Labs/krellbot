import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { FullConfig, FullResult, Suite, TestCase } from "@playwright/test/reporter";
import { afterEach, describe, expect, it, vi } from "vitest";
import JourneyOutcomesReporter, {
  classifyJourneyOutcome,
  formatJourneyOutcomes,
  summarizeJourneyOutcomes,
  type JourneyTestLike,
} from "./journeyOutcomes";

const here = dirname(fileURLToPath(import.meta.url));
const frontendDir = resolve(here, "../..");
const REPORTER_PATH = "./journeys/helpers/journeyOutcomes.ts";

type Status = JourneyTestLike["expectedStatus"];

function journey(title: string, expectedStatus: Status, ...statuses: Status[]): JourneyTestLike {
  return { title, expectedStatus, results: statuses.map((status) => ({ status })) };
}

// The five scenarios that KQ-03 found counted as "5 passed", plus ordinary outcomes.
const RUN: JourneyTestLike[] = [
  journey("J1.1 On a fresh install I can pick a starter", "passed", "passed"),
  journey("J5.4 The status shows when it last updated", "failed", "failed"),
  journey("J5.1 I can arm a validated strategy in paper mode", "failed", "failed"),
  journey("J4.1 From a backtest result, I can open any bar", "failed", "passed"),
  journey("J5.3 As paper ticks run, the status shows the resting stop", "failed", "failed"),
  journey("J7.3 An exported backtest carries its window", "failed", "failed"),
  journey("J6.2 Resuming entries lets the strategy enter again", "passed", "failed"),
  journey("J3.1 Starting a backtest creates a job", "skipped", "skipped"),
  journey("a fresh install finishes the wizard paper path", "passed", "passed"),
];

describe("classifyJourneyOutcome", () => {
  it("counts a test.fail scenario that failed as an expected failure, never as a pass", () => {
    expect(classifyJourneyOutcome("failed", "failed")).toBe("expected fail");
    expect(classifyJourneyOutcome("passed", "passed")).toBe("passed");
  });

  it("calls a test.fail scenario that passed an unexpected pass", () => {
    expect(classifyJourneyOutcome("failed", "passed")).toBe("unexpected pass");
  });

  it("counts real failures, timeouts and interruptions as failures, including under test.fail", () => {
    expect(classifyJourneyOutcome("passed", "failed")).toBe("failed");
    expect(classifyJourneyOutcome("passed", "timedOut")).toBe("failed");
    expect(classifyJourneyOutcome("passed", "interrupted")).toBe("failed");
    expect(classifyJourneyOutcome("failed", "timedOut")).toBe("failed");
  });

  it("counts skipped scenarios and scenarios that never ran as skipped", () => {
    expect(classifyJourneyOutcome("skipped", "skipped")).toBe("skipped");
    expect(classifyJourneyOutcome("passed", undefined)).toBe("skipped");
  });
});

describe("summarizeJourneyOutcomes", () => {
  it("groups scenario IDs by outcome from each scenario's final result", () => {
    const summary = summarizeJourneyOutcomes([
      ...RUN,
      journey("J8.1 Live authorization stays disabled", "passed", "failed", "passed"),
    ]);
    expect(summary).toEqual({
      passed: ["J1.1", "J8.1", "a fresh install finishes the wizard paper path"],
      "expected fail": ["J5.1", "J5.3", "J5.4", "J7.3"],
      "unexpected pass": ["J4.1"],
      failed: ["J6.2"],
      skipped: ["J3.1"],
    });
  });

  it("orders scenario IDs numerically", () => {
    const summary = summarizeJourneyOutcomes([
      journey("J5.10 later", "passed", "passed"),
      journey("J10.1 tenth", "passed", "passed"),
      journey("J5.2 second", "passed", "passed"),
    ]);
    expect(summary.passed).toEqual(["J5.2", "J5.10", "J10.1"]);
  });
});

describe("formatJourneyOutcomes", () => {
  it("prints a count and the scenario IDs for every outcome", () => {
    const text = formatJourneyOutcomes(summarizeJourneyOutcomes(RUN));
    const lines = text.split("\n");
    expect(lines).toContain("Journey outcomes: 9 scenarios");
    expect(lines).toContain("  passed           2  J1.1, a fresh install finishes the wizard paper path");
    expect(lines).toContain("  expected fail    4  J5.1, J5.3, J5.4, J7.3");
    expect(lines).toContain("  unexpected pass  1  J4.1");
    expect(lines).toContain("  failed           1  J6.2");
    expect(lines).toContain("  skipped          1  J3.1");
    expect(text).not.toMatch(/\b6 passed\b/);
  });

  it("says an expected failure is an unmet promise, not a pass", () => {
    const text = formatJourneyOutcomes(summarizeJourneyOutcomes(RUN));
    expect(text).toContain("Expected failures are unmet promises and are not counted as passes.");
  });

  it("names an unexpected pass and what it means", () => {
    const text = formatJourneyOutcomes(summarizeJourneyOutcomes(RUN));
    expect(text).toContain(
      "Unexpected pass: J4.1 is marked test.fail but passed. Playwright fails the run until the marker is removed.",
    );
  });

  it("prints every outcome even when its count is zero", () => {
    const text = formatJourneyOutcomes(summarizeJourneyOutcomes([journey("J1.1 x", "passed", "passed")]));
    expect(text.split("\n")).toEqual([
      "Journey outcomes: 1 scenario",
      "  passed           1  J1.1",
      "  expected fail    0",
      "  unexpected pass  0",
      "  failed           0",
      "  skipped          0",
      "",
    ]);
  });
});

describe("JourneyOutcomesReporter", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function fakeSuite(tests: JourneyTestLike[]): Suite {
    return { allTests: () => tests as unknown as TestCase[] } as unknown as Suite;
  }

  it("prints the outcome report at the end of the run and leaves the run status alone", async () => {
    const written: string[] = [];
    vi.spyOn(process.stdout, "write").mockImplementation((chunk: string | Uint8Array) => {
      written.push(String(chunk));
      return true;
    });
    const reporter = new JourneyOutcomesReporter();
    expect(reporter.printsToStdio()).toBe(true);
    reporter.onBegin({} as FullConfig, fakeSuite(RUN));
    const verdict = await reporter.onEnd({ status: "failed" } as FullResult);
    expect(verdict).toBeUndefined();
    const text = written.join("");
    expect(text).toContain("  expected fail    4  J5.1, J5.3, J5.4, J7.3");
    expect(text).toContain("  unexpected pass  1  J4.1");
  });
});

describe("journey outcome reporter wiring", () => {
  it("is registered in the journeys Playwright config", () => {
    const config = readFileSync(resolve(frontendDir, "playwright.journeys.config.ts"), "utf8");
    expect(config).toContain(`["${REPORTER_PATH}"]`);
  });

  it("is added to CI's journeys --reporter list, which overrides the config", () => {
    const ci = readFileSync(resolve(frontendDir, "../.github/workflows/ci.yml"), "utf8");
    const match = ci.match(/npm run test:journeys -- --reporter=(\S+)/);
    expect(match, "CI runs the journeys with an explicit --reporter list").not.toBeNull();
    expect(match![1].split(",")).toEqual(["list", "json", REPORTER_PATH]);
  });
});
