-- LoopX Agent System Schema
-- Idempotent: safe to run on every startup.

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    requirement       TEXT NOT NULL,
    goal_id           TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',
    phase             TEXT NOT NULL DEFAULT 'pending',
    planner_id        TEXT,
    correlation_id    TEXT NOT NULL DEFAULT '',
    origin_channel_id TEXT NOT NULL DEFAULT '',
    result_summary    TEXT,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS work_dir TEXT NOT NULL DEFAULT '';

-- A task runs in the directory the user chose (work_dir). project_root was a
-- second directory field the dashboard never set; fold it in and drop it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'tasks'
                 AND column_name = 'project_root') THEN
        UPDATE tasks SET work_dir = project_root WHERE work_dir = '' AND project_root <> '';
        ALTER TABLE tasks DROP COLUMN project_root;
    END IF;
END $$;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}';

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

CREATE TABLE IF NOT EXISTS llm_profiles (
    id               BIGSERIAL PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    display_name     TEXT NOT NULL DEFAULT '',
    provider_kind    TEXT NOT NULL DEFAULT 'anthropic',
    model            TEXT NOT NULL DEFAULT '',
    base_url         TEXT,
    api_key_env      TEXT,
    reasoning_effort TEXT,
    max_tokens       INTEGER,
    context_window_k INTEGER,
    rate_per_minute  INTEGER,
    streaming        BOOLEAN NOT NULL DEFAULT true,
    is_default       BOOLEAN NOT NULL DEFAULT false,
    priority         INTEGER NOT NULL DEFAULT 0,
    description      TEXT NOT NULL DEFAULT '',
    metadata         JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_configs (
    agent_id         TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL DEFAULT '',
    runtime          TEXT NOT NULL DEFAULT 'claude-code',
    llm_profile_id   BIGINT REFERENCES llm_profiles(id),
    enabled          BOOLEAN NOT NULL DEFAULT true,
    system_prompt    TEXT NOT NULL DEFAULT '',
    model            TEXT,
    max_turns        INTEGER NOT NULL DEFAULT 0,
    timeout_seconds  INTEGER NOT NULL DEFAULT 1200,
    max_concurrency  INTEGER NOT NULL DEFAULT 1,
    extra_args       JSONB,
    metadata         JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The system-recommended prompt for this agent. seed keeps it current; an
-- unedited row (system_prompt = default_prompt) follows it, an edited row does
-- not, and "reset to recommended" copies it back into system_prompt.
ALTER TABLE agent_configs ADD COLUMN IF NOT EXISTS default_prompt TEXT NOT NULL DEFAULT '';
-- The Manager's own harness prompt ('' = the default one in Settings). The
-- task agents' harness overrides belong to their project.
ALTER TABLE agent_configs ADD COLUMN IF NOT EXISTS harness_prompt TEXT NOT NULL DEFAULT '';
-- permission_mode was never applied: every agent runs with
-- --dangerously-skip-permissions (claude_cli.permission_args).
ALTER TABLE agent_configs DROP COLUMN IF EXISTS permission_mode;

-- Settings the whole system shares, one value per key. Code owns each key's
-- type and validation (loopx/agents/agent_settings.py); a key without a row
-- has its built-in default.
CREATE TABLE IF NOT EXISTS system_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The plugins and skills imported from the operator's Claude Code (the
-- library), loaded by every agent when enabled. The tables are the source of
-- truth: agents get a read-only copy materialized under the runtime root
-- (loopx/agents/library.py), never the operator's ~/.claude. An item's
-- sha256 is the hash of its files; a re-import replaces a changed item's files
-- and keeps its id and its enabled flag.
CREATE TABLE IF NOT EXISTS library_items (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('plugin', 'skill')),
    name         TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT '',
    version      TEXT NOT NULL DEFAULT '',
    sha256       TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT true,
    size_bytes   BIGINT NOT NULL,
    file_count   INTEGER NOT NULL,
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, name)
);
CREATE TABLE IF NOT EXISTS library_files (
    item_id     TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    content     TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    executable  BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (item_id, path)
);

-- One-time data migrations; schema.sql itself runs on every startup.
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Once: move the worker's old seeded limit (600 s) to the new default. A value
-- set later, including 600, stays.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'worker_timeout_default_86400') THEN
        UPDATE agent_configs SET timeout_seconds = 86400
        WHERE agent_id = 'worker' AND timeout_seconds = 600;
        INSERT INTO schema_migrations (name) VALUES ('worker_timeout_default_86400');
    END IF;
END $$;

-- Every task belongs to one project; a project may give its tasks a default
-- directory (work_dir, '' = none).
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    work_dir    TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name ON projects (lower(name));
-- What every task agent of the project (Planner, Workers, Reviewers) should
-- know about it: added to their system prompt; '' = none.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS background TEXT NOT NULL DEFAULT '';
-- The harness prompt of the project's Planner, and of its Workers and
-- Reviewers; '' = the default one in Settings.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS planner_harness_prompt TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS worker_harness_prompt TEXT NOT NULL DEFAULT '';
-- How the project's task agents run; NULL inherits today's behavior. Code owns
-- the types and validation (loopx/agents/project_execution.py).
ALTER TABLE projects ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS run_budget_usd DOUBLE PRECISION CHECK (run_budget_usd > 0);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS task_budget_usd DOUBLE PRECISION CHECK (task_budget_usd > 0);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS task_budget_action TEXT
    CHECK (task_budget_action IN ('wrap_up', 'fail'));
ALTER TABLE projects ADD COLUMN IF NOT EXISTS supervise_interval_seconds INTEGER
    CHECK (supervise_interval_seconds >= 0);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_policy TEXT CHECK (review_policy IN ('planner', 'all'));
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project_id TEXT REFERENCES projects(id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks (project_id, created_at DESC);

-- A project's knowledge: reference documents its task agents read on demand
-- instead of getting them in their prompt. A text or upload document's content
-- is stored here and materialized read-only under the runtime root; a path
-- document is a file in the project's default directory (path is relative to
-- it), read live. size_bytes and sha256 are the content's (a path document's
-- as of its last save). Code owns validation and caps (loopx/agents/knowledge.py).
CREATE TABLE IF NOT EXISTS project_documents (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    source_kind  TEXT NOT NULL CHECK (source_kind IN ('text', 'upload', 'path')),
    filename     TEXT NOT NULL DEFAULT '',
    content      TEXT,
    path         TEXT,
    size_bytes   BIGINT NOT NULL,
    sha256       TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (CASE source_kind
               WHEN 'path' THEN path IS NOT NULL AND content IS NULL
               ELSE content IS NOT NULL AND path IS NULL AND filename <> ''
           END)
);
CREATE INDEX IF NOT EXISTS idx_project_documents_project ON project_documents (project_id, created_at, id);

-- A project's own Claude Code skills: only its task agents (Planner, Workers,
-- Reviewers) load them. The enabled ones are materialized read-only under the
-- runtime root as one plugin, never in the project's directory; name is the
-- skill's directory and frontmatter name (a lowercase slug), content the body
-- of its SKILL.md. Code owns validation and caps (loopx/agents/project_skills.py).
CREATE TABLE IF NOT EXISTS project_skills (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name         TEXT NOT NULL CHECK (length(name) <= 64 AND name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    description  TEXT NOT NULL,
    content      TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);

-- Once: tasks from before projects get one project per directory, named after
-- its last path component ('' and '/' -> 'default'). The oldest directory
-- keeps the plain name; a later clash, ignoring case, gets -2, -3, ...
DO $$
DECLARE
    dir        TEXT;
    base       TEXT;
    candidate  TEXT;
    suffix     INTEGER;
    new_id     TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'tasks_project_backfill') THEN
        FOR dir IN
            SELECT work_dir FROM tasks WHERE project_id IS NULL
            GROUP BY work_dir ORDER BY min(created_at), work_dir
        LOOP
            base := left(COALESCE(NULLIF(btrim(substring(rtrim(dir, '/') FROM '[^/]*$')), ''), 'default'), 80);
            candidate := base;
            suffix := 1;
            WHILE EXISTS (SELECT 1 FROM projects WHERE lower(name) = lower(candidate)) LOOP
                suffix := suffix + 1;
                candidate := left(base, 80 - length('-' || suffix)) || '-' || suffix;
            END LOOP;
            new_id := 'proj-' || left(replace(gen_random_uuid()::text, '-', ''), 12);
            INSERT INTO projects (id, name, work_dir) VALUES (new_id, candidate, dir);
            UPDATE tasks SET project_id = new_id WHERE project_id IS NULL AND work_dir = dir;
        END LOOP;
        ALTER TABLE tasks ALTER COLUMN project_id SET NOT NULL;
        INSERT INTO schema_migrations (name) VALUES ('tasks_project_backfill');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS llm_records (
    id               BIGSERIAL PRIMARY KEY,
    agent_id         TEXT NOT NULL,
    task_id          TEXT,
    session_id       TEXT,
    input_summary    TEXT,
    output_summary   TEXT,
    input_text       TEXT,
    output_text      TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    duration_ms      INTEGER,
    model            TEXT,
    error            TEXT,
    metadata         JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS input_text TEXT;
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS output_text TEXT;

CREATE INDEX IF NOT EXISTS idx_llm_records_agent ON llm_records(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_records_task ON llm_records(task_id, created_at DESC);

-- One row per Claude run: launch command in metadata, outcome when it ends.
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed';
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION;
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
-- Heartbeat: last time the run wrote any output (partial deltas included).
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS last_output_at TIMESTAMPTZ;
-- The run's own usage beside input_tokens and output_tokens: prompt tokens read from and written to
-- the cache. For one turn of a resident agent these are that turn's share of its session's totals.
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT;
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS cache_creation_tokens BIGINT;
-- The context at the run's end: the prompt of its last main-loop model request.
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS context_tokens INTEGER;
-- The context compactions during the run, each {trigger, pre_tokens, post_tokens, duration_ms};
-- NULL for runs recorded before compactions were.
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS compactions JSONB;

-- Once: runs recorded before `status` existed all defaulted to 'completed';
-- the ones that recorded an error had failed.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'llm_records_status_backfill') THEN
        UPDATE llm_records SET status = 'failed'
        WHERE status = 'completed' AND error IS NOT NULL AND finished_at IS NULL;
        INSERT INTO schema_migrations (name) VALUES ('llm_records_status_backfill');
    END IF;
END $$;

-- The run's stream-json events, appended as they arrive.
CREATE TABLE IF NOT EXISTS llm_record_events (
    record_id   BIGINT NOT NULL REFERENCES llm_records(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    event       JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, seq)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_msg_unique ON chat_messages(session_id, message_id);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, id);

CREATE TABLE IF NOT EXISTS channel_configs (
    channel_id   TEXT PRIMARY KEY,
    channel_type TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    enabled      BOOLEAN NOT NULL DEFAULT false,
    config       JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
