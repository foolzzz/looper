"""Append immutable routed-chat provenance to tasks; legacy rows remain unassigned."""

name = "task_origin_session"
order = 2010
pending_order = 2010
fresh_ddl = (
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS origin_session TEXT "
    "CONSTRAINT tasks_origin_session_check CHECK (origin_session IS NULL OR "
    "origin_session ~ '^(group|dm):channel-[a-z_]+(-[1-9][0-9]*)?:.+$');"
    "CREATE INDEX IF NOT EXISTS idx_tasks_origin_session ON tasks (origin_session,created_at DESC) "
    "WHERE origin_session IS NOT NULL;"
)


async def pending(conn):
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('tasks') "
        "AND attname='origin_session' AND NOT attisdropped)")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES($1) ON CONFLICT DO NOTHING", name)
    return did_work
