"""MCP setup: how an MCP client starts ``krellbot mcp`` on this computer.

``GET /api/v1/agents/mcp-setup`` feeds the copy-ready snippets in Settings →
Agents & limits. It reports the launch command, its args and the
``KRELLBOT_HOME`` the server binds, plus the Claude Code line quoted for
this computer's shell, and nothing else. It sits behind the read session
gate. A frozen build launches its own executable; a source install the
``krellbot`` script on PATH, or else the interpreter with ``-m krellbot.cli``.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from krellbot.api.app import SESSION_COOKIE, _mcp_shell_line
from tests.test_ns06_api import TEST_PORT, _build_app, _do_bootstrap, asgi_call

TOKEN = "boot-agents-mcp-setup"
PATH = "/api/v1/agents/mcp-setup"
WINDOWS = sys.platform == "win32"


def _which(found: str | None):
    def which(name: str, *args: object, **kwargs: object) -> str | None:
        return found if name == "krellbot" else None

    return which


def _get(home: Path, *, session: bool = True, origin: str | None = None) -> tuple[int, dict, dict]:
    app = _build_app(home, bootstrap_token=TOKEN)
    _do_bootstrap(app, token=TOKEN)
    headers = [("Host", f"127.0.0.1:{TEST_PORT}")]
    if session:
        headers.append(("Cookie", f"{SESSION_COOKIE}={next(iter(app.state.krellbot.sessions))}"))
    if origin is not None:
        headers.append(("Origin", origin))
    status, raw_headers, raw, _cookies = asgi_call(app, method="GET", path=PATH, headers=headers)
    return status, json.loads(raw), dict(raw_headers)


def test_mcp_setup_route_is_session_gated_and_names_krellbot_home(home: Path, monkeypatch: pytest.MonkeyPatch):
    script = str(home / "venv" / "bin" / "krellbot")
    monkeypatch.setattr(shutil, "which", _which(script))

    assert _get(home, session=False)[0] == 403
    assert _get(home, origin="https://evil.example")[0] == 403

    status, body, headers = _get(home, origin=f"http://127.0.0.1:{TEST_PORT}")
    assert status == 200, body
    assert headers["cache-control"] == "no-store"
    assert body["ok"] is True
    assert body["command"] == script
    assert body["args"] == ["mcp"]
    assert body["env"] == {"KRELLBOT_HOME": str(home)}
    # Only the command, its args and the home: nothing else is exposed.
    assert set(body) == {"schema_version", "ok", "effect", "command", "args", "env", "claude_code"}
    assert body["claude_code"] == _expected_claude_code(home, script, ["mcp"])


def test_a_frozen_build_launches_its_own_executable(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Krellbot/krellbot")
    monkeypatch.setattr(shutil, "which", _which("/usr/local/bin/krellbot"))

    _status, body, _headers = _get(home)
    assert (body["command"], body["args"]) == ("/Applications/Krellbot/krellbot", ["mcp"])


def test_without_a_krellbot_script_the_interpreter_runs_the_cli_module(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shutil, "which", _which(None))

    _status, body, _headers = _get(home)
    assert body["command"] == sys.executable
    assert body["args"] == ["-m", "krellbot.cli", "mcp"]

    # The fallback really reaches `krellbot mcp` (its help goes to stderr).
    proc = subprocess.run(
        [body["command"], *body["args"], "--help"], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage: krellbot mcp" in proc.stderr
    assert proc.stdout == ""


def test_the_claude_code_line_quotes_paths_with_spaces(home: Path, monkeypatch: pytest.MonkeyPatch):
    spaced_home = home / "my krell home"
    spaced_home.mkdir()
    command = r"C:\Program Files\Krellbot\krellbot.exe" if WINDOWS else "/opt/My Apps/krellbot"
    monkeypatch.setattr(shutil, "which", _which(command))

    _status, body, _headers = _get(spaced_home)
    assert body["env"] == {"KRELLBOT_HOME": str(spaced_home)}
    assert body["claude_code"] == _expected_claude_code(spaced_home, command, ["mcp"])
    if not WINDOWS:
        assert shlex.split(body["claude_code"]) == [
            "claude",
            "mcp",
            "add",
            "krellbot",
            "--env",
            f"KRELLBOT_HOME={spaced_home}",
            "--",
            command,
            "mcp",
        ]


def test_windows_shell_lines_use_double_quotes_and_posix_lines_single_quotes():
    argv = ["claude", "mcp", "add", "krellbot", "--", r"C:\Program Files\Krellbot\krellbot.exe", "mcp"]
    assert _mcp_shell_line(argv, windows=True) == (
        r'claude mcp add krellbot -- "C:\Program Files\Krellbot\krellbot.exe" mcp'
    )
    argv = ["claude", "mcp", "add", "krellbot", "--", "/opt/it's mine/krellbot", "mcp"]
    line = _mcp_shell_line(argv, windows=False)
    assert line == "claude mcp add krellbot -- '/opt/it'\"'\"'s mine/krellbot' mcp"
    assert shlex.split(line) == argv


def _expected_claude_code(home: Path, command: str, args: list[str]) -> str:
    env = f"KRELLBOT_HOME={home}"
    if WINDOWS:

        def quote(value: str) -> str:
            return f'"{value}"' if " " in value else value
    else:
        quote = shlex.quote
    return " ".join(["claude mcp add krellbot --env", quote(env), "--", quote(command), *map(quote, args)])
