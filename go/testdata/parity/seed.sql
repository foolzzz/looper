-- Parity seed for GET /api/status, GET /api/projects, and GET /api/overview.
-- Applied after looper/db/schema.sql.
--
-- The harness clock is local noon on 2026-06-15 (make_goldens.py and the Go
-- parity test). A row at 2026-06-15T12:00:00Z is on or after that local
-- midnight for every offset from UTC-12 through UTC+14, because overview has
-- no upper bound (created_at >= local midnight). Rows in 2020 are before that
-- midnight everywhere. Today's positive spend and skipped firings must show
-- up. The 2020 negatives and the extra historical firings must not: a reader
-- that returns zero, or one that ignores the date, will not match the golden.
--
-- Alpha is active (executing, waiting_user, init). Beta is idle (completed,
-- archived). Alpha's supervise interval is inside Python's 0..86400 range.
-- Beta's 2147483647 is schema-valid (INTEGER >= 0) and Beta's budget of
-- 200000 is schema-valid (task_budget_usd > 0). ProjectExecution.from_row
-- rejects both, so the API returns null.

INSERT INTO projects (
    id, name, description, work_dir, background,
    task_budget_usd, supervise_interval_seconds, workspace_links, created_at, updated_at
) VALUES
(
    'proj-alpha0001', 'Alpha', 'first project', '/work/alpha', 'alpha notes',
    1.5, 30, ARRAY['.venv'],
    '2020-01-15T00:00:00Z', '2020-01-16T00:00:00Z'
),
(
    'proj-beta00002', 'Beta', '', '', '',
    200000, 2147483647, NULL,
    '2020-02-01T00:00:00Z', '2020-02-02T00:00:00Z'
);

INSERT INTO schedules (
    id, project_id, name, cron, timezone, requirement,
    enabled, next_fire_at, consecutive_failures, enabled_at, created_at, updated_at
) VALUES (
    'sched-nightly01', 'proj-alpha0001', 'Nightly', '0 9 * * *', 'UTC', 'run the nightly check',
    true, '2020-04-01T09:00:00Z', 2, '2020-01-20T00:00:00Z', '2020-01-20T00:00:00Z', '2020-01-20T00:00:00Z'
);

INSERT INTO tasks (id, requirement, project_id, status, created_at, updated_at) VALUES
('task-runalpha1', 'do the alpha work', 'proj-alpha0001', 'executing', '2020-03-01T00:00:00Z', '2020-03-01T00:00:00Z'),
('task-waitalpha1', 'need a decision', 'proj-alpha0001', 'waiting_user', '2020-03-02T00:00:00Z', '2020-03-02T00:00:00Z'),
('task-readyalph1', 'ready to start', 'proj-alpha0001', 'init', '2020-03-03T00:00:00Z', '2020-03-03T00:00:00Z'),
('task-donebeta01', 'beta finished', 'proj-beta00002', 'completed', '2020-03-04T00:00:00Z', '2020-03-04T00:00:00Z'),
('task-archbeta01', 'beta archived', 'proj-beta00002', 'archived', '2020-03-05T00:00:00Z', '2020-03-05T00:00:00Z');

INSERT INTO schedule_runs (
    id, schedule_id, scheduled_for, trigger, status, firings, created_at
) VALUES
(
    'run-skipbudget1', 'sched-nightly01', '2020-05-01T09:00:00Z', 'scheduled',
    'skipped_budget', 3, '2020-05-01T09:00:00Z'
),
(
    'run-skipgateold', 'sched-nightly01', '2020-05-02T09:00:00Z', 'scheduled',
    'skipped_gate', 4, '2020-05-02T09:00:00Z'
),
(
    'run-skipgatetdy', 'sched-nightly01', '2026-06-15T12:00:00Z', 'scheduled',
    'skipped_gate', 2, '2026-06-15T12:00:00Z'
),
(
    'run-skipbudgtdy', 'sched-nightly01', '2026-06-15T12:00:01Z', 'scheduled',
    'skipped_budget', 1, '2026-06-15T12:00:01Z'
);

INSERT INTO llm_records (agent_id, task_id, status, cost_usd, metadata, created_at) VALUES
('worker-alpha01', 'task-runalpha1', 'completed', 0.5, NULL, '2020-03-01T00:00:00Z'),
('worker-alphaneg', 'task-runalpha1', 'completed', -4.0, NULL, '2020-03-02T00:00:00Z'),
('worker-provold1', 'task-runalpha1', 'completed', -1.0, '{"profile":{"provider":{"auth":"api_key"}}}'::jsonb, '2020-03-03T00:00:00Z'),
('worker-alphatdy', 'task-runalpha1', 'completed', 1.25, NULL, '2026-06-15T12:00:00Z'),
('worker-provtdy1', 'task-runalpha1', 'completed', 2.0, '{"profile":{"provider":{"auth":"api_key"}}}'::jsonb, '2026-06-15T12:00:00Z');
