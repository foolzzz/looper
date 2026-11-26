"""Nullable run attribution and per-task run numbers, without a backfill."""

from . import OnlineStep

name = "attempt_ids"
order = 1102
pending_order = 1102
_DDL = """
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS attempt_id BIGINT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS run_no INTEGER NOT NULL DEFAULT 1;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='llm_records'::regclass
                   AND conname='llm_records_attempt_id_fkey') THEN
        ALTER TABLE llm_records ADD CONSTRAINT llm_records_attempt_id_fkey
            FOREIGN KEY (attempt_id) REFERENCES step_attempts(id) ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='advisor_advice'::regclass
                   AND conname='advisor_advice_attempt_id_fkey') THEN
        ALTER TABLE advisor_advice ADD CONSTRAINT advisor_advice_attempt_id_fkey
            FOREIGN KEY (attempt_id) REFERENCES step_attempts(id) ON DELETE SET NULL NOT VALID;
    END IF;
END $$;
"""
fresh_ddl = _DDL.replace(" NOT VALID", "") + """
CREATE INDEX IF NOT EXISTS idx_llm_records_attempt
    ON llm_records(attempt_id) WHERE attempt_id IS NOT NULL;
"""


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", name):
        return False
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM (VALUES ('llm_records', 'attempt_id'), ('tasks', 'run_no')) "
        "AS required(table_name, column_name) WHERE NOT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid=to_regclass(required.table_name) AND attname=required.column_name "
        "AND attnum>0 AND NOT attisdropped))")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(_DDL)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", name)
    return did_work


online_order = 1102
online = (
    OnlineStep(
        migration=name,
        marker="attempt_ids_online",
        validate=(("llm_records", "llm_records_attempt_id_fkey"),
                  ("advisor_advice", "advisor_advice_attempt_id_fkey")),
        indexes=(("idx_llm_records_attempt", "ON llm_records(attempt_id) WHERE attempt_id IS NOT NULL"),),
    ),
)
