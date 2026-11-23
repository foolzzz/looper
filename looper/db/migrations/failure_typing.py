"""Failure classification columns with resumable validation and index creation."""

from . import OnlineStep

name = "failure_typing"
order = 594
pending_order = 322


async def pending(conn):
    from .. import connection as legacy

    return await legacy._failure_typing_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await pending(conn):
        for ddl in legacy._failure_typing_ddl():
            await conn.execute(ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)


online_order = 120
online = (
    OnlineStep(
        migration=name,
        marker="failure_typing_online",
        validate=(("llm_records", "llm_records_failure_check"),),
        indexes=(("idx_llm_records_failures", "ON llm_records (created_at) WHERE failure_class IS NOT NULL"),),
    ),
)
