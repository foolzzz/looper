"""Lesson foundation tables; existing candidate indexes are built after commit."""

from __future__ import annotations

import re

from . import OnlineStep

name = "memory_lessons"
order = pending_order = online_order = 1910

# Keep the table declarations shared by fresh DDL and object-level upgrade checks.
_TABLES = {
    "task_lesson_snapshots": (
        "task_id TEXT NOT NULL, candidate_id BIGINT NOT NULL, position SMALLINT NOT NULL",
        {
            "task_lesson_snapshots_pkey": "PRIMARY KEY (task_id, candidate_id)",
            "task_lesson_snapshots_task_id_position_key": "UNIQUE (task_id, \"position\")",
            "task_lesson_snapshots_position_check": "CHECK (((\"position\" >= 1) AND (\"position\" <= 50)))",
            "task_lesson_snapshots_task_id_fkey": "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE",
            "task_lesson_snapshots_candidate_id_fkey": "FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id) ON DELETE CASCADE",
        }),
    "memory_injections": (
        "id BIGSERIAL NOT NULL, task_id TEXT NOT NULL, agent_kind TEXT NOT NULL, step_id TEXT, record_id BIGINT, "
        "block_sha256 TEXT NOT NULL, block_chars INTEGER NOT NULL, omitted INTEGER NOT NULL DEFAULT 0, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        {
            "memory_injections_pkey": "PRIMARY KEY (id)",
            "memory_injections_task_id_fkey": "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE",
            "memory_injections_record_id_fkey": "FOREIGN KEY (record_id) REFERENCES llm_records(id) ON DELETE SET NULL",
            "memory_injections_agent_kind_check": "CHECK ((agent_kind = ANY (ARRAY['planner'::text, 'worker'::text, 'reviewer'::text])))",
            "memory_injections_block_sha256_check": "CHECK ((block_sha256 ~ '^[0-9a-f]{64}$'::text))",
        }),
    "memory_injection_items": (
        "injection_id BIGINT NOT NULL, candidate_id BIGINT NOT NULL, position SMALLINT NOT NULL, "
        "cited BOOLEAN NOT NULL DEFAULT false, wrong BOOLEAN NOT NULL DEFAULT false",
        {
            "memory_injection_items_pkey": "PRIMARY KEY (injection_id, candidate_id)",
            "memory_injection_items_injection_id_fkey": "FOREIGN KEY (injection_id) REFERENCES memory_injections(id) ON DELETE CASCADE",
            "memory_injection_items_candidate_id_fkey": "FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id) ON DELETE CASCADE",
            "memory_injection_items_check": "CHECK ((NOT (cited AND wrong)))",
        }),
    "memory_lesson_stats": (
        "candidate_id BIGINT NOT NULL, injected INTEGER NOT NULL DEFAULT 0, cited INTEGER NOT NULL DEFAULT 0, "
        "wrong_tasks INTEGER NOT NULL DEFAULT 0, reinforced INTEGER NOT NULL DEFAULT 0, "
        "last_injected_at TIMESTAMPTZ, last_cited_at TIMESTAMPTZ",
        {
            "memory_lesson_stats_pkey": "PRIMARY KEY (candidate_id)",
            "memory_lesson_stats_candidate_id_fkey": "FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id) ON DELETE CASCADE",
        }),
    "memory_retro_runs": (
        "id BIGSERIAL NOT NULL, task_id TEXT NOT NULL, journal_from_id BIGINT NOT NULL, journal_to_id BIGINT NOT NULL, "
        "state TEXT NOT NULL DEFAULT 'pending', skip_reason TEXT, attempts INTEGER NOT NULL DEFAULT 0, record_id BIGINT, "
        "counts JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ",
        {
            "memory_retro_runs_pkey": "PRIMARY KEY (id)",
            "memory_retro_runs_task_id_fkey": "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE",
            "memory_retro_runs_record_id_fkey": "FOREIGN KEY (record_id) REFERENCES llm_records(id) ON DELETE SET NULL",
            "memory_retro_runs_task_id_journal_to_id_key": "UNIQUE (task_id, journal_to_id)",
            "memory_retro_runs_state_check": "CHECK ((state = ANY (ARRAY['pending'::text, 'running'::text, 'done'::text, 'failed'::text, 'skipped'::text])))",
            "memory_retro_runs_skip_reason_check": "CHECK ((skip_reason = ANY (ARRAY['few_entries'::text, 'budget'::text, 'project_off'::text, 'disabled'::text])))",
            "memory_retro_runs_counts_check": "CHECK ((jsonb_typeof(counts) = 'object'::text))",
            "memory_retro_runs_check": "CHECK ((journal_from_id <= journal_to_id))",
            "memory_retro_runs_check1": "CHECK (((state = 'skipped'::text) = (skip_reason IS NOT NULL)))",
        }),
    "memory_weekly_cards": (
        "week_start DATE NOT NULL, state TEXT NOT NULL DEFAULT 'pending', channel_id TEXT, chat TEXT, external_id TEXT, "
        "text TEXT NOT NULL DEFAULT '', shown_full BIGINT[] NOT NULL DEFAULT '{}', counts JSONB NOT NULL DEFAULT '{}', "
        "attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), sent_at TIMESTAMPTZ",
        {
            "memory_weekly_cards_pkey": "PRIMARY KEY (week_start)",
            "memory_weekly_cards_state_check": "CHECK ((state = ANY (ARRAY['pending'::text, 'sending'::text, 'sent'::text, 'failed'::text, 'uncertain'::text, 'empty'::text])))",
            "memory_weekly_cards_counts_check": "CHECK ((jsonb_typeof(counts) = 'object'::text))",
        }),
}
_PROJECT_CHECK = "CHECK ((memory_lessons = ANY (ARRAY['off'::text, 'on'::text])))"
_TOMBSTONE_CHECK = "CHECK ((deleted_via = ANY (ARRAY['dashboard'::text, 'cli'::text, 'lark'::text, 'telegram'::text])))"
_OLD_TOMBSTONE_CHECK = "CHECK ((deleted_via = ANY (ARRAY['dashboard'::text, 'cli'::text])))"
_INDEXES = (
    ("idx_memory_candidates_lessons", "ON memory_candidates (project_id, state) WHERE (destination = 'looper'::text)"),
    ("idx_memory_candidates_lesson_expiry", "ON memory_candidates (valid_until) WHERE ((destination = 'looper'::text) AND (state = 'in_effect'::text))"),
    ("idx_memory_injections_task", "ON memory_injections (task_id, id)"),
    ("idx_memory_injections_created", "ON memory_injections (created_at)"),
    ("idx_memory_injection_items_candidate", "ON memory_injection_items (candidate_id)"),
    ("idx_memory_retro_runs_pending", "ON memory_retro_runs (id) WHERE (state = 'pending'::text)"),
)


def _create(table):
    columns, constraints = _TABLES[table]
    return (f"CREATE TABLE IF NOT EXISTS {table} ({columns}, " +
            ", ".join(f"CONSTRAINT {key} {value}" for key, value in constraints.items()) + ");")


_TOMBSTONE_DDL = ("ALTER TABLE memory_tombstones DROP CONSTRAINT IF EXISTS memory_tombstones_deleted_via_check; "
                  "ALTER TABLE memory_tombstones ADD CONSTRAINT memory_tombstones_deleted_via_check " +
                  _TOMBSTONE_CHECK + " NOT VALID;")
fresh_ddl = ("\n".join(_create(table) for table in _TABLES) +
             "\nALTER TABLE projects ADD COLUMN IF NOT EXISTS memory_lessons TEXT; "
             "ALTER TABLE projects ADD CONSTRAINT projects_memory_lessons_check " + _PROJECT_CHECK + ";\n" +
             _TOMBSTONE_DDL + "ALTER TABLE memory_tombstones VALIDATE CONSTRAINT memory_tombstones_deleted_via_check;\n" +
             "\n".join(f"CREATE INDEX IF NOT EXISTS {key} {definition};" for key, definition in _INDEXES))
online = (OnlineStep(name, "memory_lessons_online",
                    validate=(("memory_tombstones", "memory_tombstones_deleted_via_check"),),
                    indexes=_INDEXES[:2]),)


def _normalized(definition):
    # Compare catalog-form predicates with their grouping intact; quoted values stay case-sensitive.
    from . import _split_literals
    return "".join(text if kind == "lit" else re.sub(r'[\s"]', "", text.lower())
                   .replace("::text", "").replace("::jsonb", "").replace("::bigint[]", "")
                   for kind, text in _split_literals(re.sub(r"\s+NOT VALID$", "", definition)))


async def _column(conn, table, declaration):
    column, rest = declaration.strip().split(" ", 1)
    row = await conn.fetchrow(
        "SELECT format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default "
        "FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
        "WHERE a.attrelid=to_regclass($1) AND a.attname=$2 AND a.attnum>0 AND NOT a.attisdropped", table, column)
    if row is None:
        return f"ALTER TABLE {table} ADD COLUMN {declaration};"
    raw_type = rest.split()[0]
    expected_type = {"BIGSERIAL": "bigint", "TIMESTAMPTZ": "timestamp with time zone"}.get(raw_type, raw_type.lower())
    expected_default = rest.split(" DEFAULT ", 1)[1] if " DEFAULT " in rest else ""
    actual_default = row["default"] or ""
    default_matches = (actual_default.startswith("nextval(") if raw_type == "BIGSERIAL" else
                       _normalized(actual_default) == _normalized(expected_default))
    if (row["type"] != expected_type or row["attnotnull"] != ("NOT NULL" in rest) or not default_matches):
        raise RuntimeError(f"incompatible memory lessons column: {table}.{column}")
    return None


async def _constraint(conn, table, key, definition, *, old=None):
    row = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                              "WHERE conrelid=to_regclass($1) AND conname=$2", table, key)
    if row is None:
        return f"ALTER TABLE {table} ADD CONSTRAINT {key} {definition};"
    if _normalized(row) == _normalized(definition):
        return None
    if old is not None and _normalized(row) == _normalized(old):
        return _TOMBSTONE_DDL
    raise RuntimeError(f"incompatible memory lessons constraint: {table}.{key}")


async def _statements(conn):
    statements = []
    for table, (columns, constraints) in _TABLES.items():
        if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
            statements.append(_create(table))
            continue
        for declaration in columns.split(", "):
            if ddl := await _column(conn, table, declaration):
                statements.append(ddl)
        for key, definition in constraints.items():
            if ddl := await _constraint(conn, table, key, definition):
                statements.append(ddl)
    if ddl := await _column(conn, "projects", "memory_lessons TEXT"):
        statements.append(ddl)
    if ddl := await _constraint(conn, "projects", "projects_memory_lessons_check", _PROJECT_CHECK):
        statements.append(ddl)
    if ddl := await _constraint(conn, "memory_tombstones", "memory_tombstones_deleted_via_check", _TOMBSTONE_CHECK,
                                old=_OLD_TOMBSTONE_CHECK):
        # Existing rows are validated after the transactional migration commits.
        statements.append(ddl if ddl == _TOMBSTONE_DDL else ddl[:-1] + " NOT VALID;")
    return statements


async def _missing_indexes(conn):
    missing = []
    for key, definition in _INDEXES:
        table, tail = definition.removeprefix("ON ").split(" ", 1)
        row = await conn.fetchrow("SELECT indisvalid,indisunique,indrelid=to_regclass($2) AS same_table, "
            "pg_get_indexdef(indexrelid) AS definition FROM pg_index WHERE indexrelid=to_regclass($1)", key, table)
        if row is None or not row["indisvalid"]:
            missing.append((key, definition))
        elif (row["indisunique"] or not row["same_table"] or
              _normalized(row["definition"].split(" USING ", 1)[1]) != _normalized("btree " + tail)):
            raise RuntimeError(f"incompatible memory lessons index: {key}")
    return missing


async def _validation_pending(conn):
    return await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_constraint "
        "WHERE conrelid=to_regclass('memory_tombstones') "
        "AND conname='memory_tombstones_deleted_via_check' AND NOT convalidated)")


async def pending(conn):
    return bool(await _statements(conn) or await _missing_indexes(conn) or await _validation_pending(conn))


async def apply(conn, *, sql, state) -> bool:
    statements = await _statements(conn)
    missing_indexes = await _missing_indexes(conn)
    if not statements and not missing_indexes and not await _validation_pending(conn):
        return False
    for ddl in statements:
        await conn.execute(ddl)
    for key, definition in missing_indexes:
        if (key, definition) not in _INDEXES[:2]:
            await conn.execute(f"DROP INDEX IF EXISTS {key}")
            await conn.execute(f"CREATE INDEX {key} {definition}")
    await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    await conn.execute("DELETE FROM schema_migrations WHERE name=$1", online[0].marker)
    return True
