/**
 * Settings → Agents & limits (M38.10): copy-ready setup for MCP clients.
 *
 * The snippets use the launch command, args and `KRELLBOT_HOME` the engine
 * reports (`GET /api/v1/agents/mcp-setup`); the Claude Code line is the
 * engine's own quoting for this computer's shell, shown as is.
 */
import { useEffect, useState, type JSX } from "react";
import { loadMcpSetup, type McpSetup } from "./userPolicyClient";

export type McpSnippet = {
  id: "claude-desktop" | "claude-code" | "cursor" | "vscode";
  client: string;
  where: string;
  text: string;
};

/** Setup for each client, from the engine-reported launch command. */
export function mcpSnippets(setup: McpSetup): McpSnippet[] {
  const entry = { command: setup.command, args: setup.args, env: setup.env };
  const json = (value: unknown): string => JSON.stringify(value, null, 2);
  return [
    {
      id: "claude-desktop",
      client: "Claude Desktop",
      where: "claude_desktop_config.json",
      text: json({ mcpServers: { krellbot: entry } }),
    },
    {
      id: "claude-code",
      client: "Claude Code",
      where: "run in a terminal",
      text: setup.claudeCode,
    },
    {
      id: "cursor",
      client: "Cursor",
      where: ".cursor/mcp.json in a project, or ~/.cursor/mcp.json",
      text: json({ mcpServers: { krellbot: entry } }),
    },
    {
      id: "vscode",
      client: "VS Code",
      where: ".vscode/mcp.json",
      text: json({ servers: { krellbot: { type: "stdio", ...entry } } }),
    },
  ];
}

export function McpSetupPanel({ enabled }: { enabled: boolean }): JSX.Element {
  const [setup, setSetup] = useState<McpSetup | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [copyFailed, setCopyFailed] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void loadMcpSetup().then((result) => {
      if (cancelled) return;
      if (result.ok) {
        setSetup(result.setup);
        setError(null);
      } else {
        setError(result.message);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  function copy(snippet: McpSnippet): void {
    setCopied(null);
    setCopyFailed(null);
    const fail = (): void => setCopyFailed(snippet.id);
    try {
      const clipboard = navigator.clipboard;
      if (!clipboard || typeof clipboard.writeText !== "function") {
        fail();
        return;
      }
      void clipboard
        .writeText(snippet.text)
        .then(() => setCopied(snippet.id), fail);
    } catch {
      fail();
    }
  }

  return (
    <div className="kbot-settings__card" data-testid="agents-mcp-setup">
      <h3 className="kbot-settings__subhead">Connect an MCP client</h3>
      <p className="kbot-settings__card-copy">
        Claude Desktop, Claude Code, Cursor and VS Code can use Krellbot&apos;s
        agent tools over MCP. The client starts Krellbot with the command below,
        on this computer, and talks to it over stdio. Nothing listens on the
        network, and nothing runs until a client starts it.
      </p>
      <p className="kbot-settings__card-copy">
        Through MCP, Krellbot controls only what its tools return. It cannot
        police what your client&apos;s model writes around those results.
      </p>
      <p className="kbot-settings__card-copy">
        MCP agents obey the limits above. Your client asks you before each tool
        call unless you told it not to; keep that on for changes.
      </p>
      {error ? (
        <p className="kbot-settings__error" role="alert">
          The launch command could not be loaded: {error}
        </p>
      ) : null}
      {setup === null && error === null ? (
        <p className="kbot-settings__status-line" role="status">
          Loading the launch command…
        </p>
      ) : null}
      {setup
        ? mcpSnippets(setup).map((snippet) => (
            <div key={snippet.id} className="kbot-settings__field">
              <p className="kbot-settings__field-label">
                {snippet.client} · {snippet.where}
              </p>
              <pre
                className="kbot-settings__code"
                data-testid={`mcp-snippet-${snippet.id}`}
              >
                {snippet.text}
              </pre>
              {snippet.id === "claude-code" ? (
                <p
                  className="kbot-settings__status-line"
                  data-testid="mcp-claude-code-powershell-note"
                >
                  PowerShell expands $ and backticks (`) even inside double
                  quotes. If a path in this line has either, run the line in
                  Command Prompt instead.
                </p>
              ) : null}
              <div className="kbot-settings__inline-actions">
                <button
                  type="button"
                  className="kbot-settings__btn"
                  aria-label={`Copy ${snippet.client} setup`}
                  onClick={() => copy(snippet)}
                >
                  Copy
                </button>
                {copied === snippet.id ? (
                  <span className="kbot-settings__status-line" role="status">
                    Copied
                  </span>
                ) : null}
              </div>
              {copyFailed === snippet.id ? (
                <p className="kbot-settings__error" role="alert">
                  Could not copy. Select the text and copy it yourself.
                </p>
              ) : null}
            </div>
          ))
        : null}
    </div>
  );
}
