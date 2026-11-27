"""A policy save from a stale read is refused, so a stale page never loosens the policy.

``GET /api/v1/user-policy`` returns ``digest``: the sha256 of the policy
file's exact bytes, or ``"absent"`` when there is no file. ``PUT`` takes an
optional ``expected_digest``; when the file no longer has those bytes the
save is refused with 409 ``user_policy_changed`` and nothing is written. A
``PUT`` without ``expected_digest`` replaces the file as it always has.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from krellbot.api.app import CSRF_HEADER, SESSION_COOKIE
from krellbot.application import user_policy
from tests.test_ns06_api import TEST_PORT, _build_app, _do_bootstrap, asgi_call

TOKEN = "boot-user-policy-stale-save"
PATH = "/api/v1/user-policy"


def _client(home: Path, *, csrf: bool = True):
    app = _build_app(home, bootstrap_token=TOKEN)
    _decoded, token = _do_bootstrap(app, token=TOKEN)
    headers = [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
        ("Cookie", f"{SESSION_COOKIE}={next(iter(app.state.krellbot.sessions))}"),
        ("Content-Type", "application/json"),
    ]
    if csrf:
        headers.append((CSRF_HEADER, token))

    def call(method: str, body: object = None) -> tuple[int, dict]:
        raw_body = None if body is None else json.dumps(body).encode()
        status, _h, raw, _c = asgi_call(app, method=method, path=PATH, body=raw_body, headers=headers)
        return status, json.loads(raw)

    return call


def _policy(*, allow_live: bool = True, **agents: object) -> dict:
    body = user_policy.DEFAULT_POLICY.to_dict()
    body["allow_live"] = allow_live
    body["agents"] = {**body["agents"], **agents}
    return body


def _file(home: Path) -> Path:
    return user_policy.policy_path(home)


def _journal_rows(home: Path) -> list[str]:
    return [line for f in sorted((home / "journal").glob("*.jsonl")) for line in f.read_text().splitlines()]


def _hand_edit(home: Path, body: dict) -> bytes:
    raw = json.dumps(body, indent=2).encode()
    _file(home).write_bytes(raw)
    return raw


def test_the_read_names_the_exact_bytes_of_the_policy_file(home: Path):
    call = _client(home)

    status, body = call("GET")
    assert status == 200
    assert body["digest"] == "absent"

    raw = _hand_edit(home, _policy(allow_live=False))
    _status, body = call("GET")
    assert body["digest"] == hashlib.sha256(raw).hexdigest()
    assert body["policy"]["allow_live"] is False

    # An invalid file has a digest too, so the editor can reset exactly that file.
    _file(home).write_bytes(b"{not json")
    _status, body = call("GET")
    assert body["code"] == user_policy.CODE_POLICY_INVALID
    assert body["digest"] == hashlib.sha256(b"{not json").hexdigest()


def test_a_save_from_a_stale_read_is_refused_and_writes_nothing(home: Path):
    call = _client(home)
    _status, first = call("PUT", {"policy": _policy(allow_live=True)})
    _status, read = call("GET")
    stale = read["digest"]

    # The operator tightens the file by hand while the editor page is open.
    tightened = _hand_edit(home, _policy(allow_live=False, max_agent_arms=1))
    rows_before = _journal_rows(home)

    # The stale page saves a limit change with the allow_live it loaded.
    status, body = call("PUT", {"policy": _policy(allow_live=True, max_paper_balance="5000"), "expected_digest": stale})
    assert status == 409, body
    assert body["ok"] is False
    assert body["code"] == user_policy.CODE_POLICY_CHANGED
    assert body["effect"] == "refused"
    assert "policy" not in body
    assert _file(home).read_bytes() == tightened
    assert _journal_rows(home) == rows_before

    # A stale reset (a schema "1" body) is refused the same way.
    reset = {"schema_version": "1", "allow_paper_arm": True, "allow_live": True, "allow_key_export": False}
    status, body = call("PUT", {"policy": reset, "expected_digest": stale})
    assert status == 409, body
    assert _file(home).read_bytes() == tightened

    # A policy sent unwrapped carries the digest beside its fields.
    status, body = call("PUT", {**_policy(allow_live=True), "expected_digest": stale})
    assert status == 409, body
    assert _file(home).read_bytes() == tightened
    assert first["ok"] is True


def test_a_save_with_the_current_digest_writes_and_returns_the_next_digest(home: Path):
    call = _client(home)

    _status, read = call("GET")
    status, body = call("PUT", {"policy": _policy(max_paper_balance="5000"), "expected_digest": read["digest"]})
    assert status == 200, body
    assert body["code"] == user_policy.CODE_POLICY_UPDATED
    written = _file(home).read_bytes()
    assert json.loads(written)["agents"]["max_paper_balance"] == "5000"
    assert body["digest"] == hashlib.sha256(written).hexdigest()

    # The returned digest is good for the next save, and the GET agrees with it.
    _status, again = call("GET")
    assert again["digest"] == body["digest"]
    status, body = call("PUT", {"policy": _policy(max_paper_balance="4000"), "expected_digest": body["digest"]})
    assert status == 200, body
    assert json.loads(_file(home).read_bytes())["agents"]["max_paper_balance"] == "4000"

    # An unwrapped policy with a matching digest saves too.
    status, body = call("PUT", {**_policy(max_paper_balance="3000"), "expected_digest": body["digest"]})
    assert status == 200, body
    assert json.loads(_file(home).read_bytes())["agents"]["max_paper_balance"] == "3000"


def test_a_reset_with_the_current_digest_replaces_an_invalid_file(home: Path):
    call = _client(home)
    _file(home).write_bytes(b'{"schema_version": "2", "agents": "nope"}')
    _status, read = call("GET")
    assert read["code"] == user_policy.CODE_POLICY_INVALID

    reset = {"schema_version": "1", "allow_paper_arm": True, "allow_live": False, "allow_key_export": False}
    status, body = call("PUT", {"policy": reset, "expected_digest": read["digest"]})
    assert status == 200, body
    assert body["policy"]["agents"] == user_policy.DEFAULT_AGENT_LIMITS.to_dict()
    assert body["policy"]["allow_live"] is False
    assert body["digest"] == hashlib.sha256(_file(home).read_bytes()).hexdigest()


def test_a_save_without_a_digest_replaces_the_file_as_before(home: Path):
    call = _client(home)
    _hand_edit(home, _policy(allow_live=False))

    status, body = call("PUT", {"policy": _policy(allow_live=True)})
    assert status == 200, body
    assert set(body) == {"schema_version", "ok", "code", "message", "effect", "policy"}
    assert body["code"] == user_policy.CODE_POLICY_UPDATED
    assert json.loads(_file(home).read_bytes())["allow_live"] is True

    # Unwrapped, without a digest, as before.
    status, body = call("PUT", _policy(allow_live=False))
    assert status == 200, body
    assert json.loads(_file(home).read_bytes())["allow_live"] is False


@pytest.mark.parametrize(("digest", "expected_status"), [(None, 400), (7, 400), (["absent"], 400), ("", 409)])
def test_a_digest_that_is_not_the_files_digest_is_refused(home: Path, digest: object, expected_status: int):
    call = _client(home)
    before = _hand_edit(home, _policy(allow_live=False))

    # A null digest is not read as "no digest": a sent digest must match.
    status, _body = call("PUT", {"policy": _policy(allow_live=True), "expected_digest": digest})
    assert status == expected_status
    assert _file(home).read_bytes() == before


def test_a_stale_save_still_needs_the_csrf_token(home: Path):
    call = _client(home, csrf=False)
    before = _hand_edit(home, _policy(allow_live=False))

    status, _body = call("PUT", {"policy": _policy(allow_live=True), "expected_digest": "absent"})
    assert status == 403
    assert _file(home).read_bytes() == before


def test_a_change_landing_between_the_read_and_the_write_is_still_refused(home: Path, monkeypatch: pytest.MonkeyPatch):
    raw = _hand_edit(home, _policy(allow_live=False))
    digest = hashlib.sha256(raw).hexdigest()
    later = _policy(allow_live=False, max_agent_arms=0)
    real_load = user_policy._load

    def load_then_edit(at: Path):
        loaded = real_load(at)
        _hand_edit(home, later)
        return loaded

    monkeypatch.setattr(user_policy, "_load", load_then_edit)
    result = user_policy.save_policy(home, _policy(allow_live=True), expected_digest=digest)
    assert result.ok is False
    assert result.code == user_policy.CODE_POLICY_CHANGED
    assert json.loads(_file(home).read_bytes()) == later
