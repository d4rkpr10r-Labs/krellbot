"""Reading an invalid ``user-policy.json`` says so, with the engine's message.

The engine fails closed on a malformed policy file (agents locked out). The
Agents & limits editor must not show that snapshot as if the operator had
chosen it, so ``GET /api/v1/user-policy`` names the invalid file with its
code and message. A valid or missing file reads as ``ok``, and a save
through ``PUT`` replaces the invalid file.
"""

from __future__ import annotations

import json
from pathlib import Path

from krellbot.api.app import CSRF_HEADER, SESSION_COOKIE
from krellbot.application import user_policy
from tests.test_ns06_api import TEST_PORT, _build_app, _do_bootstrap, asgi_call

TOKEN = "boot-user-policy-invalid-read"


def _client(home: Path):
    app = _build_app(home, bootstrap_token=TOKEN)
    _decoded, csrf = _do_bootstrap(app, token=TOKEN)
    headers = [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
        (CSRF_HEADER, csrf),
        ("Cookie", f"{SESSION_COOKIE}={next(iter(app.state.krellbot.sessions))}"),
        ("Content-Type", "application/json"),
    ]

    def call(method: str, body: dict | None = None) -> tuple[int, dict]:
        raw_body = None if body is None else json.dumps(body).encode()
        status, _h, raw, _c = asgi_call(app, method=method, path="/api/v1/user-policy", body=raw_body, headers=headers)
        return status, json.loads(raw)

    return call


def test_reading_an_invalid_policy_file_reports_the_engines_message(home: Path):
    call = _client(home)

    status, body = call("GET")
    assert status == 200
    assert body["code"] == user_policy.CODE_OK
    assert "message" not in body

    user_policy.policy_path(home).write_text('{"schema_version": "2", "agents": "nope"}', encoding="utf-8")
    status, body = call("GET")
    assert status == 200
    assert body["code"] == user_policy.CODE_POLICY_INVALID
    assert body["message"] == user_policy.MESSAGE_INVALID
    # The fail-closed snapshot the engine enforces, not a guess.
    assert body["policy"]["agents"] == user_policy.LOCKED_AGENT_LIMITS.to_dict()
    assert body["policy"]["allow_paper_arm"] is False

    # A v1 save replaces the invalid file and takes the engine's default agent limits.
    status, body = call(
        "PUT",
        {"policy": {"schema_version": "1", "allow_paper_arm": True, "allow_live": False, "allow_key_export": False}},
    )
    assert status == 200, body
    status, body = call("GET")
    assert body["code"] == user_policy.CODE_OK
    assert body["policy"]["agents"] == user_policy.DEFAULT_AGENT_LIMITS.to_dict()
    assert body["policy"]["allow_paper_arm"] is True
