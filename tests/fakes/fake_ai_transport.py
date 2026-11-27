"""Scriptable fake HttpTransport for AI provider adapter tests.

Each entry in ``responses`` answers one ``post_stream`` call and is one of:

* a list of lines (``str`` or ``bytes``) to stream back; an ``Exception``
  element inside the list is raised at that point, which simulates a
  connection dropped mid-stream;
* an ``Exception`` (typically ``TransportError``) raised when the call starts,
  which simulates a non-2xx answer or an unreachable host.

Every call is recorded in ``requests`` as ``{"url", "body", "headers", "timeout"}``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

from krellbot.ai.http import TransportError


class FakeAiTransport:
    def __init__(self, responses: Sequence[Any] = (), json_responses: Sequence[Any] = ()) -> None:
        self._responses = list(responses)
        self._json_responses = list(json_responses)
        self.requests: list[dict[str, Any]] = []
        self.get_requests: list[dict[str, Any]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def post_stream(self, url: str, body: dict, headers: dict[str, str], timeout: float) -> Iterator[bytes]:
        self.requests.append({"url": url, "body": body, "headers": dict(headers), "timeout": timeout})
        if not self._responses:
            raise TransportError(None, "fake transport has no scripted response")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return self._iterate(item)

    @staticmethod
    def _iterate(lines: Sequence[Any]) -> Iterator[bytes]:
        for line in lines:
            if isinstance(line, Exception):
                raise line
            yield line if isinstance(line, bytes) else line.encode("utf-8")

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        self.get_requests.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if not self._json_responses:
            raise TransportError(None, "fake transport has no scripted json response")
        item = self._json_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def sse(*events: dict | str, named: bool = False) -> list[str]:
    """Build SSE lines. A str event is sent verbatim as the data payload.

    With ``named=True`` each dict event is preceded by an ``event: <type>`` line,
    the way Anthropic streams do.
    """
    lines: list[str] = []
    for event in events:
        if isinstance(event, dict):
            if named and "type" in event:
                lines.append(f"event: {event['type']}\n")
            lines.append(f"data: {json.dumps(event)}\n")
        else:
            lines.append(f"data: {event}\n")
        lines.append("\n")
    return lines
