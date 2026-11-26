"""The uncertain_deliveries migration, in its released execution order."""

from . import OnlineStep

name = "uncertain_deliveries"
order = 570
pending_order = 320


async def pending(conn):
    from .. import connection as legacy

    return await legacy._uncertain_deliveries_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._uncertain_deliveries_pending(conn):
        did_work = True
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._UNCERTAIN_DELIVERIES,
        )
    return did_work


online_order = 110
online = (
    OnlineStep(
        migration="uncertain_deliveries",
        marker="uncertain_deliveries_online",
        validate=(),
        indexes=(
            (
                "idx_subscription_deliveries_uncertain",
                "ON subscription_deliveries (updated_at) WHERE state = 'uncertain'",
            ),
            (
                "idx_intent_posts_uncertain",
                "ON intent_posts (updated_at) WHERE state = 'uncertain'",
            ),
            (
                "idx_manager_events_uncertain",
                "ON manager_events (created_at) WHERE delivery = 'uncertain'",
            ),
        ),
    ),
)
