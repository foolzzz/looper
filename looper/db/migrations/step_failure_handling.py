"""Durable step failure sequences, versioned holds, and their retained history."""
from __future__ import annotations

from . import OnlineStep, _widen_check
from ...agents.failures import CAUSE_CLASS, check_sql

name = "step_failure_handling"
order = pending_order = online_order = 1920

_COLUMNS = {
    "step_failure_state": {
        "task_id": "TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE",
        "step_key": "TEXT NOT NULL",
        "step_id": "TEXT REFERENCES task_steps(id) ON DELETE SET NULL",
        "streak": "INTEGER NOT NULL DEFAULT 0",
        "epoch": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
    },
    "step_failure_runs": {
        "task_id": "TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE",
        "run_id": "TEXT NOT NULL",
        "step_key": "TEXT NOT NULL",
        "outcome": "TEXT NOT NULL",
        "created_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
    },
    "step_failures": {
        "id": "BIGSERIAL PRIMARY KEY",
        "task_id": "TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE",
        "step_id": "TEXT REFERENCES task_steps(id) ON DELETE SET NULL",
        "step_identity": "TEXT NOT NULL",
        "step_key": "TEXT NOT NULL",
        "run_id": "TEXT NOT NULL",
        "attempt": "TEXT",
        "record_id": "BIGINT REFERENCES llm_records(id) ON DELETE SET NULL",
        "role": "TEXT NOT NULL",
        "failure_class": "TEXT NOT NULL",
        "failure_cause": "TEXT NOT NULL",
        "streak": "INTEGER NOT NULL",
        "epoch": "INTEGER NOT NULL DEFAULT 0",
        "action": "TEXT NOT NULL",
        "target": "JSONB NOT NULL DEFAULT '{}'",
        "result": "JSONB NOT NULL DEFAULT '{}'",
        "created_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
    },
    "step_holds": {
        "step_id": "TEXT PRIMARY KEY REFERENCES task_steps(id) ON DELETE CASCADE",
        "task_id": "TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE",
        "kind": "TEXT NOT NULL",
        "cause": "TEXT NOT NULL",
        "provider_id": "TEXT REFERENCES llm_providers(id) ON DELETE SET NULL",
        "until": "TIMESTAMPTZ",
        "failure_id": "BIGINT NOT NULL REFERENCES step_failures(id) ON DELETE CASCADE",
        "targets": "JSONB NOT NULL DEFAULT '[]'",
        "created_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
        "reminded_at": "TIMESTAMPTZ",
    },
    "step_failure_releases": {
        "failure_id": "BIGINT PRIMARY KEY REFERENCES step_failures(id) ON DELETE CASCADE",
        "task_id": "TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE",
        "step_identity": "TEXT NOT NULL",
        "action": "TEXT NOT NULL",
        "payload": "JSONB NOT NULL",
        "created_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
        "delivered_at": "TIMESTAMPTZ",
    },
    "task_quota_waits": {
        "task_id": "TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE",
        "started_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
        "deadline": "TIMESTAMPTZ NOT NULL",
    },
}
_KEYS = {
    "step_failure_state": "PRIMARY KEY(task_id, step_key)",
    "step_failure_runs": "PRIMARY KEY(task_id, run_id)",
}
_CHECKS = {
    ("step_failure_state", "step_failure_state_counts_check"): "streak >= 0 AND epoch >= 0",
    ("step_failure_runs", "step_failure_runs_outcome_check"): "outcome IN ('failed','success')",
    ("step_failures", "step_failures_role_check"): "role IN ('worker','reviewer')",
    ("step_failures", "step_failures_cause_check"): check_sql(),
    ("step_failures", "step_failures_counts_check"): "streak >= 1 AND epoch >= 0",
    ("step_failures", "step_failures_action_check"): "action IN ('hold','planner','breaker')",
    ("step_failures", "step_failures_target_check"): "jsonb_typeof(target) = 'object' AND jsonb_typeof(result) = 'object'",
    ("step_holds", "step_holds_kind_check"): "kind IN ('quota','transient','environment','breaker')",
    ("step_holds", "step_holds_until_check"): "(kind IN ('environment','breaker')) = (until IS NULL)",
    ("step_holds", "step_holds_targets_check"): "jsonb_typeof(targets) = 'array'",
    ("step_holds", "step_holds_cause_check"): " OR ".join(
        f"(kind = '{kind}' AND cause IN (" + ",".join(f"'{cause.value}'" for cause, cls in CAUSE_CLASS.items()
            if kind == 'breaker' or cls.value == kind) + "))"
        for kind in ('quota', 'transient', 'environment', 'breaker')),
}
_PRIMARY = {
    ("step_failure_state", "step_failure_state_pkey"): "PRIMARY KEY(task_id,step_key)",
    ("step_failure_runs", "step_failure_runs_pkey"): "PRIMARY KEY(task_id,run_id)",
    ("step_failures", "step_failures_pkey"): "PRIMARY KEY(id)",
    ("step_holds", "step_holds_pkey"): "PRIMARY KEY(step_id)",
    ("step_failure_releases", "step_failure_releases_pkey"): "PRIMARY KEY(failure_id)",
    ("task_quota_waits", "task_quota_waits_pkey"): "PRIMARY KEY(task_id)",
}
_FOREIGN = {
    (table, f"{table}_{column}_fkey"): f"FOREIGN KEY({column}) " + definition[definition.index("REFERENCES"):]
    for table, columns in _COLUMNS.items() for column, definition in columns.items() if "REFERENCES" in definition
}


async def _has_constraint(conn, table, constraint):
    return await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass($1) "
                               "AND conname=$2)", table, constraint)


_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_step_failures_step ON step_failures(step_id,id);
CREATE INDEX IF NOT EXISTS idx_step_failures_task ON step_failures(task_id,id);
CREATE INDEX IF NOT EXISTS idx_step_failures_sequence ON step_failures(task_id,step_key,epoch,id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_step_failures_run ON step_failures(task_id,run_id);
CREATE INDEX IF NOT EXISTS idx_step_failures_day ON step_failures(created_at,role,failure_class);
CREATE INDEX IF NOT EXISTS idx_step_failure_releases_pending ON step_failure_releases(task_id,failure_id) WHERE delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_step_failure_releases_day ON step_failure_releases(created_at) WHERE action='retry';
CREATE INDEX IF NOT EXISTS idx_step_holds_task ON step_holds(task_id);
CREATE INDEX IF NOT EXISTS idx_step_holds_until ON step_holds(until) WHERE until IS NOT NULL;
"""
_TABLE_DDL = "\n".join(
    f"CREATE TABLE IF NOT EXISTS {table} (" + ",".join(f"{column} {definition}" for column, definition in columns.items())
    + ("," + _KEYS[table] if table in _KEYS else "")
    + "," + ",".join(f"CONSTRAINT {constraint} CHECK ({predicate})" for (of, constraint), predicate in _CHECKS.items()
                     if of == table) + ");"
    if any(of == table for of, _ in _CHECKS) else
    f"CREATE TABLE IF NOT EXISTS {table} (" + ",".join(f"{column} {definition}" for column, definition in columns.items())
    + ("," + _KEYS[table] if table in _KEYS else "") + ");"
    for table, columns in _COLUMNS.items()) + _INDEX_DDL
_WIDEN_DDL = (_widen_check.ddl("tasks", "fail_kind", "tasks_fail_kind_check", ("quota_exhausted", "environment"))
              + _widen_check.ddl("task_journal", "kind", "task_journal_kind_check", ("failure_hold",)))
fresh_ddl = (_TABLE_DDL + _WIDEN_DDL + "ALTER TABLE tasks VALIDATE CONSTRAINT tasks_fail_kind_check;"
             "ALTER TABLE task_journal VALIDATE CONSTRAINT task_journal_kind_check;")
online = (OnlineStep(name, "step_failure_handling_online", validate=(
    ("tasks", "tasks_fail_kind_check"), ("task_journal", "task_journal_kind_check"))),)


async def pending(conn):
    for table, columns in _COLUMNS.items():
        if await conn.fetchval("SELECT count(*) FROM pg_attribute WHERE attrelid=to_regclass($1) "
                               "AND attname=ANY($2::text[]) AND attnum>0 AND NOT attisdropped", table, list(columns)) != len(columns):
            return True
    for index in ('idx_step_failures_step', 'idx_step_failures_task', 'idx_step_failures_sequence',
                  'idx_step_failures_run', 'idx_step_failures_day', 'idx_step_holds_task', 'idx_step_holds_until',
                  'idx_step_failure_releases_pending', 'idx_step_failure_releases_day'):
        if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", index):
            return True
    for table, constraint in (*_CHECKS, *_PRIMARY, *_FOREIGN):
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass($1) "
                                   "AND conname=$2)", table, constraint):
            return True
    for table, column, values in (("tasks", "fail_kind", ("quota_exhausted", "environment")),
                                  ("task_journal", "kind", ("failure_hold",))):
        if await _widen_check.pending(conn, table, column, values):
            return True
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass($1) "
                                   "AND conname=$2)", table, f"{table}_{column}_check"):
            return True
    return False


async def apply(conn, *, sql, state):
    if not await pending(conn):
        return
    # Restore objects even when a previous installation already wrote the marker.
    # Columns and checks are inspected separately: CREATE TABLE IF NOT EXISTS alone cannot repair a partial restore.
    for table, columns in _COLUMNS.items():
        if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
            for column, definition in columns.items():
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
    for (table, constraint), definition in _PRIMARY.items():
        if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table) and not await _has_constraint(conn, table, constraint):
            await conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} {definition}")
    await conn.execute(_TABLE_DDL)
    for (table, constraint), definition in _FOREIGN.items():
        if not await _has_constraint(conn, table, constraint):
            await conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} {definition}")
    for (table, constraint), predicate in _CHECKS.items():
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass($1) "
                                   "AND conname=$2)", table, constraint):
            await conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({predicate})")
    await conn.execute(_WIDEN_DDL)
    # A partial restore may have lost the whole old constraint, leaving nothing for _widen_check to extend.
    from ..journal import KINDS
    from ..tasks import FailKind
    for table, column, values in (("tasks", "fail_kind", tuple(item.value for item in FailKind)),
                                 ("task_journal", "kind", KINDS)):
        constraint = f"{table}_{column}_check"
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass($1) "
                                   "AND conname=$2)", table, constraint):
            literals = ",".join("'" + value + "'" for value in values)
            await conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({column} IN ({literals})) NOT VALID")
    await conn.execute("INSERT INTO schema_migrations(name) VALUES($1) ON CONFLICT DO NOTHING", name)
    await conn.execute("DELETE FROM schema_migrations WHERE name='step_failure_handling_online'")
