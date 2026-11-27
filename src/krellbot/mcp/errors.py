"""Typed MCP refusals — closed codes for agent-visible errors.

Refusals never carry key material or live grant payloads. Messages are
from a closed set so an agent transcript cannot leak secrets.

The agent tool core (:mod:`krellbot.agent.core`) owns these codes so the MCP
adapter and Krell refuse identically. This module re-exports them under
their MCP names; ``McpToolRefusal`` is the core's ``ToolRefusal``.
"""

from __future__ import annotations

from krellbot.agent.core import (
    CODE_AGENT_CHANGE_NOT_ALLOWED,
    CODE_AGENT_LIMIT_BALANCE,
    CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED,
    CODE_AGENT_LIMIT_MAX_ARMS,
    CODE_AGENT_LIMIT_PAIR,
    CODE_AGENT_LIMIT_RISK_CAP,
    CODE_AGENT_LIMIT_VENUE,
    CODE_INVALID_REQUEST,
    CODE_KEY_READ_REFUSED,
    CODE_LIVE_ARM_REFUSED,
    CODE_POLICY_DENIED,
    CODE_UNKNOWN_TOOL,
    MESSAGE_KEY_READ_REFUSED,
    MESSAGE_LIVE_ARM_REFUSED,
    MESSAGE_UNKNOWN_TOOL,
    SCHEMA_VERSION,
    invalid_request,
    key_read_refused,
    live_arm_refused,
    policy_denied,
    unknown_tool,
)
from krellbot.agent.core import ToolRefusal as McpToolRefusal

__all__ = [
    "CODE_AGENT_CHANGE_NOT_ALLOWED",
    "CODE_AGENT_LIMIT_BALANCE",
    "CODE_AGENT_LIMIT_DAILY_LOSS_REQUIRED",
    "CODE_AGENT_LIMIT_MAX_ARMS",
    "CODE_AGENT_LIMIT_PAIR",
    "CODE_AGENT_LIMIT_RISK_CAP",
    "CODE_AGENT_LIMIT_VENUE",
    "CODE_INVALID_REQUEST",
    "CODE_KEY_READ_REFUSED",
    "CODE_LIVE_ARM_REFUSED",
    "CODE_POLICY_DENIED",
    "CODE_UNKNOWN_TOOL",
    "MESSAGE_KEY_READ_REFUSED",
    "MESSAGE_LIVE_ARM_REFUSED",
    "MESSAGE_UNKNOWN_TOOL",
    "SCHEMA_VERSION",
    "McpToolRefusal",
    "invalid_request",
    "key_read_refused",
    "live_arm_refused",
    "policy_denied",
    "unknown_tool",
]
