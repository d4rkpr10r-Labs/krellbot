# Krellbot

Krellbot is a free, open-source workstation for testing crypto trading rules on your computer. Load a strategy pack, replay candles to inspect its decisions, or arm the strategy with a simulated balance. The public release is paper-only. You can start with an included strategy and no exchange account.

Site: https://krellbot.dev
Docs: https://krellbot.dev/docs/
Security: [SECURITY.md](SECURITY.md)

## Install

The [installation guide](https://krellbot.dev/docs/install) lists the supported processors for each operating system. Minimal Linux images need CA certificates,
curl and unzip first.

```sh
# Linux
curl -fsSL 'https://krellbot.dev/api/install?os=linux' | sh

# macOS
curl -fsSL 'https://krellbot.dev/api/install?os=mac' | sh
```

```powershell
# Windows
irm 'https://krellbot.dev/api/install?os=win' | iex
```

The site installer downloads a per-user, one-directory frozen release
(artifact + SHA-256 manifest pinned by the site), verifies the digest
**before** staging anything, drops the binary under
`~/.local/share/krellbot/versions/<version>/` (POSIX) or
`%LOCALAPPDATA%\Krellbot\versions\<version>\` (Windows), and creates a
`krellbot` launcher at `~/.local/bin/krellbot` (POSIX) or
`%LOCALAPPDATA%\Krellbot\bin\krellbot.cmd` (Windows). Running `krellbot`
with no arguments starts the workstation on loopback and opens it in your
default browser. Then [arm your first paper strategy](#arm-your-first-paper-strategy).

**Signing status (this release):** the downloaded artifact is verified
against an HTTPS-pinned SHA-256 manifest, but the artifact itself is
**not** code-signed and **not** notarized. macOS Gatekeeper and Windows
SmartScreen may show a first-run warning; the SHA-256 check before
extraction is the integrity guarantee today. Authenticode, notarization,
and signed publisher releases are a future hardening step.

## Update policy

Re-run the same install command. Each release is staged under
`~/.local/share/krellbot/versions/<version>/` (or the Windows
equivalent) and the launcher swaps only after a smoke check. The prior
launcher is kept at `~/.local/bin/krellbot.prior` until the new one
passes; rollback is manual (rename). The data home
(`$KRELLBOT_HOME`, default `~/.krellbot`) is **never** touched by
install or update.

## Uninstall

Uninstall, PATH checks, Tailscale Serve setup and other operational
snippets live in [docs/cli.md](docs/cli.md). Uninstall removes only the
managed launcher and staged version directory; `$KRELLBOT_HOME` is
retained by default.

## Where things live

| What | POSIX | Windows |
| --- | --- | --- |
| Data home (`$KRELLBOT_HOME`, default) | `~/.krellbot/` | `%USERPROFILE%\.krellbot\` |
| Frozen binary | `~/.local/share/krellbot/versions/<ver>/krellbot` | `%LOCALAPPDATA%\Krellbot\versions\<ver>\krellbot.exe` |
| Launcher | `~/.local/bin/krellbot` | `%LOCALAPPDATA%\Krellbot\bin\krellbot.cmd` |
| Prior launcher (after update) | `~/.local/bin/krellbot.prior` | `%LOCALAPPDATA%\Krellbot\bin\krellbot.cmd.prior` |

`$KRELLBOT_HOME` is independent of the install path and is the only
place keys, paper state, the journal, and receipts live.

## Arm your first paper strategy

Run `krellbot` to open the local workstation. Follow the [first paper strategy walkthrough](https://krellbot.dev/docs/getting-started) for the version installed by the website. It shows how to save and validate a free starter, arm it with simulated cash, and check its first candle. An armed row confirms the configuration. A completed tick tells you that the engine evaluated the strategy. A strategy can complete a tick without making a trade.

A research run is optional before arming. If you want an offline demonstration,
[paper trading](docs/paper.md) documents the public repository pack/candle
fixtures; demonstration fixture results are not market performance.

## Private access from your Tailscale devices

For an always-on workstation, keep the backend on loopback and let
[Tailscale Serve](https://tailscale.com/kb/1312/serve) provide tailnet-only
HTTPS. The exact launcher flags and Serve command are in
[docs/cli.md](docs/cli.md#private-access-from-your-tailscale-devices).

## Advanced and diagnostic commands

Use the terminal to install and launch the workstation. Once it opens, you can configure and arm a paper strategy in the browser. See [CLI commands](docs/cli.md) for diagnostics and scheduled ticks.

## Live and the UI gate

The workstation listens on loopback. The public build blocks live orders, including cancellations and edits. An environment variable, stored key or authorization file cannot turn them on. See [live trading](docs/live.md) for the policy and [paper trading](https://krellbot.dev/docs/paper) to run a simulation.

## Working on the engine

Building from source is for engine and UI development, separate from the
installed release. See [Build from source](docs/cli.md#build-from-source).
