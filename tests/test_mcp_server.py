"""Integration-ish tests for :mod:`tokie_cli.mcp_server.server`.

We don't stand up a real stdio transport — that would require a child
process and the MCP SDK's client. Instead we build the server, pull the
decorated handlers off it, and call them as plain coroutines. This
covers the registration plumbing (``list_tools`` returns ``Tool`` objects
in the SDK's format; ``call_tool`` wraps results in ``TextContent``)
without needing to drive a bidirectional JSON-RPC loop.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from tokie_cli.config import SubscriptionBinding, TokieConfig
from tokie_cli.mcp_server.server import SERVER_NAME, build_server


def _ctx(tmp_path: Path) -> dict[str, object]:
    config = TokieConfig(
        db_path=tmp_path / "tokie.db",
        audit_log_path=tmp_path / "audit.log",
        subscriptions=(
            SubscriptionBinding(plan_id="claude_pro_personal", account_id="default"),
        ),
    )
    return {
        "config": config,
        "plans_loader": lambda: [],
        "events_loader": lambda _cfg: [],
    }


def _find_list_tools_handler(server: Any) -> Callable[..., Awaitable[Any]]:
    """Pull the registered ``list_tools`` coroutine back off the Server.

    The MCP SDK stores decorated handlers on the server's request
    handler map keyed by the request type. The adapter wraps our
    decorated function so it takes a request object and returns a
    ``ServerResult`` — we call it directly to avoid spinning up a real
    stdio transport in tests.
    """

    from mcp import types

    return server.request_handlers[types.ListToolsRequest]  # type: ignore[no-any-return]


def _find_call_tool_handler(server: Any) -> Callable[..., Awaitable[Any]]:
    from mcp import types

    return server.request_handlers[types.CallToolRequest]  # type: ignore[no-any-return]


def test_build_server_has_expected_name(tmp_path: Path) -> None:
    server = build_server(**_ctx(tmp_path))
    assert server.name == SERVER_NAME


@pytest.mark.asyncio
async def test_list_tools_returns_four_tools(tmp_path: Path) -> None:
    from mcp import types

    server = build_server(**_ctx(tmp_path))
    handler = _find_list_tools_handler(server)
    req = types.ListToolsRequest(method="tools/list", params=None)
    result = await handler(req)
    names = {t.name for t in result.root.tools}
    assert names == {
        "list_subscriptions",
        "get_usage",
        "get_remaining",
        "suggest_tool",
    }


@pytest.mark.asyncio
async def test_call_tool_dispatches_to_handler(tmp_path: Path) -> None:
    from mcp import types

    server = build_server(**_ctx(tmp_path))
    handler = _find_call_tool_handler(server)
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="list_subscriptions", arguments={}),
    )
    result = await handler(req)
    content = result.root.content
    assert content, "MCP call_tool must return at least one content block"
    payload = json.loads(content[0].text)
    assert "subscriptions" in payload


@pytest.mark.asyncio
async def test_call_tool_reports_unknown_tool(tmp_path: Path) -> None:
    from mcp import types

    server = build_server(**_ctx(tmp_path))
    handler = _find_call_tool_handler(server)
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="nope", arguments={}),
    )
    result = await handler(req)
    # The SDK converts raised ValueError into an is_error=True response
    # rather than propagating — the key thing we're testing is that a
    # bad tool name doesn't crash the server process.
    assert result.root.isError is True
