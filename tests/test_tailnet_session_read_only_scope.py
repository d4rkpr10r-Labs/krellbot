"""Tailscale sessions are read-only unless a local session opts in (D14).

A tailnet session is admitted only through Tailscale Serve with the configured
owner login. Its scope (``read_only`` or ``full``) is stored in the home and
defaults to ``read_only``: a missing, empty or malformed record never widens
it. Under ``read_only`` every state change from a tailnet session is refused
with 403 ``remote_read_only`` by one gate in the Tailscale middleware, while
reads keep working. The scope changes only through a state-change-gated route
on a loopback session, never from a tailnet session and never by a CLI flag.
Loopback sessions are unaffected.

Real loopback HTTP, an isolated home and no venue transport. Planted values
are named CANARY.
"""

from __future__ import annotations

import contextlib
import http.client
import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from krellbot.api.serve import WorkstationServer
from krellbot.api.tailnet import read_remote_scope, write_remote_scope
from krellbot.application import live_gate

ORIGIN = "https://workstation.example.ts.net"
TAILNET_HOST = "workstation.example.ts.net"
USER = "owner@example.com"
SCOPE_ROUTE = "/api/v1/remote-access/scope"
LIVE_ARM = {"command": "live.arm", "payload": {"mode": "live"}}
PAPER_ONLY = (b"live_disabled_paper_only_release", b"paper-only")
READ_METHODS = {"GET", "HEAD"}


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text("<html><head></head><body>Workstation</body></html>")
    return dist


@contextlib.contextmanager
def _serve(tmp_path: Path, home: Path, *, tailnet: bool):
    server = WorkstationServer(
        home=home,
        dist_dir=_dist(tmp_path),
        tailscale_origin=ORIGIN if tailnet else None,
        tailscale_user=USER if tailnet else None,
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _http(server: WorkstationServer, method: str, path: str, headers: dict[str, str], payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=5)
    try:
        body = json.dumps(payload) if payload is not None else None
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


class _Session:
    """One authenticated browser session: cookie, CSRF and the right Host/Origin."""

    def __init__(self, server: WorkstationServer, headers: dict[str, str]) -> None:
        self.server = server
        self.headers = headers

    def get(self, path: str):
        return _http(self.server, "GET", path, self.headers)

    def send(self, method: str, path: str, payload=None, *, csrf: bool = True):
        headers = dict(self.headers)
        if not csrf:
            headers.pop("X-Krellbot-CSRF", None)
        headers["Content-Type"] = "application/json"
        return _http(self.server, method, path, headers, {} if payload is None else payload)


def _tailnet_session(server: WorkstationServer) -> _Session:
    remote = {"Host": TAILNET_HOST, "Tailscale-User-Login": USER}
    status, headers, _ = _http(server, "POST", "/login", {**remote, "Origin": ORIGIN})
    assert status == 303
    cookie = headers["set-cookie"].split(";", 1)[0]
    authed = {**remote, "Cookie": cookie, "Origin": ORIGIN}
    status, _, body = _http(server, "GET", "/api/v1/session/csrf", authed)
    assert status == 200, body
    return _Session(server, {**authed, "X-Krellbot-CSRF": json.loads(body)["csrf_token"]})


def _loopback_session(server: WorkstationServer) -> _Session:
    local = {
        "Host": f"127.0.0.1:{server.bound_port}",
        "Origin": f"http://127.0.0.1:{server.bound_port}",
    }
    status, headers, body = _http(
        server,
        "POST",
        "/api/v1/session/bootstrap",
        {**local, "Content-Type": "application/json"},
        {"token": server.bootstrap_token},
    )
    assert status == 200, body
    cookie = headers["set-cookie"].split(";", 1)[0]
    return _Session(server, {**local, "Cookie": cookie, "X-Krellbot-CSRF": json.loads(body)["csrf_token"]})


def _code(body: bytes) -> str | None:
    try:
        decoded = json.loads(body)
    except ValueError:
        return None
    return decoded.get("code") if isinstance(decoded, dict) else None


def _mutating_routes(home: Path) -> list[tuple[str, str]]:
    """Every (method, path) the app serves with a method other than GET/HEAD."""

    from krellbot.api.app import create_app

    app = create_app(home, port=1, bootstrap_token="CANARY")
    found: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        # A mount or websocket route would escape this enumeration; there is none.
        assert isinstance(route, APIRoute) or methods is not None, route
        for method in sorted(set(methods or ()) - READ_METHODS):
            found.append((method, route.path))
    return found


def _concrete(path: str) -> str:
    out = []
    for part in path.split("/"):
        out.append("CANARY" if part.startswith("{") and part.endswith("}") else part)
    return "/".join(out)


def test_tailnet_session_is_read_only_by_default_and_reads_still_work(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with _serve(tmp_path, home, tailnet=True) as server:
        remote = _tailnet_session(server)

        status, _, body = remote.get("/api/v1/trust")
        assert status == 200, body
        decoded = json.loads(body)
        assert decoded["remote_scope"] == "read_only"
        assert decoded["remote_access"] == {"mode": "tailscale_serve", "origin": ORIGIN}
        status, _, body = remote.get("/")
        assert status == 200 and b"Workstation" in body
        assert remote.get("/api/v1/operations")[0] == 200

        status, _, body = remote.send("POST", "/api/v1/commands", LIVE_ARM)
        assert status == 403, body
        refusal = json.loads(body)
        assert refusal["code"] == "remote_read_only"
        assert refusal["ok"] is False and refusal["effect"] == "refused"
        assert not any(marker in body for marker in PAPER_ONLY)

        status, _, body = remote.send(
            "POST", "/api/v1/commands", {"command": "operations.kill", "payload": {"reason": "CANARY"}}
        )
        assert status == 403 and _code(body) == "remote_read_only"
        status, _, body = remote.send("POST", "/api/v1/commands", {"command": "paper.pause_entries", "payload": {}})
        assert status == 403 and _code(body) == "remote_read_only"

    assert live_gate.kill_state(home).engaged is False
    assert not live_gate.kill_path(home).exists()
    # A refusal never writes a scope record either.
    assert not (home / "remote_access.json").exists()


def test_every_mutating_route_refuses_a_read_only_tailnet_session(tmp_path: Path) -> None:
    routes = _mutating_routes(tmp_path / "routes-home")
    for expected in [
        ("POST", "/api/v1/commands"),
        ("POST", "/api/v1/operations/live-authorization"),
        ("DELETE", "/api/v1/operations/live-authorization"),
        ("PUT", "/api/v1/telemetry"),
        ("PUT", "/api/v1/notifications"),
        ("POST", "/api/v1/packs/install"),
        ("POST", "/api/v1/research/jobs"),
        ("POST", "/api/v1/keys/{venue}"),
        ("DELETE", "/api/v1/keys/{venue}"),
        ("PUT", SCOPE_ROUTE),
    ]:
        assert expected in routes

    home = tmp_path / "home"
    with _serve(tmp_path, home, tailnet=True) as server:
        remote = _tailnet_session(server)
        refused: dict[tuple[str, str], tuple[int, str | None]] = {}
        for method, path in routes:
            status, _, body = remote.send(method, _concrete(path))
            refused[(method, path)] = (status, _code(body))
            if path == "/api/v1/session/bootstrap":
                # Refused for every tailnet request before any session exists.
                assert status == 403 and b"use Tailscale login" in body
                continue
            assert (status, _code(body)) == (403, "remote_read_only"), (method, path, status, body)
        # The gate is by method, so a state change on a path with no route is refused too.
        status, _, body = remote.send("PATCH", "/api/v1/trust")
        assert (status, _code(body)) == (403, "remote_read_only")
        status, _, body = remote.send("POST", "/api/v1/CANARY")
        assert (status, _code(body)) == (403, "remote_read_only")
        # Reads still work after the sweep.
        assert remote.get("/api/v1/trust")[0] == 200

    assert len(refused) == len(routes)
    assert read_remote_scope(home) == "read_only"
    assert not live_gate.kill_path(home).exists()


def test_scope_changes_only_from_a_loopback_session(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with _serve(tmp_path, home, tailnet=False) as local_server, _serve(tmp_path, home, tailnet=True) as remote_server:
        local = _loopback_session(local_server)
        remote = _tailnet_session(remote_server)

        status, _, body = local.get("/api/v1/trust")
        assert status == 200
        assert json.loads(body)["remote_scope"] == "read_only"
        assert json.loads(body)["remote_access"] == {"mode": "off", "origin": None}

        # The existing state-change gate still applies to the new control.
        status, _, body = local.send("PUT", SCOPE_ROUTE, {"scope": "full"}, csrf=False)
        assert status == 403 and b"csrf required" in body
        status, _, _ = local.send("PUT", SCOPE_ROUTE, {"scope": "CANARY"})
        assert status == 400
        status, _, _ = local.send("PUT", SCOPE_ROUTE, ["full"])
        assert status == 400
        assert read_remote_scope(home) == "read_only"

        status, _, body = local.send("PUT", SCOPE_ROUTE, {"scope": "full"})
        assert status == 200, body
        assert json.loads(body)["remote_scope"] == "full"
        assert read_remote_scope(home) == "full"

        # The running tailnet server reads the stored scope on each state change.
        status, _, body = remote.send("POST", "/api/v1/commands", LIVE_ARM)
        assert status == 403
        assert any(marker in body for marker in PAPER_ONLY), body
        assert _code(body) != "remote_read_only"
        status, _, body = remote.get("/api/v1/trust")
        assert json.loads(body)["remote_scope"] == "full"

        # Even under full, a tailnet session cannot change the scope.
        for scope in ("read_only", "full"):
            status, _, body = remote.send("PUT", SCOPE_ROUTE, {"scope": scope})
            assert (status, _code(body)) == (403, "remote_scope_loopback_only"), body
        assert read_remote_scope(home) == "full"

        status, _, body = local.send("PUT", SCOPE_ROUTE, {"scope": "read_only"})
        assert status == 200, body
        status, _, body = remote.send("POST", "/api/v1/commands", LIVE_ARM)
        assert (status, _code(body)) == (403, "remote_read_only")
        status, _, body = remote.send("PUT", SCOPE_ROUTE, {"scope": "full"})
        assert (status, _code(body)) == (403, "remote_read_only")

    assert read_remote_scope(home) == "read_only"


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"",
        b"full",
        b'"full"',
        b"not json",
        b'{"scope": "FULL"}',
        b'{"scope": "full "}',
        b'{"scope": true}',
        b'{"scope": null}',
        b'{"other": "full"}',
        b'["full"]',
        b"\xff\xfe\x00",
        b"[" * 100_000,
        "directory",
    ],
    ids=lambda value: "missing" if value is None else str(value)[:24],
)
def test_missing_or_unreadable_scope_reads_as_read_only(tmp_path: Path, content) -> None:
    home = tmp_path / "home"
    home.mkdir()
    record = home / "remote_access.json"
    if content == "directory":
        record.mkdir()
    elif content is not None:
        record.write_bytes(content)

    assert read_remote_scope(home) == "read_only"
    with _serve(tmp_path, home, tailnet=True) as server:
        remote = _tailnet_session(server)
        status, _, body = remote.get("/api/v1/trust")
        assert status == 200 and json.loads(body)["remote_scope"] == "read_only"
        status, _, body = remote.send("POST", "/api/v1/commands", LIVE_ARM)
        assert (status, _code(body)) == (403, "remote_read_only")


def test_stored_scope_round_trips_and_rejects_unknown_values(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert write_remote_scope(home, "full") == "full"
    assert read_remote_scope(home) == "full"
    assert json.loads((home / "remote_access.json").read_bytes()) == {"scope": "full"}
    for bad in ("FULL", "", "admin", None, True):
        with pytest.raises(ValueError):
            write_remote_scope(home, bad)  # type: ignore[arg-type]
    assert read_remote_scope(home) == "full"
    assert write_remote_scope(home, "read_only") == "read_only"
    assert read_remote_scope(home) == "read_only"


def test_loopback_session_is_unaffected_by_the_read_only_scope(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with _serve(tmp_path, home, tailnet=False) as server:
        local = _loopback_session(server)
        status, _, body = local.send("POST", "/api/v1/commands", LIVE_ARM)
        assert status == 403
        assert any(marker in body for marker in PAPER_ONLY), body
        assert _code(body) != "remote_read_only"
        status, _, body = local.send("PUT", "/api/v1/telemetry", {"enabled": False})
        assert status == 200, body
        status, _, body = local.send(
            "POST", "/api/v1/commands", {"command": "operations.kill", "payload": {"reason": "CANARY"}}
        )
        assert status == 200 and _code(body) == "kill_switch_engaged", body
    assert read_remote_scope(home) == "read_only"
    assert live_gate.kill_state(home).engaged is True


@pytest.mark.parametrize(
    "flag",
    [
        ["--remote-scope", "full"],
        ["--tailscale-scope", "full"],
        ["--tailscale-origin", ORIGIN, "--tailscale-user", USER, "--remote-scope", "full"],
    ],
)
def test_no_cli_flag_changes_the_remote_scope(tmp_path: Path, monkeypatch, capsys, flag) -> None:
    from krellbot import cli

    home = tmp_path / "home"
    monkeypatch.setenv("KRELLBOT_HOME", str(home))
    assert cli.cmd_workstation([*flag, "--dist", str(_dist(tmp_path))]) == 2
    assert "Unknown argument" in capsys.readouterr().err
    assert read_remote_scope(home) == "read_only"
    assert not (home / "remote_access.json").exists()
