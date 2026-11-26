"""The mcp_tool_levels migration, in its released execution order."""

name = "mcp_tool_levels"
order = 600
pending_order = 321


async def pending(conn):
    from .. import connection as legacy

    return await legacy._mcp_tool_levels_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._mcp_tool_levels_pending(conn):
        did_work = True
        await conn.execute(legacy._mcp_tool_levels_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._MCP_TOOL_LEVELS
        )
    return did_work
