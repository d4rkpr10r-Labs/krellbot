"""``krellbot mcp``: the agent tool core and Krell's prompts over stdio, for any local MCP client.

An MCP client (Claude Desktop, Claude Code, Cursor, VS Code) launches
``krellbot mcp`` and talks to it over stdin and stdout with the official MCP
Python SDK's low-level :class:`~mcp.server.lowlevel.Server`.

- One agent tool core per process (:func:`krellbot.agent.assemble.build_agent_core`,
  no confirm ledger: the client asks the human before each call).
- The actor is ``mcp:<client>``, from the ``clientInfo`` the client sends with
  ``initialize`` (or with each request on 2026-07-28 connections), reduced by
  :meth:`krellbot.agent.actors.Actor.mcp`. A client that sends none is
  ``mcp:local``.
- Tools are listed from ``core.specs(actor)`` with their input schemas. A call
  runs through ``core.call``, so the hard refusals, operator limits and the
  ``agent_action`` audit record are the core's. The result is the core's dict,
  unchanged, as one JSON text block. A refusal (``ok: false``) is a normal
  result the client can show, not a protocol error; ``isError`` is set only
  when the core reports an internal failure (``effect: error``).
- Prompts: ``krell`` (Krell's MCP system prompt, then the opening line and the
  standard/advanced question) and ``krell_tour`` (the curated tour as text).
  Prompts are content, so they are not audited.

stdout carries protocol frames only. Nothing here prints on import. Tool calls
run with ``sys.stdout`` pointed at stderr, because facades print (``run``'s
``armed ...``); the SDK's stdio transport also points fd 1 at stderr while it
serves, where it can. :func:`run_stdio` sends Python logging to stderr at
WARNING. No HTTP or remote transport.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
import threading
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import anyio
from anyio import to_thread
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from krellbot import __version__
from krellbot.agent import persona, tour
from krellbot.agent.actors import Actor
from krellbot.agent.assemble import build_agent_core

SERVER_NAME = "krellbot"
SERVER_TITLE = "Krellbot"
SERVER_INSTRUCTIONS = (
    "Krellbot is a local crypto workstation. These tools read its data, draft and backtest strategies, "
    "and act on paper strategies within the operator's hard limits. Every result is JSON with ok and labels "
    "(simulated, not_advice, paper); a refusal (ok false) carries a code and is final. No tool arms live, "
    "reads keys, places or closes orders, or changes a live strategy. The krell prompt starts Krell."
)

PROMPT_KRELL = "krell"
PROMPT_KRELL_TOUR = "krell_tour"
PROMPT_NAMES: tuple[str, ...] = (PROMPT_KRELL, PROMPT_KRELL_TOUR)
_PROMPT_DESCRIPTIONS: dict[str, str] = {
    PROMPT_KRELL: "Talk with Krell, Krellbot's guide: her persona, honesty rules and the standard/advanced opening.",
    PROMPT_KRELL_TOUR: (
        "Krell's guided tour: how algo trading works, what agentic AI changes in crypto markets "
        "and how Krellbot keeps agents in bounds."
    ),
}


def krell_prompt_text() -> str:
    """Krell's MCP system prompt, then her opening line and the mode question, word for word."""
    opening = (
        "Open with this line, word for word:\n"
        f'"{persona.OPENING_LINE}"\n'
        "Then ask this, word for word:\n"
        f'"{persona.MODE_QUESTION}"\n'
        "The mode only changes how you talk, and they can switch by asking."
    )
    return persona.system_prompt(mode=None, profile={}, surface="mcp") + "\n\n" + opening


def tour_prompt_text() -> str:
    """The curated tour, step by step, with each step's facts in order."""
    intro = (
        f"Krell's guided tour (version {tour.TOUR_VERSION}). Deliver it as Krell, in your own words, "
        "one step at a time, and check in before moving on. General education only: no return figures, "
        "promises or hype, and not financial advice."
    )
    lines = [intro]
    for number, step in enumerate(tour.TOUR_STEPS, start=1):
        lines.append("")
        lines.append(f"{number}. {step.title}")
        lines.extend(f"- {fact}" for fact in step.facts)
    return "\n".join(lines)


def _prompt_text(name: str) -> str:
    return krell_prompt_text() if name == PROMPT_KRELL else tour_prompt_text()


def _actor(ctx: Any) -> Actor:
    """The session's MCP actor, from the client's ``clientInfo.name`` (``mcp:local`` without one)."""
    client_params = ctx.session.client_params
    client_info = getattr(client_params, "client_info", None)
    return Actor.mcp(getattr(client_info, "name", None))


def _tool_result(result: dict) -> types.CallToolResult:
    text = json.dumps(result, ensure_ascii=False, default=str)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        is_error=result.get("effect") == "error",
    )


def build_server(
    home: Path,
    *,
    clock: Callable[[], float] | None = None,
    fetch: Callable[..., Any] | None = None,
) -> Server:
    """Return the SDK server for ``home``: one agent core, its tools and Krell's prompts."""
    core = build_agent_core(Path(home), clock=clock, fetch=fetch)
    # One core call at a time, in a worker thread so the protocol loop stays
    # responsive. The facades print (``run.arm_pack`` says "armed ..."), so a
    # call's prints go to stderr; the protocol writer never uses sys.stdout.
    call_lock = threading.Lock()

    def run_call(actor: Actor, name: str, arguments: dict) -> dict:
        with call_lock, redirect_stdout(sys.stderr):
            return core.call(actor, name, arguments)

    async def list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        tools = [
            types.Tool(
                name=spec.name,
                description=spec.description,
                input_schema=copy.deepcopy(spec.input_schema),
            )
            for spec in core.specs(_actor(ctx))
        ]
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        arguments = dict(params.arguments or {})
        result = await to_thread.run_sync(run_call, _actor(ctx), params.name, arguments)
        return _tool_result(result)

    async def list_prompts(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListPromptsResult:
        prompts = [types.Prompt(name=name, description=_PROMPT_DESCRIPTIONS[name]) for name in PROMPT_NAMES]
        return types.ListPromptsResult(prompts=prompts)

    async def get_prompt(ctx: Any, params: types.GetPromptRequestParams) -> types.GetPromptResult:
        if params.name not in PROMPT_NAMES:
            raise MCPError(code=types.INVALID_PARAMS, message=f"unknown prompt; available: {', '.join(PROMPT_NAMES)}")
        message = types.PromptMessage(
            role="user", content=types.TextContent(type="text", text=_prompt_text(params.name))
        )
        return types.GetPromptResult(description=_PROMPT_DESCRIPTIONS[params.name], messages=[message])

    return Server(
        SERVER_NAME,
        version=__version__,
        title=SERVER_TITLE,
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
    )


def _log_to_stderr() -> None:
    """Send Python logging to stderr: stdout is the protocol channel."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "stream", None) is sys.stdout:
            root.removeHandler(handler)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("krellbot mcp: %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.WARNING)


def run_stdio(home: Path) -> int:
    """Serve ``home`` over stdin and stdout until the client closes stdin. Returns the exit code."""
    _log_to_stderr()
    server = build_server(Path(home))

    async def serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(serve)
    return 0


__all__ = [
    "PROMPT_KRELL",
    "PROMPT_KRELL_TOUR",
    "PROMPT_NAMES",
    "SERVER_NAME",
    "build_server",
    "krell_prompt_text",
    "run_stdio",
    "tour_prompt_text",
]
