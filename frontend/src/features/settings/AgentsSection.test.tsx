/**
 * Settings → Agents & limits (M38.10): the operator policy editor and the
 * MCP client setup snippets.
 *
 * Pins: every agent limit plus paper arming round-trips through
 * `PUT /api/v1/user-policy` without loss; an invalid policy file shows the
 * engine's message and locks the form until it is reset; MCP changes stay
 * off unless the user turns them on; the snippets use the command the engine
 * reports, and the Claude Code line is the engine's own quoting.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentsSection } from "./AgentsSection";
import { SettingsView } from "./SettingsView";

vi.mock("../../session", () => ({
  getCsrf: (): string => "test-csrf",
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Json = Record<string, unknown>;

const DEFAULT_AGENTS = {
  allowed_venues: null,
  allowed_pairs: null,
  max_paper_balance: "10000",
  max_risk_cap_pct: "2",
  require_daily_loss_limit: true,
  max_agent_arms: 3,
  krell_allow_pause_resume: true,
  krell_allow_disarm: true,
  mcp_allow_pause_resume: false,
  mcp_allow_disarm: false,
  token_budget_per_conversation: 250000,
  token_budget_per_month: 2500000,
};

const LOCKED_AGENTS = {
  allowed_venues: [],
  allowed_pairs: [],
  max_paper_balance: "0",
  max_risk_cap_pct: "0",
  require_daily_loss_limit: true,
  max_agent_arms: 0,
  krell_allow_pause_resume: false,
  krell_allow_disarm: false,
  mcp_allow_pause_resume: false,
  mcp_allow_disarm: false,
  token_budget_per_conversation: 0,
  token_budget_per_month: 0,
};

function policy(over: Json = {}, agents: Json = {}): Json {
  return {
    schema_version: "2",
    allow_paper_arm: true,
    allow_live: true,
    allow_key_export: false,
    ...over,
    agents: { ...DEFAULT_AGENTS, ...agents },
  };
}

const INVALID_MESSAGE = "user policy file is invalid; fail closed";

const SETUP = {
  schema_version: "1",
  ok: true,
  effect: "unchanged",
  command: "/opt/My Apps/krellbot",
  args: ["mcp"],
  env: { KRELLBOT_HOME: "/home/ada/krell home" },
  claude_code:
    "claude mcp add krellbot --env 'KRELLBOT_HOME=/home/ada/krell home' -- '/opt/My Apps/krellbot' mcp",
};

type Call = { method: string; path: string; body: unknown; headers: Headers };
type Reply = { status?: number; json: unknown };

function installFetch(handler: (call: Call) => Reply | undefined): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call: Call = {
        method: init?.method ?? "GET",
        path: String(input),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: new Headers(init?.headers),
      };
      calls.push(call);
      const reply =
        handler(call) ??
        (call.path === "/api/v1/agents/mcp-setup" ? { json: SETUP } : undefined);
      if (!reply) throw new Error(`unexpected ${call.method} ${call.path}`);
      return new Response(JSON.stringify(reply.json), {
        status: reply.status ?? 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

function read(body: Json, extra: Json = {}): Reply {
  return {
    json: {
      schema_version: "1",
      ok: true,
      effect: "unchanged",
      code: "ok",
      path: "user-policy.json",
      policy: body,
      ...extra,
    },
  };
}

function saved(body: Json, code = "user_policy_updated"): Reply {
  return {
    json: {
      schema_version: "1",
      ok: true,
      code,
      message: "user policy updated",
      effect: "changed",
      policy: body,
    },
  };
}

function puts(calls: Call[]): Call[] {
  return calls.filter((c) => c.method === "PUT");
}

async function loaded(): Promise<HTMLElement> {
  const section = await screen.findByTestId("settings-section-agents");
  await screen.findByTestId("agents-limits-form");
  return section;
}

describe("AgentsSection", () => {
  it("loads the v2 policy and saves a changed max paper balance", async () => {
    let stored = policy();
    const calls = installFetch((call) => {
      if (call.path !== "/api/v1/user-policy") return undefined;
      if (call.method === "PUT") {
        stored = (call.body as { policy: Json }).policy;
        return saved(stored);
      }
      return read(stored);
    });
    render(<AgentsSection />);
    await loaded();

    expect(
      screen.getByRole("heading", { name: "Agents & limits" }),
    ).toBeTruthy();
    const balance = screen.getByLabelText(/largest paper balance/i);
    expect((balance as HTMLInputElement).value).toBe("10000");
    // MCP changes are off by default and shown as off.
    expect(
      (screen.getByLabelText(/mcp agents may pause/i) as HTMLInputElement)
        .checked,
    ).toBe(false);
    expect(
      (screen.getByLabelText(/mcp agents may disarm/i) as HTMLInputElement)
        .checked,
    ).toBe(false);
    // Paper only: nothing in this section controls live trading or keys.
    expect(screen.queryByLabelText(/live/i)).toBeNull();
    expect(screen.queryByLabelText(/key export/i)).toBeNull();

    fireEvent.change(balance, { target: { value: "5000" } });
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));

    await screen.findByText("Saved.");
    const [put] = puts(calls);
    expect(put.headers.get("X-Krellbot-CSRF")).toBe("test-csrf");
    const sent = (put.body as { policy: Json }).policy;
    expect((sent.agents as Json).max_paper_balance).toBe("5000");
    // Everything else goes back exactly as loaded; MCP stays off.
    expect(sent).toEqual(policy({}, { max_paper_balance: "5000" }));
  });

  it("round-trips every field without loss", async () => {
    const custom = policy(
      { allow_paper_arm: false, allow_live: false, allow_key_export: false },
      {
        allowed_venues: ["kraken"],
        allowed_pairs: [],
        max_paper_balance: "2500.50",
        max_risk_cap_pct: "0.0000001",
        require_daily_loss_limit: false,
        max_agent_arms: 0,
        krell_allow_pause_resume: false,
        krell_allow_disarm: true,
        mcp_allow_pause_resume: true,
        mcp_allow_disarm: true,
        token_budget_per_conversation: 1,
        token_budget_per_month: 9007199254740991,
      },
    );
    const calls = installFetch((call) => {
      if (call.path !== "/api/v1/user-policy") return undefined;
      return call.method === "PUT" ? saved(custom, "ok") : read(custom);
    });
    render(<AgentsSection />);
    await loaded();

    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    await screen.findByText("No changes to save.");
    expect((puts(calls)[0].body as { policy: Json }).policy).toEqual(custom);
  });

  it("edits the venue and pair allowlists", async () => {
    const calls = installFetch((call) => {
      if (call.path !== "/api/v1/user-policy") return undefined;
      return call.method === "PUT"
        ? saved((call.body as { policy: Json }).policy)
        : read(policy({}, { allowed_pairs: ["SUIUSD"] }));
    });
    render(<AgentsSection />);
    await loaded();

    fireEvent.click(screen.getByLabelText(/only these venues/i));
    fireEvent.change(screen.getByLabelText(/allowed venues/i), {
      target: { value: " kraken , coinbase,, " },
    });
    fireEvent.click(screen.getByLabelText(/every supported pair/i));
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    await screen.findByText("Saved.");

    const agents = (puts(calls)[0].body as { policy: { agents: Json } }).policy
      .agents;
    expect(agents.allowed_venues).toEqual(["kraken", "coinbase"]);
    expect(agents.allowed_pairs).toBeNull();
  });

  it("refuses a malformed number before sending it", async () => {
    const calls = installFetch((call) =>
      call.path === "/api/v1/user-policy" ? read(policy()) : undefined,
    );
    render(<AgentsSection />);
    await loaded();

    fireEvent.change(screen.getByLabelText(/largest risk cap/i), {
      target: { value: "101" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    expect((await screen.findByRole("alert")).textContent).toMatch(
      /risk cap/i,
    );
    fireEvent.change(screen.getByLabelText(/largest risk cap/i), {
      target: { value: "2" },
    });
    fireEvent.change(screen.getByLabelText(/agent arms running at once/i), {
      target: { value: "1e3" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    expect((await screen.findByRole("alert")).textContent).toMatch(
      /agent arms/i,
    );
    expect(puts(calls)).toHaveLength(0);
  });

  it("shows the server's message when a save is refused", async () => {
    installFetch((call) => {
      if (call.path !== "/api/v1/user-policy") return undefined;
      return call.method === "PUT"
        ? {
            status: 400,
            json: {
              ok: false,
              code: "user_policy_bad_input",
              message: "invalid user policy payload",
              effect: "refused",
            },
          }
        : read(policy());
    });
    render(<AgentsSection />);
    await loaded();

    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    expect((await screen.findByRole("alert")).textContent).toContain(
      "invalid user policy payload",
    );
    expect(
      (screen.getByRole("button", { name: /save limits/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it("locks the form and shows the reason when the policy file is invalid", async () => {
    let invalid = true;
    const calls = installFetch((call) => {
      if (call.path !== "/api/v1/user-policy") return undefined;
      if (call.method === "PUT") {
        invalid = false;
        return saved(policy({ allow_live: false }));
      }
      return invalid
        ? read(
            policy(
              { allow_paper_arm: false, allow_live: false },
              LOCKED_AGENTS,
            ),
            { code: "user_policy_invalid", message: INVALID_MESSAGE },
          )
        : read(policy({ allow_live: false }));
    });
    render(<AgentsSection />);
    const section = await loaded();

    const alert = within(section).getByTestId("agents-policy-invalid");
    expect(alert.textContent).toContain(INVALID_MESSAGE);
    const form = screen.getByTestId("agents-limits-form");
    for (const control of within(form).getAllByRole("textbox")) {
      expect((control as HTMLInputElement).disabled).toBe(true);
    }
    for (const control of within(form).getAllByRole("checkbox")) {
      expect((control as HTMLInputElement).disabled).toBe(true);
    }
    expect(
      (screen.getByRole("button", { name: /save limits/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /reset to default limits/i }));
    await waitFor(() => {
      expect(screen.queryByTestId("agents-policy-invalid")).toBeNull();
    });
    // The engine fills in its own default agent limits; live and key export
    // go back exactly as the engine reported them.
    expect(puts(calls)[0].body).toEqual({
      policy: {
        schema_version: "1",
        allow_paper_arm: true,
        allow_live: false,
        allow_key_export: false,
      },
    });
    expect(
      (screen.getByLabelText(/largest paper balance/i) as HTMLInputElement)
        .value,
    ).toBe("10000");
    expect(
      (screen.getByRole("button", { name: /save limits/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it("shows MCP snippets with the engine-reported command", async () => {
    installFetch((call) =>
      call.path === "/api/v1/user-policy" ? read(policy()) : undefined,
    );
    const writeText = vi.fn(async (_text: string) => undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(<AgentsSection />);
    await loaded();

    const snippet = async (id: string): Promise<string> =>
      (await screen.findByTestId(`mcp-snippet-${id}`)).textContent ?? "";
    const entry = { command: SETUP.command, args: SETUP.args, env: SETUP.env };

    expect(JSON.parse(await snippet("claude-desktop"))).toEqual({
      mcpServers: { krellbot: entry },
    });
    expect(JSON.parse(await snippet("cursor"))).toEqual({
      mcpServers: { krellbot: entry },
    });
    expect(JSON.parse(await snippet("vscode"))).toEqual({
      servers: { krellbot: { type: "stdio", ...entry } },
    });
    // The engine quotes the shell line for this computer; it is shown as is.
    expect(await snippet("claude-code")).toBe(SETUP.claude_code);

    fireEvent.click(screen.getByRole("button", { name: /copy claude code/i }));
    await screen.findByText("Copied");
    expect(writeText).toHaveBeenCalledWith(SETUP.claude_code);
  });

  it("says so when a snippet cannot be copied", async () => {
    installFetch((call) =>
      call.path === "/api/v1/user-policy" ? read(policy()) : undefined,
    );
    vi.stubGlobal("navigator", {});
    render(<AgentsSection />);
    await loaded();

    fireEvent.click(
      await screen.findByRole("button", { name: /copy vs code/i }),
    );
    expect((await screen.findByRole("alert")).textContent).toMatch(
      /could not copy/i,
    );
  });
});

describe("Settings navigation", () => {
  it("lists Agents & limits and opens it", async () => {
    installFetch((call) =>
      call.path === "/api/v1/user-policy" ? read(policy()) : undefined,
    );
    const onSectionChange = vi.fn();
    render(
      <SettingsView
        section="agents"
        onSectionChange={onSectionChange}
        onNavigate={() => undefined}
      />,
    );
    await loaded();

    const nav = screen.getByTestId("settings-nav-agents");
    expect(nav.textContent).toContain("Agents & limits");
    expect(nav.getAttribute("aria-current")).toBe("true");
    fireEvent.click(screen.getByTestId("settings-nav-ai"));
    expect(onSectionChange).toHaveBeenCalledWith("ai");
  });
});

const CHANGED_MESSAGE =
  "The policy changed since this page loaded. Reload to see the current limits.";

type FileState = { policy: Json; digest: string; invalid: boolean };

type PolicyServer = {
  calls: Call[];
  /** Every policy the fake engine accepted and wrote. */
  written: Json[];
  file(): FileState;
  /** The operator edits user-policy.json by hand while the page is open. */
  editByHand(policy: Json, digest: string): void;
  /** Hold the next PUT reply until the returned function is called. */
  holdNextPut(): () => void;
};

/**
 * A fake engine that checks `expected_digest` the way the real one does:
 * a PUT naming a digest other than the file's is refused with 409 and
 * writes nothing; a write gives the file a new digest.
 */
function policyServer(start: FileState): PolicyServer {
  let state = start;
  let writes = 0;
  let held: Promise<void> | null = null;
  const calls: Call[] = [];
  const written: Json[] = [];
  const reply = (status: number, json: unknown): Response =>
    new Response(JSON.stringify(json), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call: Call = {
        method: init?.method ?? "GET",
        path: String(input),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: new Headers(init?.headers),
      };
      calls.push(call);
      if (call.path === "/api/v1/agents/mcp-setup") return reply(200, SETUP);
      if (call.path !== "/api/v1/user-policy") {
        throw new Error(`unexpected ${call.method} ${call.path}`);
      }
      if (call.method === "GET") {
        return reply(
          200,
          read(state.policy, {
            digest: state.digest,
            ...(state.invalid
              ? { code: "user_policy_invalid", message: INVALID_MESSAGE }
              : {}),
          }).json,
        );
      }
      if (held) {
        const wait = held;
        held = null;
        await wait;
      }
      const sent = call.body as { policy: Json; expected_digest?: string };
      if (
        sent.expected_digest !== undefined &&
        sent.expected_digest !== state.digest
      ) {
        return reply(409, {
          schema_version: "1",
          ok: false,
          code: "user_policy_changed",
          message:
            "user policy changed since it was read; read it again before saving",
          effect: "refused",
        });
      }
      const next =
        sent.policy.schema_version === "1"
          ? {
              ...sent.policy,
              schema_version: "2",
              agents: state.invalid
                ? DEFAULT_AGENTS
                : (state.policy.agents as Json),
            }
          : sent.policy;
      writes += 1;
      state = { policy: next, digest: `written-${writes}`, invalid: false };
      written.push(next);
      return reply(200, {
        ...(saved(next).json as Json),
        digest: state.digest,
      });
    }),
  );
  return {
    calls,
    written,
    file: () => state,
    editByHand(policy: Json, digest: string): void {
      state = { policy, digest, invalid: false };
    },
    holdNextPut(): () => void {
      let release: () => void = () => undefined;
      held = new Promise<void>((resolve) => {
        release = resolve;
      });
      return release;
    },
  };
}

function sentDigest(call: Call): unknown {
  return (call.body as { expected_digest?: unknown }).expected_digest;
}

describe("AgentsSection when the policy file changes under the page", () => {
  it("checks each save against the file it read and then the file it wrote", async () => {
    const server = policyServer({
      policy: policy(),
      digest: "read-1",
      invalid: false,
    });
    render(<AgentsSection />);
    await loaded();

    const balance = screen.getByLabelText(/largest paper balance/i);
    fireEvent.change(balance, { target: { value: "5000" } });
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    await screen.findByText("Saved.");
    fireEvent.change(balance, { target: { value: "4000" } });
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    await screen.findByText("Saved.");

    const sent = puts(server.calls);
    expect(sent.map(sentDigest)).toEqual(["read-1", "written-1"]);
    expect(server.written).toHaveLength(2);
    expect((server.file().policy.agents as Json).max_paper_balance).toBe(
      "4000",
    );
  });

  it("refuses a save from a page loaded before the policy changed", async () => {
    const server = policyServer({
      policy: policy({ allow_live: true }),
      digest: "read-1",
      invalid: false,
    });
    render(<AgentsSection />);
    await loaded();

    // The operator turns live off by hand while this page is open.
    const tightened = policy({ allow_live: false }, { max_agent_arms: 1 });
    server.editByHand(tightened, "by-hand");
    fireEvent.change(screen.getByLabelText(/largest paper balance/i), {
      target: { value: "5000" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      CHANGED_MESSAGE,
    );
    expect(screen.queryByText("Saved.")).toBeNull();
    expect(sentDigest(puts(server.calls)[0])).toBe("read-1");
    // Saving again from the same page is refused again.
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    await waitFor(() => expect(puts(server.calls)).toHaveLength(2));
    await screen.findByRole("button", { name: "Save limits" });
    expect(screen.getByRole("alert").textContent).toBe(CHANGED_MESSAGE);

    // Nothing was written: live stays off and the hand edit stands.
    expect(server.written).toEqual([]);
    expect(server.file().policy).toEqual(tightened);
    expect(server.written.some((p) => p.allow_live === true)).toBe(false);
  });

  it("refuses a reset from a page loaded before the policy file was fixed", async () => {
    const server = policyServer({
      policy: policy(
        { allow_paper_arm: false, allow_live: false },
        LOCKED_AGENTS,
      ),
      digest: "read-invalid",
      invalid: true,
    });
    render(<AgentsSection />);
    await loaded();
    expect(screen.getByTestId("agents-policy-invalid")).toBeTruthy();

    // The operator fixes the file by hand, with paper arming and live off.
    const fixed = policy({ allow_paper_arm: false, allow_live: false });
    server.editByHand(fixed, "by-hand");
    fireEvent.click(
      screen.getByRole("button", { name: /reset to default limits/i }),
    );

    expect(await screen.findByText(CHANGED_MESSAGE)).toBeTruthy();
    await screen.findByRole("button", { name: /reset to default limits/i });
    expect(sentDigest(puts(server.calls)[0])).toBe("read-invalid");
    // Nothing was written: paper arming and live stay off.
    expect(server.written).toEqual([]);
    expect(server.file().policy).toEqual(fixed);
    expect(server.written.some((p) => p.allow_live === true)).toBe(false);
    // The page still shows the read it has, locked, until it is reloaded.
    expect(screen.getByTestId("agents-policy-invalid")).toBeTruthy();
  });

  it("labels a pending reset as resetting, not saving", async () => {
    const server = policyServer({
      policy: policy(
        { allow_paper_arm: false, allow_live: false },
        LOCKED_AGENTS,
      ),
      digest: "read-invalid",
      invalid: true,
    });
    render(<AgentsSection />);
    await loaded();

    const release = server.holdNextPut();
    fireEvent.click(
      screen.getByRole("button", { name: /reset to default limits/i }),
    );
    expect(screen.getByRole("button", { name: "Resetting…" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save limits" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Saving…" })).toBeNull();

    release();
    await screen.findByText("Reset to the default limits.");
    expect(sentDigest(puts(server.calls)[0])).toBe("read-invalid");
  });
});

describe("AgentsSection field feedback", () => {
  it("clears the saved notice when a field is edited", async () => {
    policyServer({ policy: policy(), digest: "read-1", invalid: false });
    render(<AgentsSection />);
    await loaded();

    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    await screen.findByText("Saved.");
    fireEvent.click(screen.getByLabelText(/krell may disarm/i));
    expect(screen.queryByText("Saved.")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    await screen.findByText("Saved.");
    fireEvent.change(screen.getByLabelText(/tokens per month/i), {
      target: { value: "100" },
    });
    expect(screen.queryByText("Saved.")).toBeNull();
  });

  it("ties a refused number to its field and clears it when the field is edited", async () => {
    const server = policyServer({
      policy: policy(),
      digest: "read-1",
      invalid: false,
    });
    render(<AgentsSection />);
    await loaded();

    const risk = screen.getByLabelText(/largest risk cap/i);
    const balance = screen.getByLabelText(/largest paper balance/i);
    fireEvent.change(risk, { target: { value: "101" } });
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/risk cap/i);
    expect(alert.id).not.toBe("");
    expect(risk.getAttribute("aria-invalid")).toBe("true");
    const describedBy = (risk.getAttribute("aria-describedby") ?? "").split(
      " ",
    );
    expect(describedBy).toContain(alert.id);
    // The hint still describes the field too.
    expect(describedBy).toHaveLength(2);
    expect(balance.getAttribute("aria-invalid")).toBeNull();
    expect(balance.getAttribute("aria-describedby")).not.toContain(alert.id);

    // Editing any field clears the client-side error and its marks.
    fireEvent.change(balance, { target: { value: "9000" } });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(risk.getAttribute("aria-invalid")).toBeNull();
    expect(risk.getAttribute("aria-describedby")).not.toContain(alert.id);
    expect(puts(server.calls)).toHaveLength(0);
  });
});

describe("AgentsSection replies it cannot read", () => {
  it("tells the user to fix or remove the policy file", async () => {
    installFetch((call) =>
      call.path === "/api/v1/user-policy"
        ? read(policy({}, { max_agent_arms: 2 ** 53 }), { digest: "read-1" })
        : undefined,
    );
    render(<AgentsSection />);

    const alert = await screen.findByText(/could not be loaded/i);
    expect(alert.textContent).toMatch(/fix or remove user-policy\.json/i);
    expect(screen.queryByTestId("agents-limits-form")).toBeNull();
    expect(
      screen.queryByRole("button", { name: /reset to default limits/i }),
    ).toBeNull();
  });
});

describe("McpSetupPanel", () => {
  it("warns that PowerShell expands $ and backticks in the Claude Code line", async () => {
    installFetch((call) =>
      call.path === "/api/v1/user-policy" ? read(policy()) : undefined,
    );
    render(<AgentsSection />);
    await loaded();

    await screen.findByTestId("mcp-snippet-claude-code");
    const note = screen.getByTestId("mcp-claude-code-powershell-note");
    expect(note.textContent).toMatch(/PowerShell/);
    expect(note.textContent).toContain("$");
    expect(note.textContent).toMatch(/backtick/i);
  });
});
