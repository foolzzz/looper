-- LoopX Agent System Schema
-- Idempotent: safe to run on every startup.

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    requirement       TEXT NOT NULL,
    goal_id           TEXT NOT NULL DEFAULT '',
    project_root      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',
    planner_id        TEXT,
    correlation_id    TEXT NOT NULL DEFAULT '',
    origin_channel_id TEXT NOT NULL DEFAULT '',
    result_summary    TEXT,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS activity (
    seq              BIGSERIAL PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id),
    worker           TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL,
    summary          TEXT,
    detail           TEXT,
    metadata         JSONB,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_activity_task ON activity(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_activity_task_worker ON activity(task_id, worker, seq);

CREATE TABLE IF NOT EXISTS task_steps (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id),
    step_index       INTEGER NOT NULL,
    instruction      TEXT NOT NULL,
    target_files     TEXT[],
    status           TEXT NOT NULL DEFAULT 'pending',
    worker_id        TEXT,
    result           JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_steps_task ON task_steps(task_id, step_index);
