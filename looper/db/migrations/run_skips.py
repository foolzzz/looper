"""The run_skips migration, in its released execution order."""

from . import OnlineStep

name = "run_skips"
order = 540
pending_order = 300


async def pending(conn):
    from .. import connection as legacy

    return await legacy._run_skips_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._run_skips_pending(conn):
        await conn.execute(legacy._run_skips_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
            legacy._RUN_SKIPS,
        )
        await conn.execute(
            "DELETE FROM schema_migrations WHERE name = $1", legacy._RUN_SKIPS_ONLINE
        )
    elif state == "empty":
        for table, constraint in legacy._online_step(legacy._RUN_SKIPS).validate:
            await conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")
    elif await legacy._unvalidated(conn, legacy._RUN_SKIPS):
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
            legacy._RUN_SKIPS,
        )
        await conn.execute(
            "DELETE FROM schema_migrations WHERE name = $1", legacy._RUN_SKIPS_ONLINE
        )


online_order = 70
online = (
    OnlineStep(
        migration="run_skips",
        marker="run_skips_online",
        validate=(
            ("subscription_runs", "subscription_runs_outcome_check"),
            ("subscription_runs", "subscription_runs_skip_reason_check"),
            ("subscription_runs", "subscription_runs_skipped"),
            ("schedule_notifications", "schedule_notifications_kind_check"),
        ),
        indexes=(),
    ),
)
