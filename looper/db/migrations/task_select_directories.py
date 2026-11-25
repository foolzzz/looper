"""Canonical directories for tasks.select, looked up by index.

``work_dir``, ``workspace_dir`` and an active worktree path are stored again as
``realpath`` when they are written. The selection resolves the working directory
once and compares that form. Python still decides which directory is deepest.
"""

from . import OnlineStep

name = "task_select_directories"
order = 2080
pending_order = 2080
online_order = 2080

_COLUMNS = """
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS work_dir_real TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS workspace_dir_real TEXT NOT NULL DEFAULT '';
ALTER TABLE task_worktrees ADD COLUMN IF NOT EXISTS path_real TEXT NOT NULL DEFAULT '';
"""
# The raw path indexes from task_select_reads do not match a symlink's real path.
_DROP_RAW = """
DROP INDEX IF EXISTS idx_tasks_select_work_dir;
DROP INDEX IF EXISTS idx_tasks_select_workspace_dir;
DROP INDEX IF EXISTS idx_task_worktrees_select_path;
"""
INDEXES = (
    ("idx_tasks_select_work_dir_real", "ON tasks (work_dir_real) WHERE work_dir_real <> ''"),
    ("idx_tasks_select_workspace_dir_real", "ON tasks (workspace_dir_real) WHERE workspace_dir_real <> ''"),
    ("idx_task_worktrees_select_path_real",
     "ON task_worktrees (path_real) WHERE state = 'active' AND path_real <> ''"),
)
fresh_ddl = _COLUMNS + _DROP_RAW + "".join(
    f"CREATE INDEX IF NOT EXISTS {index} {definition};\n" for index, definition in INDEXES)


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('tasks') "
        "AND attname = 'work_dir_real' AND NOT attisdropped)")


async def _backfill(conn):
    from ..directories import canonical_directory

    rows = await conn.fetch(
        "SELECT id, work_dir, workspace_dir FROM tasks WHERE work_dir <> '' OR workspace_dir <> ''")
    for row in rows:
        await conn.execute(
            "UPDATE tasks SET work_dir_real = $2, workspace_dir_real = $3 WHERE id = $1",
            row["id"], canonical_directory(row["work_dir"]), canonical_directory(row["workspace_dir"]))
    trees = await conn.fetch("SELECT task_id, path FROM task_worktrees WHERE path <> ''")
    for row in trees:
        await conn.execute(
            "UPDATE task_worktrees SET path_real = $2 WHERE task_id = $1",
            row["task_id"], canonical_directory(row["path"]))


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(_COLUMNS)
        await conn.execute(_DROP_RAW)
        await _backfill(conn)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)


online = (
    OnlineStep(name, "task_select_directories_online", indexes=tuple(INDEXES)),
)
