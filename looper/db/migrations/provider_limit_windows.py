"""The provider_limit_windows migration, in its released execution order."""

from . import OnlineStep

name = "provider_limit_windows"
order = 650
pending_order = 291


async def pending(conn):
    from .. import connection as legacy

    return await legacy._provider_limit_windows_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._provider_limit_windows_pending(conn):
        await conn.execute(legacy._provider_limit_windows_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._PROVIDER_LIMIT_WINDOWS,
        )


online_order = 130
online = (
    OnlineStep(
        "provider_limit_windows",
        "provider_limit_windows_online",
        indexes=(
            (
                "idx_llm_records_provider_day",
                "ON llm_records ((metadata->'profile'->'provider'->>'id'), created_at)",
            ),
        ),
    ),
)
