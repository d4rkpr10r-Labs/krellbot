# Live venue qualification evidence

Use this runbook to reproduce the engineering checks for Kraken and Coinbase.
Each venue needs its own evidence record. A successful fixture run records
how Krellbot handled the response under test. A venue qualification also
requires evidence from that venue under the conditions described in M10 and
M20.

## Reproduce with fixtures

Run the following from a developer checkout after syncing the locked
dependencies. All credential material in these suites belongs to test
fixtures. The shipped release policy remains paper-only.

```sh
KRELLBOT_HOME=$(mktemp -d) uv run --no-sync pytest -q \
  tests/test_venue_kraken.py tests/test_kraken_documented_responses.py \
  tests/test_kraken_validate_smoke.py tests/test_live_tick_wiring.py \
  tests/test_live_obligations.py tests/test_venue_coinbase.py \
  tests/test_coinbase_live_gate_parity.py tests/test_refuse_withdraw_keys_live.py \
  tests/test_web_live_arm.py tests/test_live_authorization_api.py \
  tests/test_live_guardrails_surface.py tests/test_v096_paper_only_policy.py \
  tests/test_m3_fault_matrix.py tests/test_soak_report.py
```

| Evidence | Kraken | Coinbase |
| --- | --- | --- |
| Permission checks | Trade-only probe, ambiguous permissions, and a fresh probe after trade permission is revoked | Separate Advanced Trade probe, transfer-permission refusal, and a fresh probe after trade permission is revoked |
| Venue behavior | Quantity and notional minima from AssetPairs, atomic conditional stop payload, and bounded rate-limit retries | Quantity and notional minima from Products, protective stop sized from the confirmed fill, and a distinct failure when the venue rejects protection |
| Order lifecycle | Documented order, partial fill, stop, and cancellation responses. Outbox fault matrix covers restart and ambiguous acknowledgement | Documented Advanced Trade order, partial fill, stop, and cancellation responses. The adapter and engine tests cover stop placement and recovery without re-sending an unknown entry |
| Ownership | Pending exits and stop management use the strategy's owned position | The Coinbase fixture includes external holdings, and exits use only the strategy's owned quantity |

The documented rate-limit fixtures exercise venue response handling. Record
any retry or reconciliation behavior separately when reviewing real venue
evidence.

The Operations journey covers the disabled live controls in a paper-only
build. Run the authorization, unresolved-order, kill-switch, and live-arm
UI tests for scope, duplicate submissions, and unknown outcomes.

```sh
cd frontend
npm run test:unit -- --run \
  src/features/operations/LiveAuthorizationPanel.test.tsx \
  src/features/operations/LiveAuthorizationPanel.secondVenue.test.tsx \
  src/features/operations/UnresolvedOrdersList.test.tsx \
  src/features/operations/OperationsView.killCommandFeedback.test.tsx \
  src/features/deployments/LiveArmConfirm.test.tsx
npm run test:journeys -- j8-live-authorization.journey.ts
```

## Venue evidence record

Copy the fields below into a separate record for each venue and approved test
window. Keep internal receipts in a location with appropriate access. Before
sharing, remove credentials, account identifiers, private response bodies,
and balances unrelated to the scenario.

| Field | Record |
| --- | --- |
| Build and environment | Source commit, archive SHA-256, OS, UTC time, venue, market, and profile label |
| Connect and probe | Outcome and verified permission classes. Record the result after a permission change |
| Deliberate validation | For Kraken, record the guarded validate-only diagnostic result and verify that no order appeared in venue history. For Coinbase, record validation as pending unless a separate Coinbase-specific method and result are available |
| Forward paper soak | Use a dedicated profile with one venue and market. Record the UTC window, scheduled check coverage, market-data failures, and the soak report output. Run `scripts/soak_report.py --days 7` against that profile |
| Reconciliation | Under the separately approved live qualification, compare the venue receipt and journal for direction, filled quantity, commissions, protective order, and final state. Record the reconciled outcome of any unknown acknowledgement |
| Disposition | Fixture checks passed, venue checks pending, venue checks passed, or failed with a specific reason |

The soak report uses all records in the selected profile. Using a dedicated
profile prevents one venue's check from filling a gap in another venue's
record. A failed price fetch, interrupted check, missing candles, stale data,
or wake gap fails the affected hour even if a successful tick is present.
A synthetic soak validates the reporter, while forward-paper qualification
uses the journal produced by the running candidate.

See the [venue smoke and soak gates](../milestones/M10.md) and the
[paper-only release gate](../milestones/M20.md) for the separate approval and
venue evidence requirements.
