"""The operator's declarations of an MCP server's tools (``mcp_tool_levels``, the outbound-review RFC, section 6.2):
a tool declared ``inside`` acts only inside Looper and this machine, so the outbound review lets its calls run
without a review. Every other tool of a server other than Looper's own is reviewed."""

from __future__ import annotations

import re
from collections.abc import Sequence

import asyncpg

from .connection import get_pool

TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
TOOLS_MAX = 200
LEVELS = ("inside",)


class DeclarationError(ValueError):
    """A declaration with a tool name or a list it does not take."""


class NoSuchServer(DeclarationError):
    """A declaration that names no MCP server of the catalog."""


async def declared_inside(pairs: Sequence[tuple[str, str]], *, pool: asyncpg.Pool | None = None) -> bool:
    """Whether one of ``pairs`` (an MCP server's name in the catalog, a tool's name) is declared ``inside``."""
    if not pairs:
        return False
    pool = pool or get_pool()
    return bool(await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM mcp_tool_levels l JOIN library_items i ON i.id = l.item_id "
        "JOIN unnest($1::text[], $2::text[]) AS p(server, tool) ON p.server = i.name AND p.tool = l.tool "
        "WHERE i.kind = 'mcp' AND l.level = 'inside')",
        [server for server, _ in pairs], [tool for _, tool in pairs]))


async def get(item_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, object]:
    """The tools of MCP server ``item_id`` declared ``inside``, by name."""
    pool = pool or get_pool()
    if not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM library_items WHERE id = $1 AND kind = 'mcp')", item_id):
        raise NoSuchServer(f"no MCP server {item_id!r} in the catalog")
    rows = await pool.fetch("SELECT tool FROM mcp_tool_levels WHERE item_id = $1 AND level = 'inside' "
                            "ORDER BY tool COLLATE \"C\"", item_id)
    return {"item_id": item_id, "inside_tools": [row["tool"] for row in rows]}


async def put(item_id: str, inside_tools: Sequence[str], *, pool: asyncpg.Pool | None = None) -> dict[str, object]:
    """Declare exactly ``inside_tools`` of MCP server ``item_id`` ``inside``; every other tool of it is reviewed."""
    if not isinstance(inside_tools, (list, tuple)) or len(inside_tools) > TOOLS_MAX:
        raise DeclarationError(f"inside_tools is a list of at most {TOOLS_MAX} tool names")
    if any(not isinstance(tool, str) or not TOOL_NAME.fullmatch(tool) for tool in inside_tools):
        raise DeclarationError("a tool name is 1 to 128 letters, digits, '_', '.' or '-'")
    tools = sorted(set(inside_tools))
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        if await conn.fetchval("SELECT 1 FROM library_items WHERE id = $1 AND kind = 'mcp' FOR SHARE",
                               item_id) is None:
            raise NoSuchServer(f"no MCP server {item_id!r} in the catalog")
        await conn.execute("DELETE FROM mcp_tool_levels WHERE item_id = $1", item_id)
        await conn.execute("INSERT INTO mcp_tool_levels (item_id, tool, level) "
                           "SELECT $1, tool, 'inside' FROM unnest($2::text[]) AS tool", item_id, tools)
    return await get(item_id, pool=pool)
