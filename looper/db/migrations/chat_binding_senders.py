"""The chat_binding_senders migration, in its released execution order."""

name = "chat_binding_senders"
order = 420
pending_order = 430


async def pending(conn):
    from .. import connection as legacy

    return await legacy._chat_binding_senders_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._chat_binding_senders_pending(conn):
        await conn.execute(
            "ALTER TABLE manager_chat_bindings ADD COLUMN allowed_senders TEXT[] NOT NULL "
            "DEFAULT '{}' CONSTRAINT manager_chat_bindings_allowed_senders_check "
            "CHECK (cardinality(allowed_senders) <= 100)"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._CHAT_BINDING_SENDERS,
        )
