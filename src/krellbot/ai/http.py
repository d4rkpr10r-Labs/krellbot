"""Stdlib HTTP seam for the provider adapters."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Protocol

from krellbot.ai.types import ErrorCode, ProviderError
from krellbot.tls import ssl_context

_MAX_ERROR_BODY = 2000


class TransportError(Exception):
    """Non-2xx response (status set) or network failure (status None)."""

    def __init__(self, status: int | None, body: str = "") -> None:
        super().__init__(f"transport error status={status}")
        self.status = status
        self.body = body


class HttpTransport(Protocol):
    def post_stream(self, url: str, body: dict, headers: dict[str, str], timeout: float) -> Iterator[bytes]:
        """POST JSON and yield response lines. Raises TransportError."""
        ...

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict: ...


def error_from_transport(exc: TransportError) -> ProviderError:
    """Map a transport failure to a ProviderError. The body is never echoed."""
    status = exc.status
    code: ErrorCode
    if status in (401, 403):
        code, msg = "provider_auth_failed", "The provider rejected the credential."
    elif status == 429:
        code, msg = "provider_rate_limited", "The provider is rate limiting requests."
    elif status is None or status >= 500:
        code, msg = "provider_unavailable", "The provider could not be reached."
    else:
        code, msg = "provider_bad_response", f"The provider returned HTTP {status}."
    return ProviderError(code, msg)


def _http_error(exc: urllib.error.HTTPError) -> TransportError:
    try:
        text = exc.read(_MAX_ERROR_BODY).decode("utf-8", "replace")
    except (OSError, ValueError):
        text = ""
    return TransportError(exc.code, text)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would re-send the credential header to the new location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Tests inject fake handlers here (placed first) to prove behaviour without sockets.
_TEST_HANDLERS: tuple = ()

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(url: str) -> bool:
    try:
        return urllib.parse.urlsplit(url).hostname in _LOOPBACK_HOSTS
    except ValueError:
        return False


def _open(req: urllib.request.Request, timeout: float):
    handlers: list = [*_TEST_HANDLERS, _NoRedirect, urllib.request.HTTPSHandler(context=ssl_context())]
    if _is_loopback(req.full_url):
        # A local model is reached directly. An environment or system proxy would otherwise
        # receive plain-http requests, credential header included.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


class UrllibTransport:
    def post_stream(self, url: str, body: dict, headers: dict[str, str], timeout: float) -> Iterator[bytes]:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"content-type": "application/json", "user-agent": "krellbot/0.1", **headers},
                method="POST",
            )
            res = _open(req, timeout)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise TransportError(None, type(exc).__name__) from None
        try:
            with res:
                yield from res
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise TransportError(None, type(exc).__name__) from None

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        try:
            req = urllib.request.Request(url, headers={"user-agent": "krellbot/0.1", **headers}, method="GET")
            with _open(req, timeout) as res:
                parsed = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise TransportError(None, type(exc).__name__) from None
        if not isinstance(parsed, dict):
            raise TransportError(None, "not an object")
        return parsed


class BadStream(Exception):
    """The provider sent something that is not the stream shape we expect."""


class StreamFailure(Exception):
    """The provider reported an error inside the stream; carries the mapped ProviderError."""

    def __init__(self, error: ProviderError) -> None:
        super().__init__(error.code)
        self.error = error


def iter_sse_data(lines: Iterator[bytes]) -> Iterator[str]:
    """Yield the payload of each SSE ``data:`` line. Raises BadStream on anything else."""
    for raw in lines:
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except (UnicodeError, AttributeError):
            raise BadStream("undecodable stream line") from None
        if not line or line.startswith((":", "event:", "id:", "retry:")):
            continue
        if line.startswith("data:"):
            yield line[5:].strip()
            continue
        raise BadStream("unexpected stream line")


def parse_json_object(text: str, what: str) -> dict:
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        raise BadStream(f"{what} is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise BadStream(f"{what} is not a JSON object")
    return parsed


def parse_tool_arguments(text: str) -> dict:
    """Tool arguments must be a JSON object; empty means no arguments."""
    return parse_json_object(text, "tool arguments") if text.strip() else {}


def error_from_exception(exc: Exception) -> ProviderError:
    if isinstance(exc, StreamFailure):
        return exc.error
    if isinstance(exc, TransportError):
        return error_from_transport(exc)
    if isinstance(exc, BadStream):
        return ProviderError("provider_bad_response", "The provider sent a response Krellbot could not read.")
    return ProviderError("provider_bad_response", "The provider adapter failed unexpectedly.")
