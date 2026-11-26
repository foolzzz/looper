"""The outbound_reviews migration, in its released execution order."""

from . import OnlineStep

name = "outbound_reviews"
order = 560
pending_order = 310


async def pending(conn):
    from .. import connection as legacy

    return await legacy._outbound_reviews_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._outbound_reviews_pending(conn):
        did_work = True
        for ddl in await legacy._outbound_reviews_ddl(conn):
            await conn.execute(ddl)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._OUTBOUND_REVIEWS
        )
    return did_work


online_order = 100
online = (
    OnlineStep(
        migration="outbound_reviews",
        marker="outbound_reviews_online",
        validate=(
            ("llm_records", "llm_records_run_kind_check"),
            ("agent_setting_layers", "agent_setting_layers_role_check"),
        ),
        indexes=(),
    ),
)
