"""Draft tasks have never run and cannot come from a schedule."""

from . import OnlineStep

name = "task_drafts"
order = pending_order = online_order = 2050
_DDL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='tasks'::regclass
                   AND conname='tasks_draft_shape') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_draft_shape
            CHECK (status <> 'draft' OR (schedule_id IS NULL AND completed_at IS NULL)) NOT VALID;
    END IF;
END $$;
"""
fresh_ddl = _DDL.replace(" NOT VALID", "")
online = (OnlineStep(name, "task_drafts_online", validate=(("tasks", "tasks_draft_shape"),)),)


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", name):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='tasks'::regclass "
        "AND conname='tasks_draft_shape')")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(_DDL)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", name)
