"""The manager_event_reasons migration, in its released execution order."""

from . import OnlineStep

name = "manager_event_reasons"
order = 350
pending_order = 250


async def pending(conn):
    from .. import connection as legacy

    return await legacy._manager_event_reasons_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._manager_event_reasons_pending(conn):
        did_work = True
        reasons = ", ".join(f"'{reason}'" for reason in legacy._MANAGER_EVENT_REASONS)
        # NOT VALID: no scan under this transaction's lock; the online step validates it after the commit.
        await conn.execute(
            "ALTER TABLE manager_events DROP CONSTRAINT manager_events_reason_check, ADD CONSTRAINT "
            f"manager_events_reason_check CHECK (reason IN ({reasons})) NOT VALID"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._MANAGER_EVENT_REASONS_MIGRATION,
        )
    return did_work


online_order = 40
online = (
    OnlineStep(
        migration="manager_event_reasons",
        marker="manager_event_reasons_online",
        validate=(("manager_events", "manager_events_reason_check"),),
        indexes=(),
    ),
)
