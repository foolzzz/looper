"""The default-off project switch that replaces a task's vault closeout with memory proposals."""
name = "memory_closeout"
order = 1860
pending_order = 1860
fresh_ddl = """
ALTER TABLE projects ADD COLUMN IF NOT EXISTS memory_closeout TEXT
    CHECK (memory_closeout IN ('off', 'on'));
"""


async def pending(conn):
    # A fresh database already has the column from fresh_ddl and records only the baseline marker.
    # A database that recorded this marker keeps it even if the column is later dropped.
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'projects' AND column_name = 'memory_closeout')
    """)


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
