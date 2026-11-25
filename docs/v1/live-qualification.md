# Close the engineering gaps ahead of live venue qualification

Prepare a reproducible qualification record for Kraken first and Coinbase separately while keeping the release in paper mode.

## Implementation

- Use existing fake transports, the outbox fault matrix, and the signed Kraken validate-only diagnostic. Cover permission ambiguity and revocation, price and quantity minima, protective stops, partial fills, rate limits, timeout/reconcile after restart, failed stop placement, exit/cancel, and ownership isolation. All tests use synthetic fixtures and keep order sending blocked in the shipped build.
- Confirm safe UI behavior for paper-only refusal, live grant scope and expiry, kill switch, duplicate-click handling, and unknown orders. Use the working operator controls already present in the code rather than adding a parallel live path.
- Capture a separate evidence template for the connect/probe, deliberate validate-only, forward paper soak, and exchange-to-journal checks. Run the soak-report logic against clean and failing synthetic journals and spell out which evidence an actual machine and venue may later supply. Coinbase uses independent evidence and does not borrow Kraken’s qualification.

## Acceptance

- CI produces repeatable fake-only permutations for Kraken and Coinbase. No change enables live trading or depends on real credentials.
- The engineering evidence is ready before the owner starts testing a candidate. Actual live release approval and evidence are tracked in M10 and M20 separately.

During implementation, add the acceptance coverage to the existing test suites and record the actual results in the pull request. The audited baseline is Krellbot `main` at `1a3f4cc`.
