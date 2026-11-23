"""Allow the server's typed explorer-result messages in task rooms."""

from . import OnlineStep

name = "subagent_room_delivery"
order = 1020
pending_order = 1020
online_order = 1020
online = (OnlineStep(name, "subagent_room_delivery_online", validate=(
    ("room_messages", "room_messages_kind_check"),
)),)

_DDL = """
ALTER TABLE room_messages DROP CONSTRAINT room_messages_kind_check,
    ADD CONSTRAINT room_messages_kind_check CHECK (kind IN
        ('info', 'question', 'answer', 'interface', 'announce', 'notice', 'escalation', 'spawn_result')) NOT VALID;
"""
fresh_ddl = _DDL + "ALTER TABLE room_messages VALIDATE CONSTRAINT room_messages_kind_check;\n"


async def pending(conn):
    return not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", name)


async def apply(conn, *, sql, state):
    if await pending(conn):
        if state != "empty":
            await conn.execute(_DDL)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", name)
