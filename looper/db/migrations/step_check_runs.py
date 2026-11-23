"""Append-only execution evidence for step acceptance checks."""

name = "step_check_runs"
order = 1010
pending_order = 1010
fresh_ddl = """
CREATE TABLE IF NOT EXISTS step_check_runs (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL REFERENCES task_steps(id) ON DELETE CASCADE,
    check_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 0),
    run INTEGER NOT NULL CHECK (run >= 1),
    criterion INTEGER NOT NULL CHECK (criterion >= 1),
    type TEXT NOT NULL CHECK (type IN ('command', 'artifact')),
    required BOOLEAN NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running', 'passed', 'failed', 'error', 'stale')),
    error_kind TEXT CHECK (error_kind IN ('could_not_start', 'timeout', 'changed_code', 'output_limit',
        'artifact_invalid', 'evidence_unavailable', 'interrupted', 'budget', 'sandbox_unavailable')),
    exit_code INTEGER,
    duration_ms INTEGER CHECK (duration_ms >= 0),
    head_sha TEXT,
    content_sha TEXT,
    guarded BOOLEAN NOT NULL,
    output_tail TEXT NOT NULL DEFAULT '' CHECK (length(output_tail) <= 4000),
    output_bytes BIGINT NOT NULL DEFAULT 0 CHECK (output_bytes >= 0),
    output_sha256 TEXT,
    artifacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    protected_changed TEXT[] NOT NULL DEFAULT '{}',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CHECK ((state = 'error') = (error_kind IS NOT NULL)),
    CHECK ((state = 'running') = (finished_at IS NULL)),
    UNIQUE (step_id, check_id, attempt, run)
);
CREATE INDEX IF NOT EXISTS step_check_runs_task ON step_check_runs (task_id, step_id);
"""


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return await conn.fetchval("SELECT to_regclass('step_check_runs') IS NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
