"""Agent control layer: one tool core shared by Krell and external MCP agents.

- :mod:`krellbot.agent.actors`: who is calling (``krell`` or ``mcp:<client>``).
- :mod:`krellbot.agent.core`: the typed tool registry and the single call path
  (hard refusals, secret-param refusal, policy and confirm seams, handler,
  audit), with the result labels ``simulated``, ``not_advice`` and ``paper``.
- :mod:`krellbot.agent.audit`: ``kind=agent_action`` journal records and the
  activity reader.

Import from the submodules. This package init stays empty so an application
module can import :mod:`krellbot.agent.actors` without pulling in the core
(which imports the application facades).
"""
