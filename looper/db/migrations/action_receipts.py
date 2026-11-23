"""``manager_actions.category`` and ``receipt``: what a write was and the line shown for it (assistant phase 3 RFC, 9.2).

Both columns are nullable without a default, so no row is rewritten. Their checks are added ``NOT VALID`` and
validated online after commit.
"""

from . import OnlineStep

name = "action_receipts"
order = 1130
pending_order = 1130
online_order = 1130
online = (OnlineStep(name, "action_receipts_online", validate=(
    ("manager_actions", "manager_actions_category_check"),
    ("manager_actions", "manager_actions_receipt_check"),
)),)

_DDL = (
    "ALTER TABLE manager_actions ADD COLUMN IF NOT EXISTS category TEXT, ADD COLUMN IF NOT EXISTS receipt TEXT",
    "ALTER TABLE manager_actions ADD CONSTRAINT manager_actions_category_check "
    "CHECK (category IN ('create', 'update', 'control', 'remove', 'relay')) NOT VALID",
    "ALTER TABLE manager_actions ADD CONSTRAINT manager_actions_receipt_check "
    "CHECK (char_length(receipt) <= 300) NOT VALID",
)
fresh_ddl = ";\n".join((*_DDL, *(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}"
                                  for table, constraint in online[0].validate))) + ";"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('manager_actions') "
        "AND attname = 'receipt' AND NOT attisdropped)")


async def apply(conn, *, sql, state):
    if await pending(conn):
        for ddl in _DDL:
            await conn.execute(ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
