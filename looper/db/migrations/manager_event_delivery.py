"""The manager_event_delivery migration, in its released execution order."""

from . import OnlineStep

name = "manager_event_delivery"
order = 360
pending_order = 260


async def pending(conn):
    from .. import connection as legacy

    return await legacy._manager_event_delivery_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._manager_event_delivery_pending(conn):
        await conn.execute(
            "ALTER TABLE manager_events ADD COLUMN IF NOT EXISTS delivery TEXT, ADD COLUMN IF NOT EXISTS "
            "delivery_attempts INTEGER NOT NULL DEFAULT 0, ADD COLUMN IF NOT EXISTS delivery_at TIMESTAMPTZ, "
            "ADD COLUMN IF NOT EXISTS delivery_error TEXT, ADD COLUMN IF NOT EXISTS delivery_claim UUID, "
            "ADD CONSTRAINT manager_events_delivery_check CHECK (delivery IN ('pending', 'sending', 'sent', "
            "'failed', 'uncertain')) NOT VALID"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._MANAGER_EVENT_DELIVERY,
        )


online_order = 50
online = (
    OnlineStep(
        migration="manager_event_delivery",
        marker="manager_event_delivery_online",
        validate=(("manager_events", "manager_events_delivery_check"),),
        indexes=(),
    ),
)
