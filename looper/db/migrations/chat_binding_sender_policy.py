"""The chat_binding_sender_policy migration, in its released execution order."""

name = "chat_binding_sender_policy"
order = 430
pending_order = 440


async def pending(conn):
    from .. import connection as legacy

    return await legacy._chat_binding_sender_policy_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._chat_binding_sender_policy_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE manager_chat_bindings ADD COLUMN sender_policy TEXT NOT NULL "
            "DEFAULT 'listed' CHECK (sender_policy IN ('listed', 'chat'))"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._CHAT_BINDING_SENDER_POLICY,
        )
    return did_work
