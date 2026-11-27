"""AI connections: metadata on disk, credentials only in the OS keychain.

``<home>/ai/connections.json`` (0600) records which models the user connected
and which one is active. It never holds a credential. Each credential lives
in the keychain under service ``krellbot-ai:<id>``, username ``credential``,
a namespace separate from exchange keys. Connection ids are generated here
(16 lowercase hex characters) and every id from outside is checked against
that shape before it reaches a path or the keychain.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets as stdlib_secrets  # the standard library module, not krellbot.secrets
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlsplit

import keyring
import keyring.errors

try:
    from keyring.backends.chainer import ChainerBackend
except ImportError:  # pragma: no cover — present in every supported keyring release
    ChainerBackend = None  # type: ignore[assignment,misc]

from krellbot import paths
from krellbot.ai.anthropic_messages import AnthropicMessagesAdapter
from krellbot.ai.http import HttpTransport, UrllibTransport
from krellbot.ai.openai_compat import OpenAICompatAdapter
from krellbot.ai.providers import PRESETS, Format, ProviderPreset
from krellbot.ai.types import ChatAdapter

Auth = Literal["api_key", "oauth", "none"]
Capability = Literal["unprobed", "tools", "chat_only", "failed"]

KEYRING_SERVICE_PREFIX = "krellbot-ai:"
KEYRING_USERNAME = "credential"
CONNECTION_ID_RE = re.compile(r"[0-9a-f]{16}")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_SCHEMA_VERSION = "1"
_MAX_URL = 2048
_MAX_MODEL = 256
_MAX_LABEL = 80
_MAX_CREDENTIAL = 4096
# A sign-in's credential record (JSON: identity, client id, tokens, scopes, expiry), M38.23.
_MAX_CREDENTIAL_RECORD = 64 * 1024
_FORBIDDEN_URL_CHARS = frozenset("@?#; \\")

# Every store for one file shares a lock: routes run store writes in worker threads.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = os.path.abspath(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


REFUSAL_MESSAGES: dict[str, str] = {
    "ai_endpoint_insecure": (
        "Use an https:// endpoint. Plain http:// is allowed only for a model on this computer "
        "(127.0.0.1, ::1 or localhost)."
    ),
    "ai_endpoint_invalid": (
        "Enter the endpoint as a base URL with a host and no user name, password, query, parameters or fragment."
    ),
    "ai_endpoint_fixed": "This provider's endpoint is fixed. Use a custom endpoint to connect another address.",
    "ai_preset_unknown": "Choose one of the listed AI providers.",
    "ai_model_invalid": "Enter the model name the provider uses.",
    "ai_label_invalid": "Use a label of at most 80 characters with no control characters.",
    "ai_auth_invalid": "This sign-in method is not available for this provider.",
    "ai_credential_required": "This provider needs an API key.",
    "ai_credential_invalid": "That API key contains characters an API key cannot have.",
    "ai_credential_missing": "This connection's key is missing from the keychain. Remove the connection and add it again.",
    "ai_capability_invalid": "Unknown capability.",
    "ai_keychain_unavailable": "The OS keychain is not available, so the key was not stored or changed.",
    "ai_connection_not_found": "There is no AI connection with that id.",
    "ai_connections_unreadable": "The AI connections file could not be read, so it was left unchanged.",
    "ai_body_invalid": "The request body is not a valid AI connection request.",
    # Sign in with ChatGPT (M38.23).
    "ai_chatgpt_signin_required": (
        "This ChatGPT sign-in has ended. Choose Continue with ChatGPT for this connection to sign in again; "
        "the conversation is kept."
    ),
    "ai_chatgpt_plan_disabled": (
        "ChatGPT plan use was not allowed for this sign-in, so Krell cannot use it. Choose Continue with "
        "ChatGPT again and allow plan use, or connect an OpenAI API key instead."
    ),
    "ai_chatgpt_refresh_unavailable": (
        "OpenAI could not be reached to renew the ChatGPT sign-in. The sign-in is kept; try again later."
    ),
    "ai_chatgpt_client_invalid": (
        "OpenAI no longer accepts this ChatGPT registration. Remove the connection and choose Continue with "
        "ChatGPT to register again."
    ),
    "ai_chatgpt_state_unreadable": (
        "Krellbot's ChatGPT sign-in file could not be read, so nothing was changed. Fix or remove "
        "ai/chatgpt.json in the Krellbot home folder."
    ),
    "ai_chatgpt_not_chatgpt": "That connection is not a ChatGPT sign-in.",
    "ai_chatgpt_account_unknown": "That saved ChatGPT account is no longer known. Choose Continue with ChatGPT.",
}


class ConnectionRefused(Exception):
    """A typed refusal. ``message`` is a fixed string and never echoes input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = REFUSAL_MESSAGES[code]


@dataclass(frozen=True)
class Connection:
    id: str
    preset: str
    label: str
    format: Format
    base_url: str
    model: str
    auth: Auth
    capability: Capability
    created_at: str


def is_connection_id(value: object) -> bool:
    return isinstance(value, str) and CONNECTION_ID_RE.fullmatch(value) is not None


def validate_base_url(url: str) -> None:
    """Refuse anything but https, or plain http to this computer. Never raises anything but ConnectionRefused."""

    if not isinstance(url, str) or not url or len(url) > _MAX_URL or not url.isprintable():
        raise ConnectionRefused("ai_endpoint_invalid")
    # A user name, password, query or path parameter could carry a credential into
    # the metadata file; spaces and backslashes are never part of a base URL.
    if any(ch in url for ch in _FORBIDDEN_URL_CHARS):
        raise ConnectionRefused("ai_endpoint_invalid")
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        parts.port  # noqa: B018 — raises ValueError for a malformed port
    except ValueError:  # e.g. an unbalanced or non-IP "[...]" host
        raise ConnectionRefused("ai_endpoint_invalid") from None
    if scheme not in ("http", "https"):
        raise ConnectionRefused("ai_endpoint_insecure")
    if not host:
        raise ConnectionRefused("ai_endpoint_invalid")
    if scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ConnectionRefused("ai_endpoint_insecure")


def provider_host(base_url: str) -> str:
    try:
        return urlsplit(base_url).hostname or ""
    except ValueError:
        return ""


def check_endpoint(conn: Connection) -> ProviderPreset:
    """Re-check a connection's endpoint before use, so a hand-edited or corrupt record cannot widen the rules."""

    validate_base_url(conn.base_url)
    preset = PRESETS.get(conn.preset)
    if preset is None or preset.format != conn.format:
        raise ConnectionRefused("ai_preset_unknown")
    if not preset.custom and conn.base_url != preset.base_url:
        raise ConnectionRefused("ai_endpoint_fixed")
    return preset


def is_chatgpt(conn: Connection) -> bool:
    """A Sign in with ChatGPT connection: OAuth to OpenAI, served by the Responses API path."""

    return conn.auth == "oauth" and conn.preset == "openai"


def build_adapter(conn: Connection, credential: str | None, transport: HttpTransport) -> ChatAdapter:
    """The streaming adapter for ``conn``. For a ChatGPT sign-in, ``credential`` is its access token."""

    preset = check_endpoint(conn)
    if conn.format == "anthropic_messages":
        return AnthropicMessagesAdapter(conn.base_url, credential, transport)
    if is_chatgpt(conn):
        return OpenAICompatAdapter(conn.base_url, credential, transport, api="responses")
    return OpenAICompatAdapter(conn.base_url, credential, transport, token_limit_field=preset.token_limit_field)


def _service(conn_id: str) -> str:
    if not is_connection_id(conn_id):
        raise ConnectionRefused("ai_connection_not_found")
    return KEYRING_SERVICE_PREFIX + conn_id


# ---- chunked keychain values ----------------------------------------------------------
#
# Windows Credential Manager holds at most 2560 bytes (1280 UTF-16 characters)
# per secret, and a sign-in's credential record carries two JWTs and a refresh
# token. A value longer than _CHUNK_CHARS is stored in pieces under the SAME
# service: ``credential.<generation>.<i>`` hold the pieces and ``credential``
# holds a header naming the generation, the piece count and the total length.
# The generation is a random id made for each write (never derived from the
# value), and every piece starts with it. Pieces are written first and the
# header last; the previous generation's pieces are deleted only after the new
# value reads back whole. A reader requires every piece 1..N to carry the
# header's generation and the lengths to add up: anything else is a torn or
# stale write and fails closed (``TornSecret``), never a partial value. A
# short value (an API key) stays one plain ``credential`` entry, as before.
#
# A write that dies part way (or a best-effort delete that fails) could leave
# pieces no header names. So before a write touches any piece it records, in
# ``credential.pending``, the generation it is writing and the one it
# replaces (generation ids and counts only, nothing of the value). The next
# write, replacement or removal of that connection sweeps whichever of those
# generations the header does not name, then clears the marker; removal
# deletes the marker too.

_CHUNK_CHARS = 1000
_MAX_CHUNKS = 128
_CHUNK_HEADER_RE = re.compile(r"krellbot-chunked-v1:([0-9a-f]{32}):([1-9][0-9]{0,2}):([1-9][0-9]{0,6})")


_MARKER_USERNAME = f"{KEYRING_USERNAME}.pending"
_MARKER_PART_RE = re.compile(r"([0-9a-f]{32}):([1-9][0-9]{0,2})")


def _marker_value(new: tuple[str, int] | None, old: tuple[str, int] | None) -> str:
    def part(gen: tuple[str, int] | None) -> str:
        return "-" if gen is None else f"{gen[0]}:{gen[1]}"

    return f"krellbot-pending-v1 new={part(new)} old={part(old)}"


def _parse_marker(value: object) -> list[tuple[str, int]] | None:
    """The generations a marker names (new, then old), or None when it is not a marker."""

    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"krellbot-pending-v1 new=(\S+) old=(\S+)", value)
    if match is None:
        return None
    out = []
    for part in match.groups():
        if part == "-":
            continue
        found = _MARKER_PART_RE.fullmatch(part)
        if found is None or int(found.group(2)) > _MAX_CHUNKS:
            return None
        out.append((found.group(1), int(found.group(2))))
    return out


def _generation_names(generation: str, count: int) -> list[str]:
    return [f"{KEYRING_USERNAME}.{generation}.{index}" for index in range(1, count + 1)]


def _sweep_marked(backend: object, service: str, header_value: str | None) -> None:
    """Delete the pieces of every marked generation the header does not name, then the marker. Raises on a
    backend read failure; deletions are best effort."""

    raw = backend.get_password(service, _MARKER_USERNAME)  # type: ignore[attr-defined]
    if raw is None:
        return
    header = _parse_chunk_header(header_value)
    live = header[0] if header is not None else None
    for generation, count in _parse_marker(raw) or []:
        if generation != live:
            _delete_names(backend, service, _generation_names(generation, count))
    _delete_names(backend, service, [_MARKER_USERNAME])


def _sweep_quietly(backend: object, service: str) -> None:
    """Sweep against the header now in place, best effort (after a write committed or was undone)."""

    with contextlib.suppress(Exception):
        _sweep_marked(backend, service, backend.get_password(service, KEYRING_USERNAME))  # type: ignore[attr-defined]


class TornSecret(Exception):
    """A chunked keychain value whose pieces do not match its header: a torn or stale write."""


def _parse_chunk_header(value: object) -> tuple[str, int, int] | None:
    """``(generation, count, total_length)`` from a header, or None for a plain value."""

    match = _CHUNK_HEADER_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match.group(2)) > _MAX_CHUNKS:
        return None
    return match.group(1), int(match.group(2)), int(match.group(3))


def _chunk_names(header: tuple[str, int, int] | None) -> list[str]:
    if header is None:
        return []
    generation, count, _total = header
    return [f"{KEYRING_USERNAME}.{generation}.{index}" for index in range(1, count + 1)]


def _read_secret(backend: object, service: str) -> str | None:
    """A plain entry, or its pieces joined. Raises TornSecret when a piece is missing, carries another
    generation, or the lengths do not add up."""

    value = backend.get_password(service, KEYRING_USERNAME)  # type: ignore[attr-defined]
    header = _parse_chunk_header(value)
    if header is None:
        return value
    generation, _count, total = header
    prefix = generation + ":"
    parts = []
    for name in _chunk_names(header):
        part = backend.get_password(service, name)  # type: ignore[attr-defined]
        if not isinstance(part, str) or not part.startswith(prefix) or len(part) == len(prefix):
            raise TornSecret("a piece is missing or belongs to another write")
        parts.append(part[len(prefix) :])
    whole = "".join(parts)
    if len(whole) != total:
        raise TornSecret("the pieces do not add up to the recorded length")
    return whole


def _write_secret(backend: object, service: str, secret: str, previous: str | None) -> bool:
    """Write ``secret`` (chunked when long), header last. Raises on any backend failure, cleaning up its pieces.

    Returns True when it left an in-flight marker for the caller to sweep.
    """

    old = _parse_chunk_header(previous)
    old_generation = (old[0], old[1]) if old is not None else None
    if len(secret) <= _CHUNK_CHARS and _parse_chunk_header(secret) is None:
        if old_generation is not None:
            # The previous value's pieces become garbage once this commits: mark them first.
            backend.set_password(service, _MARKER_USERNAME, _marker_value(None, old_generation))  # type: ignore[attr-defined]
        backend.set_password(service, KEYRING_USERNAME, secret)  # type: ignore[attr-defined]
        return old_generation is not None
    pieces = [secret[i : i + _CHUNK_CHARS] for i in range(0, len(secret), _CHUNK_CHARS)]
    if len(pieces) > _MAX_CHUNKS:
        raise ValueError("value too long")
    # A random id for this write, never derived from the value.
    generation = stdlib_secrets.token_hex(16)
    while old is not None and generation == old[0]:
        generation = stdlib_secrets.token_hex(16)
    header = (generation, len(pieces), len(secret))
    names = _chunk_names(header)
    # Before any piece is written: which generation is in flight, and which it replaces.
    backend.set_password(service, _MARKER_USERNAME, _marker_value((generation, len(pieces)), old_generation))  # type: ignore[attr-defined]
    try:
        for name, piece in zip(names, pieces, strict=True):
            backend.set_password(service, name, f"{generation}:{piece}")  # type: ignore[attr-defined]
        backend.set_password(  # type: ignore[attr-defined]
            service, KEYRING_USERNAME, f"krellbot-chunked-v1:{generation}:{len(pieces)}:{len(secret)}"
        )
    except BaseException:
        _delete_names(backend, service, names)
        raise
    return True


def _delete_names(backend: object, service: str, names: list[str]) -> None:
    for name in names:
        with contextlib.suppress(Exception):  # best effort; a stale piece is unreadable without its header
            backend.delete_password(service, name)  # type: ignore[attr-defined]


def _delete_chunks(backend: object, service: str, header_value: str | None) -> None:
    """Delete the pieces a (previous) header names. Best effort."""

    _delete_names(backend, service, _chunk_names(_parse_chunk_header(header_value)))


def _restore_secret(backend: object, service: str, previous: str | None) -> None:
    """Put a replacement back as it was: the previous header (its pieces were never touched), or nothing."""

    current = None
    with contextlib.suppress(Exception):
        current = backend.get_password(service, KEYRING_USERNAME)  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        if previous is None:
            backend.delete_password(service, KEYRING_USERNAME)  # type: ignore[attr-defined]
        else:
            backend.set_password(service, KEYRING_USERNAME, previous)  # type: ignore[attr-defined]
    if current != previous:
        _delete_chunks(backend, service, current)


def keychain_is_safe(backend: object, *, allow_injected_fake_backend: bool = False) -> bool:
    """The exchange-key allow-list: a native OS keychain, verified by class identity.

    A chained backend (keyring's default when several native keychains exist,
    e.g. Secret Service and KWallet) passes only when every backend it chains
    passes. The in-tree test fake passes only with the tests-only opt-in.
    """

    from krellbot.keys_onboarding import _classify_backend

    if ChainerBackend is not None and type(backend) is ChainerBackend:
        try:
            chained = list(backend.backends)
        except Exception:  # noqa: BLE001 — discovery failure means unproven, so refused
            return False
        return bool(chained) and all(_classify_backend(b, allow_injected_fake_backend) for b in chained)
    return _classify_backend(backend, allow_injected_fake_backend)


def _clean_text(value: object, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_len or not value.isprintable():
        return None
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ConnectionStore:
    """Connections under ``<home>/ai``. Methods block on the keychain: async callers use a worker thread."""

    def __init__(
        self,
        home: Path,
        *,
        clock: Callable[[], str] | None = None,
        allow_injected_fake_backend: bool = False,
    ) -> None:
        self.home = Path(home)
        self._dir = self.home / "ai"
        self._path = self._dir / "connections.json"
        self._clock = clock or _now_iso
        # Tests-only: admit the in-tree FakeKeyring, as the exchange-key routes do.
        self._allow_fake = allow_injected_fake_backend
        self._lock = _lock_for(self._path)

    @property
    def allow_injected_fake_backend(self) -> bool:
        return self._allow_fake

    # ---- reads ------------------------------------------------------------

    def list(self) -> list[Connection]:
        return self._load()[0]

    def active(self) -> Connection | None:
        conns, active = self._load()
        return next((c for c in conns if c.id == active), None)

    def get(self, conn_id: str) -> Connection:
        if not is_connection_id(conn_id):
            raise ConnectionRefused("ai_connection_not_found")
        found = next((c for c in self.list() if c.id == conn_id), None)
        if found is None:
            raise ConnectionRefused("ai_connection_not_found")
        return found

    def credential(self, conn_id: str) -> str | None:
        """The stored value: a single entry, or its chunks reassembled.

        A torn or stale chunked value (a piece missing, from another write, or
        lengths that do not add up) fails closed as ``ai_keychain_unavailable``;
        a partial value is never returned.
        """

        service = _service(conn_id)
        try:
            return _read_secret(keyring.get_keyring(), service)
        except Exception:  # noqa: BLE001 — backend errors can carry arbitrary text; never surfaced
            raise ConnectionRefused("ai_keychain_unavailable") from None

    def credential_for(self, conn: Connection) -> str | None:
        """The credential a request for ``conn`` needs: None for ``auth="none"``, else it must exist."""

        if conn.auth == "none":
            return None
        value = self.credential(conn.id)
        if not value:
            raise ConnectionRefused("ai_credential_missing")
        return value

    def adapter(self, conn: Connection, transport: HttpTransport | None = None) -> ChatAdapter:
        check_endpoint(conn)  # before the keychain is touched
        if is_chatgpt(conn):
            # The access token comes from the sign-in's record, renewed near expiry. Imported here:
            # the sign-in module imports this one.
            from krellbot.ai.oauth import chatgpt_adapter

            return chatgpt_adapter(self, conn, transport=transport or UrllibTransport())
        return build_adapter(conn, self.credential_for(conn), transport or UrllibTransport())

    # ---- writes -----------------------------------------------------------

    def add(
        self,
        preset: str,
        *,
        model: str,
        credential: str | None,
        base_url: str | None = None,
        label: str | None = None,
        auth: Auth = "api_key",
    ) -> Connection:
        """Store a new connection, unprobed and inactive.

        On a custom endpoint a missing key means ``auth="none"`` (a local
        model needs none). Built-in providers always need a credential.
        """

        with self._lock:
            return self._add(preset, model, credential, base_url, label, auth)

    def _add(
        self,
        preset: str,
        model: str,
        credential: str | None,
        base_url: str | None,
        label: str | None,
        auth: Auth,
    ) -> Connection:
        conns, active = self._load()
        spec = PRESETS.get(preset) if isinstance(preset, str) else None
        if spec is None:
            raise ConnectionRefused("ai_preset_unknown")
        if base_url is not None and not isinstance(base_url, str):
            raise ConnectionRefused("ai_endpoint_invalid")
        if spec.custom:
            if base_url is None:
                raise ConnectionRefused("ai_endpoint_invalid")
            validate_base_url(base_url)
            url = base_url.rstrip("/")
            validate_base_url(url)
        else:
            if base_url is not None and base_url.rstrip("/") != spec.base_url:
                raise ConnectionRefused("ai_endpoint_fixed")
            url = spec.base_url
        clean_model = _clean_text(model, _MAX_MODEL)
        if clean_model is None:
            raise ConnectionRefused("ai_model_invalid")
        clean_label = spec.label if label is None or (isinstance(label, str) and not label.strip()) else label
        clean_label = _clean_text(clean_label, _MAX_LABEL)
        if clean_label is None:
            raise ConnectionRefused("ai_label_invalid")
        secret = self._clean_credential(credential)
        if auth not in get_args(Auth):
            raise ConnectionRefused("ai_auth_invalid")
        if auth == "oauth" and spec.signin == "none":
            raise ConnectionRefused("ai_auth_invalid")
        if auth == "api_key" and secret is None and spec.custom:
            auth = "none"
        if auth == "none" and secret is not None:
            raise ConnectionRefused("ai_auth_invalid")
        if auth != "none" and secret is None:
            raise ConnectionRefused("ai_credential_required")
        if auth == "none" and not spec.custom:
            raise ConnectionRefused("ai_credential_required")

        taken = {c.id for c in conns}
        conn_id = stdlib_secrets.token_hex(8)
        while conn_id in taken:
            conn_id = stdlib_secrets.token_hex(8)
        conn = Connection(
            id=conn_id,
            preset=spec.id,
            label=clean_label,
            format=spec.format,
            base_url=url,
            model=clean_model,
            auth=auth,
            capability="unprobed",
            created_at=self._clock(),
        )
        if secret is not None:
            self._store_secret(conn_id, secret)
        try:
            self._save([*conns, conn], active)
        except BaseException:
            self._forget_secret(conn_id, strict=False)
            raise
        return conn

    def add_record(self, preset: str, *, model: str, record: str, label: str | None = None) -> Connection:
        """Store a new ``oauth`` connection whose keychain entry is a sign-in's credential record.

        ``record`` is a JSON object (identity, client id, tokens, scopes,
        expiry); it goes only to the keychain, never to the metadata file.
        """

        clean = self._clean_record(record)
        with self._lock:
            conns, active = self._load()
            spec = PRESETS.get(preset) if isinstance(preset, str) else None
            if spec is None:
                raise ConnectionRefused("ai_preset_unknown")
            if spec.custom or spec.signin == "none":
                raise ConnectionRefused("ai_auth_invalid")
            clean_model = _clean_text(model, _MAX_MODEL)
            if clean_model is None:
                raise ConnectionRefused("ai_model_invalid")
            clean_label = _clean_text(spec.label if label is None else label, _MAX_LABEL)
            if clean_label is None:
                raise ConnectionRefused("ai_label_invalid")
            taken = {c.id for c in conns}
            conn_id = stdlib_secrets.token_hex(8)
            while conn_id in taken:
                conn_id = stdlib_secrets.token_hex(8)
            conn = Connection(
                id=conn_id,
                preset=spec.id,
                label=clean_label,
                format=spec.format,
                base_url=spec.base_url,
                model=clean_model,
                auth="oauth",
                capability="unprobed",
                created_at=self._clock(),
            )
            self._store_secret(conn_id, clean)
            try:
                self._save([*conns, conn], active)
            except BaseException:
                self._forget_secret(conn_id, strict=False)
                raise
            return conn

    def replace_record(self, conn_id: str, record: str) -> None:
        """Replace an ``oauth`` connection's credential record in one keychain write (a whole token set)."""

        clean = self._clean_record(record)
        with self._lock:
            found = next((c for c in self._load()[0] if c.id == conn_id), None) if is_connection_id(conn_id) else None
            if found is None:
                raise ConnectionRefused("ai_connection_not_found")
            if found.auth != "oauth":
                raise ConnectionRefused("ai_auth_invalid")
            self._store_secret(conn_id, clean, keep_on_failure=True)

    @staticmethod
    def _clean_record(record: object) -> str:
        if not isinstance(record, str) or not record or len(record) > _MAX_CREDENTIAL_RECORD:
            raise ConnectionRefused("ai_credential_invalid")
        try:
            parsed = json.loads(record)
        except (ValueError, RecursionError):
            raise ConnectionRefused("ai_credential_invalid") from None
        if not isinstance(parsed, dict):
            raise ConnectionRefused("ai_credential_invalid")
        return record

    def remove(self, conn_id: str) -> None:
        """Delete the keychain entry first, so a failure leaves a keyed connection listed and removable.

        A keyless connection still gets a best-effort delete (the record's
        ``auth`` is not proof that no entry exists), but a keychain failure
        never blocks its removal.
        """

        with self._lock:
            conns, active = self._load()
            found = next((c for c in conns if c.id == conn_id), None) if is_connection_id(conn_id) else None
            if found is None:
                raise ConnectionRefused("ai_connection_not_found")
            self._forget_secret(conn_id, strict=found.auth != "none")
            self._save([c for c in conns if c.id != conn_id], None if active == conn_id else active)

    def activate(self, conn_id: str) -> Connection:
        with self._lock:
            conns, _active = self._load()
            found = next((c for c in conns if c.id == conn_id), None) if is_connection_id(conn_id) else None
            if found is None:
                raise ConnectionRefused("ai_connection_not_found")
            self._save(conns, conn_id)
            return found

    def set_capability(self, conn_id: str, cap: Capability) -> Connection:
        if cap not in get_args(Capability):
            raise ConnectionRefused("ai_capability_invalid")
        with self._lock:
            conns, active = self._load()
            found = next((c for c in conns if c.id == conn_id), None) if is_connection_id(conn_id) else None
            if found is None:
                raise ConnectionRefused("ai_connection_not_found")
            updated = replace(found, capability=cap)
            self._save([updated if c.id == conn_id else c for c in conns], active)
            return updated

    # ---- keychain ---------------------------------------------------------

    @staticmethod
    def _clean_credential(credential: str | None) -> str | None:
        if credential is None:
            return None
        if not isinstance(credential, str):
            raise ConnectionRefused("ai_credential_invalid")
        value = credential.strip()
        if not value:
            return None
        # Printable ASCII without spaces: the key goes into an HTTP header.
        if len(value) > _MAX_CREDENTIAL or any(not ("!" <= ch <= "~") for ch in value):
            raise ConnectionRefused("ai_credential_invalid")
        return value

    def _store_secret(self, conn_id: str, secret: str, *, keep_on_failure: bool = False) -> None:
        """Write and read back (chunked when long). A failed new entry is deleted; a failed replacement
        (``keep_on_failure``) is put back as it was."""

        service = _service(conn_id)
        backend = keyring.get_keyring()
        if not keychain_is_safe(backend, allow_injected_fake_backend=self._allow_fake):
            raise ConnectionRefused("ai_keychain_unavailable")
        previous: str | None = None
        if keep_on_failure:
            # A replacement: the value it replaces names the chunks to delete, or to keep on failure.
            # A new connection's id is fresh, so it has no previous value to read.
            try:
                previous = backend.get_password(service, KEYRING_USERNAME)
                # A write that died part way last time: sweep the pieces no header names.
                _sweep_marked(backend, service, previous)
            except Exception:  # noqa: BLE001 — backend errors can carry arbitrary text; never surfaced
                raise ConnectionRefused("ai_keychain_unavailable") from None
        marked = False
        try:
            marked = _write_secret(backend, service, secret, previous)
            stored = _read_secret(backend, service)
        except Exception:  # noqa: BLE001
            stored = None
            marked = True  # a chunked write may have marked before failing; the sweep below is cheap
        if stored != secret:
            if keep_on_failure:
                _restore_secret(backend, service, previous)
            else:
                self._forget_secret(conn_id, strict=False)
            if marked:
                _sweep_quietly(backend, service)
            raise ConnectionRefused("ai_keychain_unavailable")
        # Only now that the new value reads back whole: drop the previous generation's chunks.
        _delete_chunks(backend, service, previous)
        if marked:
            _sweep_quietly(backend, service)

    @staticmethod
    def _forget_secret(conn_id: str, *, strict: bool) -> None:
        """Delete the entry and any chunks under its service."""

        service = _service(conn_id)
        backend = keyring.get_keyring()
        try:
            header = backend.get_password(service, KEYRING_USERNAME)
        except Exception:  # noqa: BLE001
            if strict:
                raise ConnectionRefused("ai_keychain_unavailable") from None
            header = None
        marker = None
        if _parse_chunk_header(header) is not None or strict:
            # A marker exists only beside chunked values; a plain entry's removal needs no extra read.
            try:
                marker = backend.get_password(service, _MARKER_USERNAME)
            except Exception:  # noqa: BLE001
                if strict:
                    raise ConnectionRefused("ai_keychain_unavailable") from None
        try:
            backend.delete_password(service, KEYRING_USERNAME)
        except keyring.errors.PasswordDeleteError:
            pass  # usually "not found"; absence is checked below
        except Exception:  # noqa: BLE001
            if strict:
                raise ConnectionRefused("ai_keychain_unavailable") from None
            return
        # The header's pieces, every marked generation's pieces (an interrupted write), and the marker.
        marked = [name for gen, count in _parse_marker(marker) or [] for name in _generation_names(gen, count)]
        chunks = _chunk_names(_parse_chunk_header(header)) + marked + ([_MARKER_USERNAME] if marker is not None else [])
        for name in chunks:
            try:
                backend.delete_password(service, name)
            except keyring.errors.PasswordDeleteError:
                pass
            except Exception:  # noqa: BLE001
                if strict:
                    raise ConnectionRefused("ai_keychain_unavailable") from None
        if not strict:
            return
        try:
            leftovers = [backend.get_password(service, name) for name in (KEYRING_USERNAME, *chunks)]
        except Exception:  # noqa: BLE001
            raise ConnectionRefused("ai_keychain_unavailable") from None
        if any(value is not None for value in leftovers):
            raise ConnectionRefused("ai_keychain_unavailable")

    # ---- file -------------------------------------------------------------

    def _load(self) -> tuple[list[Connection], str | None]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return [], None
        except OSError:
            raise ConnectionRefused("ai_connections_unreadable") from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise ConnectionRefused("ai_connections_unreadable") from None
        if not isinstance(data, dict) or not isinstance(data.get("connections"), list):
            raise ConnectionRefused("ai_connections_unreadable")
        conns: list[Connection] = []
        for record in data["connections"]:
            conn = _connection_from_record(record)
            if conn is not None and all(c.id != conn.id for c in conns):
                conns.append(conn)
        active = data.get("active")
        if not any(c.id == active for c in conns):
            active = None
        return conns, active

    def _save(self, conns: list[Connection], active: str | None) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self._dir, 0o700)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "active": active,
            "connections": [asdict(c) for c in conns],
        }
        paths.atomic_write(self._path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _connection_from_record(record: object) -> Connection | None:
    """A stored record, re-validated; anything off is dropped rather than trusted."""

    if not isinstance(record, dict):
        return None
    try:
        conn = Connection(**{name: record[name] for name in Connection.__dataclass_fields__})
    except (KeyError, TypeError):
        return None
    spec = PRESETS.get(conn.preset) if isinstance(conn.preset, str) else None
    if (
        spec is None
        or not is_connection_id(conn.id)
        or conn.format != spec.format
        or conn.auth not in get_args(Auth)
        or conn.capability not in get_args(Capability)
        or _clean_text(conn.model, _MAX_MODEL) != conn.model
        or _clean_text(conn.label, _MAX_LABEL) != conn.label
        or not isinstance(conn.created_at, str)
        or (not spec.custom and conn.base_url != spec.base_url)
    ):
        return None
    try:
        validate_base_url(conn.base_url)
    except ConnectionRefused:
        return None
    return conn
