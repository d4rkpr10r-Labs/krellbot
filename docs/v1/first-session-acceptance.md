# Finish first-session polish and packaged recovery journeys

Test the same first-run experience a newcomer receives, including a starter, a community pack, a clear failure state, and normal operating controls.

## Implementation

- Walk the actual frozen build from first open through Krell’s wizard, Workspace, Run, and Stop. Keep the mascot and the short onboarding. Check skip, resume, back navigation, retry, and re-open across an interrupted first run.
- Label rehearsal, historical backtest, forward paper, and exchange holdings consistently across charts, results, and exports. Include date/window, feed, fee and slippage assumptions where applicable. Give community packs clear provenance, validation errors, and a usable recovery action if their catalogue is unavailable.
- Audit plain English, empty and loading states, keyboard order and focus, visible labels, contrast, 200% zoom, and smaller windows. Extend packaged browser coverage for browser restart, sleeping machine or skipped scheduler checks, stale feed, duplicate clicks, and session recovery.

## Acceptance

- An unassisted script takes a fresh user from free starter to first truthful status and Stop. A parallel power-user script confirms Strategy Studio and Operations remain discoverable.
- Run the scripts with real packaged UI and controlled data. Record keyboard and viewport results and outstanding human usability observations separately from automation. Tests pin the path and copy without claiming a human study took place.

During implementation, add the acceptance coverage to the existing test suites and record the actual results in the pull request. The audited baseline is Krellbot `main` at `1a3f4cc`.
