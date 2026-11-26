"""The subscription_rules migration, in its released execution order."""

name = "subscription_rules"
order = 290
pending_order = 190


async def pending(conn):
    from .. import connection as legacy

    return await legacy._subscription_rules_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._subscription_rules_pending(conn):
        did_work = True
        await conn.execute(legacy._SUBSCRIPTION_RULES_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._SUBSCRIPTION_RULES,
        )
    return did_work
