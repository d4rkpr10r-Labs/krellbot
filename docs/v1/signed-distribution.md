# Prepare signed builds and clean-install release verification

Prepare the public distribution pipeline for macOS notarization, Windows signing, and a verified exact-artifact install and rollback.

## Implementation

- Build on `release-frozen.yml` and `docs/signing.md`. Test package metadata, notarization and stapling hooks, Authenticode verification, digest and manifest consistency, and channel truthfulness. Add an isolated check that missing signing inputs receive a useful message and block signed promotion.
- Verify clean install, first launch, and upgrade for macOS arm64, Windows x86-64, and Linux x86-64. Check download verification, PATH discovery, OS warnings, backup and rollback, and unsupported architecture copy. Treat Linux arm64 support as a named scope choice with matching documentation.
- Use unsigned preview artifacts for CI where signing inputs are unavailable. Document how to promote a verified candidate when platform certificates are available without exposing certificates, passwords, or keys. Leave public promotion as a separate release decision.

## Acceptance

- CI records artifact digests, platform identity, signing-verification outcome, and install smoke by build. It cannot label unsigned or unverified binaries as signed.
- Repeatable clean-install and pack reproduction checks align with M19. A public cut requires recorded native warning behavior and actual signed artifact evidence for the supported platforms.

During implementation, add the acceptance coverage to the existing test suites and record the actual results in the pull request. The audited baseline is Krellbot `main` at `1a3f4cc`.
