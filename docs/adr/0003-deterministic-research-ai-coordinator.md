# 0003 — Research is a deterministic engine coordinated, not computed, by AI

- Status: Proposed 2026-11-25 (accepted when the owner merges M29).
- Owners: backtest, research, api/jobs, mcp.
- Replaces: nothing. Constrains tranche 2 agentic research.

## Context

The brief asks for an agentic research workstation. Today the engine has a
deterministic Decimal backtester, hashed receipts, holdout refusal and a
bounded job queue (`api/jobs.py`). There is no LLM client. `mcp/tools.py`
lets a local agent read, draft, validate, backtest and paper-arm under
`user-policy.json`, never live-arm or read keys.

## Decision

### Numbers come only from the engine

Every metric, equity curve and trial result is produced by the deterministic
engine and stored with a run id (M29.1). An AI component may propose packs,
parameters and hypotheses, and may summarize stored results. It never writes
a metric, and summaries cite run ids.

### All trials are recorded

Sweeps and AI-driven searches record every trial, including failures and
cancellations (M29.9). A reported best result always carries the trial
count. Holdout data is never used to select a candidate.

### Generated packs are untrusted input

Generated strategies are pack JSON validated by the existing schema and
`pack/lint.py`. No generated executable code. Generated packs can be
backtested and paper-armed through the same policy-bound tools; live arm
stays a typed human action in the workstation.

### Bounded by construction

Research jobs have hard caps on trials, wall time, concurrency and (for AI)
tokens or spend, checked before the job starts. Cancel is cooperative and
prompt (M29.4). An AI loop cannot enqueue more work than its budget.

### Providers

Bring-your-own-key and local models are first class and unmetered. A hosted
model, if the owner approves one, is metered and capped per ADR 0002.

## Consequences

- AI adds hypotheses and convenience, not authority.
- The same research works with AI turned off.

## Test names that enforce it

- `tests/test_research_run_identity.py` (M29.1)
- `tests/test_research_sweep_trial_ledger.py` (M29.9)
- `tests/test_mcp_paper_tools_refuse_live_and_keys.py` (existing)
