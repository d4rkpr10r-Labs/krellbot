"""``GET /api/v1/agents/activity``: who did what, including refusals.

Rows come from real ``agent_action`` journal records written by
``McpTools``; the route exposes only the row contract (never raw params).
"""

from __future__ import annotations

import json
from pathlib import Path

from krellbot.agent.actors import Actor
from krellbot.agent.audit import append_agent_record
from krellbot.mcp.tools import McpTools
from tests.test_ns06_api import TEST_PORT, _bootstrap_and_get_csrf, _build_app, asgi_call

ROW_KEYS = {"ts", "actor", "tool", "outcome", "code", "venue", "pair", "confirm_ticket", "tokens"}


def _app(home: Path):
    return _build_app(home, bootstrap_token=f"boot-{home.name}")


def _get(app, home: Path, query: str = "", *, session: bool = True) -> tuple[int, dict]:
    headers = [("Host", f"127.0.0.1:{TEST_PORT}")]
    if session:
        if not app.state.krellbot.sessions:
            _bootstrap_and_get_csrf(app, f"boot-{home.name}")
        sid = next(iter(app.state.krellbot.sessions.keys()))
        headers += [("Origin", f"http://127.0.0.1:{TEST_PORT}"), ("Cookie", f"krellbot_session={sid}")]
    status, _h, body, _c = asgi_call(
        app, method="GET", path="/api/v1/agents/activity" + (f"?{query}" if query else ""), headers=headers
    )
    return status, json.loads(body)


def test_activity_requires_a_session(home: Path) -> None:
    status, _ = _get(_app(home), home, session=False)
    assert status == 403


def test_activity_lists_refusals_and_successes_newest_first(home: Path) -> None:
    ticks = iter([1_700_000_100, 1_700_000_200])
    mcp = McpTools(home, clock=lambda: next(ticks))
    mcp.call("arm_live", venue="kraken")
    mcp.call("read")

    status, body = _get(_app(home), home)

    assert status == 200
    rows = body["rows"]
    assert [r["tool"] for r in rows] == ["read", "arm_live"]
    assert [r["outcome"] for r in rows] == ["ok", "refused"]
    assert rows[1]["code"] == "mcp_live_arm_refused"
    assert rows[1]["actor"] == "mcp:local"
    assert rows[0]["ts"] == 1_700_000_200
    for row in rows:
        assert set(row) == ROW_KEYS
        assert row["pair"] is None


def test_activity_filters_by_actor_and_outcome(home: Path) -> None:
    McpTools(home, actor=Actor.mcp("cursor")).call("arm_live")
    McpTools(home, actor=Actor.mcp("cursor")).call("read")
    McpTools(home, actor=Actor.mcp("other")).call("read")
    app = _app(home)

    _, by_actor = _get(app, home, "actor=mcp:cursor")
    assert len(by_actor["rows"]) == 2
    assert {r["actor"] for r in by_actor["rows"]} == {"mcp:cursor"}

    _, both = _get(app, home, "actor=mcp:cursor&outcome=refused")
    assert [(r["tool"], r["outcome"]) for r in both["rows"]] == [("arm_live", "refused")]

    _, by_tool = _get(app, home, "tool=read")
    assert len(by_tool["rows"]) == 2

    _, future = _get(app, home, "since=99999999999")
    assert future["rows"] == []


def test_activity_pair_comes_from_params_and_params_are_not_returned(home: Path) -> None:
    append_agent_record(
        home,
        ts=1_700_000_300,
        actor=Actor.mcp("cursor"),
        tool="paper_arm",
        params={"pair": "SUIUSD", "note": "hello"},
        outcome="ok",
        code="ok",
        venue="kraken",
    )

    _, body = _get(_app(home), home)

    row = body["rows"][0]
    assert (row["venue"], row["pair"]) == ("kraken", "SUIUSD")
    assert "params" not in row and "hello" not in json.dumps(body)


def test_activity_rejects_bad_filters_with_typed_codes(home: Path) -> None:
    app = _app(home)

    status, body = _get(app, home, "outcome=maybe")
    assert (status, body["code"]) == (400, "agent_activity_outcome_invalid")

    status, body = _get(app, home, "since=yesterday")
    assert (status, body["code"]) == (400, "agent_activity_since_invalid")
