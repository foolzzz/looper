"""Project-skill versions, the current-version pointer, and the tombstone a delete leaves.

``tombstone_at`` is not in the original sketch. ``created_at`` cannot drive the
90-day cleanup: that would delete the history of a skill that is still in use.
The trigger covers every delete of a catalog item, including a project delete
cascading through ``owner_project_id``.
"""

name = "skill_versions"
order = 1010
pending_order = 1010

_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS skill_versions (
    id BIGSERIAL PRIMARY KEY,
    item_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    content TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    author_kind TEXT NOT NULL CHECK (author_kind IN ('operator', 'proposal', 'rollback')),
    author_ref TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tombstone_at TIMESTAMPTZ,
    UNIQUE (item_id, version)
)""",
    """CREATE INDEX IF NOT EXISTS idx_skill_versions_tombstone
    ON skill_versions (tombstone_at) WHERE tombstone_at IS NOT NULL""",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS skill_version INTEGER",
    """CREATE OR REPLACE FUNCTION skill_versions_entomb() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE skill_versions
       SET tombstone_at = now()
     WHERE item_id = OLD.id AND tombstone_at IS NULL;
    RETURN OLD;
END;
$$""",
    "DROP TRIGGER IF EXISTS skill_versions_entomb ON library_items",
    """CREATE TRIGGER skill_versions_entomb BEFORE DELETE ON library_items
    FOR EACH ROW EXECUTE FUNCTION skill_versions_entomb()""",
)
fresh_ddl = ";\n".join(_STATEMENTS) + ";"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    # A fresh database already applied fresh_ddl and records only the baseline marker.
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('library_items') "
        "AND attname = 'skill_version' AND NOT attisdropped)")


async def apply(conn, *, sql, state):
    if not await pending(conn):
        return
    for statement in _STATEMENTS:
        await conn.execute(statement)
    from ...agents.skill_versions import backfill

    await backfill(conn)
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
