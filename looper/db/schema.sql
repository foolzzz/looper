-- Looper Agent System Schema
-- Idempotent: safe to run on every startup.

-- One-time data migrations; schema.sql itself runs on every startup.
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Once (the names the dashboard uses): "agent" names only a running agent. A
-- role's settings (agent_configs, keyed by agent_id) are role_configs keyed by
-- role; what was registered as an agent (agents) is an Agent Profile
-- (agent_profiles), and what selects one names it profile_id: a settings
-- layer, a task's choice (task_agent_overrides, now task_profile_overrides) and
-- a saved session (session_agent_id). The tables and columns are renamed in
-- place (their foreign keys follow), and their constraints take the names a
-- new database gives them. agent_configs.role is dropped first: it repeated
-- the key in every row Looper wrote, and nothing read it. This runs before
-- anything below names the new tables, so a database of any earlier version
-- arrives at them with its rows.
DO $$
DECLARE
    c RECORD;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'profiles_roles_renamed') THEN
        IF to_regclass('agent_configs') IS NOT NULL THEN
            ALTER TABLE agent_configs DROP COLUMN IF EXISTS role;
            ALTER TABLE agent_configs RENAME COLUMN agent_id TO role;
            ALTER TABLE agent_configs RENAME TO role_configs;
        END IF;
        IF to_regclass('agents') IS NOT NULL THEN
            ALTER TABLE agents RENAME TO agent_profiles;
        END IF;
        IF to_regclass('task_agent_overrides') IS NOT NULL THEN
            ALTER TABLE task_agent_overrides RENAME COLUMN agent_id TO profile_id;
            ALTER TABLE task_agent_overrides RENAME TO task_profile_overrides;
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
                   AND table_name = 'agent_setting_layers' AND column_name = 'agent_id') THEN
            ALTER TABLE agent_setting_layers RENAME COLUMN agent_id TO profile_id;
        END IF;
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
                   AND table_name = 'agent_sessions' AND column_name = 'session_agent_id') THEN
            ALTER TABLE agent_sessions RENAME COLUMN session_agent_id TO profile_id;
        END IF;
        FOR c IN
            SELECT t.tbl, t.old_name, t.new_name
            FROM (VALUES
                ('role_configs', 'agent_configs_pkey', 'role_configs_pkey'),
                ('agent_profiles', 'agents_pkey', 'agent_profiles_pkey'),
                ('agent_profiles', 'agents_name_key', 'agent_profiles_name_key'),
                ('agent_profiles', 'agents_runtime_id_fkey', 'agent_profiles_runtime_id_fkey'),
                ('agent_profiles', 'agents_provider_id_fkey', 'agent_profiles_provider_id_fkey'),
                ('agent_profiles', 'agents_model_check', 'agent_profiles_model_check'),
                ('agent_profiles', 'agents_effort_check', 'agent_profiles_effort_check'),
                ('task_profile_overrides', 'task_agent_overrides_pkey', 'task_profile_overrides_pkey'),
                ('task_profile_overrides', 'task_agent_overrides_task_id_fkey', 'task_profile_overrides_task_id_fkey'),
                ('task_profile_overrides', 'task_agent_overrides_role_check', 'task_profile_overrides_role_check'),
                ('task_profile_overrides', 'task_agent_overrides_agent_id_fkey',
                 'task_profile_overrides_profile_id_fkey'),
                ('agent_setting_layers', 'agent_setting_layers_agent_id_fkey', 'agent_setting_layers_profile_id_fkey')
            ) AS t(tbl, old_name, new_name)
            JOIN pg_constraint k ON k.conname = t.old_name AND k.conrelid = to_regclass(t.tbl)
        LOOP
            EXECUTE format('ALTER TABLE %I RENAME CONSTRAINT %I TO %I', c.tbl, c.old_name, c.new_name);
        END LOOP;
        INSERT INTO schema_migrations (name) VALUES ('profiles_roles_renamed');
    END IF;
END $$;

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
-- The runtime type the session belongs to and the Agent Profile (agent_profiles.id) that ran it
-- last: a process of another runtime type cannot resume it, so the next turn
-- starts a new session. NULL: a session from before Agent Profiles, which Claude Code ran.
ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS runtime_type TEXT;
ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS profile_id TEXT;
-- The provider (llm_providers.id) the session ran with: a session resumes only with the same runtime type and
-- the same provider (runtimes.can_resume). NULL: a session from before this column, run with Claude Code's own
-- login.
ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS provider_id TEXT;
-- The user dropped the session (/new in a chat with the Manager): the next session starts clean, without the
-- resume brief a new session otherwise gets. Cleared once that session has started.
ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS clean_start BOOLEAN NOT NULL DEFAULT false;

-- A task's journal: what happened in it that a later agent or session needs.
-- Code writes an entry, never the model, when its event happens: a step
-- accepted ('step_accepted', in the transaction that settles it), failed or
-- retried, a Planner decision that changes the plan, a supplement (what the
-- user added to the task while it ran), a new Planner session and the handoff
-- its old session wrote.
-- step_id is '' for an entry of the whole task. The Planner's turns and a
-- Worker's first turn get the entries back in bounded sections; code owns the
-- kinds (looper/agents/tiers/journal.py).
CREATE TABLE IF NOT EXISTS task_journal (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id     TEXT NOT NULL,
    kind        TEXT NOT NULL CONSTRAINT task_journal_kind_check CHECK (kind IN (
                    'step_accepted', 'step_failed', 'step_retry', 'decision', 'supplement',
                    'session_switch', 'handoff', 'progress')),
    summary     TEXT NOT NULL DEFAULT '',
    learnings   TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A supplement's way in ('chat': sent to the Planner on the Chat page; 'manager:<channel>'; 'task_page') and when
-- its task's Planner had it in a turn (NULL: not yet; the Planner's next turn gives it). Unused by other kinds.
ALTER TABLE task_journal ADD COLUMN IF NOT EXISTS via TEXT NOT NULL DEFAULT '';
ALTER TABLE task_journal ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_task_journal_task ON task_journal (task_id, id);
-- A step is accepted once.
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_journal_accepted ON task_journal (task_id, step_id)
    WHERE kind = 'step_accepted';

-- A step's git worktree in a project with step_workspaces = 'worktree': a step
-- that runs alongside others works in one, on a branch of its own, merged into
-- the task directory's branch (target_branch) when it is accepted. A retry
-- reuses it; a retry after a failed merge gets a new one. Code owns the states
-- and their transitions (looper/agents/step_workspaces.py). removed_at: its
-- directory and branch are gone; the commits of a merged one stay on the branch.
CREATE TABLE IF NOT EXISTS step_workspaces (
    id             BIGSERIAL PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id        TEXT NOT NULL REFERENCES task_steps(id) ON DELETE CASCADE,
    repo_dir       TEXT NOT NULL,
    subdir         TEXT NOT NULL DEFAULT '',
    target_branch  TEXT NOT NULL,
    path           TEXT NOT NULL,
    branch         TEXT NOT NULL,
    base_commit    TEXT NOT NULL,
    checkpoint     BOOLEAN NOT NULL DEFAULT false,
    result_commit  TEXT,
    merge_commit   TEXT,
    state          TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'merging', 'merged', 'unmerged', 'discarded')),
    merge_error    TEXT CHECK (merge_error IN ('conflict', 'local_changes', 'branch_changed', 'step_branch_changed',
                                               'git_failed')),
    detail         TEXT NOT NULL DEFAULT '',
    removed_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- An unmerged workspace says why; one that is in use or merged has no merge error.
    CONSTRAINT step_workspaces_unmerged_reason_check CHECK (
        CASE WHEN state = 'unmerged' THEN merge_error IS NOT NULL
             WHEN state IN ('active', 'merging', 'merged') THEN merge_error IS NULL
             ELSE true END)
);
CREATE INDEX IF NOT EXISTS idx_step_workspaces_task ON step_workspaces (task_id, id);
-- A step has one workspace in use at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_step_workspaces_open ON step_workspaces (step_id)
    WHERE state IN ('active', 'merging');
-- Where the task's branch was when it dispatched its first step, in a project with step workspaces: going back
-- there undoes the task's commits ('' when not recorded).
ALTER TABLE task_plans ADD COLUMN IF NOT EXISTS start_branch TEXT NOT NULL DEFAULT '';
ALTER TABLE task_plans ADD COLUMN IF NOT EXISTS start_commit TEXT NOT NULL DEFAULT '';

-- Every version of a task's requirement once it has been edited (tasks.requirement is always the latest; a task
-- never edited has none). Version 1 is the text it was created with. delivered_at: when the task's Planner had the
-- edit in a turn (NULL: not yet; set at once for a task that has no Planner yet, which plans the latest text).
CREATE TABLE IF NOT EXISTS task_requirement_versions (
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL CHECK (version >= 1),
    text          TEXT NOT NULL,
    via           TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at  TIMESTAMPTZ,
    PRIMARY KEY (task_id, version)
);

-- What the Manager did through its platform tools (looper/agents/ops), one row per call: the chat and the message
-- of the turn it came in (empty for a turn Claude ran on its own), the tool, its arguments (secret values masked),
-- the risk tier, how it ended (ok, needs_confirmation, error with its code) and its result; a confirmed call has the
-- confirmation_id of the call that asked. task_id names the task it acted on, without a foreign key, so a deleted
-- task's trail stays.
CREATE TABLE IF NOT EXISTS manager_actions (
    id               BIGSERIAL PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    channel          TEXT NOT NULL DEFAULT '',
    chat             TEXT NOT NULL DEFAULT '',
    message_id       TEXT NOT NULL DEFAULT '',
    tool             TEXT NOT NULL,
    arguments        JSONB NOT NULL DEFAULT '{}',
    risk             TEXT NOT NULL CHECK (risk IN ('read', 'write', 'high')),
    status           TEXT NOT NULL CHECK (status IN ('ok', 'needs_confirmation', 'error')),
    error_code       TEXT,
    result           JSONB,
    confirmation_id  TEXT,
    task_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_manager_actions_task ON manager_actions (task_id, id) WHERE task_id IS NOT NULL;

-- Each role's own settings: its display name, role prompt and timeout (manager, planner, worker, reviewer).
CREATE TABLE IF NOT EXISTS role_configs (
    role             TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL DEFAULT '',
    enabled          BOOLEAN NOT NULL DEFAULT true,
    system_prompt    TEXT NOT NULL DEFAULT '',
    max_turns        INTEGER NOT NULL DEFAULT 0,
    timeout_seconds  INTEGER NOT NULL DEFAULT 1200,
    extra_args       JSONB,
    metadata         JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The system-recommended prompt for this role. seed keeps it current; an
-- unedited row (system_prompt = default_prompt) follows it, an edited row does
-- not, and "reset to recommended" copies it back into system_prompt.
ALTER TABLE role_configs ADD COLUMN IF NOT EXISTS default_prompt TEXT NOT NULL DEFAULT '';
-- permission_mode was never applied: every agent runs with
-- --dangerously-skip-permissions (claude_cli.permission_args).
ALTER TABLE role_configs DROP COLUMN IF EXISTS permission_mode;
-- Once: max_concurrency was never enforced. How many of a role run at once in
-- a project is its layer's max_concurrency (agent_setting_layers, below).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'role_configs_max_concurrency_removed') THEN
        ALTER TABLE role_configs DROP COLUMN IF EXISTS max_concurrency;
        INSERT INTO schema_migrations (name) VALUES ('role_configs_max_concurrency_removed');
    END IF;
END $$;

-- Agent Profiles (RFC decision 10): a profile is a runtime, an LLM provider and
-- a model. A runtime is a runtime type, which code provides (an adapter,
-- looper/agents/runtimes), found on this machine: built in, one per type on
-- the server's PATH, with the executable it runs ('' = the type's default, for
-- Claude Code the server's configured one) and what its last probe found
-- (version, capabilities). A runtime registered by hand before (builtin
-- false) stays read-only while a profile uses it. A provider is where an agent's
-- requests go: the protocols it serves, its models and their prices, and how
-- it authenticates: a runtime type's own login (oauth, bound to that type) or
-- an API key, kept in llm_provider_secrets and never shown by the API. Code
-- owns the types, the protocols, the capabilities and the validation
-- (looper/agents/profile_registry.py). The built-in rows (builtin) are
-- read-only; a runtime, provider or profile still referred to cannot be deleted.
CREATE TABLE IF NOT EXISTS agent_runtimes (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    type          TEXT NOT NULL,
    executable    TEXT NOT NULL DEFAULT '',
    version       TEXT,
    capabilities  TEXT[] NOT NULL DEFAULT '{}'
        CHECK (capabilities <@ ARRAY['run', 'resident_session', 'resume', 'structured_output', 'claude_plugins', 'mcp',
                                     'add_dirs', 'compaction_events', 'autonomous_turns', 'cost_report']),
    probed_at     TIMESTAMPTZ,
    probe_error   TEXT,
    builtin       BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS llm_providers (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    protocols           TEXT[] NOT NULL CHECK (cardinality(protocols) > 0
        AND protocols <@ ARRAY['anthropic-messages', 'openai-responses', 'openai-chat']),
    auth                TEXT NOT NULL CHECK (auth IN ('oauth', 'api_key')),
    oauth_runtime_type  TEXT,
    base_url            TEXT NOT NULL DEFAULT '',
    -- The model catalog: [{"name", "context_window", "input_usd_per_mtok", "output_usd_per_mtok",
    -- "cache_read_usd_per_mtok", "cache_write_usd_per_mtok", "efforts", "background_model"}], each but the name
    -- null when unknown; [] = any model the provider takes. A null price falls back to the built-in price table.
    models              JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(models) = 'array'),
    -- At most this many runs at once on the provider; NULL = no limit.
    max_concurrency     INTEGER CHECK (max_concurrency > 0),
    builtin             BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((auth = 'oauth') = (oauth_runtime_type IS NOT NULL))
);
-- The provider's own environment variables for the runs that use it, [{"key", "value"}]: not secrets (its key is
-- in llm_provider_secrets), and none a runtime's provider binding sets itself.
ALTER TABLE llm_providers ADD COLUMN IF NOT EXISTS env JSONB NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(env) = 'array');
CREATE TABLE IF NOT EXISTS llm_provider_secrets (
    provider_id  TEXT PRIMARY KEY REFERENCES llm_providers(id) ON DELETE CASCADE,
    api_key      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_profiles (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    runtime_id   TEXT NOT NULL REFERENCES agent_runtimes(id),
    provider_id  TEXT NOT NULL REFERENCES llm_providers(id),
    model        TEXT NOT NULL CHECK (model <> ''),
    -- The effort of the profile's runs (no layer sets one). NULL: the runtime's own default.
    effort       TEXT CHECK (effort IN ('low', 'medium', 'high', 'xhigh', 'max')),
    builtin      BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Claude Code's agent teams for the profile's runs, only on a Claude Code runtime. On by default there: the
-- registry sets a new profile's (profile_registry.default_agent_teams) and a migration below turned them on once
-- for the profiles before; the column's default is for the other runtimes, which have none.
ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS agent_teams BOOLEAN NOT NULL DEFAULT false;
-- A task's profile overrides: its own choice of Agent Profile for some of its
-- roles (the Planner, Workers or Reviewers), the only agent setting a task has.
-- Each wins over every layer, for that task only; a profile a task chose cannot
-- be deleted.
CREATE TABLE IF NOT EXISTS task_profile_overrides (
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('planner', 'worker', 'reviewer')),
    profile_id  TEXT NOT NULL REFERENCES agent_profiles(id),
    PRIMARY KEY (task_id, role)
);
-- The built-ins, on every start: the Claude Code runtime (the server's claude),
-- its own login as a provider, Codex's own login as a provider
-- (looper/agents/runtimes/codex_login.py), and the profile every role runs when nothing
-- selects another (looper/agents/profile_registry.py BUILTIN_*). A missing one comes back.
INSERT INTO agent_runtimes (id, name, type, executable, capabilities, builtin)
VALUES ('rt-claude-code', 'claude-code', 'claude-code', '',
        ARRAY['add_dirs', 'autonomous_turns', 'claude_plugins', 'compaction_events', 'cost_report', 'mcp',
              'resident_session', 'resume', 'run'], true)
ON CONFLICT DO NOTHING;
INSERT INTO llm_providers (id, name, protocols, auth, oauth_runtime_type, builtin)
VALUES ('prov-claude-subscription', 'claude-subscription', ARRAY['anthropic-messages'], 'oauth', 'claude-code', true)
ON CONFLICT DO NOTHING;
INSERT INTO llm_providers (id, name, protocols, auth, oauth_runtime_type, builtin)
VALUES ('prov-codex-login', 'codex-login', ARRAY['openai-responses'], 'oauth', 'codex', true)
ON CONFLICT DO NOTHING;
INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model, builtin, agent_teams)
VALUES ('agent-claude-code-opus', 'claude-code/opus', 'rt-claude-code', 'prov-claude-subscription', 'opus', true, true)
ON CONFLICT DO NOTHING;

-- Settings the whole system shares, one value per key. Code owns each key's
-- type and validation; a key without a row has its built-in default. (The
-- agent settings live in agent_setting_layers.)
CREATE TABLE IF NOT EXISTS system_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The capability catalog (the library): plugins and skills acquired into Looper.
-- An item does nothing by itself; it loads only where it is installed
-- (library_installs). The tables are the source of truth: agents get a
-- read-only copy materialized under the runtime root (looper/agents/library.py),
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

-- Once: move the worker's old seeded limit (600 s) to the new default. A value
-- set later, including 600, stays.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'worker_timeout_default_86400') THEN
        UPDATE role_configs SET timeout_seconds = 86400
        WHERE role = 'worker' AND timeout_seconds = 600;
        INSERT INTO schema_migrations (name) VALUES ('worker_timeout_default_86400');
    END IF;
END $$;

-- Once: a journal from before M7c, which held 'step_accepted' entries only,
-- accepts the other kinds.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'task_journal_kinds_m7c') THEN
        ALTER TABLE task_journal DROP CONSTRAINT IF EXISTS task_journal_kind_check;
        ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check CHECK (kind IN (
            'step_accepted', 'step_failed', 'step_retry', 'decision', 'user_instruction', 'session_switch',
            'handoff'));
        INSERT INTO schema_migrations (name) VALUES ('task_journal_kinds_m7c');
    END IF;
END $$;

-- Once (task progress): the Planner's progress notes join the journal kinds. After the block above, which
-- sets the kinds before them, so a new database ends with these too.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'task_journal_kinds_progress') THEN
        ALTER TABLE task_journal DROP CONSTRAINT IF EXISTS task_journal_kind_check;
        ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check CHECK (kind IN (
            'step_accepted', 'step_failed', 'step_retry', 'decision', 'user_instruction', 'session_switch',
            'handoff', 'progress'));
        INSERT INTO schema_migrations (name) VALUES ('task_journal_kinds_progress');
    END IF;
END $$;

-- Once (task changes): every chat message to a Planner is a supplement now. The user instructions from before
-- become supplements their Planner already had, sent on the Chat page.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'task_journal_supplements') THEN
        ALTER TABLE task_journal DROP CONSTRAINT IF EXISTS task_journal_kind_check;
        UPDATE task_journal SET kind = 'supplement', via = 'chat', delivered_at = created_at
        WHERE kind = 'user_instruction';
        ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check CHECK (kind IN (
            'step_accepted', 'step_failed', 'step_retry', 'decision', 'supplement', 'session_switch',
            'handoff', 'progress'));
        INSERT INTO schema_migrations (name) VALUES ('task_journal_supplements');
    END IF;
END $$;

-- Once (Agent Profiles replace LLM profiles): llm_profiles and the role
-- configs' runtime and llm_profile_id were never on any agent's launch path;
-- runtimes, providers and Agent Profiles replace them.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'llm_profiles_removed') THEN
        ALTER TABLE role_configs DROP COLUMN IF EXISTS llm_profile_id;
        ALTER TABLE role_configs DROP COLUMN IF EXISTS runtime;
        DROP TABLE IF EXISTS llm_profiles;
        INSERT INTO schema_migrations (name) VALUES ('llm_profiles_removed');
    END IF;
END $$;

-- Once (runtimes are built in): a runtime registered by hand, which no Agent
-- Profile uses, is dropped; one a profile uses stays (read-only), so the
-- profile keeps running the executable it ran.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'agent_runtimes_unused_custom_removed') THEN
        DELETE FROM agent_runtimes r
        WHERE NOT r.builtin AND NOT EXISTS (SELECT 1 FROM agent_profiles a WHERE a.runtime_id = r.id);
        INSERT INTO schema_migrations (name) VALUES ('agent_runtimes_unused_custom_removed');
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
-- the types and validation (looper/agents/project_execution.py).
ALTER TABLE projects ADD COLUMN IF NOT EXISTS task_budget_usd DOUBLE PRECISION CHECK (task_budget_usd > 0);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS task_budget_action TEXT
    CHECK (task_budget_action IN ('wrap_up', 'fail'));
ALTER TABLE projects ADD COLUMN IF NOT EXISTS supervise_interval_seconds INTEGER
    CHECK (supervise_interval_seconds >= 0);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS review_policy TEXT CHECK (review_policy IN ('planner', 'all'));
-- Where a step that runs alongside others works: the task directory ('shared', also when NULL) or, in a git
-- checkout, a worktree of its own merged back when it is accepted ('worktree').
ALTER TABLE projects ADD COLUMN IF NOT EXISTS step_workspaces TEXT CHECK (step_workspaces IN ('shared', 'worktree'));
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
-- Why a failed task failed, when code can tell (looper/db/tasks.py FailKind); NULL
-- otherwise. 'task_budget': it reached its project's task_budget_usd.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS fail_kind TEXT CHECK (fail_kind IN ('task_budget'));
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks (project_id, created_at DESC);

-- A project's knowledge: reference documents its task agents read on demand
-- instead of getting them in their prompt. A text or upload document's content
-- is stored here and materialized read-only under the runtime root; a path
-- document is a file in the project's default directory (path is relative to
-- it), read live. size_bytes and sha256 are the content's (a path document's
-- as of its last save). Code owns validation and caps (looper/agents/knowledge.py).
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
-- each may do (looper/agents/catalog.py).
ALTER TABLE library_items ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'machine'
    CHECK (origin IN ('machine', 'upload', 'git', 'ui', 'project'));
ALTER TABLE library_items ADD COLUMN IF NOT EXISTS owner_project_id TEXT REFERENCES projects(id) ON DELETE CASCADE;
-- One line on what the item does; '' when it has none (an imported item).
ALTER TABLE library_items ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
-- An uploaded or git item edited in Looper since it came in; a re-import from its repository clears it.
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
-- looper/agents/project_skills.py skill_file), and is installed in its project
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
-- layer set (never at the system layer). profile_id is the Agent Profile the
-- layer's roles run (its model and effort with it). Code owns the validation and the resolution
-- (looper/agents/agent_settings.py). One row per layer and place.
CREATE TABLE IF NOT EXISTS agent_setting_layers (
    scope           TEXT NOT NULL CHECK (scope IN ('system', 'role', 'project', 'role_project')),
    role            TEXT CHECK (role IN ('manager', 'planner', 'worker', 'reviewer')),
    project_id      TEXT REFERENCES projects(id) ON DELETE CASCADE,
    harness_prompt  TEXT,
    profile_id      TEXT REFERENCES agent_profiles(id),
    env             JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(env) = 'array'),
    run_budget_usd  DOUBLE PRECISION CHECK (run_budget_usd > 0),
    max_concurrency INTEGER CONSTRAINT agent_setting_layers_max_concurrency_check CHECK (max_concurrency IS NULL
                        OR (max_concurrency BETWEEN 1 AND 64 AND scope IN ('role', 'role_project')
                            AND role <> 'manager')),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((role IS NOT NULL) = (scope IN ('role', 'role_project'))),
    CHECK ((project_id IS NOT NULL) = (scope IN ('project', 'role_project'))),
    CHECK (scope <> 'role_project' OR role <> 'manager')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_setting_layers_place
    ON agent_setting_layers (scope, COALESCE(role, ''), COALESCE(project_id, ''));

-- Once (layered settings): the values that lived in four places move into
-- their layer, unchanged; the old columns and keys are dropped. The Settings
-- defaults (system_settings agent.*) become the system layer; each role's
-- model, and the Manager's harness (the only one the API took), the role
-- layer; a project's model and per-run cap the project layer; a project's
-- Planner and Worker harness the role-in-project layer (a Reviewer follows its
-- project's Workers at resolution, as before).
DO $$
DECLARE
    env_value JSONB := '[]'::jsonb;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'agent_setting_layers') THEN
        -- Layers held a model and an effort then; the profiles' migration below turns the model into an Agent
        -- Profile, and the effort moves onto profiles after this file (looper/agents/layer_effort.py).
        ALTER TABLE agent_setting_layers ADD COLUMN IF NOT EXISTS model TEXT;
        ALTER TABLE agent_setting_layers ADD COLUMN IF NOT EXISTS effort TEXT;
        ALTER TABLE role_configs ADD COLUMN IF NOT EXISTS harness_prompt TEXT NOT NULL DEFAULT '';
        ALTER TABLE role_configs ADD COLUMN IF NOT EXISTS model TEXT;
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
        SELECT 'role', role,
               CASE WHEN role = 'manager' THEN NULLIF(harness_prompt, '') END, NULLIF(btrim(model), '')
        FROM role_configs
        WHERE role IN ('manager', 'planner', 'worker', 'reviewer')
          AND ((role = 'manager' AND harness_prompt <> '') OR NULLIF(btrim(model), '') IS NOT NULL);
        INSERT INTO agent_setting_layers (scope, project_id, model, run_budget_usd)
        SELECT 'project', id, model, run_budget_usd FROM projects
        WHERE model IS NOT NULL OR run_budget_usd IS NOT NULL;
        INSERT INTO agent_setting_layers (scope, role, project_id, harness_prompt)
        SELECT 'role_project', 'planner', id, planner_harness_prompt FROM projects WHERE planner_harness_prompt <> ''
        UNION ALL
        SELECT 'role_project', 'worker', id, worker_harness_prompt FROM projects WHERE worker_harness_prompt <> '';
        ALTER TABLE role_configs DROP COLUMN harness_prompt;
        ALTER TABLE role_configs DROP COLUMN model;
        ALTER TABLE projects DROP COLUMN planner_harness_prompt;
        ALTER TABLE projects DROP COLUMN worker_harness_prompt;
        ALTER TABLE projects DROP COLUMN model;
        ALTER TABLE projects DROP COLUMN run_budget_usd;
        DELETE FROM system_settings WHERE key IN ('agent.harness_prompt', 'agent.model', 'agent.effort', 'agent.env');
        INSERT INTO schema_migrations (name) VALUES ('agent_setting_layers');
    END IF;
END $$;

-- Once (Agent Profiles select the model): a layer's model becomes the profile
-- of the Claude Code runtime, its own login and that model, named
-- claude-code/<model> (claude-code/opus is the built-in one), so every role
-- resolves the model it did. A stored model that no longer validates was
-- ignored, as if unset: it selects no profile. A layer left with nothing set is
-- removed.
ALTER TABLE agent_setting_layers ADD COLUMN IF NOT EXISTS profile_id TEXT REFERENCES agent_profiles(id);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'agent_setting_layers_agent') THEN
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
                   AND table_name = 'agent_setting_layers' AND column_name = 'model') THEN
            CREATE TEMP TABLE layer_models AS
            SELECT scope, role, project_id, btrim(model, E' \t\n\r\x0b\x0c') AS model FROM agent_setting_layers
            WHERE model IS NOT NULL;
            DELETE FROM layer_models
            WHERE NOT (model ~ '^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*$' AND length(model) <= 200);
            INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model)
            SELECT 'agent-' || left(replace(gen_random_uuid()::text, '-', ''), 12), 'claude-code/' || model,
                   'rt-claude-code', 'prov-claude-subscription', model
            FROM (SELECT DISTINCT model FROM layer_models) m
            ON CONFLICT (name) DO NOTHING;
            UPDATE agent_setting_layers l SET profile_id = a.id
            FROM layer_models m JOIN agent_profiles a ON a.name = 'claude-code/' || m.model
            WHERE l.scope = m.scope AND l.role IS NOT DISTINCT FROM m.role
              AND l.project_id IS NOT DISTINCT FROM m.project_id;
            DROP TABLE layer_models;
            ALTER TABLE agent_setting_layers DROP COLUMN model;
            DELETE FROM agent_setting_layers WHERE harness_prompt IS NULL AND profile_id IS NULL AND effort IS NULL
                AND env = '[]'::jsonb AND run_budget_usd IS NULL;
        END IF;
        INSERT INTO schema_migrations (name) VALUES ('agent_setting_layers_agent');
    END IF;
END $$;
-- How many of a task role run at once in one project (agent_settings.resolve_concurrency): set only on a
-- planner, worker or reviewer role layer, or on that role's layer in a project.
ALTER TABLE agent_setting_layers ADD COLUMN IF NOT EXISTS max_concurrency INTEGER
    CONSTRAINT agent_setting_layers_max_concurrency_check CHECK (max_concurrency IS NULL
        OR (max_concurrency BETWEEN 1 AND 64 AND scope IN ('role', 'role_project') AND role <> 'manager'));

-- Once (agent teams are on by default for Claude Code profiles): every profile on a Claude Code runtime, the
-- built-in one and those the migrations above added included, gets them. Only once, so a profile the user turns
-- off later stays off.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'agent_profiles_agent_teams_on') THEN
        UPDATE agent_profiles a SET agent_teams = true FROM agent_runtimes r
        WHERE r.id = a.runtime_id AND r.type = 'claude-code';
        INSERT INTO schema_migrations (name) VALUES ('agent_profiles_agent_teams_on');
    END IF;
END $$;

-- MCP servers are catalog items too (kind 'mcp'), installed the same way. How
-- Claude Code starts or reaches one is in library_mcp_servers, one row per
-- item: a command with its arguments and environment (stdio), or a URL with
-- its headers (http, sse). The environment's and the headers' values are
-- secrets: only their names leave the database through the API
-- (looper/agents/mcp.py), and each run that loads the server gets them in a
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

-- A project's schedules: a time rule (5-field cron, wall time in an IANA zone)
-- and the task each firing creates and starts at once (the scheduler in
-- looper serve, looper/agents/scheduler.py). A project with schedules cannot be
-- deleted. work_dir '' is the project's default when it fires. A new schedule
-- is disabled; next_fire_at is the next fire time while it is enabled, NULL
-- otherwise. Code owns validation (looper/agents/schedules.py).
CREATE TABLE IF NOT EXISTS schedules (
    id                   TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL REFERENCES projects(id),
    name                 TEXT NOT NULL,
    cron                 TEXT NOT NULL,
    timezone             TEXT NOT NULL,
    requirement          TEXT NOT NULL,
    work_dir             TEXT NOT NULL DEFAULT '',
    overlap              TEXT NOT NULL DEFAULT 'skip' CHECK (overlap IN ('skip', 'queue')),
    catch_up             TEXT NOT NULL DEFAULT 'once' CHECK (catch_up IN ('once', 'skip')),
    run_timeout_seconds  INTEGER CHECK (run_timeout_seconds > 0),
    run_budget_usd       DOUBLE PRECISION CHECK (run_budget_usd > 0),
    enabled              BOOLEAN NOT NULL DEFAULT false,
    next_fire_at         TIMESTAMPTZ,
    last_fired_at        TIMESTAMPTZ,
    last_success_at      TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_via          TEXT NOT NULL DEFAULT 'dashboard' CHECK (created_via IN ('dashboard', 'manager')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (enabled OR next_fire_at IS NULL)
);
-- S2: a gate command run in the work directory before each run (nothing printed: skipped; output: given to the
-- task; failure: the run fails), a daily spend cap over the schedule's runs, and whom to tell: notify ('failures',
-- 'always', 'never') to a chat a channel allows (notify_channel and notify_chat, both or neither; none: the
-- dashboard only). last_ok_at is the last run that completed or whose gate found nothing to do; the two alerts are
-- active while failures_alert_at (3 failures in a row) or stale_alert_at (no success for a while) is set.
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS gate_command TEXT;
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS gate_timeout_seconds INTEGER CHECK (gate_timeout_seconds > 0);
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS daily_budget_usd DOUBLE PRECISION CHECK (daily_budget_usd > 0);
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS notify TEXT NOT NULL DEFAULT 'failures'
    CHECK (notify IN ('failures', 'always', 'never'));
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS notify_channel TEXT CHECK (notify_channel IN ('telegram', 'lark'));
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS notify_chat TEXT;
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS last_ok_at TIMESTAMPTZ;
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS failures_alert_at TIMESTAMPTZ;
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS stale_alert_at TIMESTAMPTZ;
-- When the schedule was created or last enabled (set when enabled turns true): the no-success alert counts from it
-- or from last_ok_at, whichever is later, so a schedule enabled again after a pause is not stale at once. A schedule
-- from before takes its updated_at, which is at or after its last enabling.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'schedules'
                     AND column_name = 'enabled_at') THEN
        ALTER TABLE schedules ADD COLUMN enabled_at TIMESTAMPTZ;
        UPDATE schedules SET enabled_at = updated_at;
        ALTER TABLE schedules ALTER COLUMN enabled_at SET DEFAULT now();
        ALTER TABLE schedules ALTER COLUMN enabled_at SET NOT NULL;
    END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'schedules_notify_target_check'
                   AND conrelid = to_regclass('schedules')) THEN
        ALTER TABLE schedules ADD CONSTRAINT schedules_notify_target_check
            CHECK ((notify_channel IS NULL) = (notify_chat IS NULL));
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_project_name ON schedules (project_id, lower(name));
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules (next_fire_at) WHERE enabled;
-- The Agent Profile a schedule's tasks run for some of their roles, copied into each task's
-- task_profile_overrides when it fires; a profile a schedule chose cannot be deleted.
CREATE TABLE IF NOT EXISTS schedule_profile_overrides (
    schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('planner', 'worker', 'reviewer')),
    profile_id  TEXT NOT NULL REFERENCES agent_profiles(id),
    PRIMARY KEY (schedule_id, role)
);
-- One firing of a schedule: on time ('scheduled'), for missed fires ('catch_up', missed = how many it stands in
-- for) or 'manual'. 'queued' waits for the previous run's task to end (overlap 'queue', at most one); 'running'
-- has its task; the others are final, written once: the task's outcome when it ends, 'timed_out' when the scheduler
-- stopped it at run_timeout_seconds, or why the firing was skipped (no task then).
CREATE TABLE IF NOT EXISTS schedule_runs (
    id            TEXT PRIMARY KEY,
    schedule_id   TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    scheduled_for TIMESTAMPTZ NOT NULL,
    fired_at      TIMESTAMPTZ,
    trigger       TEXT NOT NULL CHECK (trigger IN ('scheduled', 'catch_up', 'manual')),
    missed        INTEGER NOT NULL DEFAULT 0 CHECK (missed >= 0),
    status        TEXT NOT NULL CHECK (status IN (
                      'queued', 'gating', 'running', 'completed', 'failed', 'stopped', 'cancelled', 'timed_out',
                      'gate_failed', 'skipped_overlap', 'skipped_missed', 'skipped_gate', 'skipped_budget')),
    task_id       TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    summary       TEXT,
    cost_usd      DOUBLE PRECISION,
    error         TEXT,
    finished_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- S2: what the gate printed (clipped), and the earlier run whose unfinished input this run was given.
ALTER TABLE schedule_runs ADD COLUMN IF NOT EXISTS gate_output TEXT;
ALTER TABLE schedule_runs ADD COLUMN IF NOT EXISTS carried_from TEXT REFERENCES schedule_runs(id) ON DELETE SET NULL;
-- Once (S2): runs from before the gate and the daily budget accept their statuses ('gating' while the gate runs).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'schedule_runs_status_s2') THEN
        ALTER TABLE schedule_runs DROP CONSTRAINT IF EXISTS schedule_runs_status_check;
        ALTER TABLE schedule_runs ADD CONSTRAINT schedule_runs_status_check CHECK (status IN (
            'queued', 'gating', 'running', 'completed', 'failed', 'stopped', 'cancelled', 'timed_out',
            'gate_failed', 'skipped_overlap', 'skipped_missed', 'skipped_gate', 'skipped_budget'));
        INSERT INTO schema_migrations (name) VALUES ('schedule_runs_status_s2');
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule ON schedule_runs (schedule_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_schedule_runs_open ON schedule_runs (schedule_id) WHERE status IN ('queued', 'running');
-- Every message about a schedule, sent or not: a run's outcome, an alert or its recovery. 'dashboard' when the
-- schedule names no chat (or notify is 'never' for an alert), 'failed' with the reason when sending failed.
CREATE TABLE IF NOT EXISTS schedule_notifications (
    id          TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    run_id      TEXT REFERENCES schedule_runs(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('run', 'failures_alert', 'failures_recovered', 'stale_alert',
                                              'stale_recovered')),
    channel     TEXT,
    chat        TEXT,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('sent', 'failed', 'dashboard')),
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_schedule_notifications_schedule ON schedule_notifications (schedule_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_schedule_runs_task ON schedule_runs (task_id);
-- The schedule that created the task (NULL once the schedule is deleted), and the task's own spend cap over its
-- project's task_budget_usd (a schedule's run_budget_usd, copied when it fires).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS schedule_id TEXT REFERENCES schedules(id) ON DELETE SET NULL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS budget_usd DOUBLE PRECISION CHECK (budget_usd > 0);
-- Who is told when the task ends (agents/task_notice.py), in the form its channel's notify takes: the Telegram
-- chat id, or the Lark sender's open_id; NULL for a task asked for elsewhere (the web chat, the dashboard).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS chat_id TEXT;
-- The user's message the Manager's task JSON answered (often the confirmation), kept as history. NULL for a task
-- created elsewhere.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS original_user_text TEXT;
-- A few words naming the task in the user's language, from the Manager's task JSON (db/tasks.py task_name falls
-- back to goal_id, then the requirement). NULL for a task created elsewhere or before.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS title TEXT;
CREATE INDEX IF NOT EXISTS idx_tasks_schedule ON tasks (schedule_id, created_at DESC) WHERE schedule_id IS NOT NULL;

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
-- Where cost_usd comes from: what the runtime reported ('runtime'; Claude Code with its own login), or the
-- run's tokens at its provider's prices ('provider_price'); NULL for runs recorded before Agent Profiles.
ALTER TABLE llm_records ADD COLUMN IF NOT EXISTS cost_source TEXT CHECK (cost_source IN ('runtime', 'provider_price'));

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

-- Which channel the turn came from (web, telegram, lark): so the console shows every channel's
-- conversation, each labelled. A non-web session id is "<channel>:<chat_id>".
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'web';

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

-- A channel's bot token, kept out of channel_configs.config so it never leaves
-- the database through the API (which returns the whole config row verbatim).
-- Only get_bot_token reads it, for the launch that builds the adapter; the API
-- exposes only has_bot_token. Mirrors llm_provider_secrets.
CREATE TABLE IF NOT EXISTS channel_secrets (
    channel_id TEXT PRIMARY KEY REFERENCES channel_configs(channel_id) ON DELETE CASCADE,
    bot_token  TEXT NOT NULL
);

-- The latest senders a channel's allowlist refused (the Lark channel records them), so Settings → Channels can
-- name them and let one in: one row per channel and sender, with how often they wrote and when last; never what
-- they wrote. The channel keeps its 20 newest.
CREATE TABLE IF NOT EXISTS channel_rejected_senders (
    channel_id  TEXT NOT NULL,
    sender_id   TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    last_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, sender_id)
);
-- A table from before kept the start of each refused message: dropped, with what it held.
ALTER TABLE channel_rejected_senders DROP COLUMN IF EXISTS snippet;
