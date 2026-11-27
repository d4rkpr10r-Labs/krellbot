/**
 * Settings → Agents & limits (M38.10): the operator's hard limits on every
 * agent (Krell and any MCP client), and copy-ready MCP client setup.
 *
 * The editor is the web editor for `user-policy.json` (absorbed from M31.5).
 * It sends back every field exactly as loaded apart from the user's edits,
 * and never changes live trading or key export: this release is paper-only.
 * When the engine reads the file as invalid, the form shows the engine's
 * message and stays locked until the user resets it. Every save and reset
 * names the file it was read from, so the engine refuses it if the file has
 * changed since: an old page can never write back over a newer policy.
 */
import { useEffect, useId, useState, type FormEvent, type JSX } from "react";
import { McpSetupPanel } from "./McpSetupPanel";
import {
  CODE_BAD_RESPONSE,
  CODE_POLICY_CHANGED,
  loadUserPolicy,
  resetUserPolicy,
  saveUserPolicy,
  type PolicyDigest,
  type PolicyInvalid,
  type PolicyRefusal,
  type UserPolicy,
} from "./userPolicyClient";

type Allowlist = { mode: "all" | "only"; text: string };

type Draft = {
  allow_paper_arm: boolean;
  venues: Allowlist;
  pairs: Allowlist;
  max_paper_balance: string;
  max_risk_cap_pct: string;
  require_daily_loss_limit: boolean;
  max_agent_arms: string;
  krell_allow_pause_resume: boolean;
  krell_allow_disarm: boolean;
  mcp_allow_pause_resume: boolean;
  mcp_allow_disarm: boolean;
  token_budget_per_conversation: string;
  token_budget_per_month: string;
};

type FlagField =
  | "allow_paper_arm"
  | "require_daily_loss_limit"
  | "krell_allow_pause_resume"
  | "krell_allow_disarm"
  | "mcp_allow_pause_resume"
  | "mcp_allow_disarm";

type TextField =
  | "max_paper_balance"
  | "max_risk_cap_pct"
  | "max_agent_arms"
  | "token_budget_per_conversation"
  | "token_budget_per_month";

type Loaded =
  | { status: "loading" }
  | { status: "failed"; code: string; message: string }
  | {
      status: "ready";
      policy: UserPolicy;
      invalid: PolicyInvalid | null;
      digest: PolicyDigest;
    };

/** What the form is waiting on, so each button names its own request. */
type Busy = "saving" | "resetting" | null;

/** A value the engine would refuse, found before sending it. */
type FieldError = { field: TextField; message: string };

const CHANGED_MESSAGE =
  "The policy changed since this page loaded. Reload to see the current limits.";

// Shown when the policy cannot be read: there is no Reset button then.
const UNREADABLE_ADVICE =
  "Fix or remove user-policy.json in your Krellbot folder, then reload this page.";

// The engine's own formats (application/user_policy.py): plain decimals with
// no sign or exponent, and whole numbers of 0 or more.
const PLAIN_DECIMAL = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
const COUNT = /^(?:0|[1-9][0-9]*)$/;

const COUNT_FIELDS: ReadonlyArray<[TextField, string]> = [
  ["max_agent_arms", "Agent arms running at once"],
  ["token_budget_per_conversation", "Tokens per conversation"],
  ["token_budget_per_month", "Tokens per month"],
];

function allowlistOf(names: string[] | null): Allowlist {
  return names === null
    ? { mode: "all", text: "" }
    : { mode: "only", text: names.join(", ") };
}

function namesOf(list: Allowlist): string[] | null {
  if (list.mode === "all") return null;
  return list.text
    .split(",")
    .map((name) => name.trim())
    .filter((name) => name.length > 0);
}

function draftOf(policy: UserPolicy): Draft {
  const a = policy.agents;
  return {
    allow_paper_arm: policy.allow_paper_arm,
    venues: allowlistOf(a.allowed_venues),
    pairs: allowlistOf(a.allowed_pairs),
    max_paper_balance: a.max_paper_balance,
    max_risk_cap_pct: a.max_risk_cap_pct,
    require_daily_loss_limit: a.require_daily_loss_limit,
    max_agent_arms: String(a.max_agent_arms),
    krell_allow_pause_resume: a.krell_allow_pause_resume,
    krell_allow_disarm: a.krell_allow_disarm,
    mcp_allow_pause_resume: a.mcp_allow_pause_resume,
    mcp_allow_disarm: a.mcp_allow_disarm,
    token_budget_per_conversation: String(a.token_budget_per_conversation),
    token_budget_per_month: String(a.token_budget_per_month),
  };
}

/** `value` is a plain decimal; compared as text so no precision is lost. */
function atMostHundred(value: string): boolean {
  const [whole, fraction = ""] = value.split(".");
  const n = Number(whole);
  return n < 100 || (n === 100 && /^0*$/.test(fraction));
}

type Built =
  | { ok: true; policy: UserPolicy }
  | ({ ok: false } & FieldError);

/** The policy to send, or the first field the engine would refuse. */
function build(draft: Draft, loaded: UserPolicy): Built {
  const balance = draft.max_paper_balance.trim();
  if (!PLAIN_DECIMAL.test(balance)) {
    return {
      ok: false,
      field: "max_paper_balance",
      message:
        "Largest paper balance must be a plain number, such as 10000 or 2500.50.",
    };
  }
  const risk = draft.max_risk_cap_pct.trim();
  if (!PLAIN_DECIMAL.test(risk) || !atMostHundred(risk)) {
    return {
      ok: false,
      field: "max_risk_cap_pct",
      message:
        "Largest risk cap must be a plain number from 0 to 100, such as 2 or 1.5.",
    };
  }
  const counts: Partial<Record<TextField, number>> = {};
  for (const [field, label] of COUNT_FIELDS) {
    const raw = draft[field].trim();
    if (!COUNT.test(raw) || !Number.isSafeInteger(Number(raw))) {
      return {
        ok: false,
        field,
        message: `${label} must be a whole number of 0 or more.`,
      };
    }
    counts[field] = Number(raw);
  }
  return {
    ok: true,
    policy: {
      schema_version: "2",
      allow_paper_arm: draft.allow_paper_arm,
      // Not editable here: sent back exactly as the engine reported them.
      allow_live: loaded.allow_live,
      allow_key_export: loaded.allow_key_export,
      agents: {
        allowed_venues: namesOf(draft.venues),
        allowed_pairs: namesOf(draft.pairs),
        max_paper_balance: balance,
        max_risk_cap_pct: risk,
        require_daily_loss_limit: draft.require_daily_loss_limit,
        max_agent_arms: counts.max_agent_arms ?? 0,
        krell_allow_pause_resume: draft.krell_allow_pause_resume,
        krell_allow_disarm: draft.krell_allow_disarm,
        mcp_allow_pause_resume: draft.mcp_allow_pause_resume,
        mcp_allow_disarm: draft.mcp_allow_disarm,
        token_budget_per_conversation:
          counts.token_budget_per_conversation ?? 0,
        token_budget_per_month: counts.token_budget_per_month ?? 0,
      },
    },
  };
}

export type AgentsSectionProps = {
  /** False until the local session is ready; nothing loads before then. */
  enabled?: boolean;
};

export function AgentsSection({
  enabled = true,
}: AgentsSectionProps): JSX.Element {
  return (
    <section
      className="kbot-settings__panel"
      aria-labelledby="kbot-settings-agents-heading"
      data-testid="settings-section-agents"
    >
      <header className="kbot-settings__panel-head">
        <h2
          className="kbot-settings__panel-title"
          id="kbot-settings-agents-heading"
        >
          Agents &amp; limits
        </h2>
        <p className="kbot-settings__panel-note">
          Hard limits for every agent: Krell in this app and any MCP client you
          connect. Agents can only be more restricted than you. The limits are
          saved in user-policy.json on this computer.
        </p>
      </header>
      <p className="kbot-settings__card-copy" data-testid="agents-paper-only">
        This release is paper-only. No agent can arm a live strategy, read
        exchange keys, place orders or close positions, and nothing on this
        screen changes that.
      </p>
      <LimitsEditor enabled={enabled} />
      <McpSetupPanel enabled={enabled} />
    </section>
  );
}

function LimitsEditor({ enabled }: { enabled: boolean }): JSX.Element {
  const ids = useId();
  const [loaded, setLoaded] = useState<Loaded>({ status: "loading" });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  // An engine refusal of the last save or reset.
  const [error, setError] = useState<string | null>(null);
  // A value refused here before sending; editing any field clears it.
  const [fieldError, setFieldError] = useState<FieldError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void loadUserPolicy().then((result) => {
      if (cancelled) return;
      if (!result.ok) {
        setLoaded({
          status: "failed",
          code: result.code,
          message: result.message,
        });
        return;
      }
      setLoaded({
        status: "ready",
        policy: result.policy,
        invalid: result.invalid,
        digest: result.digest,
      });
      setDraft(draftOf(result.policy));
    });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  if (loaded.status === "failed") {
    const advice =
      loaded.code === CODE_BAD_RESPONSE ? ` ${UNREADABLE_ADVICE}` : "";
    return (
      <p className="kbot-settings__error" role="alert">
        {`The agent limits could not be loaded: ${loaded.message}${advice}`}
      </p>
    );
  }
  if (loaded.status === "loading" || draft === null) {
    return (
      <p className="kbot-settings__status-line" role="status">
        Loading the agent limits…
      </p>
    );
  }

  const { policy, invalid, digest } = loaded;
  const off = invalid !== null || busy !== null;
  const errorId = `${ids}-field-error`;

  function adopt(
    next: UserPolicy,
    nextDigest: PolicyDigest,
    message: string,
  ): void {
    setLoaded({
      status: "ready",
      policy: next,
      invalid: null,
      digest: nextDigest,
    });
    setDraft(draftOf(next));
    setNotice(message);
  }

  /** The engine's refusal; a changed file always gets the reload advice. */
  function refused(result: PolicyRefusal, prefix: string): string {
    return result.code === CODE_POLICY_CHANGED
      ? CHANGED_MESSAGE
      : `${prefix}: ${result.message}`;
  }

  async function save(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (off || draft === null) return;
    setNotice(null);
    setError(null);
    const built = build(draft, policy);
    if (!built.ok) {
      setFieldError({ field: built.field, message: built.message });
      return;
    }
    setFieldError(null);
    setBusy("saving");
    try {
      const result = await saveUserPolicy(built.policy, digest);
      if (result.ok) {
        adopt(
          result.policy,
          result.digest,
          result.changed ? "Saved." : "No changes to save.",
        );
      } else {
        setError(refused(result, "Not saved"));
      }
    } finally {
      setBusy(null);
    }
  }

  async function reset(): Promise<void> {
    if (busy !== null) return;
    setBusy("resetting");
    setError(null);
    setFieldError(null);
    setNotice(null);
    try {
      const result = await resetUserPolicy(policy, digest);
      if (result.ok) {
        adopt(result.policy, result.digest, "Reset to the default limits.");
      } else {
        setError(refused(result, "Not reset"));
      }
    } finally {
      setBusy(null);
    }
  }

  /** An edit makes the last notice and any value refused here out of date. */
  function edited(): void {
    setNotice(null);
    setFieldError(null);
  }
  const setFlag = (field: FlagField, value: boolean): void => {
    edited();
    setDraft((d) => (d ? { ...d, [field]: value } : d));
  };
  const setText = (field: TextField, value: string): void => {
    edited();
    setDraft((d) => (d ? { ...d, [field]: value } : d));
  };
  const setList = (
    field: "venues" | "pairs",
    next: Partial<Allowlist>,
  ): void => {
    edited();
    setDraft((d) => (d ? { ...d, [field]: { ...d[field], ...next } } : d));
  };

  const toggle = (
    field: FlagField,
    title: string,
    desc?: string,
  ): JSX.Element => (
    <Toggle
      id={`${ids}-${field}`}
      checked={draft[field]}
      disabled={off}
      title={title}
      desc={desc}
      onChange={(value) => setFlag(field, value)}
    />
  );
  const text = (field: TextField, label: string, hint: string): JSX.Element => (
    <TextInput
      id={`${ids}-${field}`}
      label={label}
      hint={hint}
      value={draft[field]}
      disabled={off}
      error={
        fieldError?.field === field
          ? { id: errorId, message: fieldError.message }
          : undefined
      }
      onChange={(value) => setText(field, value)}
    />
  );
  const allowlist = (
    field: "venues" | "pairs",
    copy: { group: string; all: string; only: string; input: string; hint: string },
  ): JSX.Element => {
    const list = draft[field];
    const base = `${ids}-${field}`;
    return (
      <>
        <p className="kbot-settings__field-label" id={`${base}-group`}>
          {copy.group}
        </p>
        <div role="radiogroup" aria-labelledby={`${base}-group`}>
          {(["all", "only"] as const).map((mode) => (
            <label
              key={mode}
              className="kbot-settings__toggle-row"
              htmlFor={`${base}-${mode}`}
            >
              <input
                id={`${base}-${mode}`}
                type="radio"
                name={`${base}-mode`}
                checked={list.mode === mode}
                disabled={off}
                onChange={() => setList(field, { mode })}
              />
              <span className="kbot-settings__toggle-copy">
                <span className="kbot-settings__toggle-title">
                  {mode === "all" ? copy.all : copy.only}
                </span>
              </span>
            </label>
          ))}
        </div>
        <TextInput
          id={`${base}-names`}
          label={copy.input}
          hint={copy.hint}
          value={list.text}
          disabled={off || list.mode === "all"}
          onChange={(value) => setList(field, { text: value })}
        />
      </>
    );
  };

  return (
    <form
      className="kbot-settings__form"
      onSubmit={(event) => void save(event)}
      data-testid="agents-limits-form"
      aria-label="Agent limits"
      noValidate
    >
      {invalid ? (
        <div
          className="kbot-settings__caution-box"
          data-testid="agents-policy-invalid"
        >
          <p className="kbot-settings__card-copy" role="alert">
            {invalid.message}
          </p>
          <p className="kbot-settings__card-copy">
            Until the file is fixed, agents are locked out and nothing can be
            armed on paper. The values below are what Krellbot enforces now.
            Resetting writes the default limits and allows paper arming again;
            live trading and key export stay off.
          </p>
          <button
            type="button"
            className="kbot-settings__btn"
            disabled={busy !== null}
            onClick={() => void reset()}
          >
            {busy === "resetting" ? "Resetting…" : "Reset to default limits"}
          </button>
        </div>
      ) : null}

      <div className="kbot-settings__card">
        <h3 className="kbot-settings__subhead">Paper arming</h3>
        {toggle(
          "allow_paper_arm",
          "Allow paper arming",
          "When off, nothing can be armed on paper, by you or by an agent.",
        )}
      </div>

      <div className="kbot-settings__card">
        <h3 className="kbot-settings__subhead">What agents may arm</h3>
        {allowlist("venues", {
          group: "Venues agents may arm on",
          all: "Every supported venue",
          only: "Only these venues",
          input: "Allowed venues, separated by commas",
          hint: "For example: kraken, coinbase. Leave it empty to allow none.",
        })}
        {allowlist("pairs", {
          group: "Pairs agents may arm",
          all: "Every supported pair",
          only: "Only these pairs",
          input: "Allowed pairs, separated by commas",
          hint: "For example: SUIUSD, BTCUSD. Leave it empty to allow none.",
        })}
        {text(
          "max_paper_balance",
          "Largest paper balance per agent arm",
          "The most starting paper cash an agent may give one strategy.",
        )}
        {text(
          "max_risk_cap_pct",
          "Largest risk cap (% of account)",
          "The highest account risk an agent-armed pack may set, from 0 to 100.",
        )}
        {toggle(
          "require_daily_loss_limit",
          "Agent arms must set a daily loss limit",
        )}
        {text(
          "max_agent_arms",
          "Agent arms running at once",
          "Strategies you arm yourself never count.",
        )}
      </div>

      <div className="kbot-settings__card">
        <h3 className="kbot-settings__subhead">
          Changes agents may make to paper strategies
        </h3>
        {toggle(
          "krell_allow_pause_resume",
          "Krell may pause and resume entries",
          "Krell asks you to confirm each change.",
        )}
        {toggle(
          "krell_allow_disarm",
          "Krell may disarm a paper strategy",
          "Krell asks you to confirm each change. Holdings are not sold.",
        )}
        {toggle(
          "mcp_allow_pause_resume",
          "MCP agents may pause and resume entries",
          "Off by default. An MCP agent acts with only your client's approval; Krellbot asks no confirm of its own.",
        )}
        {toggle(
          "mcp_allow_disarm",
          "MCP agents may disarm a paper strategy",
          "Off by default. An MCP agent acts with only your client's approval. Holdings are not sold.",
        )}
      </div>

      <div className="kbot-settings__card">
        <h3 className="kbot-settings__subhead">Krell&apos;s model budget</h3>
        <p className="kbot-settings__card-copy">
          Limits on the tokens Krell may use on your AI model.
        </p>
        {text(
          "token_budget_per_conversation",
          "Tokens per conversation",
          "A whole number of tokens.",
        )}
        {text(
          "token_budget_per_month",
          "Tokens per month",
          "A whole number of tokens.",
        )}
      </div>

      {error ? (
        <p className="kbot-settings__error" role="alert">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p className="kbot-settings__status-line" role="status">
          {notice}
        </p>
      ) : null}
      <div className="kbot-settings__inline-actions">
        <button type="submit" className="kbot-settings__btn" disabled={off}>
          {busy === "saving" ? "Saving…" : "Save limits"}
        </button>
      </div>
    </form>
  );
}

function Toggle(props: {
  id: string;
  checked: boolean;
  disabled: boolean;
  title: string;
  desc?: string;
  onChange(value: boolean): void;
}): JSX.Element {
  return (
    <label className="kbot-settings__toggle-row" htmlFor={props.id}>
      <input
        id={props.id}
        type="checkbox"
        checked={props.checked}
        disabled={props.disabled}
        onChange={(event) => props.onChange(event.target.checked)}
      />
      <span className="kbot-settings__toggle-copy">
        <span className="kbot-settings__toggle-title">{props.title}</span>
        {props.desc ? (
          <span className="kbot-settings__toggle-desc">{props.desc}</span>
        ) : null}
      </span>
    </label>
  );
}

function TextInput(props: {
  id: string;
  label: string;
  hint: string;
  value: string;
  disabled: boolean;
  /** A value refused before sending: marks the input and describes it. */
  error?: { id: string; message: string };
  onChange(value: string): void;
}): JSX.Element {
  const hintId = `${props.id}-hint`;
  return (
    <div className="kbot-settings__field">
      <label className="kbot-settings__field-label" htmlFor={props.id}>
        {props.label}
      </label>
      <input
        id={props.id}
        className="kbot-settings__input"
        type="text"
        autoComplete="off"
        spellCheck={false}
        aria-invalid={props.error ? true : undefined}
        aria-describedby={
          props.error ? `${hintId} ${props.error.id}` : hintId
        }
        value={props.value}
        disabled={props.disabled}
        onChange={(event) => props.onChange(event.target.value)}
      />
      <p className="kbot-settings__status-line" id={hintId}>
        {props.hint}
      </p>
      {props.error ? (
        <p className="kbot-settings__error" role="alert" id={props.error.id}>
          {props.error.message}
        </p>
      ) : null}
    </div>
  );
}
