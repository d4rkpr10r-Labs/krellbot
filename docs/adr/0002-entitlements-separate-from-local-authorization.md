# 0002 — Commercial entitlements never gate local engine capability

- Status: Proposed 2026-11-25 (accepted when the owner merges M28).
- Owners: license, checkout, run, packs.
- Replaces: nothing. Constrains future tiers (Plus / Pro, hosted services).

## Context

Three entitlement models exist today:

1. The signed license cache (`license.py` `entries_allowed`), read by the tick
   gate in `run/__init__.py`. When it fails, entries stop and exits continue.
2. SKU entitlements (`application/checkout.py`), written by checkout and
   refund, read by install.
3. The legacy CLI `gate()` / `check_license`, which keeps a plaintext license
   key under `~/.krellbot/state.json`.

A refund revokes the SKU entitlement, but the tick gate reads the license
cache. Adding Plus / Pro tiers or hosted-service entitlements on top of this
would multiply the drift. The platform brief also requires that the free
engine is never artificially limited.

## Decision

### Two separate questions

- **Local authorization** answers "may this operator do this on this
  machine?" (session, CSRF, live typed confirmation, user policy, release
  fence). It never consults billing.
- **Commercial entitlement** answers "may this install use this paid content
  or hosted service?" It is checked only at the content or service boundary.

### What entitlements may gate

- Paid pack content (install and new entries of a paid pack).
- Hosted services (hosted AI allowance, relay, remote compute).

### What entitlements may never gate

- Number of bots, strategies, venues, backtests or paper runs.
- User-authored or community packs, BYOK AI, local compute.
- Exits, stop maintenance, export, diagnostics, journal access.

### Lapse

Lapse (expiry, refund, failed renewal, unreachable billing) stops **new
entries** of the affected paid pack only. Exits and protective stops keep
running. Nothing is force-closed. The exact policy is the owner's M28.11
decision; this ADR fixes the floor.

### One read facade

All entitlement reads go through one local service. The tick gate, install
and UI do not read cache files directly.

## Consequences

- New tiers become data (entitlement kinds), not new code paths.
- Billing outages cannot strand positions.
- The legacy CLI license path is retired or parked.

## Test names that enforce it

- `tests/test_entitlement_lapse_keeps_exits.py` (M28.9)
