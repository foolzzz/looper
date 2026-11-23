"""Append-only task outcomes observations; historical rows are not reconstructed."""

name = "task_outcomes"
order = 1101
pending_order = 1101
fresh_ddl = """
CREATE TABLE IF NOT EXISTS task_outcomes (
    id             BIGSERIAL PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_no         INTEGER NOT NULL CHECK (run_no >= 1),
    outcome        TEXT NOT NULL CHECK (outcome IN ('completed', 'failed', 'cancelled', 'stopped')),
    stop_cause     TEXT NOT NULL CHECK (stop_cause IN ('planner_end', 'task_budget', 'user_stop', 'user_cancel',
                                                       'manager_cancel', 'restart', 'teardown', 'error')),
    failure_kind   TEXT CHECK (failure_kind IN ('task_budget', 'planner_failed', 'steps_failed', 'error')),
    started_at     TIMESTAMPTZ,
    ended_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    human_wait_ms  BIGINT NOT NULL DEFAULT 0,
    steps_accepted INTEGER NOT NULL DEFAULT 0,
    steps_failed   INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    first_pass_steps INTEGER NOT NULL DEFAULT 0,
    provider_priced_usd DOUBLE PRECISION,
    equivalent_usd DOUBLE PRECISION,
    unpriced_runs  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (task_id, run_no)
);
CREATE INDEX IF NOT EXISTS idx_task_outcomes_ended ON task_outcomes (ended_at);
"""


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('task_outcomes') IS NOT NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", name)
