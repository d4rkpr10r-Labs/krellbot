# 0004 — Hosted services are key-blind and outbound-only

- Status: Proposed 2026-11-25 (accepted when the owner merges M28).
- Owners: notifications, api, telemetry, threat model.
- Replaces: nothing. Constrains Connect, hosted AI and remote compute.

## Context

`docs/threat-model.md` keeps the trust perimeter on loopback, and krellbot.dev
receives only the license check and opt-in telemetry. The brief proposes
remote monitoring (Connect), hosted AI and cloud backtesting. Today remote
access exists only as `api/tailnet.py` `TailnetAccess`, which gives a tailnet
device the full workstation.

## Decision

### Keys and order authority stay local

No hosted service receives exchange credentials, signs requests, or can
place, cancel or arm orders. Hosted workers backtest and paper-simulate only.

### Outbound only

The local engine opens connections to hosted services; nothing listens on a
public interface. A relay never forwards inbound requests to the loopback
API.

### Read-only first

The first remote scope is read-only status, alerts and portfolio summaries.
Any remote command is a separate, enumerated, locally authorized action that
the local engine journals. No remote shell, no arbitrary code.

### Explicit consent per data class

Each data class sent off-machine (health, alerts, positions, P&L, packs) is
opt-in, listed in `docs/telemetry.md`, and revocable. Device enrollment is
explicit and revocable from the local workstation.

### Fail local

If a hosted service fails or a subscription lapses, the local engine keeps
running under its local configuration (ADR 0002).

## Consequences

- Connect starts as an alert and status mirror, built on M28.10.
- `docs/threat-model.md` gets a hosted-services section before any hosted
  service ships.

## Test names that enforce it

- None yet. The first hosted-service milestone must add egress-allowlist and
  no-key-in-payload tests before it ships.
