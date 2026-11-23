"""Let ``proactive_deliveries.source`` name an idle check.

Other migrations widen the same check (the morning brief, event triage), and which of them ran differs by
database. So this one never writes a list of its own: it rebuilds the check from the values the check has now, plus
``idle_check``, on a database that has applied the others and on one that has not. The new check is ``NOT VALID``;
its validation runs after commit (the online step), so no scan of the table holds the schema lock.
"""

from . import OnlineStep

name = "idle_check_source"
order = 1210  # after idle_checks, and after every widening of the check that is released or open
pending_order = 1210
online_order = 1210
online = (OnlineStep(name, "idle_check_source_online", validate=(
    ("proactive_deliveries", "proactive_deliveries_source_check"),
)),)

_VALUE = "idle_check"
# The CHECK on ``proactive_deliveries.source`` alone. Its name is PostgreSQL's, not ours; a check that merely
# mentions a source value in another column is not this one. The new check is built from the old one's values.
_DDL = """
DO $migration$
DECLARE
    old_name text;
    old_values text[];
BEGIN
    SELECT c.conname,
           ARRAY(SELECT t.parts[1] FROM regexp_matches(pg_get_constraintdef(c.oid), '''([a-z_]+)''', 'g') AS t(parts))
      INTO old_name, old_values
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
     WHERE c.conrelid = to_regclass('proactive_deliveries') AND c.contype = 'c' AND a.attname = 'source'
           AND a.attnum > 0 AND NOT a.attisdropped AND cardinality(c.conkey) = 1
     ORDER BY c.conname LIMIT 1;
    IF old_name IS NULL THEN
        RETURN;
    END IF;
    EXECUTE format('ALTER TABLE proactive_deliveries DROP CONSTRAINT %I', old_name);
    EXECUTE format(
        'ALTER TABLE proactive_deliveries ADD CONSTRAINT proactive_deliveries_source_check '
        'CHECK (source IN (%s)) NOT VALID',
        (SELECT string_agg(quote_literal(v), ', ' ORDER BY first_seen)
           FROM (SELECT v, min(ord) AS first_seen
                   FROM unnest(old_values || ARRAY['idle_check']) WITH ORDINALITY AS u(v, ord) GROUP BY v) AS d));
END
$migration$;
"""
fresh_ddl = _DDL + "ALTER TABLE proactive_deliveries VALIDATE CONSTRAINT proactive_deliveries_source_check;\n"


async def pending(conn) -> bool:
    """The source check does not name ``idle_check`` yet. The marker is not consulted: a check another migration
    rebuilt without it is widened again. Without the table or the check there is nothing to widen."""
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
        "WHERE c.conrelid = to_regclass('proactive_deliveries') AND c.contype = 'c' AND a.attname = 'source' "
        "AND a.attnum > 0 AND NOT a.attisdropped AND cardinality(c.conkey) = 1 "
        "AND strpos(pg_get_constraintdef(c.oid), $1) = 0)", f"'{_VALUE}'"))


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await pending(conn):
        await conn.execute(_DDL)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    elif await legacy._unvalidated(conn, name):
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
