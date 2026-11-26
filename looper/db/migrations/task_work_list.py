"""Operator work-list watermarks and partial task indexes."""

from . import OnlineStep

name = "task_work_list"
order = pending_order = online_order = 2060
_TABLE = """
CREATE TABLE IF NOT EXISTS task_seen (
    principal TEXT NOT NULL CHECK (principal <> ''),
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seen_activity_seq BIGINT NOT NULL DEFAULT 0,
    seen_message_id BIGINT,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, task_id)
);
"""
_INDEXES = (
    ("idx_tasks_open", "ON tasks (project_id, id) WHERE schedule_id IS NULL "
     "AND status NOT IN ('completed','failed','stopped','cancelled')"),
    ("idx_tasks_ended", "ON tasks ((COALESCE(completed_at, attempt_ended_at, updated_at)) DESC, id DESC) "
     "WHERE schedule_id IS NULL AND status IN ('completed','failed','stopped','cancelled')"),
)
fresh_ddl = _TABLE + "\n".join(f"CREATE INDEX IF NOT EXISTS {key} {definition};" for key, definition in _INDEXES)
online = (OnlineStep(name, "task_work_list_online", indexes=_INDEXES),)


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('task_seen') IS NOT NULL")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(_TABLE)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    return did_work
