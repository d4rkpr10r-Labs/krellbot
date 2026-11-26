# Public journey specifications

### Rules for every journey
- No step asks for an account, a payment or an exchange key.
- No step sends a live order. Live trading stays disabled throughout.
- Every error says what went wrong in plain words. Nothing fails silently, and old data never appears as current.
- The same inputs always produce the same results.

## Scenario IDs

Scenario IDs use J<journey>.<scenario>. The approved set is J1.1 through J8.5: eight journeys with five scenarios each. Each journey test title starts with its exact scenario ID. J8.1 is covered by the browser journey; J8.2–J8.5 are covered by pytest against the API and fake transport.

## Time limits

| Moment | Limit |
| --- | --- |
| A new job appears in Jobs | 5 seconds after submit |
| Jobs shows a job's final state | 15 seconds after the job ends |
| A Retry recovers after the workstation is back | 10 seconds |
| 100,000-bar scroll and bar inspection | The thresholds already in `ns20-100k-rows.spec.ts` |

## Outcome report

Playwright's own footer counts a `test.fail` scenario that failed as passed. Read journey results from the outcome report that `npm run test:journeys` prints last. It comes from `frontend/journeys/helpers/journeyOutcomes.ts` and gives a count and the scenario IDs for each outcome:

| Outcome | Meaning |
| --- | --- |
| passed | Expected to pass, and passed. |
| expected fail | Marked `test.fail`, and failed. The promise is unmet. It is never counted as a pass or as delivered behaviour. |
| unexpected pass | Marked `test.fail`, but passed. Playwright fails the run until the marker is removed. |
| failed | Failed, timed out or was interrupted, with or without a `test.fail` marker. Playwright fails the run. |
| skipped | Skipped, or never ran. |

The reporter is registered in `frontend/playwright.journeys.config.ts`. CI passes `--reporter` on the command line, which replaces the config's reporters, so `.github/workflows/ci.yml` names it as well.

## Annotated scenarios

Each scenario that has carried a `test.fail` marker, with its disposition from decision D8 in `docs/milestones/M28.md`. Closing an issue as not planned is triage, not a fix: J4.1, J5.1 and J5.3 are release promises again.

| Scenario | Issue | Disposition |
| --- | --- | --- |
| J4.1 A selected bar names its decision | #103 | Release promise. M28.8 removed its `test.fail`. |
| J5.1 Armed row shows starting cash and risk cap | #99 | Release promise. Expected failure until M28.7 shows the risk cap and removes its `test.fail`. |
| J5.3 Status shows the current resting stop | #100 | Release promise. Expected failure until M28.7 shows the resting stop and removes its `test.fail`. |
| J5.4 Status shows when it last updated | #101 | Expected failure until M28.7 moves the last-updated assertion to after the first check and removes its `test.fail`. |
| J7.3 Export carries its intraday window | #107 | Stays an expected failure. |

## Journey files

- j1-author.md
- j2-validate.md
- j3-research-jobs.md
- j4-inspect.md
- j5-paper.md
- j6-pause-restart.md
- j7-export.md
- j8-live-authorization.md
