"""The gate_purpose migration, in its released execution order."""

name = "gate_purpose"
order = 490
pending_order = 510


async def pending(conn):
    from .. import connection as legacy

    return await legacy._gate_purpose_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._gate_purpose_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE task_user_gates ADD COLUMN purpose TEXT NOT NULL DEFAULT 'discussion' "
            "CHECK (purpose IN ('discussion', 'exhaustion', 'verification_exhaustion'))"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._GATE_PURPOSE
        )
    return did_work
