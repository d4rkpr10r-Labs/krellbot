"""List the models an AI provider offers, so people pick one instead of typing an API name.

One listing call per request, through the same ``HttpTransport`` seam and error
mapping the adapters use. The credential (a stored key, a ChatGPT access token or
a draft key typed in setup) goes only into the request header: nothing here logs
it, stores it or returns it, and every error is a fixed string.

* OpenAI-compatible presets: ``GET {base}/models`` (``data[].id``). OpenRouter's
  ``context_length`` and other servers' ``context_window`` are read when present.
* Anthropic: ``GET {base}/v1/models`` (``data[].id``, ``display_name``,
  ``max_input_tokens``), following ``has_more`` / ``last_id`` up to a page cap.
* ChatGPT sign-in: the same models endpoint with the sign-in token. A refusal of
  that token is ``chatgpt_models_not_allowed``, so the UI falls back to a typed ID.

An endpoint with no list answers ``listed: false`` and an empty list. ``context_window``
is the helper the chat uses to size its history: a small in-memory cache per
connection, filled by the same listing call.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from krellbot.ai.connections import Connection, ConnectionStore, check_endpoint, is_chatgpt
from krellbot.ai.http import HttpTransport, TransportError, UrllibTransport, error_from_transport
from krellbot.ai.providers import ANTHROPIC_VERSION, Format
from krellbot.ai.types import ProviderError

REASON_NO_MODEL_LIST = "no_model_list"
REASON_CHATGPT_NOT_ALLOWED = "chatgpt_models_not_allowed"

_TIMEOUT = 15.0
_MAX_PAGES = 10
_PAGE_SIZE = 1000
_MAX_MODELS = 2000
_MAX_ID = 256
_MAX_NAME = 200
_MAX_CONTEXT = 100_000_000
# Ids with any of these are not chat models. Matched case-insensitively.
NON_CHAT_MARKERS = (
    "embed",
    "tts",
    "whisper",
    "dall-e",
    "moderation",
    "transcribe",
    "image",
    "audio",
    "realtime",
    "search",
)
_CONTEXT_FIELDS = ("context_length", "context_window", "max_input_tokens")
# Statuses that mean "this server has no model list" rather than "something is wrong".
_NO_LIST_STATUSES = frozenset({404, 405, 410, 501})


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str | None = None
    context_window: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "display_name": self.display_name, "context_window": self.context_window}


@dataclass(frozen=True)
class ModelListing:
    """``listed`` is False when there is no list to show; ``reason`` says why, or ``error`` holds a provider failure."""

    models: list[ModelInfo]
    listed: bool
    reason: str | None = None
    error: ProviderError | None = None


def is_chat_model_id(model_id: str) -> bool:
    lowered = model_id.lower()
    return not any(marker in lowered for marker in NON_CHAT_MARKERS)


def filter_chat_models(models: list[ModelInfo]) -> list[ModelInfo]:
    """Drop obvious non-chat models and sort by id."""

    return sorted((m for m in models if is_chat_model_id(m.id)), key=lambda m: m.id)


def _clean_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= _MAX_ID and value.isprintable() else None


def _clean_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= _MAX_NAME and value.isprintable() else None


def _context(item: dict) -> int | None:
    for field in _CONTEXT_FIELDS:
        value = item.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= _MAX_CONTEXT:
            return value
    return None


def _entries(payload: Any) -> list[tuple[Any, dict]] | None:
    """(id, item) pairs from an OpenAI/Anthropic-shaped list or a ChatGPT plan list; None when neither."""

    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, list):
        return [(item.get("id"), item) for item in data if isinstance(item, dict)]
    models = payload.get("models")
    if isinstance(models, list):  # the ChatGPT plan list: only entries marked for listing
        return [
            (item.get("slug"), item) for item in models if isinstance(item, dict) and item.get("visibility") == "list"
        ]
    return None


def parse_models(payload: Any, into: dict[str, ModelInfo] | None = None) -> dict[str, ModelInfo] | None:
    """Untrusted input: bounded and deduplicated. None when the payload is not a model list at all."""

    entries = _entries(payload)
    if entries is None:
        return None
    out = {} if into is None else into
    for raw_id, item in entries:
        model_id = _clean_id(raw_id)
        if model_id is None or model_id in out:
            continue
        out[model_id] = ModelInfo(
            model_id, _clean_name(item.get("display_name")) or _clean_name(item.get("name")), _context(item)
        )
        if len(out) >= _MAX_MODELS:
            break
    return out


def _fetch(
    format: Format, base_url: str, credential: str | None, transport: HttpTransport
) -> dict[str, ModelInfo] | None:
    base = base_url.rstrip("/")
    if format == "anthropic_messages":
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if credential:
            headers["x-api-key"] = credential
        root = base.removesuffix("/v1") + f"/v1/models?limit={_PAGE_SIZE}"
        found: dict[str, ModelInfo] = {}
        url, seen_cursors = root, set()
        for _ in range(_MAX_PAGES):
            payload = transport.get_json(url, headers, _TIMEOUT)
            if parse_models(payload, found) is None:
                return None if not found else found
            cursor = payload.get("last_id")
            if (
                payload.get("has_more") is not True
                or not isinstance(cursor, str)
                or not cursor
                or cursor in seen_cursors
            ):
                break
            seen_cursors.add(cursor)
            url = f"{root}&after_id={quote(cursor, safe='')}"
        return found
    headers = {"authorization": f"Bearer {credential}"} if credential else {}
    return parse_models(transport.get_json(base + "/models", headers, _TIMEOUT))


def list_provider_models(
    format: Format,
    base_url: str,
    credential: str | None,
    transport: HttpTransport,
    *,
    chatgpt: bool = False,
) -> ModelListing:
    """Never raises. Models are unfiltered here; ``list_chat_models`` filters and sorts."""

    try:
        found = _fetch(format, base_url, credential, transport)
    except TransportError as exc:
        if chatgpt and exc.status in (401, 403, 404):
            return ModelListing([], False, REASON_CHATGPT_NOT_ALLOWED)
        if exc.status in _NO_LIST_STATUSES:
            return ModelListing([], False, REASON_NO_MODEL_LIST)
        return ModelListing([], False, error=error_from_transport(exc))
    if found is None:
        return ModelListing([], False, REASON_NO_MODEL_LIST)
    return ModelListing(list(found.values()), True)


def list_chat_models(
    format: Format,
    base_url: str,
    credential: str | None,
    transport: HttpTransport,
    *,
    chatgpt: bool = False,
) -> ModelListing:
    listing = list_provider_models(format, base_url, credential, transport, chatgpt=chatgpt)
    if not listing.listed:
        return listing
    return ModelListing(filter_chat_models(listing.models), True)


# ---- a connection's listing ----------------------------------------------------------------------


def _credential_for_listing(
    store: ConnectionStore, conn: Connection, transport: HttpTransport, clock: Any
) -> str | None:
    """The stored key, or the renewed ChatGPT access token. Raises ConnectionRefused."""

    if is_chatgpt(conn):
        from krellbot.ai.oauth import chatgpt_access_token  # imported here: the sign-in module imports connections

        return chatgpt_access_token(store, conn, transport=transport, clock=clock)
    return store.credential_for(conn)


def list_for_connection(
    store: ConnectionStore, conn: Connection, transport: HttpTransport, *, clock: Any = time.time
) -> ModelListing:
    """List a stored connection's models and refresh its context-window cache. Blocks on the keychain.

    Raises ConnectionRefused (endpoint, keychain or sign-in problems).
    """

    check_endpoint(conn)  # before the keychain is touched
    credential = _credential_for_listing(store, conn, transport, clock)
    listing = list_provider_models(conn.format, conn.base_url, credential, transport, chatgpt=is_chatgpt(conn))
    if listing.listed or listing.reason is not None:  # a missing list is remembered too: no repeat requests
        _remember(store.home, conn.id, listing.models)
    return ModelListing(filter_chat_models(listing.models), True) if listing.listed else listing


# ---- context windows -----------------------------------------------------------------------------

_cache: dict[tuple[str, str], dict[str, int | None]] = {}
_cache_lock = threading.Lock()


def _remember(home: Path, connection_id: str, models: list[ModelInfo]) -> None:
    with _cache_lock:
        _cache[(str(home), connection_id)] = {m.id: m.context_window for m in models}


def clear_context_cache() -> None:
    with _cache_lock:
        _cache.clear()


def context_window(
    home: Path,
    connection_id: str,
    model_id: str,
    *,
    store: ConnectionStore | None = None,
    transport: HttpTransport | None = None,
) -> int | None:
    """The model's context window in tokens, or None when the provider does not say.

    The first call for a connection lists its models once and keeps the answers in
    memory; later calls make no request. A failed listing is not cached, so the next
    call tries again. Never raises. Blocks (keychain, network): call from a worker thread.
    """

    key = (str(home), connection_id)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is None:
        try:
            store = store or ConnectionStore(Path(home))
            conn = store.get(connection_id)
            list_for_connection(store, conn, transport or UrllibTransport())
        except Exception:  # noqa: BLE001 — unknown is None; the error text is dropped
            return None
        with _cache_lock:
            cached = _cache.get(key)
        if cached is None:
            return None
    return cached.get(model_id)
