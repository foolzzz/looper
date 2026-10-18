-- Parity seed for GET /api/status, GET /api/projects, GET /api/overview,
-- and GET /api/tasks.
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
-- Alpha is active (in progress, waiting_user, init). Beta is idle: completed,
-- archived, failed, stopped, cancelled. proj-blank0001 has an empty name and
-- one init task, so overview lists it. Python sorts that list by
-- (name or project_id).lower(): Alpha, then the blank id. The projects list
-- is SQL lower(name), so the blank name is first.
-- task-zsame0001 and task-asame0001 share created_at. limit=2 returns z then
-- a (id DESC) and next_before is task-asame0001. Every task status is present.
-- Fifty-seven tasks, more than the default page of 50, so GET /api/tasks
-- sets next_before. The two newest stay z then a. task-sepname001 and
-- task-sepfall001 are stopped tasks whose titles use U+001C–U+001F, which
-- Python counts as whitespace. Forty completed fillers on Beta are dated in
-- 2019, after every named task.
-- Alpha's supervise interval is inside Python's 0..86400 range.
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
),
(
    'proj-blank0001', '', '', '', '',
    NULL, NULL, NULL,
    '2020-02-15T00:00:00Z', '2020-02-15T00:00:00Z'
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

UPDATE tasks SET completed_at = '2020-03-04T12:00:00Z' WHERE id = 'task-donebeta01';

INSERT INTO tasks (
    id, requirement, project_id, status, title, goal_id, work_dir, phase,
    planner_id, correlation_id, schedule_id, created_at, updated_at, completed_at
) VALUES
(
    'task-zsame0001', 'ship it', 'proj-alpha0001', 'running',
    '  Ship   the patch  ', 'goal-z', '/work/alpha/ship', 'executing',
    'planner-task-zsame0001', 'corr-z', 'sched-nightly01',
    '2020-06-01T00:00:00Z', '2020-06-01T00:00:00Z', NULL
),
(
    'task-asame0001', 'ignored because goal', 'proj-alpha0001', 'assigned',
    NULL, '  goal   alpha  ', '', 'pending',
    NULL, '', NULL,
    '2020-06-01T00:00:00Z', '2020-06-01T00:00:00Z', NULL
),
(
    'task-planalpha1', E'Plan the\nsecond line stays out', 'proj-alpha0001', 'planning',
    NULL, '', '', 'planning',
    NULL, '', NULL,
    '2020-05-20T00:00:00Z', '2020-05-20T00:00:00Z', NULL
),
(
    'task-verialpha1', 'verify the result', 'proj-alpha0001', 'verifying',
    NULL, '', '', 'verifying',
    NULL, '', NULL,
    '2020-05-19T00:00:00Z', '2020-05-19T00:00:00Z', NULL
),
(
    'task-stopalpha1', 'stopping at the cap', 'proj-alpha0001', 'stopping',
    NULL, '', '', 'stopping',
    NULL, '', NULL,
    '2020-05-18T00:00:00Z', '2020-05-18T00:00:00Z', NULL
),
(
    'task-pendalpha1', 'planner is starting', 'proj-alpha0001', 'pending',
    NULL, '', '', 'pending',
    NULL, '', NULL,
    '2020-05-17T00:00:00Z', '2020-05-17T00:00:00Z', NULL
),
(
    'task-failbeta01', 'beta failed', 'proj-beta00002', 'failed',
    NULL, '', '', 'pending',
    NULL, '', NULL,
    '2020-05-16T00:00:00Z', '2020-05-16T00:00:00Z', NULL
),
(
    'task-stopbeta01', 'beta stopped', 'proj-beta00002', 'stopped',
    NULL, '', '', 'pending',
    NULL, '', NULL,
    '2020-05-15T00:00:00Z', '2020-05-15T00:00:00Z', NULL
),
(
    'task-cancbeta01', 'beta cancelled', 'proj-beta00002', 'cancelled',
    NULL, '', '', 'pending',
    NULL, '', NULL,
    '2020-05-14T00:00:00Z', '2020-05-14T00:00:00Z', '2020-05-14T01:00:00Z'
),
(
    'task-readyblank1', 'blank project is ready', 'proj-blank0001', 'init',
    NULL, '', '', 'pending',
    NULL, '', NULL,
    '2020-05-13T00:00:00Z', '2020-05-13T00:00:00Z', NULL
),
(
    'task-sepname001', 'should not win', 'proj-alpha0001', 'stopped',
    E'\x1c  File\x1dseparator  \x1ename \x1f', '', '', 'pending',
    NULL, '', NULL,
    '2020-04-02T00:00:00Z', '2020-04-02T00:00:00Z', NULL
),
(
    'task-sepfall001', E'\x1cfallback line\x1f\nsecond line', 'proj-alpha0001', 'stopped',
    E'\x1c\x1d\x1e\x1f', E' \x1c ', '', 'pending',
    NULL, '', NULL,
    '2020-04-01T00:00:00Z', '2020-04-01T00:00:00Z', NULL
);

-- Older than every named task, so the default page is the named tasks and
-- then the newest fillers. 15 named + 2 separator + 40 fillers = 57.
INSERT INTO tasks (id, requirement, project_id, status, created_at, updated_at)
SELECT
    'task-fill' || lpad(n::text, 4, '0'),
    'filler ' || n::text,
    'proj-beta00002',
    'completed',
    timestamptz '2019-12-31 00:00:00+00' - (n - 1) * interval '1 day',
    timestamptz '2019-12-31 00:00:00+00' - (n - 1) * interval '1 day'
FROM generate_series(1, 40) AS n;

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

-- Task cost for task-runalpha1, all in 2020, so spend_today stays the two
-- positive rows above. The Manager is grouped into manager_by_model and left
-- out of the total. The Planner and both Reviewers are in. Models stay split.
-- The running row and the ended row with no cost are running and unpriced.
-- Tokens are not zero. channel-bot is not a task role and must not appear.
INSERT INTO llm_records (
    agent_id, task_id, status, cost_usd, model, cost_source,
    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
    metadata, created_at
) VALUES
(
    'manager', 'task-runalpha1', 'completed', 8, 'mgr-opus', NULL,
    11, 22, 33, 44,
    NULL, '2020-03-04T00:00:00Z'
),
(
    'planner-task-runalpha1', 'task-runalpha1', 'completed', 0.25, 'plan-opus', 'runtime',
    1000, 200, 50, 25,
    NULL, '2020-03-04T00:00:01Z'
),
(
    'reviewer-alpha1', 'task-runalpha1', 'completed', 0.5, 'rev-sonnet', 'provider_price',
    40, 8, 0, 0,
    '{"profile":{"provider":{"auth":"api_key"}}}'::jsonb, '2020-03-04T00:00:02Z'
),
(
    'reviewer-alpha2', 'task-runalpha1', 'completed', 0.125, 'rev-sonnet', NULL,
    10, 2, 3, 4,
    NULL, '2020-03-04T00:00:03Z'
),
(
    'worker-live0001', 'task-runalpha1', 'running', NULL, 'live-haiku', NULL,
    5, 1, 0, 0,
    NULL, '2020-03-04T00:00:04Z'
),
(
    'worker-dead0001', 'task-runalpha1', 'failed', NULL, 'dead-haiku', NULL,
    7, 0, 0, 0,
    NULL, '2020-03-04T00:00:05Z'
),
(
    'worker-cut00001', 'task-runalpha1', 'interrupted', 0.0625, 'cut-haiku', 'runtime',
    8, 9, 1, 1,
    NULL, '2020-03-04T00:00:06Z'
),
(
    'worker-big00001', 'task-runalpha1', 'completed', 4, 'big-model', 'provider_price',
    20, 30, 40, 50,
    '{"profile":{"provider":{"auth":"api_key"}}}'::jsonb, '2020-03-04T00:00:07Z'
),
(
    'channel-bot', 'task-runalpha1', 'completed', 16, 'ignore-me', 'provider_price',
    999, 999, 999, 999,
    '{"profile":{"provider":{"auth":"api_key"}}}'::jsonb, '2020-03-04T00:00:08Z'
);
