# Polish exchange connection and recovery in Settings

Make the Kraken and Coinbase key connection usable and diagnosable from the local workstation, with real and simulated balances clearly separated.

## Implementation

- Build on `KeysOnboardingPanel.tsx`, `keys_onboarding.py`, and the existing authenticated loopback routes. Explain which key fields each venue expects, why permission verification is required, which local keychain will store it, and that the bot can run paper without a key.
- Make connected, key stored but venue unavailable, permissions refused, native keychain unavailable, and disconnected states easy to distinguish. Cover duplicate submit, replacing a key, removal, revoked keys, locked keyring, bad input, clock skew when exposed by the venue, and partial keychain writes. Keep the current permission rejection and redaction guarantees.
- Keep exchange account holdings visibly separate from simulated balances. Adjust the engine getting-started guide so the normal UI instructions describe the OS keychain accurately and deprecated credential file paths are not part of the quickstart.

## Acceptance

- Using fake exchanges and keyrings for each venue, complete connect, reload, restart, reconnect after revoked permission, replace, and remove. A refused replacement retains the earlier usable credential only when readback proves it.
- Withdrawal, uncertain permission, unavailable keyring, and reached-but-revoked responses show a safe corrective message. API responses, logs, browser errors, and clipboard behavior contain no submitted secrets.
- Opening, canceling, or removing the key flow leaves valid paper runs intact. A new user can complete the paper path entirely without opening Settings.

During implementation, add the acceptance coverage to the existing test suites and record the actual results in the pull request. The audited baseline is Krellbot `main` at `1a3f4cc`.
