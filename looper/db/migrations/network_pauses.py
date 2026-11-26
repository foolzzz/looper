"""Durable task network pauses and their bounded recovery history."""

name = "network_pauses"
order = pending_order = 2120

fresh_ddl = """
CREATE TABLE IF NOT EXISTS task_network_pauses (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'paused' CHECK (state IN ('paused', 'waiting_user')),
    reason TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deadline TIMESTAMPTZ NOT NULL,
    continuation JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(continuation) = 'object'),
    ended_at TIMESTAMPTZ,
    end_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_network_pauses_active
    ON task_network_pauses(task_id) WHERE ended_at IS NULL;
"""


async def pending(conn):
    return not await conn.fetchval("SELECT to_regclass('task_network_pauses') IS NOT NULL")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    return did_work
