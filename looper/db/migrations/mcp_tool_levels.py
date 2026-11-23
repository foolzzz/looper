"""The mcp_tool_levels migration, preserving the released upgrade path."""

name = "mcp_tool_levels"
order = 592
pending_order = 321


async def pending(conn):
    from .. import connection as legacy

    return await legacy._mcp_tool_levels_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await pending(conn):
        await conn.execute(legacy._mcp_tool_levels_ddl())
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", legacy._MCP_TOOL_LEVELS)
