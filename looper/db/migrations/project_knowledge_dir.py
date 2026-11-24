"""Optional vault directory for a project's knowledge; unset preserves existing reads."""

name = "project_knowledge_dir"
order = pending_order = 1920
fresh_ddl = "ALTER TABLE projects ADD COLUMN IF NOT EXISTS knowledge_dir TEXT;\n"


async def pending(conn):
    from .. import connection

    return await connection._column_pending(conn, name, "projects", "knowledge_dir")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
