"""Indexes for task selection: the time window and a directory's stored paths.

The newest activity row is still one probe of (task_id, seq). These indexes only
narrow which tasks are worth that probe, and which paths can contain the working
directory. Python still decides the match.
"""

from . import OnlineStep

name = "task_select_reads"
order = 2070
pending_order = 2070
online_order = 2070

INDEXES = (
    ("idx_tasks_select_updated", "ON tasks (updated_at)"),
    ("idx_activity_select_created", "ON activity (created_at, task_id)"),
    ("idx_tasks_select_work_dir", "ON tasks (work_dir) WHERE work_dir <> ''"),
    ("idx_tasks_select_workspace_dir", "ON tasks (workspace_dir) WHERE workspace_dir <> ''"),
    ("idx_task_worktrees_select_path", "ON task_worktrees (path) WHERE state = 'active'"),
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


async def apply(conn, *, sql, state):
    if await pending(conn):
        # The tables are already there. The resumable runner builds the indexes after this transaction commits.
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)


online = (
    OnlineStep(name, "task_select_reads_online", indexes=tuple(INDEXES)),
)
