"""Durable journal watermarks, attempt leases and an independent spend ledger."""

from . import OnlineStep
from .memory_lessons import _column, _constraint, _normalized

name = "memory_retro_queue"
order = pending_order = online_order = 1990
_COLUMNS = ("lease_token UUID", "lease_until TIMESTAMPTZ", "started_at TIMESTAMPTZ",
            "last_failure TEXT", "completed_notified_at TIMESTAMPTZ")
_TABLES = {
    "memory_retro_cursors": (
        ("task_id TEXT NOT NULL", "last_to_id BIGINT NOT NULL"),
        {"memory_retro_cursors_pkey": "PRIMARY KEY (task_id)",
         "memory_retro_cursors_task_id_fkey": "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE"}),
    "memory_retro_task_refs": (
        ("retro_id BIGINT NOT NULL", "candidate_id BIGINT NOT NULL"),
        {"memory_retro_task_refs_pkey": "PRIMARY KEY (retro_id,candidate_id)",
         "memory_retro_task_refs_retro_id_fkey": "FOREIGN KEY (retro_id) REFERENCES memory_retro_runs(id) ON DELETE CASCADE",
         "memory_retro_task_refs_candidate_id_fkey": "FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id) ON DELETE CASCADE"}),
    "memory_lesson_reinforcements": (
        ("candidate_id BIGINT NOT NULL", "task_id TEXT NOT NULL"),
        {"memory_lesson_reinforcements_pkey": "PRIMARY KEY (candidate_id,task_id)",
         "memory_lesson_reinforcements_candidate_id_fkey": "FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id) ON DELETE CASCADE",
         "memory_lesson_reinforcements_task_id_fkey": "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE"}),
    "memory_retro_spend": (
        ("attempt_token UUID NOT NULL", "run_id BIGINT", "record_id BIGINT", "day DATE NOT NULL",
         "reserved_usd NUMERIC NOT NULL", "settled_usd NUMERIC", "state TEXT NOT NULL"),
        {"memory_retro_spend_pkey": "PRIMARY KEY (attempt_token)",
         "memory_retro_spend_run_id_fkey": "FOREIGN KEY (run_id) REFERENCES memory_retro_runs(id) ON DELETE SET NULL",
         "memory_retro_spend_record_id_fkey": "FOREIGN KEY (record_id) REFERENCES llm_records(id) ON DELETE SET NULL",
         "memory_retro_spend_reserved_check": "CHECK ((reserved_usd > (0)::numeric))",
         "memory_retro_spend_state_check": "CHECK ((state = ANY (ARRAY['reserved'::text,'settled'::text,'unknown'::text])))"}),
    "memory_retro_activation": (
        ("id BOOLEAN NOT NULL DEFAULT true", "enabled_at TIMESTAMPTZ NOT NULL", "journal_highwater BIGINT NOT NULL",
         "bootstrap_after TEXT NOT NULL DEFAULT ''", "bootstrap_done BOOLEAN NOT NULL DEFAULT false",
         "failure_tasks TEXT[] NOT NULL DEFAULT '{}'::text[]", "failure_notice_claimed BOOLEAN NOT NULL DEFAULT false"),
        {"memory_retro_activation_pkey": "PRIMARY KEY (id)", "memory_retro_activation_id_check": "CHECK (id)"}),
}

_INDEXES = (
    ("idx_intents_retro_task", "ON intents (task_id) WHERE (task_id IS NOT NULL)"),
    ("idx_tasks_retro_terminal", "ON tasks (COALESCE(completed_at,updated_at),id) WHERE (status = ANY (ARRAY['completed'::text,'failed'::text]))"),
)
online = (OnlineStep(name, "memory_retro_queue_online", indexes=_INDEXES),)


def _create(table):
    columns, constraints = _TABLES[table]
    return (f"CREATE TABLE IF NOT EXISTS {table} (" + ", ".join(columns) + ", " +
            ", ".join(f"CONSTRAINT {key} {value}" for key,value in constraints.items()) + ");")


fresh_ddl = ("\n".join(f"ALTER TABLE memory_retro_runs ADD COLUMN IF NOT EXISTS {column};" for column in _COLUMNS)
             + "\n" + "\n".join(_create(table) for table in _TABLES)
             + "\n" + "\n".join(f"CREATE INDEX IF NOT EXISTS {key} {definition};" for key,definition in _INDEXES))


async def _statements(conn):
    ddl = []
    for declaration in _COLUMNS:
        if statement := await _column(conn, "memory_retro_runs", declaration):
            ddl.append(statement)
    for table, (columns, constraints) in _TABLES.items():
        if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
            ddl.append(_create(table))
            continue
        for declaration in columns:
            if statement := await _column(conn, table, declaration):
                ddl.append(statement)
        for key, definition in constraints.items():
            if statement := await _constraint(conn, table, key, definition):
                ddl.append(statement)
    return ddl


async def _missing_indexes(conn):
    for key, definition in _INDEXES:
        table, tail = definition.removeprefix("ON ").split(" ", 1)
        row = await conn.fetchrow("SELECT indisvalid,indisunique,indrelid=to_regclass($2) AS same_table, "
            "pg_get_indexdef(indexrelid) AS definition FROM pg_index WHERE indexrelid=to_regclass($1)", key, table)
        if row is None or not row["indisvalid"]:
            return True
        if row["indisunique"] or not row["same_table"] or _normalized(
                row["definition"].split(" USING ", 1)[1]) != _normalized("btree " + tail):
            raise RuntimeError(f"incompatible retrospective index: {key}")
    return False


async def pending(conn):
    return bool(await _statements(conn) or await _missing_indexes(conn))


async def apply(conn, *, sql, state):
    statements = await _statements(conn)
    if statements or await _missing_indexes(conn):
        for statement in statements:
            await conn.execute(statement)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name=$1", online[0].marker)
