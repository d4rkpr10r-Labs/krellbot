# Cursor agent guide (local MCP)

Use **Cursor** as the first shipped agent client for Krellbot’s local MCP
surface. The web workstation remains the operator product surface; this
guide is how a Cursor agent may **validate → backtest → paper-arm**
against `$KRELLBOT_HOME` without live arm or key access. The same server
works for Claude Desktop, Claude Code and VS Code; their setup snippets
are in [mcp.md](mcp.md#set-up-your-client).

Through MCP, Krellbot controls only what its tools return. It cannot
police what your client's model writes around those results. Every result
carries its own labels (`simulated`, `not_advice`, `paper`) and, when
refused, a typed code; the `krell` prompt asks the model to keep those
labels in its own words, but Krellbot cannot enforce that.

Companion skill (project path named for install):
[`.cursor/skills/krellbot-mcp/SKILL.md`](../.cursor/skills/krellbot-mcp/SKILL.md).
Surface contract: [mcp.md](mcp.md).

## Install

1. Install / run Krellbot on the same machine as Cursor
   ([getting-started.md](getting-started.md)). Data home defaults to
   `~/.krellbot` (`$KRELLBOT_HOME`).
2. Add the server to Cursor (`.cursor/mcp.json` in a project, or
   `~/.cursor/mcp.json` for every project). Cursor launches
   `krellbot mcp` and talks to it over stdio:

   ```json
   {
     "mcpServers": {
       "krellbot": { "command": "krellbot", "args": ["mcp"] }
     }
   }
   ```

   If `krellbot` is not on Cursor's `PATH`, use the full path to the
   installed executable. To use another home, add
   `"env": {"KRELLBOT_HOME": "/path/to/home"}` to the entry.
3. Optional: work in a checkout of this repository so the project skill
   `.cursor/skills/krellbot-mcp/SKILL.md` loads, or copy or symlink it
   into a personal Cursor skills directory.

Cursor asks you before each tool call unless you turn that off, and every
call is journaled as an `agent_action` under the actor `mcp:cursor` (from
the client name Cursor sends when it connects). Use the `krell` prompt to
start Krell in Cursor, and `krell_tour` for her guided tour.

Scripts and tests can call the five original verbs in-process through
`McpTools` (same core, same refusals and audit), with an explicit home and
never a remote key store:

```python
from pathlib import Path
from krellbot.mcp import McpTools

tools = McpTools(Path.home() / ".krellbot")  # or Path(os.environ["KRELLBOT_HOME"])
tools.list_tools()  # read, draft, validate, backtest, paper_arm
```

There is no cloud MCP host, no HTTP MCP transport and no
exchange-credential channel. `krellbot mcp` runs on the operator machine
only, launched by the client.

## Auth to loopback

Krellbot’s trust perimeter is **loopback on this machine**:

- The workstation API binds `127.0.0.1` and expects Host / Origin /
  session / CSRF checks (see [threat-model.md](threat-model.md)).
- MCP tools do **not** replace that browser session. `krellbot mcp`
  speaks stdio to the client that launched it, binds `$KRELLBOT_HOME`,
  and never opens a listener, a non-loopback control plane or ships keys
  to krellbot.dev.
- Do not paste bootstrap tokens, session cookies, or reconnect codes
  into chats, tickets, or remote agents.
- Tailscale Serve (if used) is an operator-owned loopback proxy; it is
  not an MCP auth shortcut and does not authorize live arm or key read.

If you need the UI, open `http://127.0.0.1:<port>/` locally after
`krellbot` / `krellbot ui --open`. Agents stay on the MCP allowlist
below rather than scraping authenticated browser cookies.

## Allowed verbs

Effects: `read` changes nothing; `research` writes drafts or runs
backtests in-process; `change` acts on **paper** strategies only.

| Tool | Effect | Labels | Purpose | Typical params |
| --- | --- | --- | --- | --- |
| `read` | read | none | Inventory: owned drafts, pack library rows, armed paper records, operator policy | _(none)_ |
| `positions` | read | `paper` | Paper free cash and holdings per armed paper market | optional `venue`, `pair` |
| `journal_recent` | read | none | Recent paper journal records, secret fields removed | optional `since`, `tail` |
| `alerts` | read | none | Kill switch, reconcile, stale feeds and refusals | _(none)_ |
| `unresolved_orders` | read | none | Orders awaiting reconcile (read only) | _(none)_ |
| `market_snapshot` | read | none | Cached candles and paper feed freshness; no network call | `venue`, `pair`, `timeframe`, optional `bars` |
| `catalogue` | read | `not_advice` | Installed packs and the cached catalogue with disclosures | _(none)_ |
| `explain_bar` | read | `simulated` | Why one bar decided what it did | `bar_ts`, and `job_id` **or** `venue`, `pair` |
| `tour_step` | read | none | One step of Krell's tour, or the step list | optional `step_id` |
| `draft` | research | none | Create or edit a strategy draft | `pack`, optional `parent_revision_id` |
| `validate` | research | none | Lint a draft revision | `revision_id` |
| `backtest` | research | `simulated` | Offline research; returns the receipt and the run identity (`run_id`, receipt digest) the workstation records for the same inputs | `revision_id` **or** `pack_path`, `dataset_csv`, optional `venue`, `pair`, `timeframe`, costs and window |
| `compare` | research | `simulated` | Backtest 2 to 4 draft revisions on one dataset, side by side | `revision_ids`, `dataset_csv` |
| `paper_arm` | change | `paper` | Paper-arm a pack or validated revision within the agent limits | `venue`, `mode="paper"`, `pack_path` **or** `revision_id`, optional `paper_balance`, `daily_loss_limit_pct` |
| `pause_entries`, `resume_entries`, `disarm` | change | `paper` | Pause or resume entries on, or disarm, an armed **paper** strategy; a live (or non-paper) one is refused (`agent_live_change_refused`) | `venue`, `pair` |

`krellbot mcp` offers every tool above (`krellbot.agent.assemble.build_agent_core`); `McpTools` keeps the five original verbs. External agents may pause, resume or disarm only after the operator turns on `mcp_allow_pause_resume` or `mcp_allow_disarm` in the agent limits (otherwise `agent_change_not_allowed`). Before turning on `mcp_allow_pause_resume`, know that an agent's `resume_entries` also lifts the entry hold a person's close-by-operator leaves on a paper strategy, as the workstation's Resume does, so that strategy may buy again. A live target is refused before Krell asks anyone to confirm; pause or disarm a live strategy yourself in the workstation. Closing a position sells at market, so it is never an agent tool.

Happy path for an agent: **`validate` → `backtest` → `paper_arm`**.
Call `read` first when you need ids/paths. Research files must live in the
Krellbot home: a `pack_path` is a `.json` under `packs/`, `drafts/` or
`agent-packs/` (or pass a draft's `revision_id`), and a `dataset_csv` is a
`.csv` under `cache/` or `datasets/imported/`, or directly in the home
folder. Anything else is refused without echoing the path. Use offline
`dataset_csv` so CI and agents stay fake-transport / offline.

Example paper-arm shape (never `mode=live`):

```python
tools.call(
    "paper_arm",
    pack_path="/path/to/pack.json",
    venue="kraken",
    mode="paper",
    paper_balance="1000",
    daily_loss_limit_pct="5",  # the agent limits require one by default
)
```

Typed refusals return `ok=False` dicts (`mcp_live_arm_refused`,
`mcp_key_read_refused`, …) — treat them as hard stops, not retries with
broader permissions.

## Policy

Operator **policy** lives at `$KRELLBOT_HOME/user-policy.json` (allow
paper-arm, deny live, deny key export, and the agent limits; defaults in
[mcp.md](mcp.md#explicit-non-goals)). Whatever it says, the MCP boundary
still hard-refuses live arm and any key/secret/credential tool name.
Honor denials; do not work around them via CLI, env, or keyring.

## Explicit non-goals

- **Never live-arm.** No `mode=live`, no live-named tools, no typed
  `LIVE` grant through MCP. Live stays workstation-owned.
- **Never read, export, or transmit exchange keys.** No keyring probes,
  no `KRELLBOT_*_KEY` / secret exfiltration, no credentials in prompts
  or tool args.
- **Never closes positions or changes live strategies.** There is no
  close, sell or order tool, and the change tools refuse a live strategy.
- No MCP key write, hosted keys, HTTP MCP transport, or CLI-as-product
  agent UX (`krellbot mcp` is plumbing the client launches).
- No inventing a Learn view; no presenting backtest/fixture numbers as
  live market performance.
- `PAPER_ONLY_RELEASE` stays `True` for agents — only the owner flips
  release policy.

## Related

- [mcp.md](mcp.md) — tool table, prompts, client setup and refusal codes
- [paper.md](paper.md) — paper arm semantics
- [threat-model.md](threat-model.md) — loopback / keychain boundaries
- [market-position.md](market-position.md) — why local policy-bound MCP
