"""MCP setup: how an MCP client starts ``krellbot mcp`` on this computer.

``GET /api/v1/agents/mcp-setup`` feeds the copy-ready snippets in Settings →
Agents & limits. It reports the launch command, its args and the
``KRELLBOT_HOME`` the server binds, plus the Claude Code line quoted for
this computer's shell, and nothing else. It sits behind the read session
gate. A frozen build launches its own executable. A source install launches
the ``krellbot`` script installed beside the running interpreter (the same
install as the engine), else the ``krellbot`` script on PATH, else the
interpreter with ``-m krellbot.cli``. On Windows the command and the
``--env`` value are always double-quoted, so ``&`` and the like survive cmd.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from krellbot.api.app import SESSION_COOKIE, _claude_code_line
from tests.test_ns06_api import TEST_PORT, _build_app, _do_bootstrap, asgi_call

TOKEN = "boot-agents-mcp-setup"
PATH = "/api/v1/agents/mcp-setup"
WINDOWS = sys.platform == "win32"
SCRIPT_NAME = "krellbot.exe" if WINDOWS else "krellbot"


def _which(found: str | None):
    def which(name: str, *args: object, **kwargs: object) -> str | None:
        return found if name == "krellbot" else None

    return which


def _interpreter_scripts(monkeypatch: pytest.MonkeyPatch, folder: Path, *, with_krellbot: bool) -> str:
    """Make ``folder`` the running interpreter's scripts folder; return its krellbot script path."""
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / SCRIPT_NAME
    if with_krellbot:
        script.write_bytes(b"")
    real_get_path = sysconfig.get_path

    def get_path(name: str, *args: object, **kwargs: object) -> str:
        return str(folder) if name == "scripts" else real_get_path(name, *args, **kwargs)

    monkeypatch.setattr(sysconfig, "get_path", get_path)
    return str(script)


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
    script = _interpreter_scripts(monkeypatch, home / "venv" / "bin", with_krellbot=True)
    monkeypatch.setattr(shutil, "which", _which(str(home / "elsewhere" / "krellbot")))

    assert _get(home, session=False)[0] == 403
    assert _get(home, origin="https://evil.example")[0] == 403

    status, body, headers = _get(home, origin=f"http://127.0.0.1:{TEST_PORT}")
    assert status == 200, body
    assert headers["cache-control"] == "no-store"
    assert body["ok"] is True
    # The script beside the engine's own interpreter, not another one on PATH.
    assert body["command"] == script
    assert body["args"] == ["mcp"]
    assert body["env"] == {"KRELLBOT_HOME": str(home)}
    # Only the command, its args and the home: nothing else is exposed.
    assert set(body) == {"schema_version", "ok", "effect", "command", "args", "env", "claude_code"}
    assert body["claude_code"] == _expected_claude_code(home, script, ["mcp"])


@pytest.mark.parametrize(
    ("beside_interpreter", "on_path", "launches"),
    [
        (True, True, "beside_interpreter"),
        (True, False, "beside_interpreter"),
        (False, True, "on_path"),
        (False, False, "cli_module"),
    ],
)
def test_the_launch_command_prefers_the_interpreters_script_then_path_then_the_cli_module(
    home: Path, monkeypatch: pytest.MonkeyPatch, beside_interpreter: bool, on_path: bool, launches: str
):
    beside = _interpreter_scripts(monkeypatch, home / "interpreter" / "bin", with_krellbot=beside_interpreter)
    path_script = str(home / "on path" / "krellbot")
    monkeypatch.setattr(shutil, "which", _which(path_script if on_path else None))

    _status, body, _headers = _get(home)
    expected = {
        "beside_interpreter": (beside, ["mcp"]),
        "on_path": (path_script, ["mcp"]),
        "cli_module": (sys.executable, ["-m", "krellbot.cli", "mcp"]),
    }[launches]
    assert (body["command"], body["args"]) == expected


def test_a_frozen_build_launches_its_own_executable(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Krellbot/krellbot")
    _interpreter_scripts(monkeypatch, home / "interpreter" / "bin", with_krellbot=True)
    monkeypatch.setattr(shutil, "which", _which("/usr/local/bin/krellbot"))

    _status, body, _headers = _get(home)
    assert (body["command"], body["args"]) == ("/Applications/Krellbot/krellbot", ["mcp"])


def test_without_a_krellbot_script_the_interpreter_runs_the_cli_module(home: Path, monkeypatch: pytest.MonkeyPatch):
    _interpreter_scripts(monkeypatch, home / "interpreter" / "bin", with_krellbot=False)
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
    command = _interpreter_scripts(monkeypatch, home / "My Apps" / "bin", with_krellbot=True)
    monkeypatch.setattr(shutil, "which", _which(None))

    _status, body, _headers = _get(spaced_home)
    assert body["command"] == command
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


@pytest.mark.parametrize(
    ("command", "home", "line"),
    [
        (
            r"C:\Program Files\Krellbot\krellbot.exe",
            r"C:\Users\Ada\krell home",
            (
                r'claude mcp add krellbot --env "KRELLBOT_HOME=C:\Users\Ada\krell home" -- '
                r'"C:\Program Files\Krellbot\krellbot.exe" mcp'
            ),
        ),
        # cmd splits an unquoted line at &, so a path with & must be quoted too.
        (
            r"C:\R&D\krellbot.exe",
            r"C:\R&D\home",
            r'claude mcp add krellbot --env "KRELLBOT_HOME=C:\R&D\home" -- "C:\R&D\krellbot.exe" mcp',
        ),
        # Paths with no spaces at all are quoted as well.
        (
            r"C:\krellbot\krellbot.exe",
            r"C:\krell",
            r'claude mcp add krellbot --env "KRELLBOT_HOME=C:\krell" -- "C:\krellbot\krellbot.exe" mcp',
        ),
        # A trailing backslash is doubled so it does not escape the closing quote.
        (
            r"C:\krellbot.exe",
            "C:\\",
            'claude mcp add krellbot --env "KRELLBOT_HOME=C:\\\\" -- "C:\\krellbot.exe" mcp',
        ),
    ],
)
def test_windows_lines_always_double_quote_the_command_and_the_home(command: str, home: str, line: str):
    assert _claude_code_line(command, ["mcp"], home, windows=True) == line


def test_windows_lines_leave_the_cli_module_args_bare():
    line = _claude_code_line(r"C:\Python313\python.exe", ["-m", "krellbot.cli", "mcp"], r"C:\krell", windows=True)
    assert line == (
        r'claude mcp add krellbot --env "KRELLBOT_HOME=C:\krell" -- "C:\Python313\python.exe" -m krellbot.cli mcp'
    )


def test_posix_lines_use_single_quotes_only_where_needed():
    line = _claude_code_line("/opt/it's mine/krellbot", ["mcp"], "/home/ada/krell", windows=False)
    assert line == "claude mcp add krellbot --env KRELLBOT_HOME=/home/ada/krell -- '/opt/it'\"'\"'s mine/krellbot' mcp"
    assert shlex.split(line) == [
        "claude",
        "mcp",
        "add",
        "krellbot",
        "--env",
        "KRELLBOT_HOME=/home/ada/krell",
        "--",
        "/opt/it's mine/krellbot",
        "mcp",
    ]


def _expected_claude_code(home: Path, command: str, args: list[str]) -> str:
    env = f"KRELLBOT_HOME={home}"
    if WINDOWS:
        # The command and the --env value are always double-quoted; the args are bare words.
        return " ".join(["claude mcp add krellbot --env", f'"{env}"', "--", f'"{command}"', *args])
    return " ".join(["claude mcp add krellbot --env", shlex.quote(env), "--", shlex.quote(command), *args])
