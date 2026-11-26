import type { FullConfig, FullResult, Reporter, Suite, TestCase } from "@playwright/test/reporter";

/**
 * Journey outcome reporter (KQ-03).
 *
 * Playwright's footer counts a `test.fail` scenario that failed as "passed",
 * so the stock report showed unmet journey promises as delivered. This
 * reporter prints one count and the scenario IDs for each outcome. An expected
 * failure is never a pass, and a `test.fail` scenario that passes is named.
 *
 * It only reports. Playwright already fails the run on a real failure or an
 * unexpected pass, and this reporter never changes that status.
 */

export type JourneyOutcome = "passed" | "expected fail" | "unexpected pass" | "failed" | "skipped";

export const JOURNEY_OUTCOMES: readonly JourneyOutcome[] = [
  "passed",
  "expected fail",
  "unexpected pass",
  "failed",
  "skipped",
];

type Status = TestCase["expectedStatus"];

/** The parts of a Playwright TestCase the report reads. */
export interface JourneyTestLike {
  readonly title: string;
  readonly expectedStatus: Status;
  readonly results: ReadonlyArray<{ readonly status: Status }>;
}

export type JourneyOutcomeSummary = Record<JourneyOutcome, string[]>;

/**
 * Classify a scenario by what it was expected to do and what its final
 * attempt did. A scenario with no result never ran, so it counts as skipped.
 * A timeout or interruption is a failure even under `test.fail`, as it is for
 * Playwright's exit code.
 */
export function classifyJourneyOutcome(expectedStatus: Status, finalStatus: Status | undefined): JourneyOutcome {
  if (finalStatus === undefined || finalStatus === "skipped") return "skipped";
  if (expectedStatus === "failed") {
    if (finalStatus === "failed") return "expected fail";
    if (finalStatus === "passed") return "unexpected pass";
    return "failed";
  }
  if (expectedStatus === "passed" && finalStatus === "passed") return "passed";
  return "failed";
}

const SCENARIO_ID = /^J(\d+)\.(\d+)\b/;

/** A journey title starts with its scenario ID; a title without one is its own label. */
export function scenarioId(title: string): string {
  return SCENARIO_ID.exec(title)?.[0] ?? title;
}

function compareScenarios(a: string, b: string): number {
  const left = SCENARIO_ID.exec(a);
  const right = SCENARIO_ID.exec(b);
  if (left && right) {
    return Number(left[1]) - Number(right[1]) || Number(left[2]) - Number(right[2]);
  }
  if (left) return -1;
  if (right) return 1;
  return a < b ? -1 : a > b ? 1 : 0;
}

export function summarizeJourneyOutcomes(tests: Iterable<JourneyTestLike>): JourneyOutcomeSummary {
  const summary = Object.fromEntries(JOURNEY_OUTCOMES.map((outcome) => [outcome, [] as string[]])) as JourneyOutcomeSummary;
  for (const test of tests) {
    const outcome = classifyJourneyOutcome(test.expectedStatus, test.results.at(-1)?.status);
    summary[outcome].push(scenarioId(test.title));
  }
  for (const outcome of JOURNEY_OUTCOMES) summary[outcome].sort(compareScenarios);
  return summary;
}

const LABEL_WIDTH = Math.max(...JOURNEY_OUTCOMES.map((outcome) => outcome.length));

export function formatJourneyOutcomes(summary: JourneyOutcomeSummary): string {
  const total = JOURNEY_OUTCOMES.reduce((sum, outcome) => sum + summary[outcome].length, 0);
  const countWidth = Math.max(...JOURNEY_OUTCOMES.map((outcome) => String(summary[outcome].length).length));
  const lines = [`Journey outcomes: ${total} ${total === 1 ? "scenario" : "scenarios"}`];
  for (const outcome of JOURNEY_OUTCOMES) {
    const ids = summary[outcome];
    const row = `  ${outcome.padEnd(LABEL_WIDTH)}  ${String(ids.length).padStart(countWidth)}`;
    lines.push(ids.length ? `${row}  ${ids.join(", ")}` : row);
  }
  if (summary["expected fail"].length) {
    lines.push("Expected failures are unmet promises and are not counted as passes.");
  }
  for (const id of summary["unexpected pass"]) {
    lines.push(
      `Unexpected pass: ${id} is marked test.fail but passed. Playwright fails the run until the marker is removed.`,
    );
  }
  return `${lines.join("\n")}\n`;
}

export default class JourneyOutcomesReporter implements Reporter {
  private suite: Suite | undefined;

  printsToStdio(): boolean {
    return true;
  }

  onBegin(_config: FullConfig, suite: Suite): void {
    this.suite = suite;
  }

  async onEnd(_result: FullResult): Promise<void> {
    if (!this.suite) return;
    process.stdout.write(`\n${formatJourneyOutcomes(summarizeJourneyOutcomes(this.suite.allTests()))}`);
  }
}
