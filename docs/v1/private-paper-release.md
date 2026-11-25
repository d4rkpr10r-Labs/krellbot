# Package a testable paper-only release candidate

Build the exact merged commit into installable preview artifacts, verify upgrade and rollback, and make it possible to begin owner testing without a live release or signing dependency.

## Implementation

- Use the existing `release-frozen.yml` packaging path and report the same commit, version, artifact names, digests, and platform matrix in the artifact manifest, About panel, and diagnostics. Ensure prerelease build and release metadata derive from a single source where practical.
- Provide an artifact-only candidate path that leaves the public release and site installer pins alone. Run smoke tests on macOS arm64, Linux x86-64, and Windows x86-64, and record the platform-specific support boundary. Provide local build or controlled artifact delivery. Artifacts on a public repository should be treated as potentially accessible to people who can read the run.
- Exercise unpack, launch, fresh profile session, upgrades from v0.9.7, rollback, and uninstall behavior using throwaway profiles. Check PATH recovery and old tabs after restart. Keep keys, history, paper strategies, and the live-order release block intact. Leave current public signing and production promotion separate.

## Acceptance

- A candidate is traceable from the exact commit to each downloadable archive and SHA-256, and opens the current workstation with key Settings. The candidate can be exercised with fake exchanges and a fake keychain before any account is connected.
- One starter can arm, consume a controlled tick, pause/resume, restart, stop, and export on the packaged candidate. Upgrade and rollback preserve data and reuse the stable launcher and scheduler path.
- Provide signed-build preparation in the later distribution PR. Native OS keychains and real-account connectivity are feedback to collect once the candidate is in use.

During implementation, add the acceptance coverage to the existing test suites and record the actual results in the pull request. The audited baseline is Krellbot `main` at `1a3f4cc`.
