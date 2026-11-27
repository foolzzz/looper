"""The task branch recorded for a direct session, and the ref reports a turn leaves.

Two new tables, so existing rows are untouched and no online step is needed.
"""

name = "task_manager_refs"
order = 2180
pending_order = 2180
fresh_ddl = """\
CREATE TABLE IF NOT EXISTS task_manager_branches (
    task_id     TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    branch      TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_by TEXT NOT NULL CHECK (recorded_by IN ('first_enable', 'operator'))
);
CREATE TABLE IF NOT EXISTS task_manager_ref_reports (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    message_id  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    changes     JSONB NOT NULL,
    needs_you   BOOLEAN NOT NULL,
    resolved_at TIMESTAMPTZ,
    resolved_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_manager_ref_reports_pending
    ON task_manager_ref_reports (created_at) WHERE needs_you AND resolved_at IS NULL;
"""


async def pending(conn) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval(
        "SELECT to_regclass('task_manager_branches') IS NOT NULL "
        "AND to_regclass('task_manager_ref_reports') IS NOT NULL")


async def apply(conn, *, sql, state) -> bool:
    if not await pending(conn):
        return False
    await conn.execute(fresh_ddl)
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return True
