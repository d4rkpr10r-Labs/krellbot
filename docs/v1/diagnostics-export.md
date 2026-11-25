# Export a safe bug report from the workstation

Add a local export that gives testers the exact build and recent paper and scheduler evidence without asking them to copy raw logs or keys.

## Implementation

- Add a visible export action in Settings or Operations and a shortcut from Run errors. Reuse authenticated, no-store status and journal projections. Use an allowlist for build ID, OS, mode, market, scheduler status, and a bounded recent set of events with safe refusal codes.
- Keep the report local and user-initiated. Let the user see what it contains and set aside a short reproduction-steps template. Attach timestamps, source mode, and useful IDs while omitting account balances, credential fields, tokens, sensitive paths, headers, raw vendor payloads, and unexpected nested fields.
- Handle no journal yet, malformed records, multiple markets, and unavailable scheduler without breaking the export.

## Acceptance

- An isolated browser journey can run a paper bot, simulate a refusal, download a report, and match the build and tick details to the journal.
- Fixtures containing credential material, private paths, and raw payloads prove the exported data stays within the allowlist. Missing files and partial data are handled honestly.

During implementation, add the acceptance coverage to the existing test suites and record the actual results in the pull request. The audited baseline is Krellbot `main` at `1a3f4cc`.
