"""The gate_message_sender migration: ``task_user_gate_messages.from_operator``.

A user's gate message: whether the operator wrote it in their own turn, as code decides when the row
is stored (NULL: not known, a row from before the column). The untrusted fence takes any but TRUE as
outside content. Nullable without a default, so adding it rewrites no row."""

name = "gate_message_sender"
order = 660
pending_order = 590
fresh_ddl = "ALTER TABLE task_user_gate_messages ADD COLUMN IF NOT EXISTS from_operator BOOLEAN;\n"


async def pending(conn):
    from .. import connection

    return await connection._column_pending(conn, name, "task_user_gate_messages", "from_operator")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute("ALTER TABLE task_user_gate_messages ADD COLUMN from_operator BOOLEAN")
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work
