"""MCP stdio transport adapter.

Thin wrapper around :mod:`tokie_cli.mcp_server.handlers` that plugs the
pure handler dispatch into the official ``mcp`` Python SDK. The MCP SDK
is an optional dependency (``tokie-cli[mcp]``); importing this module
without ``mcp`` installed raises a clear, actionable error rather than
a cryptic ImportError deep in the stack.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from tokie_cli.mcp_server.handlers import (
    ToolArgumentError,
    ToolNotFoundError,
    build_tool_catalog,
    handle_call_tool,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

logger = logging.getLogger(__name__)

SERVER_NAME = "tokie"


class MCPNotInstalledError(RuntimeError):
    """Raised when ``tokie mcp serve`` runs but ``mcp`` is not installed.

    Carries an actionable remediation string so the CLI error renderer
    can print it verbatim without wrapping it in yet another traceback.
    """

    def __init__(self) -> None:
        super().__init__(
            "The `mcp` Python package is not installed. "
            "Install it with `pip install 'tokie-cli[mcp]'` and try again."
        )


def _require_mcp() -> tuple[Any, Any, Any, Any]:
    """Import the MCP SDK, raising :class:`MCPNotInstalledError` on failure.

    Returns the symbols the adapter needs: ``Server``, ``stdio_server``,
    ``Tool``, ``TextContent``. Kept as a single entry point so the
    "mcp is optional" contract lives in exactly one place.
    """

    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import TextContent, Tool
    except ImportError as exc:
        raise MCPNotInstalledError() from exc
    return Server, stdio_server, Tool, TextContent


def build_server(**context: Any) -> Any:
    """Construct the MCP :class:`Server` with all Tokie tools registered.

    ``context`` is forwarded to every :func:`handle_call_tool` invocation
    so tests can swap in stub config / plans / events without running a
    stdio transport.
    """

    server_cls, _stdio, tool_cls, text_content_cls = _require_mcp()

    server = server_cls(SERVER_NAME)

    @server.list_tools()  # type: ignore[untyped-decorator]
    async def _list_tools() -> list[Any]:
        return [tool_cls(**defn) for defn in build_tool_catalog()]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[Any]:
        try:
            result = handle_call_tool(name, arguments, **context)
        except ToolNotFoundError as exc:
            # Re-raising lets the MCP SDK surface this as a protocol-level
            # error. ``is_error`` on ``CallToolResult`` would also work
            # but varies across SDK versions.
            raise ValueError(str(exc)) from exc
        except ToolArgumentError as exc:
            raise ValueError(str(exc)) from exc
        return [text_content_cls(type="text", text=json.dumps(result, indent=2))]

    return server


def run_stdio(**context: Any) -> None:
    """Run the Tokie MCP server over stdio until the client disconnects.

    Blocks the calling thread. Used by ``tokie mcp serve``.
    """

    _server_cls, stdio_server, _tool_cls, _text_cls = _require_mcp()
    server = build_server(**context)

    async def _main() -> None:
        async with stdio_server() as (read, write):
            await server.run(
                read, write, server.create_initialization_options()
            )

    asyncio.run(_main())


__all__ = [
    "SERVER_NAME",
    "MCPNotInstalledError",
    "build_server",
    "run_stdio",
]
