"""Step attempt observations; historical rows are not reconstructed."""

name = "step_attempts"
order = 1100
pending_order = 1100
fresh_ddl = """
CREATE TABLE IF NOT EXISTS step_attempts (
    id               BIGSERIAL PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id          TEXT REFERENCES task_steps(id) ON DELETE SET NULL,
    step_key         TEXT NOT NULL,
    task_run_no      INTEGER NOT NULL CHECK (task_run_no >= 1),
    attempt          INTEGER NOT NULL CHECK (attempt >= 1),
    trigger          TEXT NOT NULL CHECK (trigger IN ('first', 'replanned', 'retry', 'restart',
                                                      'reopen_late_review', 'reopen_verification')),
    profile_id       TEXT,
    worker_runs      INTEGER NOT NULL DEFAULT 0 CHECK (worker_runs >= 0),
    worker_record_id BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    run_outcome      TEXT CHECK (run_outcome IN ('completed', 'reported_failed', 'files_mismatch', 'contract',
                                                 'run_error', 'timeout', 'run_cap', 'crash')),
    review_outcome   TEXT CHECK (review_outcome IN ('none', 'passed', 'not_passed', 'passed_by_adjudication')),
    review_runs      INTEGER NOT NULL DEFAULT 0 CHECK (review_runs >= 0),
    blocking_findings INTEGER NOT NULL DEFAULT 0 CHECK (blocking_findings >= 0),
    adjudicated      BOOLEAN NOT NULL DEFAULT false,
    decision         TEXT CHECK (decision IN ('accept', 'retry', 'replan', 'fail', 'skip', 'task_ended')),
    failure_kind     TEXT CHECK (failure_kind IN ('review_rejected', 'contract', 'files_mismatch', 'reported_failed',
                                                  'run_error', 'timeout', 'run_cap', 'crash', 'task_budget',
                                                  'check_failed', 'stopped')),
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at       TIMESTAMPTZ,
    UNIQUE (task_id, task_run_no, step_key, attempt),
    CONSTRAINT step_attempts_decided CHECK ((decision IS NULL) = (decided_at IS NULL)),
    CONSTRAINT step_attempts_failure CHECK (decision IS DISTINCT FROM 'accept' OR failure_kind IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_step_attempts_step ON step_attempts (step_id) WHERE step_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_step_attempts_open
    ON step_attempts (task_id, step_key) WHERE decision IS NULL;
CREATE INDEX IF NOT EXISTS idx_step_attempts_decided ON step_attempts (decided_at) WHERE decided_at IS NOT NULL;
"""


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('step_attempts') IS NOT NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", name)
