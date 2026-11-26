# Parking lot

One line per item. Nothing here is work until the owner moves it into a milestone file.

- 79 Python test files are named after retired task IDs (`test_m1r_*`, `test_ns*`, `test_int*`); rename by behavior when touched.
- CLI commands with no web equivalent could be removed once the web app covers them.
- `scripts/release_archive.py` still accepts a root `static/index.html` as the UI payload (the test fixtures use it); only `frontend/dist/index.html` is real after M0.5.
- Signal concept artboard shows a Learn top-nav; no Learn view exists yet — do not invent Learn in the cockpit. Portfolio history API + Workstation surface shipped in M4.4 (real journal/fills only; never fabricate sample bots).
- CI frontend flake: `Editor.ownedSeedIdentity.test.tsx` intermittently misses `[data-testid="editor-revision-id"]` under full suite load (seen on #426; passes alone locally; main tip green).
- CI macOS flake: `tests/test_int01_workstation_launch.py` under full-suite load (seen on #450); wait/HTTP budgets raised — keep watching macOS matrix if TimeoutError returns.
- CI Windows 3.13 flake: `tests/test_installer_wrapper_vs_engine.py` live installer fetch can still fail after retries under sustained SSL loss (retries landed; keep watching Windows matrix).
- `docs/telemetry.md` does not list the `backup_completed` event that `storage/backup.py` sends when telemetry is on.
- `SECURITY.md` says the bootstrap token comes from `token_hex(32)`; `api/serve.py` uses `token_urlsafe(32)`.
- `GET /api/v1/catalogue` returns 502 when the transport fails instead of serving the verified cached catalogue.
- Canonical-JSON + sha256 is reimplemented in six modules (`backtest/receipt.py`, `packs/launch_family.py`, `strategy_ir/canonical.py`, `strategy_ir/identity.py`, `application/packs.py`, `api/jobs.py`).
- Hosting operator exchange keys (cloud key custody) — explicitly out of P0–P4; never promote without a custody redesign.
- Paid ads for trading signals — restricted channels; out of P0–P4 growth plan.
- Personalized / individualized trading advice framing — packs stay tools; out of scope.
- CLI user-facing feature expansion — AGENTS.md web-only surface; keep CLI as plumbing.
- More than two qualified live venues — Binance live still waits after M23.4 Coinbase second-venue product path; park further live venues.
- Exchange-native free AI agent tooling parity race — spike only in M22.5; no cloud-key me-too product.
- Multi-pack reservation / breaking one-pack-per-(venue,pair) before independent qualification (still true post-M16).
- Learn top-nav / Learn view (Signal artboard) — still do not invent in cockpit.
- `run.tick(home=X)` appends the journal under `$KRELLBOT_HOME` but reads ownership from `X/journal`; harmless while both are the same directory.
- Paper taker-fee tables exist in four copies (`venues/paper._fee_bps_for` and `run._fee_bps_for_venue`, both on risk paths, plus `application/paper._PAPER_TAKER_BPS` and `application/research._default_fee`). They agree on every listed venue but fall back differently for an unlisted one: `run` gives 0, `venues/paper` 120 (Coinbase's), `_PAPER_TAKER_BPS.get(venue, 40)` 40, and `_default_fee` 120 for any venue but Kraken.
- `notifications.EmailSmtpNotifier` swallows a failed `starttls()` and still calls `smtp.login`, so with `KRELLBOT_SMTP_PASSWORD` set the password can cross an unencrypted SMTP session; it should refuse to log in without TLS.
- `frontend/e2e/journey-harness/harness-negative-controls.spec.ts` "Python IPv6 and DNS refusals precede OS calls" fails on hosts without IPv6: `socket.socket(AF_INET6)` raises `EAFNOSUPPORT` before the guarded `connect` runs, so the control proves nothing there.
- `run.tick` journals nothing when a pair's latest candle was already journaled (`_already_journaled`), so a feed that keeps returning one old candle leaves no stale read: `feed_stale` only sees a new candle that is already old, and a stuck pair shows only as health `stale_check` or venue `scheduler_stale`.
- `npm run test:journeys` writes its HTML report to `frontend/playwright-journey-report/` (playwright.journeys.config.ts:26), which `.gitignore` does not list, so every local journeys run leaves an untracked folder.
- The backtest never raises the stop: `backtest/engine.Backtester.run` sets `stop_price` only on an entry from flat, while `run.tick` calls `raise_stop` on a later long bar whose stop is higher. So `explainPack.ts` `STOP_RAISE_SENTENCE` ("only raised, never lowered") holds for paper and live but not for research backtests, and the same pack can stop out at different prices in the two.
- `frontend/src/features/referral/ReferralDisclosurePanel.tsx` says "Exchange credentials never leave the OS keychain" and Settings → Privacy says exchange keys are "never transmitted": keys are sent to the exchange, and a `KRELLBOT_<VEN>_KEY`/`_SECRET` override or `_KEYFILE` bypasses the keychain (`secrets.get`).
- `PortfolioHistoryPanel` and `PaperPnlPositionChart` draw axis labels at a fixed 10px inside a fixed-width SVG `viewBox` scaled to the panel, so at 390px they render at roughly 3–5px; legible phone charts need width-aware chart sizing in the TSX, which CSS alone cannot do.
- `npx tsc -p frontend/e2e/tsconfig.json` reports 3 errors in `e2e/ns20-100k-rows.spec.ts:180` and `e2e/workspace-setup-tour.spec.ts:11,53`; `npm run typecheck` does not cover `e2e/`, so nothing catches type errors in specs.
- Operations Alerts search's accessible name stays "Search alerts by code, venue, or kind" (pinned verbatim by `frontend/src/features/operations/AlertsFilter.aria.test.tsx`) although M28.16 made it match pair too; only the placeholder says pair.
- J5.4 failed once in a local full journeys run on 272fc1f before its `test.fail` line: `armPaper` → `openTechnicalJson` (`frontend/journeys/helpers/journey-steps.ts:278`) waited 20 s for the raw JSON textarea, which stayed hidden; it passed as an expected failure in the next two full runs.
- Paper slippage exists in two copies: `application/paper._PAPER_SLIPPAGE_BPS` (5) copies `venues/paper.SLIPPAGE_BPS` so `GET /api/v1/paper/assumptions` never imports the venue risk path. `tests/test_paper_assumptions_route.py` pins them equal, so changing one fails CI until the other changes too.
- `tests/test_ns12_operational_store.py::test_second_writer_is_refused_while_the_lock_is_held` failed once on Windows 3.13 CI at `assert started.wait(timeout=2)` (PR #659, run 45862473531), then passed on the next run with the same code; the 2 s thread-start wait looks too tight for Windows runners.
