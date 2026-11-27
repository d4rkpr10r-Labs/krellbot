"""Who is calling an agent tool: Krell (in the engine) or an external MCP client.

An actor's ``label`` is what the audit journal, draft origins and the Agent
activity view show: ``"krell"`` or ``"mcp:<client>"``. MCP client names come
from the client's ``initialize`` info, so they are untrusted text and are
reduced to ``[a-z0-9._-]`` (at most 40 characters, ``"local"`` when empty).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ActorKind = Literal["krell", "mcp"]
ACTOR_KINDS: frozenset[str] = frozenset({"krell", "mcp"})

CLIENT_MAX_CHARS = 40
CLIENT_DEFAULT = "local"
_CLIENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


def _sanitize_client(name: str | None) -> str:
    lowered = str(name or "").lower()
    kept = "".join(ch for ch in lowered if ch in _CLIENT_CHARS)
    return kept[:CLIENT_MAX_CHARS] or CLIENT_DEFAULT


@dataclass(frozen=True)
class Actor:
    """The caller of an agent tool. Frozen; compare by value."""

    kind: ActorKind
    client: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor kind: {self.kind!r}")
        # Normalize so a hand-built Actor can never carry an unsafe label.
        client = _sanitize_client(self.client) if self.kind == "mcp" else ""
        object.__setattr__(self, "client", client)

    @property
    def label(self) -> str:
        if self.kind == "krell":
            return "krell"
        return f"mcp:{self.client}"

    @classmethod
    def mcp(cls, client_name: str | None) -> Actor:
        """Build an MCP actor from an untrusted client name."""
        return cls("mcp", _sanitize_client(client_name))


KRELL = Actor("krell")


__all__ = [
    "ACTOR_KINDS",
    "KRELL",
    "Actor",
    "ActorKind",
]
