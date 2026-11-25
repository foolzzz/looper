"""The schedule_notification_events migration, in its released execution order."""

from . import OnlineStep

name = "schedule_notification_events"
order = 340
pending_order = 240


async def pending(conn):
    from .. import connection as legacy

    return await legacy._schedule_notification_events_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._schedule_notification_events_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE schedule_notifications ADD COLUMN IF NOT EXISTS manager_event_id BIGINT, "
            "ADD CONSTRAINT schedule_notifications_manager_event_id_fkey FOREIGN KEY "
            "(manager_event_id) REFERENCES manager_events(id) ON DELETE SET NULL NOT VALID"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._SCHEDULE_NOTIFICATION_EVENTS,
        )
    return did_work


online_order = 30
online = (
    OnlineStep(
        migration="schedule_notification_events",
        marker="schedule_notification_events_online",
        validate=(
            ("schedule_notifications", "schedule_notifications_manager_event_id_fkey"),
        ),
        indexes=(
            (
                "idx_schedule_notifications_manager_event",
                "ON schedule_notifications (manager_event_id) WHERE manager_event_id IS NOT NULL",
            ),
        ),
    ),
)
