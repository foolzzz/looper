"""Distinguish an interrupted claim from a channel send that may have succeeded."""

name = "reminder_post_started"
order = 2020
pending_order = 2020
fresh_ddl = "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS post_started_at TIMESTAMPTZ;\n"


async def pending(conn):
    from .. import connection

    return await connection._column_pending(conn, name, "reminders", "post_started_at")


async def apply(conn, *, sql, state) -> bool:
    if not await pending(conn):
        return False
    await conn.execute(fresh_ddl)
    # Old claims have no evidence that the channel was never called: preserve at-most-once recovery.
    await conn.execute("UPDATE reminders SET post_started_at = updated_at WHERE status = 'sending'")
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return True
