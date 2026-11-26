"""Step acceptance definitions and the nullable, default-off project permission."""
name = "acceptance_checks"
order = 600
pending_order = 590
fresh_ddl = """
ALTER TABLE task_steps ADD COLUMN IF NOT EXISTS checks JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS acceptance_checks TEXT
    CHECK (acceptance_checks IN ('local', 'network'));
"""


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("""
        SELECT count(*) = 2 FROM information_schema.columns
        WHERE table_schema = current_schema() AND
          ((table_name = 'task_steps' AND column_name = 'checks') OR
           (table_name = 'projects' AND column_name = 'acceptance_checks'))
    """)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work
