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

-- The rest of a Planner's step, so the table is the step's only record:
-- step_key is the Planner's label for it (unique within the task), depends_on
-- the step ids it waits for, review whether a Reviewer must pass it; retries,
-- feedback (one entry per retry) and missed_messages (user text forwarded too
-- late for its Worker) feed the Planner's next decision on it.
ALTER TABLE task_steps ADD COLUMN IF NOT EXISTS step_key TEXT NOT NULL DEFAULT '';
ALTER TABLE task_steps ADD COLUMN IF NOT EXISTS depends_on TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE task_steps ADD COLUMN IF NOT EXISTS review BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE task_steps ADD COLUMN IF NOT EXISTS validation_commands TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE task_steps ADD COLUMN IF NOT EXISTS acceptance_criteria TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE task_steps ADD COLUMN IF NOT EXISTS retries INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task_steps ADD COLUMN IF NOT EXISTS feedback TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE task_steps ADD COLUMN IF NOT EXISTS reviewer_id TEXT;
ALTER TABLE task_steps ADD COLUMN IF NOT EXISTS missed_messages TEXT[] NOT NULL DEFAULT '{}';

-- A task's plan beside its steps: one row per task, replaced when the task
-- is planned again. The task's requirement, directory and project stay on
-- tasks; the project itself is read again before every Planner turn.
CREATE TABLE IF NOT EXISTS task_plans (
    task_id           TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    plan_id           TEXT NOT NULL,
    status            TEXT NOT NULL
        CHECK (status IN ('planning', 'executing', 'verifying', 'stopping', 'completed', 'failed')),
    correlation_id    TEXT NOT NULL DEFAULT '',
    origin_channel_id TEXT NOT NULL DEFAULT '',
    final_summary     TEXT NOT NULL DEFAULT '',
    fail_reason       TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The Claude session a resident agent resumes after a restart: 'manager', or
-- 'planner-<task_id>'. session_totals is the session's running cost and
-- tokens at its last result (run_usage.Usage), the baseline a resumed process
-- counts from; session_id NULL means the next turn starts a new session.
CREATE TABLE IF NOT EXISTS agent_sessions (
    agent_id        TEXT PRIMARY KEY,
    session_id      TEXT,
    session_totals  JSONB NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A task's journal: what its steps delivered and found. Code writes an entry,
-- never the model: 'step_accepted' in the transaction that settles an
-- accepted step, with the step's summary and the learnings its Worker and
-- Reviewer reported. The Planner's turns and a Worker's first turn get the
-- entries back in bounded sections (loopx/agents/tiers/journal.py).
CREATE TABLE IF NOT EXISTS task_journal (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('step_accepted')),
    summary     TEXT NOT NULL DEFAULT '',
    learnings   TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_task_journal_task ON task_journal (task_id, id);
-- A step is accepted once.
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_journal_accepted ON task_journal (task_id, step_id)
    WHERE kind = 'step_accepted';

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
-- permission_mode was never applied: every agent runs with
-- --dangerously-skip-permissions (claude_cli.permission_args).
ALTER TABLE agent_configs DROP COLUMN IF EXISTS permission_mode;

-- Settings the whole system shares, one value per key. Code owns each key's
-- type and validation; a key without a row has its built-in default. (The
-- agent settings live in agent_setting_layers.)
CREATE TABLE IF NOT EXISTS system_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The capability catalog (the library): plugins and skills acquired into LoopX.
-- An item does nothing by itself; it loads only where it is installed
-- (library_installs). The tables are the source of truth: agents get a
-- read-only copy materialized under the runtime root (loopx/agents/library.py),
-- never the operator's ~/.claude. An item's sha256 is the hash of its files; a
-- re-import replaces a changed item's files and keeps its id and its installs.
-- origin, owner_project_id and description are added below, after projects.
CREATE TABLE IF NOT EXISTS library_items (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('plugin', 'skill', 'mcp')),
    name         TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT '',
    version      TEXT NOT NULL DEFAULT '',
    sha256       TEXT NOT NULL,
    size_bytes   BIGINT NOT NULL,
    file_count   INTEGER NOT NULL,
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
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
-- How the project's task agents run; NULL inherits today's behavior. Code owns
-- the types and validation (loopx/agents/project_execution.py).
ALTER TABLE projects ADD COLUMN IF NOT EXISTS task_budget_usd DOUBLE PRECISION CHECK (task_budget_usd > 0);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS task_budget_action TEXT
    CHECK (task_budget_action IN ('wrap_up', 'fail'));
ALTER TABLE projects ADD COLUMN IF NOT EXISTS supervise_interval_seconds INTEGER
    CHECK (supervise_interval_seconds >= 0);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_policy TEXT CHECK (review_policy IN ('planner', 'all'));
-- What a task that completes leaves in the project's knowledge, from one more
-- Planner turn over the task journal: nothing ('off'), a draft the user
-- confirms or discards on the project page ('draft'), or a document ('write').
-- Once: the boolean from before the three modes; true becomes 'write', false
-- and NULL 'off'.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'distill_lessons_modes') THEN
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS distill_lessons BOOLEAN;
        ALTER TABLE projects ALTER COLUMN distill_lessons TYPE TEXT
            USING CASE WHEN distill_lessons THEN 'write' ELSE 'off' END;
        ALTER TABLE projects ALTER COLUMN distill_lessons SET DEFAULT 'off';
        ALTER TABLE projects ALTER COLUMN distill_lessons SET NOT NULL;
        ALTER TABLE projects ADD CONSTRAINT projects_distill_lessons_check
            CHECK (distill_lessons IN ('off', 'draft', 'write'));
        INSERT INTO schema_migrations (name) VALUES ('distill_lessons_modes');
    END IF;
END $$;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project_id TEXT REFERENCES projects(id);
-- Why a failed task failed, when code can tell (loopx/db/tasks.py FailKind); NULL
-- otherwise. 'task_budget': it reached its project's task_budget_usd.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS fail_kind TEXT CHECK (fail_kind IN ('task_budget'));
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
-- Drafts of knowledge documents (distill_lessons 'draft'): the lessons a task's
-- Planner wrote, waiting for the user to confirm one into project_documents as a
-- text document or discard it. No run reads this table, so agents never see a
-- draft. task_id is the task that wrote it (NULL once that task is deleted).
CREATE TABLE IF NOT EXISTS project_document_drafts (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id     TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_project_document_drafts_project
    ON project_document_drafts (project_id, created_at, id);

-- How a catalog item was acquired: imported from this machine's Claude Code
-- ('machine'), uploaded, taken from git, written in the UI, or written in a
-- project ('project'). A project-authored item belongs to that project
-- (owner_project_id) and goes with it. Code owns the kinds of origin and what
-- each may do (loopx/agents/catalog.py).
ALTER TABLE library_items ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'machine'
    CHECK (origin IN ('machine', 'upload', 'git', 'ui', 'project'));
ALTER TABLE library_items ADD COLUMN IF NOT EXISTS owner_project_id TEXT REFERENCES projects(id) ON DELETE CASCADE;
-- One line on what the item does; '' when it has none (an imported item).
ALTER TABLE library_items ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
-- An uploaded or git item edited in LoopX since it came in; a re-import from its repository clears it.
ALTER TABLE library_items ADD COLUMN IF NOT EXISTS modified BOOLEAN NOT NULL DEFAULT false;
-- A name is unique among the items outside projects, and within the project that owns an item.
CREATE UNIQUE INDEX IF NOT EXISTS idx_library_items_name ON library_items (kind, name)
    WHERE owner_project_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_library_items_project_name ON library_items (owner_project_id, kind, name)
    WHERE owner_project_id IS NOT NULL;

-- Where a catalog item is installed: everywhere ('global') or in one project,
-- for some roles. Every agent run loads the enabled global installs and its
-- project's that name its role. A project install never names the Manager,
-- which spans every project. One install per item and place.
CREATE TABLE IF NOT EXISTS library_installs (
    id          TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
    scope       TEXT NOT NULL CHECK (scope IN ('global', 'project')),
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
    roles       TEXT[] NOT NULL CHECK (cardinality(roles) > 0
                                       AND roles <@ ARRAY['manager', 'planner', 'worker', 'reviewer']),
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((scope = 'project') = (project_id IS NOT NULL)),
    CHECK (scope = 'global' OR NOT 'manager' = ANY (roles))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_library_installs_place
    ON library_installs (item_id, scope, COALESCE(project_id, ''));
CREATE INDEX IF NOT EXISTS idx_library_installs_project ON library_installs (project_id)
    WHERE project_id IS NOT NULL;

-- Once (catalog and installs): the items imported before stay in the catalog,
-- installed nowhere, so they load nowhere until installed. Each project skill
-- (the retired project_skills table) becomes a catalog item owned by its
-- project, keeping its id, name and description, its SKILL.md written the way
-- runs wrote it (frontmatter from the fields, then the body;
-- loopx/agents/project_skills.py skill_file), and is installed in its project
-- for the Planner, Workers and Reviewers, enabled as it was.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'library_catalog_installs') THEN
        ALTER TABLE library_items DROP CONSTRAINT IF EXISTS library_items_kind_name_key;
        ALTER TABLE library_items DROP COLUMN IF EXISTS enabled;
        ALTER TABLE library_items ADD CONSTRAINT library_items_owner_check
            CHECK ((origin = 'project') = (owner_project_id IS NOT NULL));
        IF to_regclass('project_skills') IS NOT NULL THEN
            CREATE TEMP TABLE catalog_project_skills AS
            SELECT s.id, s.project_id, s.name, s.description, s.enabled, s.created_at, s.updated_at, f.text,
                   encode(sha256(convert_to(f.text, 'UTF8')), 'hex') AS file_sha256
            FROM project_skills s,
                 LATERAL (SELECT '---' || E'\n' || 'name: ' || s.name || E'\n'
                                 || 'description: ' || to_json(s.description)::text || E'\n' || '---' || E'\n' || E'\n'
                                 || s.content || CASE WHEN right(s.content, 1) = E'\n' THEN '' ELSE E'\n' END
                                 AS text) f;
            INSERT INTO library_items (id, kind, name, source, version, sha256, size_bytes, file_count,
                                       imported_at, updated_at, origin, owner_project_id, description)
            SELECT id, 'skill', name, '', '',
                   encode(sha256(convert_to('[["SKILL.md","' || file_sha256 || '",false]]', 'UTF8')), 'hex'),
                   octet_length(text), 1, created_at, updated_at, 'project', project_id, description
            FROM catalog_project_skills;
            INSERT INTO library_files (item_id, path, content, sha256, executable)
            SELECT id, 'SKILL.md', text, file_sha256, false FROM catalog_project_skills;
            INSERT INTO library_installs (id, item_id, scope, project_id, roles, enabled, created_at, updated_at)
            SELECT 'inst-' || left(replace(gen_random_uuid()::text, '-', ''), 12), id, 'project', project_id,
                   ARRAY['planner', 'worker', 'reviewer'], enabled, created_at, updated_at
            FROM catalog_project_skills;
            DROP TABLE catalog_project_skills;
            DROP TABLE project_skills;
        END IF;
        INSERT INTO schema_migrations (name) VALUES ('library_catalog_installs');
    END IF;
END $$;

-- Agent settings by layer: system default -> role -> project -> role in a
-- project, a later layer winning. The Manager spans every project and has only
-- the first two. A NULL field is not set at its layer (it inherits); env is an
-- ordered list of {"key", "value"}, where a null value unsets a key an earlier
-- layer set (never at the system layer). Code owns the validation and the
-- resolution (loopx/agents/agent_settings.py). One row per layer and place.
CREATE TABLE IF NOT EXISTS agent_setting_layers (
    scope           TEXT NOT NULL CHECK (scope IN ('system', 'role', 'project', 'role_project')),
    role            TEXT CHECK (role IN ('manager', 'planner', 'worker', 'reviewer')),
    project_id      TEXT REFERENCES projects(id) ON DELETE CASCADE,
    harness_prompt  TEXT,
    model           TEXT,
    effort          TEXT,
    env             JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(env) = 'array'),
    run_budget_usd  DOUBLE PRECISION CHECK (run_budget_usd > 0),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((role IS NOT NULL) = (scope IN ('role', 'role_project'))),
    CHECK ((project_id IS NOT NULL) = (scope IN ('project', 'role_project'))),
    CHECK (scope <> 'role_project' OR role <> 'manager')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_setting_layers_place
    ON agent_setting_layers (scope, COALESCE(role, ''), COALESCE(project_id, ''));

-- Once (layered settings): the values that lived in four places move into
-- their layer, unchanged; the old columns and keys are dropped. The Settings
-- defaults (system_settings agent.*) become the system layer; each agent's
-- model, and the Manager's harness (the only one the API took), the role
-- layer; a project's model and per-run cap the project layer; a project's
-- Planner and Worker harness the role-in-project layer (a Reviewer follows its
-- project's Workers at resolution, as before).
DO $$
DECLARE
    env_value JSONB := '[]'::jsonb;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'agent_setting_layers') THEN
        ALTER TABLE agent_configs ADD COLUMN IF NOT EXISTS harness_prompt TEXT NOT NULL DEFAULT '';
        ALTER TABLE agent_configs ADD COLUMN IF NOT EXISTS model TEXT;
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS planner_harness_prompt TEXT NOT NULL DEFAULT '';
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS worker_harness_prompt TEXT NOT NULL DEFAULT '';
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS model TEXT;
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS run_budget_usd DOUBLE PRECISION;
        BEGIN
            SELECT value::jsonb INTO env_value FROM system_settings WHERE key = 'agent.env';
            IF env_value IS NULL OR jsonb_typeof(env_value) <> 'array' THEN
                env_value := '[]'::jsonb;
            END IF;
        EXCEPTION WHEN others THEN
            env_value := '[]'::jsonb;  -- unreadable: it was ignored at runtime too
        END;
        INSERT INTO agent_setting_layers (scope, harness_prompt, model, effort, env)
        SELECT 'system', h.value, m.value, e.value, env_value
        FROM (SELECT 1) one
        LEFT JOIN system_settings h ON h.key = 'agent.harness_prompt'
        LEFT JOIN system_settings m ON m.key = 'agent.model'
        LEFT JOIN system_settings e ON e.key = 'agent.effort'
        WHERE h.value IS NOT NULL OR m.value IS NOT NULL OR e.value IS NOT NULL OR env_value <> '[]'::jsonb;
        INSERT INTO agent_setting_layers (scope, role, harness_prompt, model)
        SELECT 'role', agent_id,
               CASE WHEN agent_id = 'manager' THEN NULLIF(harness_prompt, '') END, NULLIF(btrim(model), '')
        FROM agent_configs
        WHERE agent_id IN ('manager', 'planner', 'worker', 'reviewer')
          AND ((agent_id = 'manager' AND harness_prompt <> '') OR NULLIF(btrim(model), '') IS NOT NULL);
        INSERT INTO agent_setting_layers (scope, project_id, model, run_budget_usd)
        SELECT 'project', id, model, run_budget_usd FROM projects
        WHERE model IS NOT NULL OR run_budget_usd IS NOT NULL;
        INSERT INTO agent_setting_layers (scope, role, project_id, harness_prompt)
        SELECT 'role_project', 'planner', id, planner_harness_prompt FROM projects WHERE planner_harness_prompt <> ''
        UNION ALL
        SELECT 'role_project', 'worker', id, worker_harness_prompt FROM projects WHERE worker_harness_prompt <> '';
        ALTER TABLE agent_configs DROP COLUMN harness_prompt;
        ALTER TABLE agent_configs DROP COLUMN model;
        ALTER TABLE projects DROP COLUMN planner_harness_prompt;
        ALTER TABLE projects DROP COLUMN worker_harness_prompt;
        ALTER TABLE projects DROP COLUMN model;
        ALTER TABLE projects DROP COLUMN run_budget_usd;
        DELETE FROM system_settings WHERE key IN ('agent.harness_prompt', 'agent.model', 'agent.effort', 'agent.env');
        INSERT INTO schema_migrations (name) VALUES ('agent_setting_layers');
    END IF;
END $$;

-- MCP servers are catalog items too (kind 'mcp'), installed the same way. How
-- Claude Code starts or reaches one is in library_mcp_servers, one row per
-- item: a command with its arguments and environment (stdio), or a URL with
-- its headers (http, sse). The environment's and the headers' values are
-- secrets: only their names leave the database through the API
-- (loopx/agents/mcp.py), and each run that loads the server gets them in a
-- read-only file under the runtime root. The item's sha256 is the hash of
-- the whole configuration, so changing a value is a new version.
CREATE TABLE IF NOT EXISTS library_mcp_servers (
    item_id    TEXT PRIMARY KEY REFERENCES library_items(id) ON DELETE CASCADE,
    transport  TEXT NOT NULL CHECK (transport IN ('stdio', 'http', 'sse')),
    command    TEXT NOT NULL DEFAULT '',
    args       TEXT[] NOT NULL DEFAULT '{}',
    env        JSONB NOT NULL DEFAULT '{}',
    url        TEXT NOT NULL DEFAULT '',
    headers    JSONB NOT NULL DEFAULT '{}',
    CHECK ((transport = 'stdio') = (command <> '')),
    CHECK ((transport = 'stdio') = (url = '')),
    CHECK (transport = 'stdio' OR (cardinality(args) = 0 AND env = '{}'::jsonb)),
    CHECK (transport <> 'stdio' OR headers = '{}'::jsonb)
);
-- Once: a catalog from before MCP servers accepts them.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'library_items_kind_mcp') THEN
        ALTER TABLE library_items DROP CONSTRAINT IF EXISTS library_items_kind_check;
        ALTER TABLE library_items ADD CONSTRAINT library_items_kind_check
            CHECK (kind IN ('plugin', 'skill', 'mcp'));
        INSERT INTO schema_migrations (name) VALUES ('library_items_kind_mcp');
    END IF;
END $$;

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
