# Krell and the agent control layer: design

Status: design for owner review (2026-11-27). Not yet a milestone. Once
approved, it is broken into a milestone file through an owner-reviewed PR,
following `AGENTS.md` (§9 covers where it sits in the queue).

Decisions marked **(owner)** were made by the owner in conversation.
Everything else is the designer's proposal and is marked **(call)** where it
is a judgement a builder might otherwise make differently.

## 1. Intent

Krellbot should feel like part of the AI world. Two things deliver that:

1. **Krellbot is the control layer for agents.** Any AI agent can connect
   to Krellbot and use it for what agents cannot be trusted to do alone:
   deterministic execution, hard limits, and an audit trail that shows why
   everything happened. This extends what the product already promises
   ("see why the strategy entered, exited or did nothing", journey J4).
2. **Krell, an in-app agent.** When a user connects their own model, the
   workstation gains a chat-driven mode in which Krell, the Krellbot
   mascot, informs the user and helps them make strategy decisions from
   real data. **(owner)**

Krell's opening line **(owner)**: "Hey there. I'm Krell! Welcome to
Krellbot. Let me know what you're interested in, or if you want I can take
you on a guided tour of how algo trading works and all the new advances
that have come with agentic AI and crypto markets."

### Success looks like

- A brand-new user picks "Meet Krell (connect your AI)" on the first-run
  screen (or connects a model later in Settings), says they are a standard
  user, answers questions about budget, goals and risk tolerance,
  is shown suitable starter strategies with simulated evidence, and taps
  Confirm on a paper run Krell prepared.
- An advanced user asks Krell to compare three strategies on a pair; Krell
  drafts, validates and backtests them (asking first if the work is heavy)
  and explains the results bar by bar.
- A Claude Desktop or Cursor user adds Krellbot's MCP server, types
  `/krell`, and gets the same Krell, tools, limits and audit trail on their
  own subscription.
- Every agent action, by Krell or an external agent, appears in an Agent
  activity view next to the bot's own decisions, with who did it, what, and
  the result or refusal.

## 2. Goals, non-goals, anti-goals

**Goals**

- A real, local MCP server any MCP client can launch.
- Krell: chat-driven, persona-led, inside the workstation, using the user's
  own model.
- Connect a model by API key for every major provider, by sign-in (OAuth)
  where the provider permits it for third-party apps, or by a custom
  OpenAI-compatible or Anthropic-compatible endpoint. **(owner)**
- Hard limits the operator sets, which bind every agent equally.
- A complete, readable audit trail of agent activity.

**Non-goals**

- Remote MCP (ChatGPT connectors, claude.ai remote connectors). These need
  Krellbot exposed to the public internet, which breaks the loopback-only
  trust model (`docs/threat-model.md`). Local clients only. **(call)**
- Claude Pro/Max subscription credentials inside Krellbot (setup tokens or
  OAuth). Anthropic's terms forbid third-party apps routing requests through
  subscription credentials
  (<https://code.claude.com/docs/en/legal-and-compliance>). Claude users
  connect an API key, or use Krell through the MCP server in Claude Desktop
  or Claude Code. **(owner)**
- Agents placing individual orders. **(owner)** This includes closing a
  position: `close_position` sells at market, so it stays a human-only
  control (M28.32) and is not in the agent catalogue. **(call)**
- Agents changing anything live. Every agent change tool acts on paper
  strategies only; live pause, disarm and close stay human controls.
  **(call)**
- A new Learn view. The tour lives in Krell's conversation (`M24.md`:
  "Do not invent a Learn view").

**Anti-goals (failure even if it works)**

- **Autopilot.** Krell must not act as an autopilot. The human authorizes
  every use of money: the human chooses and arms strategies. **(owner)**
- **Generative AI in the execution path.** `docs/product-direction.md`
  holds: "generative AI does not choose orders or override risk in the
  execution path." Agents build, test and propose; Krellbot's deterministic
  rules make every trade. **(owner: option A)**
- **Personalized advice.** Krell may not say "this is right for you, put
  your money here". She narrows options, explains trade-offs, and sets
  conservative paper defaults, framed as education and simulation, not
  financial advice. This matches the existing promise that packs are "not
  personalized advice" (`docs/creator-packs.md`, `docs/paid-disclosures.md`).
  **(owner)**
- **Backtest or paper results presented as market performance.** Always
  labelled simulated (`AGENTS.md`).
- **Silent egress.** Nothing leaves the machine for an AI provider without
  the user having been told what is sent and to whom.

## 3. Hard constraints (from the project)

- `PAPER_ONLY_RELEASE` stays `True`. Only the owner changes it.
- No agent (Krell or MCP) can arm live, grant live authorization, or read,
  export or transmit exchange keys. Existing MCP hard refusals
  (`mcp_live_arm_refused`, `mcp_key_read_refused`) stay.
- Preserve the loopback API's authentication, session, CSRF, origin and
  host checks, and M28.18's read-only Tailscale gate. Agent features must
  not open any non-loopback listener.
- The web workstation is the only user surface. `krellbot mcp` is a launcher
  entry for MCP clients (plumbing), not a user feature surface.
- Telemetry stays opt-in. No agent data goes to `krellbot.dev`.
- UI follows `docs/design-system.md` (contract `KRELL-SIGNAL-20261010`):
  tokens, Manrope / IBM Plex Mono, density rules, no new top-level patterns
  without fitting the shell rules.
- Brand art may not be regenerated, repainted or substituted
  (`frontend/src/assets/character/PROVENANCE.md`). Krell's avatar is a
  downscaled face crop of the existing `krell_portrait.png`, recorded in
  that provenance file with its hash like the existing `-768` downscales.
- Tests use fake transports: fake AI providers, fake MCP clients, fake
  keyrings, isolated `KRELLBOT_HOME`. No real API calls in CI.
- Risk paths (`AGENTS.md`) apply; anything touching `secrets.py`,
  `keys/`, `run/`, `application/live_gate.py` is labelled `risk-path`.

## 4. Approaches considered

### 4.1 What agents may decide

- **A. Agents build, Krellbot trades (chosen, owner).** Agents draft,
  validate, backtest, compare and propose strategies; humans arm them;
  Krellbot's rules make every trade. Keeps the product-direction principle
  and makes "see why the bot acted" literally true.
- B. Agents trade, Krellbot enforces. Agents submit their own orders under
  hard limits. Rejected: reverses the principle and makes Krellbot an order
  gateway.

### 4.2 How Krellbot talks to AI providers

- **A. Krellbot's own thin provider adapters (chosen, owner).** Every
  provider on the list speaks either the OpenAI-compatible format or
  Anthropic's Messages format. Two small adapters plus the sign-in flows
  cover all of them, with no large new dependency in a frozen,
  security-sensitive release and exact control over egress.
- B. A multi-provider library (e.g. LiteLLM). Rejected: a large,
  fast-moving dependency inside the process that holds exchange keys, and
  harder-to-audit network behaviour.
- Browser-side calls. Rejected: API keys in browser storage, outside the
  keychain and outside the audit journal.

## 5. Architecture

```
            external MCP clients                  Krellbot web workstation
   (Claude Desktop, Claude Code, Cursor, VS Code)   Krell tab + side panel
                    │ stdio                                │ loopback API (SSE)
             ┌──────▼────────┐                    ┌────────▼─────────┐
             │ MCP server    │                    │ Krell agent loop │──► provider adapter ──► user's model
             │ krellbot mcp  │                    │ (engine)         │    (OpenAI-compat / Anthropic)
             └──────┬────────┘                    └────────┬─────────┘
                    └──────────────┬───────────────────────┘
                          ┌────────▼────────┐
                          │ agent tool core │  policy limits · confirm gate · audit journal
                          └────────┬────────┘
                    existing application facades (packs, research, paper, operations)
```

### 5.1 Agent tool core (one layer, two front doors) **(call)**

`krellbot.mcp` grows into an agent tool core: a registry of typed tools,
each with a JSON schema, a cost class and an effect class. Both front doors
call the same registry, so limits and audit are identical by construction.

Each call carries an **actor**: `krell` or `mcp:<client name>` (from the MCP
`initialize` client info). The core:

1. Checks hard refusals (live, keys), then the operator policy (§6).
2. For effect class `change`, requires a human confirmation token (§5.4)
   when the actor is `krell`. External MCP actors need no Krellbot-side
   approval: their client already asks the human before each tool call, and
   the policy limits in §6 bind them. **(owner)** External agents may
   paper-arm within those limits; pause, resume and disarm are off for them
   unless the operator turns them on.
3. Runs the tool through the existing application facade.
4. Appends an audit record (§7).

The two front doors run in different processes: Krell's loop in the engine,
the MCP server in the process its client launches. Both read the same
`user-policy.json` and append to the same journal in `$KRELLBOT_HOME`, as
`McpTools` does today. Count-based limits (`max_agent_arms`) are checked
against the on-disk config at call time, not a per-process counter.
**(call)**

**Tool catalogue (initial)**

| Tool | Effect | Cost | Notes |
| --- | --- | --- | --- |
| `read` (inventory) | read | light | existing |
| `explain_bar` | read | light | bar-trace decision for a run and bar (J4) |
| `positions`, `journal_recent`, `alerts`, `unresolved_orders` | read | light | read-only views of existing data |
| `market_snapshot` | read | light | recent candles and feed freshness for a pair |
| `catalogue` | read | light | packs available, with their disclosures |
| `draft`, `validate` | research | light | existing |
| `backtest` | research | heavy when long | existing; cost estimated from bars × packs |
| `compare` | research | heavy | several backtests side by side |
| `paper_arm` | change | heavy | existing |
| `pause_entries`, `resume_entries`, `disarm` | change | light | new to agents; paper strategies only |

Nothing in the catalogue arms live, grants live, touches keys, places or
closes an order, or changes a live strategy. A change tool aimed at a live
strategy refuses with a typed, journaled code (`agent_live_change_refused`)
before any state changes. **(call)** Where a change tool's facade reaches
into `run/`, that task is labelled `risk-path`.

Backtests and comparisons requested over MCP run in-process through
`ResearchService`, not as jobs, and return the M29.1 run id and receipt
digest so an agent's claim about a run can be checked in the workstation.
The MCP server creates no jobs and runs no second JobManager over the home
(roadmap decision 16). Krell, inside the engine, submits heavy work through
the existing JobManager.

### 5.2 MCP server **(call)**

- `krellbot mcp` starts an MCP server over stdio, bound to the user's
  `KRELLBOT_HOME`, using the official MCP Python SDK (pinned in `uv.lock`).
- It exposes the tool core's tools, plus MCP **prompts**: `krell` (persona,
  honesty rules and the standard/advanced opening) and `krell_tour` (the
  curated tour), so `/krell` in an MCP client starts the same Krell.
- Through MCP, Krellbot controls only what its tools return: every result
  carries its own labels (simulated, not advice, refused and why). It cannot
  police what the client's model writes around those results. `docs/mcp.md`
  and the setup screen say so plainly. **(call)**
- Settings gains an "Agents (MCP)" section with copy-ready setup snippets
  for Claude Desktop, Claude Code, Cursor and VS Code, and the policy limits
  (§6).
- No HTTP/remote transport.

### 5.3 Krell agent loop **(call)**

- Runs in the engine process. The browser sends user messages over the
  loopback API (behind the same session/CSRF/origin checks) and receives
  streamed events (server-sent events): text, tool-call start/finish,
  confirm requests, highlight requests, token usage.
- Krell sees Krellbot data only through tools. No bulk dumps of the journal
  or keys into the prompt, so what reaches the provider is exactly the tool
  results shown in the conversation.
- The persona/system prompt and tour content are versioned files in the
  repo, not generated at runtime.
- One conversation thread per home in V1, shared by the Krell tab and the
  side panel. Stored locally in `$KRELLBOT_HOME` (SQLite); the user can
  clear it. Never sent to `krellbot.dev`.

### 5.4 Asking before heavy or state-changing work **(owner + call)**

- **(owner)** Krell is as helpful as possible but asks before anything that
  burns a serious amount of tokens or time, such as a paper run.
- **(call)** Rule: tools with cost `heavy` or effect `change` require a
  confirm card. The card states the exact action and parameters, the limits
  that will apply, an estimate of time and tokens, and Confirm / Not now.
  Confirm issues a single-use token bound to those exact parameters; the
  core refuses a `change` from `krell` without a matching token. Light
  `read`/`research` calls run without asking.
- Confirming a paper-arm card is the human choosing and arming it. **(owner)**
- Token budgets: per conversation and per month, set in Settings, with a
  hard stop and a plain message when reached. **(call)**

### 5.5 Provider layer **(owner: approach A; details are calls)**

**Adapters:** `openai_compatible` (Chat Completions with tools; Responses API
where a sign-in requires it) and `anthropic_messages` (Messages with tools).
Gemini uses its OpenAI-compatible endpoint by default. HTTP over the
standard library, as the existing venue and licence clients do. Streaming
supported.

**Connections**

| Provider | API key | Sign-in (OAuth) |
| --- | --- | --- |
| Anthropic (Claude) | yes | no: Anthropic forbids subscription credentials in third-party apps |
| OpenAI | yes | "Sign in with ChatGPT" (plan usage, Plus/Pro); reported self-serve for open-source apps, to verify against OpenAI's docs before building |
| Google Gemini | yes | later: needs a registered, verified Google Cloud OAuth client |
| xAI, Mistral, DeepSeek | yes | no |
| OpenRouter | yes | "Sign in with OpenRouter" (PKCE, no client registration) |
| Custom | optional key | n/a: any OpenAI-compatible or Anthropic-compatible base URL (Ollama, LM Studio, vLLM, gateways) |

- OAuth uses the native-app pattern: browser sign-in with a loopback
  redirect on `127.0.0.1` and PKCE, handled by the engine without weakening
  the API's host/origin checks.
- API keys and OAuth tokens are stored in the OS keychain, in a credential
  namespace separate from exchange keys, never in files, logs, journal or
  telemetry.
- **Capability probe** on connect: list models where the provider supports
  it, and run one tiny tool-call round trip. A model that cannot call tools
  is marked "chat only"; Krell then explains rather than acts.
- **Egress disclosure:** Settings and Krell's first message in a
  conversation state that the conversation and the data Krell reads go to
  the named provider. `docs/telemetry.md` and `docs/threat-model.md` gain an
  "AI providers" section.

### 5.6 Krell's persona and modes **(owner + call)**

- **(owner)** On introduction she asks whether the user is standard or
  advanced. Advanced: she dives straight into the deep end. Standard: "kid
  gloves"; she asks about budget, goals and risk tolerance and shows what
  fits.
- **(owner)** Standard mode stays within "education and simulation, not
  financial advice": she narrows the catalogue, explains trade-offs, and
  proposes conservative paper settings (balance, risk cap, daily-loss limit)
  matching what the user said.
- **(call)** The mode only changes how Krell talks; the interface does not
  change (M24 rejected a Beginner/Power toggle). The user can switch by
  asking. The mode and answers are stored locally with the conversation.
- **(call)** Guided tour: a curated, versioned list of steps (how algo
  trading works, rules vs discretion, stops and risk, backtests vs reality,
  paper first, what agentic AI changes in crypto markets, how Krellbot keeps
  agents in bounds). Krell delivers them in her voice and adapts depth to
  the mode; the facts come from the curated steps. The owner reviews tour
  content before release.
- **(call)** Honesty rules, enforced in three layers:
  1. **The UI renders the facts, not the model.** Simulated labels, the
     "not financial advice" disclosure, and every "this happened" claim
     (armed, paused, refused) come from structured tool results and are
     drawn by the workstation as cards next to Krell's text. The model's
     prose can't remove or forge them.
  2. **The persona prompt** carries the same rules for Krell's own words:
     simulated numbers always labelled, no return promises, no live arming,
     "not financial advice" when discussing what fits a user, never claiming
     an action happened unless a tool result says so.
  3. **Evals.** CI runs scripted fake-model conversations that check the
     cards and refusals. Before each release the owner runs a real-model
     eval suite against the providers Krellbot supports; it needs real API
     keys, so it never runs in CI.

### 5.7 Workstation UI **(owner + call)**

- **(owner)** Krell has her own **Krell tab** and a **side panel** on every
  other screen. It is one conversation in both places. AI-forward.
- **(call)** The Krell tab is the first navigation item once a model is
  connected. With no model connected, the tab shows a connect screen and the
  rest of Krellbot is unchanged.
- **(call)** The side panel opens from an "Ask Krell" header button, docks
  right on desktop (about 340px), becomes a full-screen sheet on phones,
  and offers "Open in Krell tab". In the panel, Krell can highlight the
  element she is discussing on the current screen (highlight events name
  stable test ids).
- **(owner)** Krell's avatar is the mascot's face. **(owner)** It appears only
  in the conversation, once at the start of each run of her messages; the
  tab and the button use her name, never the picture.
- Confirm cards, budget meters, disclosures and the connect flow follow the
  Signal design system. Reference mockup:
  <https://claude.ai/artifact/6nCcLspbr21FpNc531TzsE> (v3).
- Drafts an agent made carry an origin (`krell` or `mcp:<client>`) in draft
  metadata, not pack bytes, so revision ids stay content-addressed. The
  Strategies list badges them "Drafted by agent". The badge informs; it
  does not block a paper arm. **(owner, resolving M34.8)**

### 5.8 First run: meeting Krell **(owner)**

- The M26 first-run wizard's welcome step
  (`frontend/src/features/onboarding/FirstRunWizard.tsx`) gains a second
  choice, "Meet Krell (connect your AI)", beside the existing path. It is a
  secondary button, so the wizard keeps its one big sage primary per step.
  **(call)**
- Choosing it opens the connect flow (§5.5). Once a model passes the
  capability probe, Krell takes over the first run in the Krell tab with
  her opening line and the standard/advanced question, and the wizard
  closes as complete.
- If the connect flow is cancelled or fails, the user returns to the
  wizard's first screen with nothing changed.
- Otherwise the wizard is unchanged. The aim is that its existing tests
  pass without edits; if a pinned test counts the welcome step's controls,
  the task names that test for the owner's approval rather than editing it.

## 6. Hard limits: operator policy v2 **(call)**

Extends `user-policy.json` (`application/user_policy.py`) with a schema
version bump. Missing file keeps safe defaults; malformed fails closed;
agents can only be more restricted than the human.

| Limit | Default |
| --- | --- |
| `allow_paper_arm` (existing) | true |
| `allowed_venues`, `allowed_pairs` for agent-armed strategies | all supported |
| `max_paper_balance` per agent-armed strategy | 10,000 |
| `max_risk_cap_pct` | 2 |
| `require_daily_loss_limit` on agent-armed strategies (M28.31) | true |
| `max_agent_arms` running at once | 3 |
| `allow_agent_pause_resume`, `allow_agent_disarm` (paper only) | Krell: true with confirm; external MCP: false |
| `token_budget_per_conversation`, `token_budget_per_month` | set in Settings |

There is no `allow_agent_close` and no live equivalent of any row: those
actions are outside the agent catalogue altogether (§2).

Edited in Settings → "Agents & limits". This is the first web editor for
`user-policy.json`, absorbed from M31.5 (§9). Every refusal is journaled
with its reason code (extends M22.3).

## 7. Audit trail **(call)**

- Every tool call appends a journal record: time, actor, tool, parameters
  (with long values truncated and anything secret-shaped refused at the
  core), result code or refusal code, the confirm token's identity when one
  was used, and token usage for Krell turns.
- Operations gains an **Agent activity** view: a filterable list of agent
  actions, linked to the strategies and bars they concern, so "why did this
  happen" covers the agent too.
- Conversation text is stored only in the local conversation store, not in
  the journal.

## 8. Security

- **Prompt injection.** Pack descriptions, catalogue text, alerts and
  provider responses are untrusted. Tool results are delimited as data in
  the prompt. An injected instruction can at most ask for a paper change:
  for Krell that change still needs a human confirm card with a token bound
  to its exact parameters; for an external MCP agent it still passes the
  client's own per-call approval and the §6 limits. Live, keys, orders and
  closes are not reachable by any agent at all.
- **No new listeners.** MCP is stdio; the OAuth redirect listener is
  short-lived, loopback-only, and bound to a PKCE state.
- **Keys.** AI provider credentials live in the keychain namespace. No tool
  returns any credential. Exchange keys remain unreachable to agents.
- **Rate and cost.** Budgets stop runaway loops; tool calls per turn are
  capped.

## 9. Roadmap position and delivery order

### 9.1 Where it goes **(owner)**

The new milestone goes next in the queue, ahead of M29, and replaces M34's
agent tasks:

| M34 task | What happens to it |
| --- | --- |
| M34.1–M34.4 (research contract: preflight, discovery, docs, bundle) | stay in M34, unchanged |
| M34.5 agent origin and badge | moves into the new milestone (§5.7), with origin values `krell` and `mcp:<client>` |
| M34.6 stdio MCP server | replaced by §5.2, which also adds the new tools and Krell prompts |
| M34.7 MCP backtest run id and digest | moves into the new milestone (§5.1) |
| M34.8 [owner] agent paper-arm default | resolved: external agents may paper-arm within limits; the badge informs and does not block |
| M34.9 [owner] MCP SDK | resolved: the official MCP Python SDK, pinned in `uv.lock` |
| M34.10 [owner] first BYOK provider | resolved: all providers in §5.5 by API key, plus custom endpoints |
| M34.11 [owner] scopes for non-browser clients | still open; nothing here needs it, because the MCP server works in-process and creates no jobs over the loopback API |
| M34.12 [owner] gate | trimmed to the research-contract checks |

Two pieces from later milestones come first because this milestone needs
them:

- **M29.1 run record** (run id and receipt digest), which M34.7's MCP run
  identity depends on. It stays as M29.1 (already the next agent task) and
  lands before the new milestone's tasks.
- **M31.5's policy editor** (the first web editor for `user-policy.json`).
  The editor moves here as the Agents & limits screen; M31.5 keeps only
  its `excluded_assets` field and enforcement, written to edit that screen.

The file is proposed as `M38.md`, with a status line recording the owner's
reorder ("agents take M38's tasks before M29's"), the same mechanism M34's
status line already allows. The milestone PR edits M29, M31 and M34 to
match, so it needs the owner's review and is not auto-merged (`AGENTS.md`).

### 9.2 Delivery order

0. Prerequisites: M29.1 run record; agent origin on drafts.
1. Agent tool core: registry, actors, cost/effect classes, policy v2, audit.
2. MCP server over stdio, Krell prompts, setup snippets, Agents & limits
   settings.
3. Provider layer: adapters, API keys, custom endpoints, capability probe,
   egress disclosure.
4. Krell agent loop: streaming API, conversation store, confirm tokens,
   budgets.
5. Krell tab and side panel UI, avatar asset, confirm cards, result-rendered
   honesty labels, highlights.
6. Persona, standard/advanced, tour content, honesty evals (scripted fake
   model conversations in CI; the real-model suite as an owner task).
7. First run: "Meet Krell (connect your AI)" on the wizard's first screen.
8. Sign-in: OpenRouter, then Sign in with ChatGPT once eligibility is
   confirmed.
9. Operations Agent activity view.
10. Owner gate: a fresh home chooses "Meet Krell", connects a model, and
    follows the standard-user path to a confirmed paper run; an external MCP
    client completes validate → backtest → paper_arm under limits; an agent
    attempt on a live strategy is refused; everything is visible in Agent
    activity.

## 10. Left to the builder

- Internal shape of the tool registry and adapter modules.
- SSE event schema details; SQLite schema for conversations.
- Token-estimate heuristic for confirm cards (it must not under-promise).
- Model defaults per provider and the model picker's filtering.
- Exact wording of tour steps (owner reviews), confirm cards and errors.
