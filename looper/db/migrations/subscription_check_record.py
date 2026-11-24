"""Link a subscription's retry and check, so daily spend reads use record primary keys."""

name = "subscription_check_record"
order = 1820
pending_order = 1820
fresh_ddl = (
    "ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS check_record_id BIGINT "
    "REFERENCES llm_records(id) ON DELETE SET NULL;\n"
    "ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS retry_record_id BIGINT "
    "REFERENCES llm_records(id) ON DELETE SET NULL;\n"
)


async def pending(conn):
    from .. import connection

    return (await connection._column_pending(conn, name, "subscription_runs", "check_record_id")
            or await connection._column_pending(conn, name, "subscription_runs", "retry_record_id"))


async def apply(conn, *, sql, state):
    if not await pending(conn):
        return
    await conn.execute(fresh_ddl)
    # Scan historical metadata once on upgrade, never once per firing in a spend read.
    await conn.execute("""
        UPDATE subscription_runs sr SET check_record_id = checks.id
        FROM (
            SELECT metadata->'leads_check'->>'run_id' AS run_id, max(id) AS id
            FROM llm_records WHERE metadata->'leads_check'->>'run_id' IS NOT NULL
            GROUP BY metadata->'leads_check'->>'run_id'
        ) checks
        WHERE sr.run_id = checks.run_id AND sr.check_record_id IS NULL
    """)
    await conn.execute("""
        UPDATE subscription_runs sr SET retry_record_id = retries.id
        FROM (
            SELECT metadata->'subscription'->>'run_id' AS run_id, min(id) AS id
            FROM llm_records WHERE metadata->'subscription'->>'run_id' IS NOT NULL
            GROUP BY metadata->'subscription'->>'run_id' HAVING count(*) > 1
        ) retries
        WHERE sr.run_id = retries.run_id AND sr.retry_record_id IS NULL AND sr.record_id <> retries.id
    """)
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
