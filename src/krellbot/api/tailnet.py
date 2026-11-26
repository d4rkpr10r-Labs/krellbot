"""Opt-in login behind Tailscale Serve, never a public listener.

Serve authenticates the peer and replaces inbound Tailscale-User-Login headers.
Only the explicitly configured owner and HTTPS origin are admitted here. The
backend must remain loopback-only; local processes are already trusted by the
ordinary workstation's bootstrap model.

A tailnet session has a scope stored in the home (D14): ``read_only``, the
default, or ``full``. Under ``read_only`` this middleware refuses every state
change (any method but GET and HEAD) with 403 ``remote_read_only`` before it
reaches a route. Anything other than a well-formed stored ``full`` reads as
``read_only``. The scope is written only by a loopback session's
state-change-gated route, never by a tailnet session or a CLI flag.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from krellbot import paths as kb_paths
from krellbot.api.security import generate_csrf_token, generate_session_token

RemoteScope = Literal["read_only", "full"]

REMOTE_SCOPE_FILE = "remote_access.json"
REMOTE_SCOPES: tuple[RemoteScope, ...] = ("read_only", "full")
DEFAULT_REMOTE_SCOPE: RemoteScope = "read_only"
# Methods a read-only tailnet session may use. Everything else is a state change.
READ_METHODS = frozenset({"GET", "HEAD"})
CODE_REMOTE_READ_ONLY = "remote_read_only"
_REMOTE_READ_ONLY_MESSAGE = (
    "Tailscale sessions are read-only. Change this in Settings from a local session on this computer."
)
# A scope record is a few bytes; anything larger is not one.
_MAX_SCOPE_RECORD_BYTES = 4096


def remote_scope_path(home: Path) -> Path:
    return Path(home) / REMOTE_SCOPE_FILE


def read_remote_scope(home: Path) -> RemoteScope:
    """Return the stored tailnet scope; anything but a stored ``full`` is ``read_only``.

    A missing, unreadable, oversized or malformed record never widens the
    scope.
    """

    try:
        with open(remote_scope_path(home), "rb") as handle:
            raw = handle.read(_MAX_SCOPE_RECORD_BYTES + 1)
        if len(raw) > _MAX_SCOPE_RECORD_BYTES:
            return DEFAULT_REMOTE_SCOPE
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return DEFAULT_REMOTE_SCOPE
    if isinstance(data, dict) and data.get("scope") == "full":
        return "full"
    return DEFAULT_REMOTE_SCOPE


def write_remote_scope(home: Path, scope: RemoteScope) -> RemoteScope:
    """Store the tailnet scope atomically (owner-only file). Raise on an unknown scope."""

    if not isinstance(scope, str) or scope not in REMOTE_SCOPES:
        raise ValueError("scope must be 'read_only' or 'full'")
    root = Path(home)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (json.dumps({"scope": scope}, sort_keys=True) + "\n").encode("utf-8")
    kb_paths.atomic_write(remote_scope_path(root), payload)
    return scope


@dataclass(frozen=True)
class TailnetAccess:
    origin: str
    user: str

    @classmethod
    def from_options(cls, origin: str | None, user: str | None) -> TailnetAccess | None:
        if origin is None and user is None:
            return None
        if not origin or not user:
            raise ValueError("--tailscale-origin and --tailscale-user must be supplied together")
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".ts.net")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("--tailscale-origin must be an HTTPS .ts.net origin with no path")
        # Validate any explicit port before the listener is created.
        _ = parsed.port
        return cls(origin, user)

    def install(self, app: FastAPI, sessions: dict[str, str], *, port: int, dist_dir: Path, home: Path) -> None:
        expected_host = urlsplit(self.origin).netloc
        home = Path(home)
        no_store = {"Cache-Control": "no-store"}

        @app.middleware("http")
        async def tailscale_login(request: Request, call_next):
            # Uvicorn proxy_headers is disabled: client is the actual TCP peer,
            # not an untrusted X-Forwarded-For value.
            if (
                request.client is None
                or request.client.host not in {"127.0.0.1", "::1"}
                or request.headers.get("host") != expected_host
                or request.headers.get("tailscale-user-login") != self.user
            ):
                return JSONResponse({"detail": "Tailscale owner login required"}, status_code=403, headers=no_store)
            origin = request.headers.get("origin")
            if origin is not None and origin != self.origin:
                return JSONResponse({"detail": "origin not allowed"}, status_code=403, headers=no_store)
            path = request.url.path
            if path == "/login":
                if request.method == "GET":
                    styles = "".join(
                        f'<link rel="stylesheet" href="/assets/{html.escape(css.name, quote=True)}">'
                        for css in sorted((dist_dir / "assets").glob("*.css"))
                    )
                    return HTMLResponse(
                        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width, initial-scale=1">'
                        f"<title>Log in · Krellbot</title>{styles}</head>"
                        '<body style="background:var(--navy,#0E1C29);color:var(--text,#E9F0F5);'
                        'font-family:Manrope,system-ui,sans-serif">'
                        '<main style="max-width:32rem;margin:12vh auto;padding:2rem;'
                        'background:var(--surface,#14222E);border-radius:12px">'
                        "<h1>Krellbot</h1><p>Your private paper workstation.</p>"
                        "<p>Use the owner account already signed in on this device in Tailscale. "
                        "No separate Krellbot password or exchange key is needed.</p>"
                        '<form method="post" action="/login"><button type="submit" '
                        'style="min-height:44px;padding:0.75rem 1rem;background:var(--sage,#B3DDA8);'
                        'color:var(--navy,#0E1C29);border-radius:7px;font:inherit">'
                        "Log in with Tailscale</button></form></main></body></html>",
                        headers={
                            **no_store,
                            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
                        },
                    )
                if request.method != "POST" or origin != self.origin:
                    return JSONResponse({"detail": "same-origin login required"}, status_code=403, headers=no_store)
                session_id = generate_session_token()
                sessions[session_id] = generate_csrf_token()
                response = RedirectResponse("/", status_code=303, headers=no_store)
                response.set_cookie(
                    "krellbot_session", session_id, httponly=True, secure=True, samesite="strict", path="/"
                )
                return response
            if path == "/api/v1/session/bootstrap":
                return JSONResponse({"detail": "use Tailscale login"}, status_code=403, headers=no_store)
            if request.cookies.get("krellbot_session") not in sessions and not path.startswith("/assets/"):
                if path.startswith("/api/") or request.method != "GET":
                    return JSONResponse({"detail": "session required"}, status_code=401, headers=no_store)
                return RedirectResponse("/login", status_code=303, headers=no_store)
            # D14: the one read-only gate for tailnet sessions. It runs after the
            # checks above and before any route, so no state-changing handler is
            # reached. The scope is read per request so a change made from a
            # loopback session applies without a restart.
            if request.method not in READ_METHODS and read_remote_scope(home) != "full":
                return JSONResponse(
                    {
                        "code": CODE_REMOTE_READ_ONLY,
                        "ok": False,
                        "effect": "refused",
                        "message": _REMOTE_READ_ONLY_MESSAGE,
                        "detail": _REMOTE_READ_ONLY_MESSAGE,
                    },
                    status_code=403,
                    headers=no_store,
                )
            # Normalize only AFTER peer, identity, Host and Origin validation.
            # The existing session/CSRF/live gates remain unchanged downstream.
            request.scope["headers"] = [
                (key, f"127.0.0.1:{port}".encode())
                if key == b"host"
                else (key, f"http://127.0.0.1:{port}".encode())
                if key == b"origin"
                else (key, value)
                for key, value in request.scope["headers"]
            ]
            return await call_next(request)
