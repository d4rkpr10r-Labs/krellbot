# MCP agent surface (local)

Krellbot serves its agent tools to any **local** MCP client (Claude Desktop,
Claude Code, Cursor, VS Code) over stdio. The client launches
`krellbot mcp`; there is no HTTP or remote transport and no new network
listener. Tools bind to application facades under `$KRELLBOT_HOME`
(loopback domain / offline datasets). They never talk to exchange key
stores and never live-arm.

Every call runs through the same agent tool core as Krell in the
workstation, so the hard refusals, the operator's agent limits and the
audit trail are identical. Each call appends one `agent_action` record to
the journal under the actor `mcp:<client>`, taken from the name the client
sends when it connects and reduced to lowercase letters, digits, `.`, `_`
and `-`. For example, a client that names itself `Cursor` is recorded as
`mcp:cursor`, and one that sends no usable name as `mcp:local`.

## What MCP can and cannot control

Through MCP, Krellbot controls only what its tools return. It cannot police
what your client's model writes around those results.

Every tool result is one JSON text block: `ok`, `effect`, `tool`, `actor`
and `labels`, plus `code` and `message` when the call is refused. The
labels are `simulated` (backtest or simulated numbers, not market
performance), `not_advice` and `paper` (simulated funds). A refusal
(`ok: false`) is a normal result with a typed code, not a protocol error,
and it is final: an agent should report it, not retry with broader
permissions. Krellbot cannot make the client's model repeat those labels;
the `krell` prompt asks it to.

## Set up your client

Each client starts the server itself with `krellbot mcp`. If `krellbot` is
not on the client's `PATH`, use the full path to the installed `krellbot`
executable. The server uses `$KRELLBOT_HOME` (default `~/.krellbot`); to
point it at another home, add `"env": {"KRELLBOT_HOME": "/path/to/home"}`
to the server entry. Where each config file lives is the client's own
documentation to confirm.

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "krellbot": { "command": "krellbot", "args": ["mcp"] }
  }
}
```

**Claude Code**:

```sh
claude mcp add krellbot -- krellbot mcp
```

**Cursor** (`.cursor/mcp.json` in a project, or `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "krellbot": { "command": "krellbot", "args": ["mcp"] }
  }
}
```

**VS Code** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "krellbot": { "type": "stdio", "command": "krellbot", "args": ["mcp"] }
  }
}
```

MCP clients ask you before each tool call unless you have told them not
to; keep that on for the `change` tools. Krellbot adds no second approval
for MCP agents; the operator's agent limits bind them instead.

## Prompts

| Prompt | What it gives the client's model |
| --- | --- |
| `krell` | Krell's persona and honesty rules for an MCP client, her opening line and the standard/advanced question, then how she talks in each mode |
| `krell_tour` | Krell's curated tour (how algo trading works, agentic AI in crypto markets, how Krellbot keeps agents in bounds) as text |

Clients that support MCP prompts offer them as slash commands, for example
`/krell` (Claude Code lists it as `/mcp__krellbot__krell`).

## Tools

Effects: `read` changes nothing; `research` writes drafts or runs
backtests in-process (no jobs, no orders); `change` acts on **paper**
strategies only. Cost `heavy` work can take a while.

| Tool | Effect | Labels | Cost | What it does |
| --- | --- | --- | --- | --- |
| `read` | read | none | light | Inventory: strategy drafts, installed packs, armed strategies and the operator policy |
| `positions` | read | `paper` | light | Paper free cash and holdings per armed paper market; live markets are never shown |
| `journal_recent` | read | none | light | Recent paper journal records (at most 50), secret fields removed |
| `alerts` | read | none | light | Current alerts: kill switch, reconcile, stale feeds and refusals |
| `unresolved_orders` | read | none | light | Orders still awaiting reconcile, with their age and last lookup result (read only) |
| `market_snapshot` | read | none | light | Last cached candles for a market (at most 200) and paper feed freshness; no network call |
| `catalogue` | read | `not_advice` | light | Installed packs and the cached catalogue with each pack's disclosures |
| `explain_bar` | read | `simulated` | light | Why one bar decided what it did, for a stored backtest job or the armed paper strategy |
| `tour_step` | read | none | light | One step of Krell's curated tour, or the list of steps |
| `draft` | research | none | light | Save a strategy pack as a draft revision, or edit one (`pack`, optional `parent_revision_id`); a draft never trades |
| `validate` | research | none | light | Lint a draft revision (`revision_id`); a clean pack becomes runnable on paper |
| `backtest` | research | `simulated` | heavy | Offline backtest of a pack or draft revision against a local `dataset_csv`; returns the receipt and run identity |
| `compare` | research | `simulated` | heavy | Backtest 2 to 4 draft revisions on one local dataset, side by side |
| `paper_arm` | change | `paper` | heavy | Arm a validated draft or pack on paper within the agent limits (`mode` must be `paper`) |
| `pause_entries` | change | `paper` | light | Pause new entries on an armed paper strategy; off for MCP agents unless the operator allows it |
| `resume_entries` | change | `paper` | light | Resume entries on a paused paper strategy (also lifts a close-by-operator entry hold); off for MCP agents unless the operator allows it |
| `disarm` | change | `paper` | light | Disarm a paper strategy without selling its holdings; off for MCP agents unless the operator allows it |

A change tool aimed at a live (or non-paper) strategy refuses with
`agent_live_change_refused` before anything changes. The full parameter
guide is in [agent-cursor.md](agent-cursor.md).

Python entry point (tests and scripts): `krellbot.mcp.McpTools(home).call(tool, **params)`
serves the five original verbs through the same core.

## Explicit non-goals

- **Never live-arm.** `mode=live` or any live-named tool returns
  `mcp_live_arm_refused`.
- **Never read or transmit exchange keys.** Key/secret/credential tool
  names return `mcp_key_read_refused` without touching the OS keyring.
- **Never closes positions or changes live strategies.** There is no
  close, sell or order tool (`close_position` returns `mcp_unknown_tool`),
  and the change tools refuse a live strategy.
- No MCP key write, no hosted keys, no HTTP or remote MCP transport, no CLI
  agent UX as the product surface (the web workstation remains the user
  surface; `krellbot mcp` is plumbing a client launches).

Operator policy lives at `$KRELLBOT_HOME/user-policy.json`. Missing
file defaults: allow paper-arm, allow live at the policy layer (still
blocked by `PAPER_ONLY_RELEASE` / MCP), deny key export. A written
policy can deny paper-arm, live, or key export. MCP `paper_arm` and
`policy_check` honor it; `read` includes the policy snapshot. Hard MCP
refuses for live and key tool names still apply regardless — policy
can only further restrict.

Schema "2" adds the agent limits every agent obeys: allowed venues and
pairs, a maximum paper balance (10,000) and risk cap (2%), a required
daily-loss limit, at most 3 agent arms at once, and per-actor flags for
pause/resume and disarm (off for external MCP agents by default). Over a
limit, a call is refused with an `agent_limit_*` or
`agent_change_not_allowed` code and journaled.

## Cursor client

Install, loopback auth, allowed verbs, and non-goals for the Cursor
agent client: [agent-cursor.md](agent-cursor.md). Project skill path:
`.cursor/skills/krellbot-mcp/SKILL.md`.
