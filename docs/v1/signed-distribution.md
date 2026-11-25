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

## Native candidate evidence

The release matrix qualifies macOS arm64, Windows x86-64, and Linux
x86-64. Linux arm64 is outside this release matrix and has no frozen
download. On an unsupported OS or architecture, stop before download and
report the detected pair and the supported pairs.

Use the `release-frozen` run for the candidate commit. For each target,
compare the ZIP SHA-256 and size against the manifest. Confirm the source
commit and target in both the manifest and `native-install.json` (preview)
or `signing-status-<os>-<arch>.json` (dispatch/tag). These reports come
from verifying and opening the exact archive on its native runner. The
CI paper smoke exercises the installed runtime, workstation shell, and
example pack with an isolated profile. Its upgrade check replaces an
existing same-version program and restores its prior files. Linux and
Windows also exercise the bundled installer, desktop entry or
Start-menu shortcut, no-argument launch, and onboarding in Chromium.
See `linux-installed-preview-evidence` and
`windows-installed-preview-evidence`. The macOS archive smoke stages the
extracted bundle or one-dir payload.

For the M19 outsider check, use a clean account and then an account with
a previous supported release. Record the exact digest and OS version,
download and verify the ZIP before extraction, launch using the native
launcher and the documented PATH entry where applicable, and reproduce
`docs/example-pack.json` in Paper. Confirm an upgrade preserves user
data, restore the saved program, then launch it and confirm the same
profile is visible. Record Gatekeeper or SmartScreen behavior observed
on the actual download. CI's native verification and runners do not
establish those interactive prompts or a cross-version upgrade.

Dispatch `release-frozen` with `sign_release=true` once the owner has
configured signing inputs as described in [Signing](../signing.md).
A dispatch creates Actions evidence artifacts. If an input is missing,
the relevant job names the missing setting and fails without a signed
marker. For signed evidence, require the extracted macOS app to pass
Developer ID, Gatekeeper assessment, and staple validation, and require
Windows Authenticode with a trusted timestamp. Linux remains
checksum-pinned. Verify the final distribution archives and complete
the M19 owner checks before public promotion.
