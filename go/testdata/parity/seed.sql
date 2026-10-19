-- Parity seed for GET /api/status, GET /api/projects, GET /api/overview,
-- GET /api/tasks, and GET /api/tasks/<id>.
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
-- init tasks, so overview lists it. Python sorts that list by
-- (name or project_id).lower(): Alpha, then the blank id. The projects list
-- is SQL lower(name), so the blank name is first.
-- task-zsame0001 and task-asame0001 share created_at. limit=2 returns z then
-- a (id DESC) and next_before is task-asame0001. Every task status is present.
-- Fifty-nine tasks, more than the default page of 50, so GET /api/tasks
-- sets next_before. The two newest stay z then a. task-sepname001 and
-- task-sepfall001 are stopped tasks whose titles use U+001C–U+001F, which
-- Python counts as whitespace. Forty completed fillers on Beta are dated in
-- 2019, after every named task except task-rich00001 and task-min000001,
-- which are dated in 2018 so they stay off that page. Those two exist for
-- GET /api/tasks/<id>: the rich one carries steps, runs, a plan, a role, a
-- gate, room messages, room consults, a step workspace and its own task
-- worktree; the minimal one carries none of those. Agent settings layers
-- use different profiles, so the resolved profile is not the built-in one.
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

-- task-rich00001 is executing on Alpha, so its task cap is Alpha's 1.5 and
-- its agents stay editable. It runs in a worktree of /work/alpha, so
-- task_workspace.worktree is set and the step workspace's undo list is empty
-- (those commands would change the user's branch). task-min000001 is init on
-- the blank project, so its cap is null, it has no worktree, and its agents
-- come from the role layer rather than the built-in profile. Both are in 2018.
INSERT INTO tasks (
    id, requirement, title, goal_id, project_id, status, phase, planner_id,
    correlation_id, work_dir, workspace_mode, workspace_dir, workspace_base,
    created_at, updated_at
) VALUES (
    'task-rich00001', 'Build the read and write API.', 'Rich task', 'goal-rich',
    'proj-alpha0001', 'executing', 'executing', 'planner-task-rich00001',
    'corr-rich', '/work/alpha/wt/task', 'worktree', '/work/alpha', 'abc1234base',
    '2018-01-01T00:00:00Z', '2018-01-01T00:00:00Z'
);

INSERT INTO task_worktrees (
    task_id, repo_dir, path, branch, base_commit, links, state, created_at
) VALUES (
    'task-rich00001', '/work/alpha', '/work/alpha/wt/task',
    'looper/task-rich00001/task', 'abc1234base', ARRAY['.venv'], 'active',
    '2020-04-04T00:00:00Z'
);

INSERT INTO tasks (id, requirement, title, project_id, status, created_at, updated_at) VALUES
('task-min000001', 'a minimal task', 'Minimal', 'proj-blank0001', 'init', '2018-01-02T00:00:00Z', '2018-01-02T00:00:00Z');

-- s1 is a verified work step of role backend. s2 is a running built-in Worker.
-- g1 is a gate. s4 is a failed built-in Worker. s5 is a skipped backend step,
-- so the role's step list leaves it out and the built-in Worker does not take it.
INSERT INTO task_steps (
    id, task_id, step_index, step_key, instruction, status, kind,
    role_key, role_version, review, review_stage, review_roles,
    worker_id, reviewer_id, retries, created_at, updated_at
) VALUES
(
    'step-rich0001', 'task-rich00001', 0, 's1', 'Write the read API.', 'verified', 'work',
    'backend', 2, true, 'builtin', '[{"role":"qa","required":true,"version":1}]'::jsonb,
    'worker-rich0001', 'reviewer-rich01', 1, '2018-01-01T00:00:00Z', '2018-01-01T00:00:00Z'
),
(
    'step-rich0002', 'task-rich00001', 1, 's2', 'Write the write API.', 'running', 'work',
    NULL, NULL, false, NULL, '[]'::jsonb,
    'worker-rich0002', NULL, 0, '2018-01-01T00:00:01Z', '2018-01-01T00:00:01Z'
),
(
    'step-rich0003', 'task-rich00001', 2, 'g1', 'Ask whether to ship.', 'waiting_user', 'gate',
    NULL, NULL, false, NULL, '[]'::jsonb,
    NULL, NULL, 0, '2018-01-01T00:00:02Z', '2018-01-01T00:00:02Z'
),
(
    'step-rich0004', 'task-rich00001', 3, 's4', 'Write the schema.', 'failed', 'work',
    NULL, NULL, false, NULL, '[]'::jsonb,
    NULL, NULL, 0, '2018-01-01T00:00:03Z', '2018-01-01T00:00:03Z'
),
(
    'step-rich0005', 'task-rich00001', 4, 's5', 'Skip the old client.', 'skipped', 'work',
    'backend', 1, false, NULL, '[]'::jsonb,
    NULL, NULL, 0, '2018-01-01T00:00:04Z', '2018-01-01T00:00:04Z'
);

-- The plan's start is what the undo commands reset to. The merged workspace
-- on s1 makes the per-step revert and the whole-task resets non-empty.
INSERT INTO task_plans (task_id, plan_id, status, start_branch, start_commit, created_at, updated_at) VALUES
('task-rich00001', 'plan-rich00001', 'executing', 'refs/heads/main', 'abc1234dead', '2018-01-01T00:00:00Z', '2018-01-01T00:00:00Z');

INSERT INTO step_workspaces (
    task_id, step_id, repo_dir, target_branch, path, branch, base_commit,
    result_commit, merge_commit, state, created_at, updated_at
) VALUES (
    'task-rich00001', 'step-rich0001', '/work/alpha', 'refs/heads/main',
    '/work/alpha/wt/s1', 'looper/task-rich00001/s1-1', 'abc1234dead',
    'cafebabe1', 'deadbeef1', 'merged', '2020-04-03T00:00:00Z', '2020-04-03T00:00:00Z'
);

INSERT INTO task_requirement_versions (task_id, version, text, via, created_at) VALUES
('task-rich00001', 1, 'Build the read API.', 'user', '2018-01-01T00:00:00Z'),
('task-rich00001', 2, 'Build the read and write API.', 'user', '2018-01-04T00:00:00Z');

INSERT INTO task_journal (task_id, step_id, kind, summary, via, created_at) VALUES
('task-rich00001', '', 'supplement', 'Also cover the write path.', 'task_page', '2018-01-04T00:00:00Z'),
('task-rich00001', 'step-rich0001', 'progress', '{"done":"schema","now":"handlers","next":"tests","risks":"none","estimate":"one day"}', '', '2018-01-05T00:00:00Z'),
('task-rich00001', 'step-rich0001', 'step_accepted', 's1 accepted', '', '2018-01-06T00:00:00Z');

INSERT INTO manager_actions (created_at, channel, tool, arguments, risk, status, task_id) VALUES
('2020-04-02T00:00:00Z', 'web', 'task_note', '{"text":"look at the gate"}'::jsonb, 'read', 'ok', 'task-rich00001');

-- Discussing, no failed turn, and one user message still past delivered_through,
-- so the gate waits on the Planner. No origin chat and no dashboard target,
-- so channel_posts stays null. Agenda has no numbers, so the body is stable.
INSERT INTO task_user_gates (step_id, task_id, topic, agenda, findings, state, failures, opened_at, created_at) VALUES (
    'step-rich0003', 'task-rich00001', 'Ship the API?',
    '[{"id":"q1","question":"Ship it?","options":["yes","no"],"status":"open"}]'::jsonb,
    'waiting on the API shape', 'discussing', 0, '2020-05-02T00:00:00Z', '2020-05-02T00:00:00Z'
);

INSERT INTO task_user_gate_messages (step_id, task_id, role, kind, text, post_state, created_at) VALUES
('step-rich0003', 'task-rich00001', 'user', 'message', 'Please ship the read API.', 'none', '2020-05-02T00:01:00Z');

-- s1 sends two questions. s2 answers the first. s4 leaves the second open.
-- The reviewer seat on s1 gets nothing, and the gate has no seat.
WITH q1 AS (
    INSERT INTO room_messages (
        task_id, sender_seat, sender_kind, sender_step, via, audience, kind, body, created_at
    ) VALUES (
        'task-rich00001', 'backend@s1', 'work', 'step-rich0001', 'agent',
        ARRAY['worker@s2'], 'question', 'Does s2 own the port?', '2020-05-01T00:00:00Z'
    )
    RETURNING id
), a1 AS (
    INSERT INTO room_messages (
        task_id, sender_seat, sender_kind, sender_step, via, audience, kind, body, reply_to, created_at
    )
    SELECT 'task-rich00001', 'worker@s2', 'work', 'step-rich0002', 'agent',
           ARRAY['backend@s1'], 'answer', 'Yes, port 8080.', q1.id, '2020-05-01T00:00:01Z'
    FROM q1
    RETURNING id
)
INSERT INTO room_messages (
    task_id, sender_seat, sender_kind, sender_step, via, audience, kind, body, created_at
)
SELECT 'task-rich00001', 'backend@s1', 'work', 'step-rich0001', 'agent',
       ARRAY['worker@s4'], 'question', 'Can s4 take the schema?', '2020-05-01T00:00:02Z'
FROM a1;

-- Answered, a fallback, and a failed consult with no cost. total, answered,
-- by_reason and cost_usd are then all nonzero or non-empty. A null cost is 0.
INSERT INTO room_consults (
    task_id, seat, questions, state, fallback_reason, read_only, cost_usd, created_at, finished_at
) VALUES
(
    'task-rich00001', 'worker@s2', '{}', 'answered', NULL, 'runtime', 0.125,
    '2020-05-03T00:00:00Z', '2020-05-03T00:00:01Z'
),
(
    'task-rich00001', 'backend@s1', '{}', 'fallback', 'no_session', NULL, 0.25,
    '2020-05-03T00:00:02Z', '2020-05-03T00:00:03Z'
),
(
    'task-rich00001', 'worker@s4', '{}', 'failed', NULL, 'snapshot', NULL,
    '2020-05-03T00:00:04Z', '2020-05-03T00:00:05Z'
);

INSERT INTO library_items (id, kind, name, sha256, size_bytes, file_count) VALUES
('lib-rich0001', 'skill', 'rich-skill', 'abc', 12, 1);

-- Two versions, oldest first. v2 names one real catalog item and one missing id.
-- The role's profile is allowed on Alpha, so profile.allowed is true, and it
-- is not the built-in profile.
INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model, effort) VALUES
('agent-layer-system', 'claude-code/layer-system', 'rt-claude-code', 'prov-claude-subscription', 'layer-system', 'low'),
('agent-layer-planner', 'claude-code/layer-planner', 'rt-claude-code', 'prov-claude-subscription', 'layer-planner', 'medium'),
('agent-layer-worker', 'claude-code/layer-worker', 'rt-claude-code', 'prov-claude-subscription', 'layer-worker', 'high'),
('agent-layer-reviewer', 'claude-code/layer-reviewer', 'rt-claude-code', 'prov-claude-subscription', 'layer-reviewer', 'xhigh'),
('agent-layer-project', 'claude-code/layer-project', 'rt-claude-code', 'prov-claude-subscription', 'layer-project', 'max'),
('agent-layer-rp-plan', 'claude-code/layer-rp-plan', 'rt-claude-code', 'prov-claude-subscription', 'layer-rp-plan', 'low'),
('agent-layer-rp-work', 'claude-code/layer-rp-work', 'rt-claude-code', 'prov-claude-subscription', 'layer-rp-work', 'medium'),
('agent-layer-rp-rev', 'claude-code/layer-rp-rev', 'rt-claude-code', 'prov-claude-subscription', 'layer-rp-rev', 'high'),
('agent-layer-pt-work', 'claude-code/layer-pt-work', 'rt-claude-code', 'prov-claude-subscription', 'layer-pt-work', 'xhigh'),
('agent-layer-pt-rev', 'claude-code/layer-pt-rev', 'rt-claude-code', 'prov-claude-subscription', 'layer-pt-rev', 'max'),
('agent-layer-task-work', 'claude-code/layer-task-work', 'rt-claude-code', 'prov-claude-subscription', 'layer-task-work', 'low'),
('agent-layer-roledef', 'claude-code/layer-roledef', 'rt-claude-code', 'prov-claude-subscription', 'layer-roledef', NULL);

INSERT INTO agent_setting_layers (scope, profile_id) VALUES
('system', 'agent-layer-system');

INSERT INTO agent_setting_layers (scope, role, profile_id, max_concurrency) VALUES
('role', 'planner', 'agent-layer-planner', NULL),
('role', 'worker', 'agent-layer-worker', 4),
('role', 'reviewer', 'agent-layer-reviewer', 5);

INSERT INTO agent_setting_layers (scope, project_id, profile_id) VALUES
('project', 'proj-alpha0001', 'agent-layer-project');

INSERT INTO agent_setting_layers (scope, role, project_id, profile_id) VALUES
('role_project', 'planner', 'proj-alpha0001', 'agent-layer-rp-plan'),
('role_project', 'worker', 'proj-alpha0001', 'agent-layer-rp-work'),
('role_project', 'reviewer', 'proj-alpha0001', 'agent-layer-rp-rev');

INSERT INTO project_allowed_profiles (project_id, profile_id) VALUES
('proj-alpha0001', 'agent-layer-pt-work'),
('proj-alpha0001', 'agent-layer-pt-rev'),
('proj-alpha0001', 'agent-layer-roledef');

INSERT INTO task_roles (
    task_id, role_key, version, kind, profile_id, role_prompt, reason, via, items, created_at
) VALUES
(
    'task-rich00001', 'backend', 1, 'work', 'agent-layer-roledef',
    'Write the API.', 'first cut', 'planner', '{}', '2018-01-02T00:00:00Z'
),
(
    'task-rich00001', 'backend', 2, 'work', 'agent-layer-roledef',
    'Write the API and the tests.', 'add tests', 'planner',
    ARRAY['lib-rich0001', 'lib-missing01'], '2018-01-03T00:00:00Z'
);

-- The Worker's task choice beats the Planner's choice for the rest of the
-- task. The Reviewer has no task row, so the Planner's choice wins. Both
-- Planner choices are on Alpha's allowed list; a profile that is not allowed
-- would not count.
INSERT INTO task_profile_overrides (task_id, role, profile_id) VALUES
('task-rich00001', 'worker', 'agent-layer-task-work');

INSERT INTO task_concurrency_limits (task_id, role, max_concurrency) VALUES
('task-rich00001', 'worker', 3);

INSERT INTO task_planner_profiles (task_id, role, step_id, profile_id) VALUES
('task-rich00001', 'worker', NULL, 'agent-layer-pt-work'),
('task-rich00001', 'reviewer', NULL, 'agent-layer-pt-rev');

-- Costs are in 2020, so spend_today stays the two positive rows on
-- task-runalpha1. opus and sonnet stay split. The Manager is grouped into
-- manager_by_model and left out of the total. The running Worker has no cost.
INSERT INTO llm_records (
    agent_id, task_id, status, cost_usd, model, cost_source,
    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
    metadata, created_at
) VALUES
(
    'planner-task-rich00001', 'task-rich00001', 'completed', 0.25, 'opus', 'runtime',
    100, 20, 5, 1,
    '{"step_id":"","outcome":"passed"}'::jsonb, '2020-04-01T00:00:00Z'
),
(
    'worker-rich0001', 'task-rich00001', 'completed', 0.5, 'opus', 'runtime',
    200, 40, 8, 2,
    '{"step_id":"step-rich0001","outcome":"passed","prompt_layers":{"task_role":{"id":"backend","kind":"work","version":2}}}'::jsonb,
    '2020-04-01T00:00:01Z'
),
(
    'worker-rich0003', 'task-rich00001', 'completed', 0.25, 'sonnet', 'runtime',
    80, 16, 4, 1,
    '{"step_id":"step-rich0001","outcome":"passed","prompt_layers":{"task_role":{"id":"backend","kind":"work","version":2}}}'::jsonb,
    '2020-04-01T00:00:02Z'
),
(
    'reviewer-rich01', 'task-rich00001', 'completed', 0.125, 'sonnet', 'runtime',
    40, 8, 2, 1,
    '{"step_id":"step-rich0001","outcome":"passed"}'::jsonb, '2020-04-01T00:00:03Z'
),
(
    'worker-rich0002', 'task-rich00001', 'running', NULL, 'sonnet', NULL,
    10, 2, 0, 0,
    '{"step_id":"step-rich0002"}'::jsonb, '2020-04-01T00:00:04Z'
),
(
    'manager', 'task-rich00001', 'completed', 0.5, 'opus', 'runtime',
    11, 22, 3, 4,
    NULL, '2020-04-01T00:00:05Z'
);
