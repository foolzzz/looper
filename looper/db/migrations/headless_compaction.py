"""The headless_compaction migration, in its released execution order."""

from . import OnlineStep

name = "headless_compaction"
order = 170
pending_order = 550


async def pending(conn):
    from .. import connection as legacy

    return await legacy._headless_compaction_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._headless_compaction_pending(conn):
        did_work = True
        await conn.execute(legacy._HEADLESS_COMPACTION_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._HEADLESS_COMPACTION,
        )
    return did_work


online_order = 90
online = (
    OnlineStep(
        migration="headless_compaction",
        marker="headless_compaction_online",
        validate=(("agent_runtimes", "agent_runtimes_capabilities_check"),),
        indexes=(),
    ),
)
