"""The operator's verdict on a completed task's delivery; it never reaches an agent."""

name = "task_feedback"
order = 1220
pending_order = 1220
fresh_ddl = """
CREATE TABLE IF NOT EXISTS task_feedback (
    task_id    TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    verdict    TEXT NOT NULL CHECK (verdict IN ('defect', 'incomplete', 'wrong_approach', 'other')),
    note       TEXT CHECK (char_length(note) <= 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('task_feedback') IS NOT NULL")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", name)
    return did_work
