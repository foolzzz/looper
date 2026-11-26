"""Record whether a chat row is a message, a fixed command, or that command's code reply."""

from . import OnlineStep

name = "chat_message_entry_kind"
order = pending_order = online_order = 2100

_COLUMN = "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS entry_kind TEXT;"
_CHECK = """
ALTER TABLE chat_messages ADD CONSTRAINT chat_messages_entry_kind_check
    CHECK (entry_kind IS NULL OR entry_kind IN ('message', 'command', 'command_reply')) NOT VALID;
"""
online = (OnlineStep(name, "chat_message_entry_kind_online", validate=(
    ("chat_messages", "chat_messages_entry_kind_check"),
)),)
fresh_ddl = """
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS entry_kind TEXT;
ALTER TABLE chat_messages ADD CONSTRAINT chat_messages_entry_kind_check
    CHECK (entry_kind IS NULL OR entry_kind IN ('message', 'command', 'command_reply'));
"""


async def _constraint(conn):
    return await conn.fetchrow(
        "SELECT convalidated FROM pg_constraint "
        "WHERE conrelid = to_regclass('chat_messages') AND conname = 'chat_messages_entry_kind_check'")


async def pending(conn):
    column = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('chat_messages') "
        "AND attname = 'entry_kind' AND attnum > 0 AND NOT attisdropped)")
    if not column:
        return True
    constraint = await _constraint(conn)
    if constraint is None:
        return True
    if constraint["convalidated"]:
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name)


async def apply(conn, *, sql, state) -> bool:
    if not await pending(conn):
        return False
    await conn.execute(_COLUMN)
    if await _constraint(conn) is None:
        await conn.execute(_CHECK)
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    return True
