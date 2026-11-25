# Show when a paper bot has actually checked the market

Replace the first-run and Run status derived from arm configuration with one market- and revision-scoped status based on scheduler and tick evidence. Put the normal recovery action in Run.

## Implementation

- Extend the existing paper read model around `src/krellbot/api/app.py`, the fleet projection, and `src/krellbot/service/supervise.py`. Reuse durable journal evidence. Associate it with the current arm so re-arming a pair cannot inherit an earlier run’s healthy badge.
- Expose plain-language states for ready, waiting for first check, checked or no signal, entries paused, and needs attention. Surface scheduler availability, last successful check, an expected next check only when known, and a safe explanation of refusals. Distinguish a candle with no signal from missing candles, stale data, and an uninstalled scheduler.
- Use the same status contract in `FirstRunWizard.tsx`, Workspace, and Run. Keep the wizard short. In Run, link directly to scheduler setup or reuse the existing authenticated install and enable controls. Preserve existing arm, pause, stop, and reconciliation semantics.

## Acceptance

- Fresh profile → start paper → no journal tick shows “Waiting for first check” with an actionable scheduler state. A previously successful tick for the same pair cannot make a newly armed revision appear current.
- Success with no trade, success with a fill, pause, stale candle, unavailable scheduler, and feed error each render visibly different states. A restart preserves durable state and refresh shows the new result.
- Extend the cold-start wizard, J5 browser journey, and scheduler tests with fake data and an isolated home.

During implementation, add the acceptance coverage to the existing test suites and record the actual results in the pull request. The audited baseline is Krellbot `main` at `1a3f4cc`.
