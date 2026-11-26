"""Indexes for task selection's time window.

The newest activity row is still one probe of (task_id, seq). These indexes only
narrow which tasks are worth that probe. A directory match uses the canonical
path columns, not the stored path text.
"""

from . import OnlineStep

name = "task_select_reads"
order = 2070
pending_order = 2070
online_order = 2070

INDEXES = (
    ("idx_tasks_select_updated", "ON tasks (updated_at)"),
    ("idx_activity_select_created", "ON activity (created_at, task_id)"),
)
fresh_ddl = "".join(
    f"CREATE INDEX IF NOT EXISTS {index} {definition};\n" for index, definition in INDEXES)


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    if not await conn.fetchval("SELECT to_regclass('tasks') IS NOT NULL"):
        return False
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM unnest($1::text[]) AS index_name WHERE to_regclass(index_name) IS NULL)",
        [index for index, _definition in INDEXES])


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        # The tables are already there. The resumable runner builds the indexes after this transaction commits.
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work


online = (
    OnlineStep(name, "task_select_reads_online", indexes=tuple(INDEXES)),
)
