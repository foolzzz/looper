"""Whether a catalog item is active or deprecated.

A constant default adds the column without rewriting ``library_items``. The
check is validated inline; the table stays small. Deprecated items stay
installed and keep their files and versions. New runs skip them.
"""

name = "skill_lifecycle"
order = 1020
pending_order = 1020

_STATEMENTS = (
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS lifecycle TEXT NOT NULL DEFAULT 'active'",
    """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'library_items_lifecycle_check'
          AND conrelid = to_regclass('library_items')
    ) THEN
        ALTER TABLE library_items
            ADD CONSTRAINT library_items_lifecycle_check
            CHECK (lifecycle IN ('active', 'deprecated'));
    END IF;
END $$""",
)
fresh_ddl = ";\n".join(_STATEMENTS) + ";"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    # A fresh database already applied fresh_ddl and records only the baseline marker.
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('library_items') "
        "AND attname = 'lifecycle' AND NOT attisdropped)")


async def apply(conn, *, sql, state) -> bool:
    if not await pending(conn):
        return False
    for statement in _STATEMENTS:
        await conn.execute(statement)
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return True
