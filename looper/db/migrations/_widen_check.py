"""Widen a single-column CHECK from the values it has now, never from a list of its own.

Several migrations add values to the same CHECK (a shared status or source column), and which of them ran differs by
database. So this builds the new check from the values the old one names today plus the ones to add, on a database that
has applied the others and on one that has not. The new check is ``NOT VALID``: its validation runs after commit (the
online step), so no scan of the table holds the schema lock. The check is found by the one column it constrains, not by
its name; it is created under ``constraint``.
"""


def ddl(table: str, column: str, constraint: str, add: tuple[str, ...]) -> str:
    values = ", ".join(f"'{value}'" for value in add)
    return f"""
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
     WHERE c.conrelid = to_regclass('{table}') AND c.contype = 'c' AND a.attname = '{column}'
           AND a.attnum > 0 AND NOT a.attisdropped AND cardinality(c.conkey) = 1
     ORDER BY c.conname LIMIT 1;
    IF old_name IS NULL THEN
        RETURN;
    END IF;
    EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', old_name);
    EXECUTE format(
        'ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({column} IN (%s)) NOT VALID',
        (SELECT string_agg(quote_literal(v), ', ' ORDER BY first_seen)
           FROM (SELECT v, min(ord) AS first_seen
                   FROM unnest(old_values || ARRAY[{values}]) WITH ORDINALITY AS u(v, ord) GROUP BY v) AS d));
END
$migration$;
"""


async def pending(conn, table: str, column: str, add: tuple[str, ...]) -> bool:
    """The column's check misses one of ``add``. The marker is not consulted: a check another migration rebuilt
    without a value is widened again. Without the table or the check there is nothing to widen."""
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
        "WHERE c.conrelid = to_regclass($1) AND c.contype = 'c' AND a.attname = $2 "
        "AND a.attnum > 0 AND NOT a.attisdropped AND cardinality(c.conkey) = 1 "
        "AND EXISTS (SELECT 1 FROM unnest($3::text[]) AS v "
        "WHERE strpos(pg_get_constraintdef(c.oid), '''' || v || '''') = 0))", table, column, list(add)))
