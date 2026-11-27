"""``manager_actions.sender_id``, ``sender_kind``, ``initiator_sender_id`` and ``initiator_kind``: who started the turn
a call ran in and, for a confirmed call, who asked for it.

All four columns are nullable without a default, so no row is rewritten and history keeps NULLs. Their checks are
added ``NOT VALID`` and validated online after commit.
"""

from . import OnlineStep

name = "manager_action_sender"
order = 2170
pending_order = 2170
online_order = 2170
online = (OnlineStep(name, "manager_action_sender_online", validate=(
    ("manager_actions", "manager_actions_sender_kind_check"),
    ("manager_actions", "manager_actions_initiator_kind_check"),
)),)

_DDL = (
    "ALTER TABLE manager_actions ADD COLUMN IF NOT EXISTS sender_id TEXT, ADD COLUMN IF NOT EXISTS sender_kind TEXT, "
    "ADD COLUMN IF NOT EXISTS initiator_sender_id TEXT, ADD COLUMN IF NOT EXISTS initiator_kind TEXT",
    "ALTER TABLE manager_actions ADD CONSTRAINT manager_actions_sender_kind_check "
    "CHECK (sender_kind IN ('operator', 'allowed', 'member', 'client', 'autonomous')) NOT VALID",
    "ALTER TABLE manager_actions ADD CONSTRAINT manager_actions_initiator_kind_check "
    "CHECK (initiator_kind IN ('operator', 'allowed', 'member', 'client')) NOT VALID",
)
fresh_ddl = ";\n".join((*_DDL, *(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}"
                                  for table, constraint in online[0].validate))) + ";"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('manager_actions') "
        "AND attname = 'sender_kind' AND NOT attisdropped)")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        for ddl in _DDL:
            await conn.execute(ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work
