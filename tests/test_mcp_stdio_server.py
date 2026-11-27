"""The stdio MCP server: the agent core's tools and Krell's prompts for any local MCP client.

``krellbot mcp`` serves :func:`krellbot.mcp.stdio.build_server` over stdio. The
tests drive the server through the MCP SDK's in-process client, both with the
``initialize`` handshake (``mode="legacy"``) and with the per-request client
envelope (``mode="auto"``), and once as a real ``krellbot mcp`` subprocess to
prove stdout carries protocol frames only. Everything stays local: an isolated
``KRELLBOT_HOME``, no network and no keyring.
"""

from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.types import CallToolResult, Implementation, TextContent

from krellbot import cli
from krellbot.agent import persona, tour
from krellbot.agent.actors import Actor
from krellbot.agent.assemble import build_agent_core
from krellbot.agent.audit import read_agent_activity
from krellbot.agent.core import ToolSpec
from krellbot.mcp import stdio as mcp_stdio
from krellbot.mcp.errors import CODE_LIVE_ARM_REFUSED, CODE_UNKNOWN_TOOL

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = 1_700_000_100
MODES = ("legacy", "auto")
# Words no agent tool name may carry: nothing closes, sells, buys or places an
# order, and nothing reaches live trading or keys.
FORBIDDEN_NAME_WORDS = ("close", "live", "key", "secret", "credential", "sell", "buy", "place", "submit")


def _with_client(
    home: Path,
    body: Callable[[Client], Awaitable[Any]],
    *,
    client_name: str = "Cursor",
    mode: str = "legacy",
) -> Any:
    server = mcp_stdio.build_server(home, clock=lambda: NOW)

    async def main() -> Any:
        info = Implementation(name=client_name, version="1.0.0")
        async with Client(server, client_info=info, mode=mode) as client:
            return await body(client)

    return anyio.run(main)


def _payload(result: CallToolResult) -> dict:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return json.loads(block.text)


def _prompt_text(result) -> str:
    texts = []
    for message in result.messages:
        assert isinstance(message.content, TextContent)
        texts.append(message.content.text)
    return "\n".join(texts)


@pytest.mark.parametrize("mode", MODES)
def test_initialize_lists_only_core_tools_and_no_close_or_live_tools(home: Path, mode: str):
    async def body(client: Client):
        return client.server_capabilities, client.server_info, await client.list_tools()

    caps, server_info, listing = _with_client(home, body, mode=mode)

    assert caps.tools is not None
    assert caps.prompts is not None
    if mode == "legacy":
        assert server_info is not None
        assert server_info.name == "krellbot"

    specs = build_agent_core(home).specs(Actor.mcp("Cursor"))
    assert [tool.name for tool in listing.tools] == [spec.name for spec in specs]
    by_name = {spec.name: spec for spec in specs}
    for tool in listing.tools:
        assert tool.input_schema == by_name[tool.name].input_schema
        assert tool.description == by_name[tool.name].description
    names = {tool.name for tool in listing.tools}
    # The read, research and change tools all come from the one core.
    assert {"read", "positions", "tour_step", "backtest", "compare", "paper_arm", "disarm"} <= names
    for name in names:
        assert not any(word in name for word in FORBIDDEN_NAME_WORDS), name
    assert "close_position" not in names


@pytest.mark.parametrize("mode", MODES)
def test_tool_call_is_audited_under_the_client_name(home: Path, mode: str):
    params = {"step_id": "algo-basics"}

    async def body(client: Client):
        return await client.call_tool("tour_step", params)

    result = _with_client(home, body, client_name="Cursor", mode=mode)

    assert result.is_error is False
    payload = _payload(result)
    assert payload["actor"] == "mcp:cursor"
    assert payload["ok"] is True

    # The served call wrote exactly one record, before any direct core call below.
    rows = read_agent_activity(home)
    assert len(rows) == 1
    served = rows[0]
    assert served["ts"] == NOW
    assert served["detail"]["actor"] == "mcp:cursor"
    assert served["detail"]["tool"] == "tour_step"
    assert served["detail"]["params"] == params
    assert served["detail"]["outcome"] == "ok"

    # The JSON text is the core's result dict, unchanged.
    expected = build_agent_core(home, clock=lambda: NOW).call(Actor.mcp("Cursor"), "tour_step", params)
    assert payload == expected


def test_client_without_a_usable_name_is_audited_as_local(home: Path):
    async def body(client: Client):
        return await client.call_tool("read", {})

    payload = _payload(_with_client(home, body, client_name="  !!  "))

    assert payload["actor"] == "mcp:local"
    assert [row["detail"]["actor"] for row in read_agent_activity(home)] == ["mcp:local"]


@pytest.mark.parametrize("mode", MODES)
def test_krell_and_tour_prompts_are_served(home: Path, mode: str):
    async def body(client: Client):
        listing = await client.list_prompts()
        krell = await client.get_prompt("krell")
        krell_tour = await client.get_prompt("krell_tour")
        return listing, krell, krell_tour

    listing, krell, krell_tour = _with_client(home, body, mode=mode)

    assert [prompt.name for prompt in listing.prompts] == ["krell", "krell_tour"]
    assert all(prompt.description for prompt in listing.prompts)
    assert all(not prompt.arguments for prompt in listing.prompts)

    krell_text = _prompt_text(krell)
    assert persona.system_prompt(mode=None, profile={}, surface="mcp") in krell_text
    assert persona.OPENING_LINE in krell_text
    assert persona.MODE_QUESTION in krell_text
    for rule in persona.HONESTY_RULES:
        assert rule in krell_text
    assert krell_text.index(persona.OPENING_LINE) < krell_text.index(persona.MODE_QUESTION)
    # The client's model can act on the answer: both mode texts, word for word,
    # after the question, and nothing suggested before the person answers.
    assert persona.opening_text() in krell_text
    assert "Do not suggest anything until they answer." in krell_text
    assert "Follow the one they choose" in krell_text
    modes = persona.mode_texts()
    assert set(modes) == {"standard", "advanced"}
    for text in modes.values():
        assert text in krell_text
        assert krell_text.index(persona.MODE_QUESTION) < krell_text.index(text)
    assert "budget" in modes["standard"]
    assert "deep end" in modes["advanced"]
    # Krellbot does not confirm an MCP agent's calls; the client's approval is the human check.
    assert "confirm" not in krell_text.lower()

    tour_text = _prompt_text(krell_tour)
    position = 0
    for step in tour.TOUR_STEPS:
        for line in (step.title, *step.facts):
            found = tour_text.find(line, position)
            assert found >= 0, line
            position = found + len(line)
    # Prompts are content, not tool calls: nothing is audited.
    assert read_agent_activity(home) == []


def test_unknown_prompt_is_a_protocol_error(home: Path):
    from mcp import MCPError

    async def body(client: Client):
        with pytest.raises(MCPError):
            await client.get_prompt("krell_live")

    _with_client(home, body)


@pytest.mark.parametrize("mode", MODES)
def test_live_tool_name_over_stdio_returns_mcp_live_arm_refused(home: Path, mode: str):
    async def body(client: Client):
        live = await client.call_tool("live_arm", {"venue": "kraken"})
        close = await client.call_tool("close_position", {"venue": "kraken", "pair": "SUIUSD"})
        return live, close

    live, close = _with_client(home, body, mode=mode)

    # Refusals are normal tool results the client can show, not protocol errors.
    assert live.is_error is False
    refused = _payload(live)
    assert refused["ok"] is False
    assert refused["effect"] == "refused"
    assert refused["code"] == CODE_LIVE_ARM_REFUSED
    assert refused["actor"] == "mcp:cursor"
    assert _payload(close)["code"] == CODE_UNKNOWN_TOOL

    rows = read_agent_activity(home, actor="mcp:cursor")
    assert [(r["detail"]["tool"], r["detail"]["outcome"], r["detail"]["code"]) for r in reversed(rows)] == [
        ("live_arm", "refused", CODE_LIVE_ARM_REFUSED),
        ("close_position", "refused", CODE_UNKNOWN_TOOL),
    ]


def _serve_extra_tools(monkeypatch: pytest.MonkeyPatch, *specs: ToolSpec) -> None:
    """Make ``build_server`` use the real assembled core plus ``specs``."""

    def build(home: Path, **kwargs: Any):
        core = build_agent_core(home, **kwargs)
        for spec in specs:
            core.register(spec)
        return core

    monkeypatch.setattr(mcp_stdio, "build_agent_core", build)


def _test_spec(name: str, handler: Callable[[Any, dict], dict]) -> ToolSpec:
    return ToolSpec(
        name=name,
        effect="read",
        cost="light",
        input_schema={"type": "object", "properties": {}},
        labels=(),
        description=f"test tool {name}",
        handler=handler,
    )


def _ok(name: str) -> dict:
    return {"schema_version": "1", "ok": True, "effect": "unchanged", "tool": name}


@pytest.mark.parametrize("mode", MODES)
def test_a_call_cancelled_while_queued_never_runs(home: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    started, release = threading.Event(), threading.Event()
    ran: list[str] = []

    def slow(ctx, params) -> dict:
        ran.append("slow_probe")
        started.set()
        release.wait(timeout=30)
        return _ok("slow_probe")

    def queued(ctx, params) -> dict:
        ran.append("queued_probe")
        return _ok("queued_probe")

    _serve_extra_tools(monkeypatch, _test_spec("slow_probe", slow), _test_spec("queued_probe", queued))

    async def body(client: Client):
        results: dict[str, CallToolResult] = {}

        async def call_slow() -> None:
            results["slow"] = await client.call_tool("slow_probe", {})

        async with anyio.create_task_group() as tg:
            tg.start_soon(call_slow)
            with anyio.fail_after(10):
                while not started.is_set():
                    await anyio.sleep(0.01)
            # The client gives up on a call queued behind the slow one.
            with anyio.move_on_after(0.3) as gave_up:
                await client.call_tool("queued_probe", {})
            assert gave_up.cancelled_caught
            await anyio.sleep(0.2)
            release.set()
        # Later calls still run, one at a time, after the slow one.
        results["after"] = await client.call_tool("tour_step", {})
        return results

    results = _with_client(home, body, mode=mode)

    assert _payload(results["slow"])["ok"] is True
    assert _payload(results["after"])["ok"] is True
    assert ran == ["slow_probe"]
    assert [row["detail"]["tool"] for row in reversed(read_agent_activity(home))] == ["slow_probe", "tour_step"]


def test_a_result_that_is_not_json_is_a_protocol_error_not_a_coerced_string(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    from mcp import MCPError

    _serve_extra_tools(monkeypatch, _test_spec("decimal_probe", lambda ctx, params: {**_ok("x"), "n": Decimal(1)}))

    async def body(client: Client):
        with pytest.raises(MCPError):
            await client.call_tool("decimal_probe", {})

    _with_client(home, body)


def test_backtest_over_mcp_returns_the_simulated_receipt_and_run_identity(home: Path):
    from tests.test_research_run_identity import _pack, _write_csv, _write_pack

    pack_path = _write_pack(home / "packs", "run-identity.json", _pack())
    csv_path = _write_csv(home / "cache", "kraken__SUIUSD__1h.csv")
    params = {"pack_path": str(pack_path), "dataset_csv": str(csv_path), "venue": "kraken"}

    async def body(client: Client):
        return await client.call_tool("backtest", params)

    result = _with_client(home, body)

    assert result.is_error is False
    payload = _payload(result)
    assert payload["ok"] is True, payload
    assert payload["labels"] == ["simulated"]
    assert payload["run"]["run_id"]
    assert [row["detail"]["tool"] for row in read_agent_activity(home, actor="mcp:cursor")] == ["backtest"]


def _paper_pack(home: Path) -> Path:
    """A pack within the default agent limits (2% risk cap)."""
    pack = home / "agent-work" / "agent-suiusd.json"
    pack.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 1,
        "id": "agent-suiusd",
        "version": "1.0.0",
        "label": "Agent stdio",
        "author": "krellbot stdio server tests",
        "timeframe": "1h",
        "indicators": {"sma20": {"fn": "sma", "src": "close", "len": 20}},
        "entry": ["close", ">", "sma20"],
        "exit": ["close", "<", "sma20"],
        "risk": {"max_account_pct": 2, "stop": {"type": "pct", "pct": 5}},
        "markets": [{"venue": "kraken", "pair": "SUIUSD"}],
    }
    pack.write_text(json.dumps(body), encoding="utf-8")
    return pack


def _paper_arm_params(pack: Path) -> dict:
    return {
        "pack_path": str(pack),
        "venue": "kraken",
        "mode": "paper",
        "paper_balance": "1000",
        "daily_loss_limit_pct": "5",
    }


@pytest.mark.usefixtures("fresh_keyring")
def test_facade_prints_during_a_tool_call_go_to_stderr_not_stdout(home: Path, capsys: pytest.CaptureFixture[str]):
    params = _paper_arm_params(_paper_pack(home))

    async def body(client: Client):
        return await client.call_tool("paper_arm", params)

    payload = _payload(_with_client(home, body))

    assert payload["ok"] is True, payload
    assert payload["labels"] == ["paper"]
    captured = capsys.readouterr()
    # ``run.arm_pack`` prints "armed ..."; under MCP that must never reach stdout.
    assert captured.out == ""
    assert "armed agent-suiusd" in captured.err


def test_cli_mcp_entry_is_registered(home: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[Path] = []

    def fake_run_stdio(home_dir: Path) -> int:
        calls.append(home_dir)
        return 0

    def no_key_migration() -> bool:
        raise AssertionError("the MCP server reads no keys and runs before the legacy key migration")

    monkeypatch.setattr(mcp_stdio, "run_stdio", fake_run_stdio)
    monkeypatch.setattr(cli.kb_secrets, "migrate_legacy", no_key_migration)

    err, out = io.StringIO(), io.StringIO()
    with redirect_stderr(err), redirect_stdout(out):
        assert cli.main(["krellbot", "mcp", "--help"]) == 0
        assert calls == []
        assert cli.main(["krellbot", "mcp", "--port", "1"]) == 2
        assert calls == []
        assert cli.main(["krellbot", "mcp"]) == 0
        assert cli.usage() == 2
    assert "krellbot mcp" in err.getvalue()
    assert "| mcp" in err.getvalue()
    assert out.getvalue() == ""
    assert calls == [home]


def test_krellbot_mcp_writes_only_protocol_frames_to_stdout(home: Path):
    pack = _paper_pack(home)
    env = dict(os.environ)
    env["KRELLBOT_HOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    env.pop("PYTHONUNBUFFERED", None)  # buffered stdout, as a client launches it
    proc = subprocess.Popen(
        [sys.executable, "-m", "krellbot.cli", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(home),
    )
    lines: queue.Queue[bytes] = queue.Queue()
    errors: list[bytes] = []

    def pump() -> None:
        for raw in iter(proc.stdout.readline, b""):
            lines.put(raw)

    def pump_stderr() -> None:
        errors.extend(iter(proc.stderr.readline, b""))

    readers = [threading.Thread(target=pump, daemon=True), threading.Thread(target=pump_stderr, daemon=True)]
    for reader in readers:
        reader.start()

    def send(message: dict) -> None:
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def reply() -> dict:
        frame = json.loads(lines.get(timeout=60).decode("utf-8"))
        assert frame["jsonrpc"] == "2.0"
        return frame

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "Claude Code", "version": "1.0.0"},
                },
            }
        )
        init = reply()
        assert init["id"] == 1
        assert init["result"]["serverInfo"]["name"] == "krellbot"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = reply()
        assert listed["id"] == 2
        assert "read" in [tool["name"] for tool in listed["result"]["tools"]]
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read", "arguments": {}}})
        called = reply()
        assert called["id"] == 3
        payload = json.loads(called["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["actor"] == "mcp:claudecode"
        # ``run.arm_pack`` prints "armed ..." while it arms.
        arm = {"name": "paper_arm", "arguments": _paper_arm_params(pack)}
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": arm})
        armed = reply()
        assert armed["id"] == 4
        assert json.loads(armed["result"]["content"][0]["text"])["ok"] is True
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        for reader in readers:
            reader.join(timeout=10)
        proc.stdout.close()
        proc.stderr.close()

    stderr = b"".join(errors).decode("utf-8", "replace")
    assert proc.returncode == 0, stderr
    assert lines.empty(), "stdout carried something after the last reply"
    assert "armed agent-suiusd" in stderr
    assert [r["detail"]["tool"] for r in read_agent_activity(home, actor="mcp:claudecode")] == ["paper_arm", "read"]


def test_mcp_doc_lists_every_tool_with_its_effect_and_labels(home: Path):
    doc = (REPO_ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    rows = {}
    for line in doc.splitlines():
        if line.startswith("| `"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows[cells[0].strip("`")] = cells

    for spec in build_agent_core(home).specs(Actor.mcp("Cursor")):
        assert spec.name in rows, spec.name
        cells = rows[spec.name]
        assert cells[1] == spec.effect, spec.name
        labels = ", ".join(f"`{label}`" for label in spec.labels) or "none"
        assert cells[2] == labels, spec.name

    flat = " ".join(doc.split())
    assert (
        "Through MCP, Krellbot controls only what its tools return. "
        "It cannot police what your client's model writes around those results."
    ) in flat
    assert "never closes positions or changes live strategies" in flat.lower()
    for client in ("Claude Desktop", "Claude Code", "Cursor", "VS Code"):
        assert client in doc
    assert "krellbot mcp" in doc
