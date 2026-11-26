"""The trust snapshot says where each venue's credentials come from.

``trust_snapshot`` reports a per-venue ``credential_source`` (``keychain``,
``env_override`` or ``keyfile_fallback``) from environment variable *names*
alone. It never reads a credential, a key file or a keyring entry, and it
never returns a value or a key file path. Each kind is cross-checked against
``secrets.get``'s real precedence so the disclosure stays true.

``GET /api/v1/trust`` adds ``bind_measured`` (the local address the request
actually arrived on) and ``remote_access`` (Tailscale Serve state). The
literal ``trust.bind`` stays ``127.0.0.1``.

Fake keyring, isolated home, no credentials, no network beyond loopback.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import sys
from pathlib import Path

import pytest

from krellbot import secrets as kb_secrets
from krellbot.api.serve import WorkstationServer
from krellbot.trust import trust_snapshot

TEST_PORT = 18766
BOOT = "boot-credential-source"

ENV_KEY = "env-override-key-sentinel"
ENV_SECRET = "env-override-secret-sentinel"
FILE_KEY = "keyfile-key-sentinel"
FILE_SECRET = "keyfile-secret-sentinel"
RING_KEY = "keyring-key-sentinel"
RING_SECRET = "keyring-secret-sentinel"
KEYFILE_DIR = "private-keyfile-dir-sentinel"

TAILNET_ORIGIN = "https://workstation.example.ts.net"
TAILNET_USER = "owner-login-sentinel@example.com"

_SENTINELS = (ENV_KEY, ENV_SECRET, FILE_KEY, FILE_SECRET, RING_KEY, RING_SECRET, KEYFILE_DIR)


@pytest.fixture(autouse=True)
def _no_credential_env(monkeypatch):
    for venue in kb_secrets.VENUES:
        ven = venue.upper()
        for suffix in ("KEY", "SECRET", "KEYFILE"):
            monkeypatch.delenv(f"KRELLBOT_{ven}_{suffix}", raising=False)


def _assert_no_sentinel(blob: str) -> None:
    for sentinel in _SENTINELS:
        assert sentinel not in blob


def _write_keyfile(tmp_path: Path) -> Path:
    folder = tmp_path / KEYFILE_DIR
    folder.mkdir()
    keyfile = folder / "kraken.keys"
    keyfile.write_text(f"{FILE_KEY}\n{FILE_SECRET}\n", encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(keyfile, 0o600)
    return keyfile


# ---- trust_snapshot ------------------------------------------------------


def test_every_venue_reads_the_keychain_when_no_override_is_set(home: Path, fresh_keyring) -> None:
    del fresh_keyring
    snap = trust_snapshot(home)
    assert snap["credential_source"] == {venue: "keychain" for venue in sorted(kb_secrets.VENUES)}
    assert list(snap["credential_source"]) == sorted(kb_secrets.VENUES)


def test_env_key_and_secret_report_env_override_and_win_over_the_keychain(
    home: Path, fresh_keyring, monkeypatch
) -> None:
    del fresh_keyring
    kb_secrets.store("kraken", RING_KEY, RING_SECRET)
    monkeypatch.setenv("KRELLBOT_KRAKEN_KEY", ENV_KEY)
    monkeypatch.setenv("KRELLBOT_KRAKEN_SECRET", ENV_SECRET)

    snap = trust_snapshot(home)

    assert snap["credential_source"]["kraken"] == "env_override"
    assert snap["credential_source"]["coinbase"] == "keychain"
    # The disclosure is true: secrets.get uses the environment, not the keychain.
    assert kb_secrets.get("kraken") == (ENV_KEY, ENV_SECRET)
    _assert_no_sentinel(json.dumps(snap))


def test_one_env_variable_alone_still_overrides_the_keychain(home: Path, fresh_keyring, monkeypatch) -> None:
    del fresh_keyring
    kb_secrets.store("coinbase", RING_KEY, RING_SECRET)
    monkeypatch.setenv("KRELLBOT_COINBASE_SECRET", ENV_SECRET)

    snap = trust_snapshot(home)

    assert snap["credential_source"]["coinbase"] == "env_override"
    # secrets.get refuses the partial pair and never falls back to the keychain.
    with pytest.raises(ValueError):
        kb_secrets.get("coinbase")
    _assert_no_sentinel(json.dumps(snap))


def test_keyfile_reports_fallback_without_its_path_or_contents(
    home: Path, tmp_path: Path, fresh_keyring, monkeypatch
) -> None:
    del fresh_keyring
    keyfile = _write_keyfile(tmp_path)
    monkeypatch.setenv("KRELLBOT_KRAKEN_KEYFILE", str(keyfile))

    snap = trust_snapshot(home)

    assert snap["credential_source"]["kraken"] == "keyfile_fallback"
    blob = json.dumps(snap)
    assert str(keyfile) not in blob
    assert "kraken.keys" not in blob
    _assert_no_sentinel(blob)
    if sys.platform != "win32":
        # The plaintext file is read only when the keychain has no entry ...
        assert kb_secrets.get("kraken") == (FILE_KEY, FILE_SECRET)
        # ... and the keychain still wins once it holds one: a fallback.
        kb_secrets.store("kraken", RING_KEY, RING_SECRET)
        assert kb_secrets.get("kraken") == (RING_KEY, RING_SECRET)
        assert trust_snapshot(home)["credential_source"]["kraken"] == "keyfile_fallback"


def test_env_pair_outranks_a_configured_keyfile(home: Path, tmp_path: Path, fresh_keyring, monkeypatch) -> None:
    del fresh_keyring
    monkeypatch.setenv("KRELLBOT_KRAKEN_KEYFILE", str(_write_keyfile(tmp_path)))
    monkeypatch.setenv("KRELLBOT_KRAKEN_KEY", ENV_KEY)
    monkeypatch.setenv("KRELLBOT_KRAKEN_SECRET", ENV_SECRET)

    assert trust_snapshot(home)["credential_source"]["kraken"] == "env_override"
    assert kb_secrets.get("kraken") == (ENV_KEY, ENV_SECRET)


def test_empty_keyfile_variable_is_ignored_like_secrets_get(home: Path, fresh_keyring, monkeypatch) -> None:
    del fresh_keyring
    monkeypatch.setenv("KRELLBOT_KRAKEN_KEYFILE", "")

    assert trust_snapshot(home)["credential_source"]["kraken"] == "keychain"
    with pytest.raises(FileNotFoundError):
        kb_secrets.get("kraken")


def test_credential_source_never_reads_a_credential(home: Path, tmp_path: Path, monkeypatch) -> None:
    import keyring

    def _refuse(*_args, **_kwargs):
        raise AssertionError("trust_snapshot must not read a credential")

    monkeypatch.setattr(keyring, "get_password", _refuse)
    monkeypatch.setattr(kb_secrets, "get", _refuse)
    monkeypatch.setattr(kb_secrets, "_read_keyfile_lines", _refuse)
    # A key file that cannot be read at all: only the variable's presence counts.
    monkeypatch.setenv("KRELLBOT_KRAKEN_KEYFILE", str(tmp_path / "missing" / "kraken.keys"))
    monkeypatch.setenv("KRELLBOT_BINANCE_KEY", ENV_KEY)
    monkeypatch.setenv("KRELLBOT_BINANCE_SECRET", ENV_SECRET)

    sources = trust_snapshot(home)["credential_source"]

    assert sources["kraken"] == "keyfile_fallback"
    assert sources["binance"] == "env_override"
    assert set(sources.values()) <= {"keychain", "env_override", "keyfile_fallback"}


# ---- GET /api/v1/trust over ASGI -------------------------------------------


def _asgi_get(app, path: str, headers: list[tuple[str, str]], *, server) -> tuple[int, bytes, list[str]]:
    return _asgi(app, "GET", path, b"", headers, server=server)


def _asgi(app, method: str, path: str, body: bytes, headers, *, server) -> tuple[int, bytes, list[str]]:
    sent = {"done": False}
    captured: dict[str, object] = {"status": 500, "headers": [], "body": bytearray()}

    async def receive() -> dict[str, object]:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            captured["status"] = int(message["status"])
            captured["headers"] = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            captured["body"] = captured["body"] + message.get("body", b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "server": server,
        "client": ("127.0.0.1", 40000),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    cookies = [v.decode("latin-1") for k, v in captured["headers"] if k.decode("latin-1").lower() == "set-cookie"]
    return int(captured["status"]), bytes(captured["body"]), cookies


def _session(app, *, server) -> list[tuple[str, str]]:
    body = json.dumps({"token": BOOT}).encode("utf-8")
    status, raw, cookies = _asgi(
        app,
        "POST",
        "/api/v1/session/bootstrap",
        body,
        [
            ("Host", f"127.0.0.1:{TEST_PORT}"),
            ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ],
        server=server,
    )
    assert status == 200, raw
    cookie = next(c for c in cookies if c.startswith("krellbot_session="))
    return [
        ("Host", f"127.0.0.1:{TEST_PORT}"),
        ("Origin", f"http://127.0.0.1:{TEST_PORT}"),
        ("Cookie", cookie.split(";", 1)[0]),
    ]


def _trust_over_asgi(home: Path, server) -> tuple[int, bytes]:
    from krellbot.api.app import create_app

    app = create_app(home=home, port=TEST_PORT, bootstrap_token=BOOT)
    headers = _session(app, server=("127.0.0.1", TEST_PORT))
    status, body, _ = _asgi_get(app, "/api/v1/trust", headers, server=server)
    return status, body


def test_trust_route_serves_sources_measured_bind_and_remote_access(home: Path, fresh_keyring, monkeypatch) -> None:
    del fresh_keyring
    monkeypatch.setenv("KRELLBOT_KRAKEN_KEY", ENV_KEY)
    monkeypatch.setenv("KRELLBOT_KRAKEN_SECRET", ENV_SECRET)

    status, body = _trust_over_asgi(home, ("127.0.0.1", TEST_PORT))

    assert status == 200, body
    decoded = json.loads(body)
    assert decoded["trust"]["bind"] == "127.0.0.1"
    assert decoded["trust"]["credential_source"]["kraken"] == "env_override"
    assert decoded["trust"]["credential_source"]["coinbase"] == "keychain"
    assert decoded["bind_measured"] == "127.0.0.1"
    assert decoded["remote_access"] == {"mode": "off", "origin": None}
    assert decoded["keys_leave_machine"] is False
    _assert_no_sentinel(body.decode("utf-8"))


def test_measured_bind_is_the_arrival_address_not_the_literal(home: Path, fresh_keyring) -> None:
    del fresh_keyring
    status, body = _trust_over_asgi(home, ("192.0.2.10", TEST_PORT))
    assert status == 200, body
    decoded = json.loads(body)
    assert decoded["bind_measured"] == "192.0.2.10"
    assert decoded["trust"]["bind"] == "127.0.0.1"

    status, body = _trust_over_asgi(home, ("testserver", 80))
    assert status == 200, body
    assert json.loads(body)["bind_measured"] is None

    status, body = _trust_over_asgi(home, None)
    assert status == 200, body
    assert json.loads(body)["bind_measured"] is None


def test_trust_route_keeps_its_session_gate(home: Path, fresh_keyring) -> None:
    del fresh_keyring
    from krellbot.api.app import create_app

    app = create_app(home=home, port=TEST_PORT, bootstrap_token=BOOT)
    status, body, _ = _asgi_get(
        app,
        "/api/v1/trust",
        [("Host", f"127.0.0.1:{TEST_PORT}"), ("Origin", f"http://127.0.0.1:{TEST_PORT}")],
        server=("127.0.0.1", TEST_PORT),
    )
    assert status == 403
    assert b"session required" in body
    assert b"bind_measured" not in body


# ---- real loopback listener ----------------------------------------------


def _http(server: WorkstationServer, method: str, path: str, headers: dict[str, str], payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=5)
    try:
        conn.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><head></head><body>Workstation</body></html>")
    return dist


def test_real_loopback_listener_measures_its_bind(tmp_path: Path) -> None:
    server = WorkstationServer(home=tmp_path / "home", dist_dir=_dist(tmp_path))
    server.start()
    try:
        local = {"Host": f"127.0.0.1:{server.bound_port}", "Origin": f"http://127.0.0.1:{server.bound_port}"}
        status, headers, body = _http(
            server,
            "POST",
            "/api/v1/session/bootstrap",
            {**local, "Content-Type": "application/json"},
            {"token": server.bootstrap_token},
        )
        assert status == 200, body
        cookie = headers["set-cookie"].split(";", 1)[0]
        status, _, body = _http(server, "GET", "/api/v1/trust", {**local, "Cookie": cookie})
    finally:
        server.stop()
    assert status == 200, body
    decoded = json.loads(body)
    assert decoded["bind_measured"] == "127.0.0.1"
    assert decoded["trust"]["bind"] == "127.0.0.1"
    assert decoded["remote_access"] == {"mode": "off", "origin": None}


def test_tailscale_session_sees_remote_access_without_the_owner_login(tmp_path: Path) -> None:
    server = WorkstationServer(
        home=tmp_path / "home",
        dist_dir=_dist(tmp_path),
        tailscale_origin=TAILNET_ORIGIN,
        tailscale_user=TAILNET_USER,
    )
    server.start()
    try:
        remote = {"Host": "workstation.example.ts.net", "Tailscale-User-Login": TAILNET_USER}
        status, headers, _ = _http(server, "POST", "/login", {**remote, "Origin": TAILNET_ORIGIN})
        assert status == 303
        cookie = headers["set-cookie"].split(";", 1)[0]
        status, _, body = _http(server, "GET", "/api/v1/trust", {**remote, "Cookie": cookie})
    finally:
        server.stop()
    assert status == 200, body
    decoded = json.loads(body)
    assert decoded["remote_access"] == {"mode": "tailscale_serve", "origin": TAILNET_ORIGIN}
    # Serve proxies to the loopback listener; the backend never widens its bind.
    assert decoded["bind_measured"] == "127.0.0.1"
    assert decoded["trust"]["bind"] == "127.0.0.1"
    assert TAILNET_USER.encode() not in body
