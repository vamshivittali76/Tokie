"""Tokie MCP server — expose read-only usage tools to LLM agents.

Runs a stdio-based Model Context Protocol server (``tokie mcp serve``)
that hands the calling agent four tools:

- ``list_subscriptions`` — enumerate configured subscriptions with live
  saturation numbers.
- ``get_usage`` — aggregate token/message counts for the requested
  window.
- ``get_remaining`` — remaining capacity (tokens / messages / USD) per
  window type.
- ``suggest_tool`` — deterministic :func:`recommend` result for a task,
  so the agent can ask "which of my subs should I use for
  ``code_generation`` right now?" and get a rationale.

Everything is read-only. The server never writes to the DB, never calls
out to third-party APIs, and never emits prompt/output content.
"""

from __future__ import annotations

from tokie_cli.mcp_server.handlers import (
    build_tool_catalog,
    handle_call_tool,
)

__all__ = [
    "build_tool_catalog",
    "handle_call_tool",
]
