"""Egress disclosure: what leaves this computer for an AI provider, and where it goes."""

from __future__ import annotations

from krellbot.ai.connections import Connection, provider_host


def egress_disclosure(conn: Connection) -> str:
    return (
        f"Your conversation with Krell and the Krellbot data she reads are sent to {conn.label} "
        f"({provider_host(conn.base_url)}). Nothing is sent to krellbot.dev."
    )
