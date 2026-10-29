-- Looper Agent System Schema
-- Executed by connection.ensure_schema in a single locked transaction.

-- The applied baseline text, used when deciding whether to make an upgrade backup.
CREATE TABLE IF NOT EXISTS schema_fingerprint (
    id          BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    sha256      TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Runtime, provider, model and profile registry.
CREATE TABLE IF NOT EXISTS agent_runtimes (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    type          TEXT NOT NULL,
    executable    TEXT NOT NULL DEFAULT '',
    version       TEXT,
    capabilities  TEXT[] NOT NULL DEFAULT '{}'
        CHECK (capabilities <@ ARRAY['run', 'resident_session', 'resume', 'structured_output', 'claude_plugins', 'mcp',
                                     'add_dirs', 'compaction_events', 'autonomous_turns', 'cost_report',
                                     'read_tools', 'steer']),
    probed_at     TIMESTAMPTZ,
    probe_error   TEXT,
    builtin       BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS llm_providers (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    auth                TEXT NOT NULL CHECK (auth IN ('oauth', 'api_key')),
    oauth_runtime_type  TEXT,
    -- At most this many runs at once on the provider; NULL = no limit.
    max_concurrency     INTEGER CHECK (max_concurrency > 0),
    builtin             BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((auth = 'oauth') = (oauth_runtime_type IS NOT NULL)),
    env JSONB NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(env) = 'array'),
    vendor_type TEXT NOT NULL
    CHECK (vendor_type IN ('glm-bigmodel', 'glm-zai', 'deepseek', 'kimi-moonshot-cn', 'kimi-moonshot',
                          'cpa', 'opencode', 'sub2api', 'claude-subscription', 'codex-login', 'grok-login', 'custom'))
    CHECK (
        (vendor_type = 'claude-subscription' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'claude-code')
        OR (vendor_type = 'codex-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'codex')
        OR (vendor_type = 'grok-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'grok')
        OR (vendor_type NOT IN ('claude-subscription', 'codex-login', 'grok-login')
            AND auth = 'api_key' AND oauth_runtime_type IS NULL)
    )
);
CREATE TABLE IF NOT EXISTS llm_provider_secrets (
    provider_id  TEXT PRIMARY KEY REFERENCES llm_providers(id) ON DELETE CASCADE,
    api_key      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registry_schema_version (
    id       BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    version  INTEGER NOT NULL CHECK (version > 0)
);
CREATE TABLE IF NOT EXISTS llm_provider_endpoints (
    provider_id          TEXT NOT NULL REFERENCES llm_providers(id) ON DELETE CASCADE,
    protocol             TEXT NOT NULL CHECK (protocol IN ('anthropic-messages', 'openai-responses', 'openai-chat')),
    base_url             TEXT NOT NULL CHECK (length(base_url) <= 2000 AND base_url !~ '[[:space:][:cntrl:]]'
                             AND (base_url = '' OR base_url ~ '^https?://[^/?#@]+([/?#]|$)')),
    use_runtime_default  BOOLEAN NOT NULL DEFAULT false CHECK (NOT use_runtime_default OR base_url = ''),
    PRIMARY KEY (provider_id, protocol)
);

-- CHECK constraints cannot contain subqueries. Keep the array validation in a
-- single immutable predicate, including NULL elements and duplicate efforts.
CREATE OR REPLACE FUNCTION llm_model_efforts_valid(value TEXT[]) RETURNS BOOLEAN
LANGUAGE SQL IMMUTABLE STRICT AS $$
    SELECT coalesce(array_ndims(value), 1) = 1 AND cardinality(value) <= 10
        AND NOT EXISTS (SELECT 1 FROM unnest(value) e WHERE e IS NULL OR e !~ '^[a-z][a-z0-9_-]{0,31}$')
        AND (SELECT count(DISTINCT e) FROM unnest(value) e) = cardinality(value)
$$;
CREATE TABLE IF NOT EXISTS llm_models (
    id                        TEXT PRIMARY KEY CHECK (id LIKE 'model-%' AND length(id) > 6),
    provider_id               TEXT NOT NULL,
    protocol                  TEXT NOT NULL CHECK (protocol IN ('anthropic-messages', 'openai-responses', 'openai-chat')),
    name                      TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200
                                  AND name ~ '^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*$'),
    context_window            INTEGER CHECK (context_window BETWEEN 1 AND 100000000),
    input_usd_per_mtok        DOUBLE PRECISION CHECK (input_usd_per_mtok BETWEEN 0 AND 10000),
    output_usd_per_mtok       DOUBLE PRECISION CHECK (output_usd_per_mtok BETWEEN 0 AND 10000),
    cache_read_usd_per_mtok   DOUBLE PRECISION CHECK (cache_read_usd_per_mtok BETWEEN 0 AND 10000),
    cache_write_usd_per_mtok  DOUBLE PRECISION CHECK (cache_write_usd_per_mtok BETWEEN 0 AND 10000),
    efforts                   TEXT[] CHECK (llm_model_efforts_valid(efforts)),
    background_model          TEXT CHECK (length(background_model) BETWEEN 1 AND 200
                                  AND background_model ~ '^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*$'),
    catalog_order             INTEGER NOT NULL DEFAULT 0 CHECK (catalog_order >= 0),
    builtin                   BOOLEAN NOT NULL DEFAULT false,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (provider_id, protocol) REFERENCES llm_provider_endpoints(provider_id, protocol) ON DELETE RESTRICT,
    UNIQUE (provider_id, name, protocol)
);
CREATE TABLE IF NOT EXISTS agent_profiles (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    runtime_id   TEXT NOT NULL REFERENCES agent_runtimes(id),
    -- The effort of the profile's runs (no layer sets one). NULL: the runtime's own default.
    effort       TEXT CHECK (effort IN ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')),
    builtin      BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_teams BOOLEAN NOT NULL DEFAULT false,
    custom_prompt TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL REFERENCES llm_models(id) ON DELETE RESTRICT
);

-- Append-only migration history, intentionally without live-object FKs: normal
-- deletion of a model/provider must not erase or be blocked by its old mapping.
CREATE TABLE IF NOT EXISTS llm_model_migration_map (
    legacy_provider_id  TEXT NOT NULL,
    legacy_model        TEXT NOT NULL,
    protocol            TEXT NOT NULL CHECK (protocol IN ('anthropic-messages', 'openai-responses', 'openai-chat')),
    provider_id         TEXT NOT NULL,
    model_id            TEXT NOT NULL,
    PRIMARY KEY (legacy_provider_id, legacy_model, protocol)
);

-- Every task belongs to one project; a project may give its tasks a default
-- directory (work_dir, '' = none).
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    work_dir    TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    background TEXT NOT NULL DEFAULT '',
    task_budget_usd DOUBLE PRECISION CHECK (task_budget_usd > 0),
    task_budget_action TEXT
    CHECK (task_budget_action IN ('wrap_up', 'fail')),
    supervise_interval_seconds INTEGER
    CHECK (supervise_interval_seconds >= 0),
    review_policy TEXT CHECK (review_policy IN ('planner', 'all')),
    step_workspaces TEXT CHECK (step_workspaces IN ('shared', 'worktree')),
    workspace_links TEXT[] CHECK (cardinality(workspace_links) <= 20),
    task_workspaces TEXT CHECK (task_workspaces IN ('in_place', 'worktree')),
    operator_notes TEXT CHECK (operator_notes IN ('off', 'on')),
    vault TEXT CHECK (vault IN ('off', 'on')),
    max_step_retries INTEGER
    CHECK (max_step_retries >= 0 AND max_step_retries <= 20),
    planner_roles TEXT CHECK (planner_roles IN ('off', 'on')),
    role_text_max_chars INTEGER
    CHECK (role_text_max_chars >= 500 AND role_text_max_chars <= 20000),
    user_gates TEXT CHECK (user_gates IN ('off', 'on')),
    gate_reminder_hours INTEGER
    CHECK (gate_reminder_hours >= 0 AND gate_reminder_hours <= 168),
    agent_room TEXT CHECK (agent_room IN ('off', 'on')),
    room_post_per_minute INTEGER
    CHECK (room_post_per_minute >= 1 AND room_post_per_minute <= 60),
    room_broadcasts_per_run INTEGER
    CHECK (room_broadcasts_per_run >= 0 AND room_broadcasts_per_run <= 50),
    room_message_max_chars INTEGER
    CHECK (room_message_max_chars >= 500 AND room_message_max_chars <= 20000),
    room_messages_per_task INTEGER
    CHECK (room_messages_per_task >= 20 AND room_messages_per_task <= 5000),
    room_ping_pong_rounds INTEGER
    CHECK (room_ping_pong_rounds >= 2 AND room_ping_pong_rounds <= 50),
    room_wait_max_seconds INTEGER
    CHECK (room_wait_max_seconds >= 0 AND room_wait_max_seconds <= 120),
    room_consult_budget_usd DOUBLE PRECISION
    CHECK (room_consult_budget_usd >= 0.01 AND room_consult_budget_usd <= 5),
    room_consult_timeout_seconds INTEGER
    CHECK (room_consult_timeout_seconds >= 30 AND room_consult_timeout_seconds <= 1800),
    room_consults_per_task INTEGER
    CHECK (room_consults_per_task >= 0 AND room_consults_per_task <= 100),
    wait_commands TEXT CHECK (wait_commands IN ('off', 'on')),
    distill_lessons TEXT NOT NULL DEFAULT 'off' CONSTRAINT projects_distill_lessons_check
        CHECK (distill_lessons IN ('off', 'draft', 'write'))
);

-- A Manager: the default one ('manager', the one every install had) or a scenario Manager the operator defined
-- (manager-<key>). Its id is its agent id: its session (agent_sessions), its runs (llm_records.agent_id), its bus
-- queue. The default Manager's prompt and profile stay in role_configs.manager and Settings → Roles, so its row
-- holds neither, uses every tool, sees every task and cannot be disabled.
CREATE TABLE IF NOT EXISTS managers (
    id            TEXT PRIMARY KEY
                  CONSTRAINT managers_id_check CHECK (id = 'manager' OR id ~ '^manager-[a-z][a-z0-9-]{1,30}$'),
    name          TEXT NOT NULL CONSTRAINT managers_name_check CHECK (btrim(name) <> '' AND length(name) <= 60),
    description   TEXT NOT NULL DEFAULT '' CONSTRAINT managers_description_check CHECK (length(description) <= 300),
    system_prompt TEXT,
    profile_id    TEXT REFERENCES agent_profiles(id) ON DELETE RESTRICT,
    tools         TEXT NOT NULL DEFAULT 'tasks' CONSTRAINT managers_tools_check CHECK (tools IN ('all', 'tasks')),
    -- Which tasks the Manager sees and may act on (its task tools, overview counts, spend, task-derived reads):
    -- 'all' every task, 'own' only the tasks it created. Independent of tools; any combination is valid.
    task_scope    TEXT NOT NULL DEFAULT 'own' CONSTRAINT managers_task_scope_check CHECK (task_scope IN ('all', 'own')),
    task_policy   TEXT NOT NULL DEFAULT 'free'
                  CONSTRAINT managers_task_policy_check CHECK (task_policy IN ('free', 'templates_only')),
    enabled       BOOLEAN NOT NULL DEFAULT true,
    idle_timeout_seconds INTEGER NOT NULL DEFAULT 1800
                  CONSTRAINT managers_idle_timeout_seconds_check
                  CHECK (idle_timeout_seconds = 0 OR idle_timeout_seconds BETWEEN 60 AND 86400),
    -- Bumped in the same transaction by every write that changes what the Manager may do (tools, task_scope,
    -- task_policy, enabled, a template saved or removed): ManagerOps compares it at each call.
    revision      BIGINT NOT NULL DEFAULT 0,
    -- Deleted on the dashboard: the row stays so its chat threads keep their owner (they never fall to the default
    -- Manager) and its id is never reused; a deleted row is disabled and hidden from every view.
    deleted_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT managers_default_check CHECK (id <> 'manager' OR
        (system_prompt IS NULL AND profile_id IS NULL AND tools = 'all' AND task_scope = 'all' AND enabled
         AND deleted_at IS NULL AND idle_timeout_seconds = 0)),
    CONSTRAINT managers_deleted_check CHECK (deleted_at IS NULL OR NOT enabled),
    CONSTRAINT managers_prompt_check CHECK (id = 'manager' OR btrim(coalesce(system_prompt, '')) <> '')
);

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
    completed_at      TIMESTAMPTZ,
    work_dir TEXT NOT NULL DEFAULT '',
    attempt_ended_at TIMESTAMPTZ,
    config JSONB NOT NULL DEFAULT '{}',
    workspace_dir TEXT NOT NULL DEFAULT '',
    workspace_mode TEXT CHECK (workspace_mode IN ('in_place', 'worktree')),
    workspace_base TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL REFERENCES projects(id),
    fail_kind TEXT CHECK (fail_kind IN ('task_budget')),
    manager_id TEXT NOT NULL DEFAULT 'manager'
    REFERENCES managers(id) ON DELETE RESTRICT,
    template JSONB,
    origin_thread TEXT,
    schedule_id TEXT,
    budget_usd DOUBLE PRECISION CHECK (budget_usd > 0),
    planner_session_label TEXT,
    chat_id TEXT,
    chat_mention TEXT,
    original_user_text TEXT,
    title TEXT,
    client_id TEXT,
    continues_schedule_session BOOLEAN NOT NULL DEFAULT false,
    chat_kind TEXT CHECK (chat_kind IN ('user', 'chat'))
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
    completed_at     TIMESTAMPTZ,
    step_key TEXT NOT NULL DEFAULT '',
    depends_on TEXT[] NOT NULL DEFAULT '{}',
    review BOOLEAN NOT NULL DEFAULT false,
    validation_commands TEXT[] NOT NULL DEFAULT '{}',
    acceptance_criteria TEXT[] NOT NULL DEFAULT '{}',
    retries INTEGER NOT NULL DEFAULT 0,
    feedback TEXT[] NOT NULL DEFAULT '{}',
    reviewer_id TEXT,
    missed_messages TEXT[] NOT NULL DEFAULT '{}',
    started_at TIMESTAMPTZ,
    role_key TEXT,
    role_version INTEGER,
    review_stage TEXT CHECK (review_stage IN ('builtin', 'role')),
    review_roles JSONB NOT NULL DEFAULT '[]'::jsonb,
    review_position INTEGER,
    kind TEXT NOT NULL DEFAULT 'work' CHECK (kind IN ('work', 'gate', 'wait')),
    CONSTRAINT task_steps_review_roles_reviewed
        CHECK (jsonb_typeof(review_roles) = 'array' AND (jsonb_array_length(review_roles) = 0 OR review))
);

CREATE INDEX IF NOT EXISTS idx_steps_task ON task_steps(task_id, step_index);

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
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    start_branch TEXT NOT NULL DEFAULT '',
    start_commit TEXT NOT NULL DEFAULT ''
);

-- The Claude session a resident agent resumes after a restart: 'manager', or
-- 'planner-<task_id>'. session_totals is the session's running cost and
-- tokens at its last result (run_usage.Usage), the baseline a resumed process
-- counts from; session_id NULL means the next turn starts a new session.
CREATE TABLE IF NOT EXISTS agent_sessions (
    agent_id        TEXT PRIMARY KEY,
    session_id      TEXT,
    session_totals  JSONB NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    runtime_type TEXT,
    profile_id TEXT,
    provider_id TEXT,
    clean_start BOOLEAN NOT NULL DEFAULT false,
    pending_handoff TEXT,
    selected_profile_id TEXT REFERENCES agent_profiles(id) ON DELETE RESTRICT,
    CONSTRAINT agent_sessions_selected_profile_manager
        CHECK (selected_profile_id IS NULL OR agent_id = 'manager' OR agent_id LIKE 'manager-%')
);

-- One protocol per api session; agent_sessions keeps the current-session pointer.
CREATE TABLE IF NOT EXISTS api_sessions (
    session_id  TEXT PRIMARY KEY,
    protocol    TEXT NOT NULL CHECK (protocol IN ('openai-responses', 'anthropic-messages', 'openai-chat')),
    context_tokens INTEGER
);
-- Neutral items, appended per completed request and removed with their session.
CREATE TABLE IF NOT EXISTS api_session_items (
    session_id  TEXT NOT NULL REFERENCES api_sessions(session_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    turn        INTEGER NOT NULL,
    model       TEXT NOT NULL,
    item        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, seq)
);

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
                    'session_switch', 'handoff', 'progress', 'profile_choice', 'concurrency_change',
                    'role_defined', 'gate_opened', 'gate_updated', 'gate_cancelled', 'gate_closed',
                    'wait_opened', 'wait_extended', 'wait_closed')),
    summary     TEXT NOT NULL DEFAULT '',
    learnings   TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    via TEXT NOT NULL DEFAULT '',
    delivered_at TIMESTAMPTZ
);
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
             ELSE true END),
    links TEXT[] NOT NULL DEFAULT '{}',
    merge_files TEXT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_step_workspaces_task ON step_workspaces (task_id, id);
-- A step has one workspace in use at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_step_workspaces_open ON step_workspaces (step_id)
    WHERE state IN ('active', 'merging');
-- The git worktree a task runs in, in a worktree workspace: made when the task first starts, removed when it
-- completes or is deleted (its branch stays in the user's repository), kept while it is stopped or failed.
-- Code owns the states (looper/agents/task_workspaces.py).
CREATE TABLE IF NOT EXISTS task_worktrees (
    task_id        TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    repo_dir       TEXT NOT NULL,
    subdir         TEXT NOT NULL DEFAULT '',
    path           TEXT NOT NULL,
    branch         TEXT NOT NULL,
    base_commit    TEXT NOT NULL,
    links          TEXT[] NOT NULL DEFAULT '{}',
    state          TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'removed')),
    result_commit  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at     TIMESTAMPTZ,
    CONSTRAINT task_worktrees_removed_check CHECK ((state = 'removed') = (removed_at IS NOT NULL))
);

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
    task_id          TEXT,
    manager_id TEXT NOT NULL DEFAULT 'manager',
    client_id TEXT
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
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    default_prompt TEXT NOT NULL DEFAULT ''
);

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
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    origin TEXT NOT NULL DEFAULT 'machine'
    CHECK (origin IN ('machine', 'upload', 'git', 'ui', 'project')),
    owner_project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL DEFAULT '',
    modified BOOLEAN NOT NULL DEFAULT false,
    CONSTRAINT library_items_owner_check CHECK ((origin = 'project') = (owner_project_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS library_files (
    item_id     TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    content     TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    executable  BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (item_id, path)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name ON projects (lower(name));
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
-- A name is unique among the items outside projects, and within the project that owns an item.
CREATE UNIQUE INDEX IF NOT EXISTS idx_library_items_name ON library_items (kind, name)
    WHERE owner_project_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_library_items_project_name ON library_items (owner_project_id, kind, name)
    WHERE owner_project_id IS NOT NULL;

-- Where a catalog item is installed: everywhere ('global') or in one project,
-- for some roles. Every agent run loads the enabled global installs and its
-- project's that name its role. 'task_role' is the roles a task's Planner
-- defines: such an install loads only for a run of a role that names its item
-- (task_roles.items). A project install never names the Manager, which spans
-- every project. One install per item and place.
CREATE TABLE IF NOT EXISTS library_installs (
    id          TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
    scope       TEXT NOT NULL CHECK (scope IN ('global', 'project')),
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
    roles       TEXT[] NOT NULL CHECK (cardinality(roles) > 0
                                       AND roles <@ ARRAY['manager', 'planner', 'worker', 'reviewer', 'task_role']),
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
                        OR (max_concurrency BETWEEN 1 AND 64 AND scope = 'role' AND role IN ('worker', 'reviewer'))),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((role IS NOT NULL) = (scope IN ('role', 'role_project'))),
    CHECK ((project_id IS NOT NULL) = (scope IN ('project', 'role_project'))),
    CHECK (scope <> 'role_project' OR role <> 'manager'),
    boundaries TEXT
    CONSTRAINT agent_setting_layers_boundaries_check CHECK (boundaries IS NULL OR scope = 'system'),
    system_prompt_mode TEXT
    CONSTRAINT agent_setting_layers_system_prompt_mode_check CHECK (system_prompt_mode IS NULL
        OR (system_prompt_mode IN ('append', 'replace') AND scope = 'role'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_setting_layers_place
    ON agent_setting_layers (scope, COALESCE(role, ''), COALESCE(project_id, ''));
-- A task's own concurrency limit: at most this many of its Workers or Reviewers run at once
-- (agent_settings.resolve_task_concurrency). One Planner runs a task, so the Planner has none. With the
-- task's profile overrides it is the task layer (agent_setting_layers.list_layers).
CREATE TABLE IF NOT EXISTS task_concurrency_limits (
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('worker', 'reviewer')),
    max_concurrency  INTEGER NOT NULL CHECK (max_concurrency BETWEEN 1 AND 64),
    PRIMARY KEY (task_id, role)
);
-- A task's own system prompt mode per role (append or replace): the first choice of
-- agent_settings.system_prompt_mode_choice, over the role layer's. A table of its own, because a task's profile
-- override always names a profile and a task may change the mode without choosing one.
CREATE TABLE IF NOT EXISTS task_system_prompt_modes (
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    role                TEXT NOT NULL CHECK (role IN ('planner', 'worker', 'reviewer')),
    system_prompt_mode  TEXT NOT NULL CHECK (system_prompt_mode IN ('append', 'replace')),
    PRIMARY KEY (task_id, role)
);
-- The Agent Profiles a project's Planners may choose for their tasks' Workers and Reviewers
-- (ops/planner.py, looper_task_profile_set); none by default, which lets a Planner choose any profile (a list
-- restricts the choice to it). Whether a profile can run the role is checked when it is chosen. A profile on a list
-- cannot be deleted; setting a list that leaves one out drops what the project's Planners chose of it.
CREATE TABLE IF NOT EXISTS project_allowed_profiles (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    profile_id  TEXT NOT NULL REFERENCES agent_profiles(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, profile_id)
);
-- A task's Planner's choice of Agent Profile for its Workers or Reviewers: for the rest of the task (step_id NULL)
-- or for one step, each of its attempts. Over every layer and under the task's own choice
-- (agent_settings.Layer.PLANNER_TASK, PLANNER_STEP), while the task's project allows the profile; read when a run
-- starts. Planning the task anew drops them.
CREATE TABLE IF NOT EXISTS task_planner_profiles (
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('worker', 'reviewer')),
    step_id     TEXT REFERENCES task_steps(id) ON DELETE CASCADE,
    profile_id  TEXT NOT NULL REFERENCES agent_profiles(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_planner_profiles_place
    ON task_planner_profiles (task_id, role, (COALESCE(step_id, '')));
-- The roles a task's Planner defines for its steps (looper/agents/task_roles.py; ops/planner.py,
-- looper_task_role_set): one row per version of a role, the newest its current one. kind 'work' runs a step in
-- the built-in Worker's place, 'review' reviews a step after the built-in Reviewer passed it. profile_id NULL:
-- the profile the step's built-in role of that kind would get; one its project no longer allows counts as none.
-- harness NULL: the working-style part of the harness that applies. Planning the task anew drops them.
CREATE TABLE IF NOT EXISTS task_roles (
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    role_key     TEXT NOT NULL,
    version      INTEGER NOT NULL CHECK (version >= 1),
    kind         TEXT NOT NULL CHECK (kind IN ('work', 'review')),
    profile_id   TEXT REFERENCES agent_profiles(id) ON DELETE SET NULL,
    role_prompt  TEXT NOT NULL CHECK (role_prompt <> ''),
    harness      TEXT,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, role_key, version),
    via TEXT NOT NULL DEFAULT 'planner'
    CONSTRAINT task_roles_via_check CHECK (via IN ('planner', 'user', 'template')),
    template_key TEXT,
    items TEXT[] NOT NULL DEFAULT '{}'
);
-- A project's role templates: roles its tasks' Planners copy as task roles (looper_task_role_set's template), the
-- copy a version of the task role of its own. Checked like a task role (looper/agents/role_api.py); its
-- profile counts only while the project allows it, and its items (as a task role's) only while installed so.
CREATE TABLE IF NOT EXISTS project_role_templates (
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    template_key  TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('work', 'review')),
    description   TEXT NOT NULL DEFAULT '',
    role_prompt   TEXT NOT NULL CHECK (role_prompt <> ''),
    harness       TEXT,
    profile_id    TEXT REFERENCES agent_profiles(id) ON DELETE SET NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, template_key),
    items TEXT[] NOT NULL DEFAULT '{}'
);
-- A deleted Manager's name is free again; its id is not.
CREATE UNIQUE INDEX IF NOT EXISTS managers_name_key ON managers (lower(btrim(name))) WHERE deleted_at IS NULL;

-- A Manager's task templates: the task configuration code applies when the Manager creates a task with one
-- (looper/agents/task_templates.py checks every field; inputs is a typed list, see TemplateInput).
CREATE TABLE IF NOT EXISTS manager_task_templates (
    manager_id           TEXT NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
    key                  TEXT NOT NULL CONSTRAINT manager_task_templates_key_check
                         CHECK (key ~ '^[a-z][a-z0-9_-]{0,31}$'),
    name                 TEXT NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 60),
    description          TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 300),
    project_id           TEXT REFERENCES projects(id) ON DELETE RESTRICT,
    work_dir             TEXT NOT NULL DEFAULT '',
    workspace_mode       TEXT CHECK (workspace_mode IN ('worktree', 'in_place')),
    requirement          TEXT NOT NULL DEFAULT '',
    planner_instructions TEXT NOT NULL DEFAULT '',
    planner_profile_id   TEXT REFERENCES agent_profiles(id) ON DELETE RESTRICT,
    worker_profile_id    TEXT REFERENCES agent_profiles(id) ON DELETE RESTRICT,
    reviewer_profile_id  TEXT REFERENCES agent_profiles(id) ON DELETE RESTRICT,
    role_templates       TEXT[] NOT NULL DEFAULT '{}',
    inputs               JSONB NOT NULL DEFAULT '[]',
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_id, key)
);

-- Additive capabilities owned by one Manager; global/project installs keep their existing scopes.
CREATE TABLE IF NOT EXISTS manager_library_installs (
    manager_id TEXT NOT NULL REFERENCES managers(id) ON DELETE RESTRICT,
    item_id TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_manager ON tasks (manager_id, created_at DESC);

-- The Manager a chat thread belongs to, for good; a thread without a row (from before, Lark's and Telegram's) is
-- the default Manager's. A Manager is only ever soft-deleted, so the owner of a thread never changes or vanishes.
CREATE TABLE IF NOT EXISTS chat_threads (
    session_id  TEXT PRIMARY KEY,
    manager_id  TEXT NOT NULL REFERENCES managers(id) ON DELETE RESTRICT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    client_id TEXT
);
-- A wait step's wait (looper/agents/waits.py). The Planner writes the spec, state, times and resolution;
-- the loop writes its checks, baseline and notice, and moves 'waiting' to 'ready' or 'timed_out'; the user's
-- actions move 'waiting' to 'ready' or 'cancel_requested'. Every transition checks the state it read.
CREATE TABLE IF NOT EXISTS task_waits (
    step_id          TEXT PRIMARY KEY REFERENCES task_steps(id) ON DELETE CASCADE,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    topic            TEXT NOT NULL CHECK (topic <> ''),
    spec             JSONB NOT NULL,   -- max_wait_seconds, not_before_seconds, not_before, check, every_seconds
    state            TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'waiting', 'ready', 'timed_out',
                     'cancel_requested', 'done', 'failed', 'skipped')),
    outcome          TEXT CHECK (outcome IN ('ready', 'resumed', 'proceeded', 'cancelled')),
    opened_at        TIMESTAMPTZ,
    due_at           TIMESTAMPTZ,
    deadline_at      TIMESTAMPTZ,
    next_check_at    TIMESTAMPTZ,
    baseline         JSONB,            -- github_pr: {"head", "comments"}
    checks           INTEGER NOT NULL DEFAULT 0,
    last_checked_at  TIMESTAMPTZ,
    last_result      TEXT CHECK (last_result IN ('ready', 'not_ready', 'error')),
    last_output      TEXT NOT NULL DEFAULT '',   -- at most 2,048 bytes
    last_error       TEXT NOT NULL DEFAULT '',
    resolution       TEXT NOT NULL DEFAULT '',
    notice           TEXT CHECK (notice IN ('sent', 'dashboard', 'failed')),
    extensions       INTEGER NOT NULL DEFAULT 0,
    failures         INTEGER NOT NULL DEFAULT 0, -- [WAIT] turns in a row that failed
    failed_at        TIMESTAMPTZ,
    closed_at        TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A ready wait already carries the outcome the service or the user gave it; done keeps one too.
    CONSTRAINT task_waits_outcome_done CHECK ((outcome IS NOT NULL) = (state IN ('ready', 'done')))
);
CREATE INDEX IF NOT EXISTS idx_task_waits_task ON task_waits (task_id);
CREATE INDEX IF NOT EXISTS idx_task_waits_due ON task_waits (next_check_at) WHERE state = 'waiting';
-- A gate step's gate. The Planner writes all but first_reply_at, which the user's first message sets with the
-- state's move from 'open' to 'discussing', and the reminders' columns; a closed gate ('passed', 'cancelled',
-- 'skipped') stays closed. agenda: [{id, question, options, status, answer}]. summary: what a gate that passed
-- closed with ({decisions, assumptions, open}). delivered_through: the last of the user's messages a turn of the
-- Planner had; the close turns those after it into supplements of the task. failures, failed_through, failed_at:
-- the [GATE] turns in a row that failed, the last of the user's messages the latest of them had, and when.
-- plan_change: the steps the turn that closed it added, dropped and stopped.
CREATE TABLE IF NOT EXISTS task_user_gates (
    step_id            TEXT PRIMARY KEY REFERENCES task_steps(id) ON DELETE CASCADE,
    task_id            TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    topic              TEXT NOT NULL CHECK (topic <> ''),
    agenda             JSONB NOT NULL DEFAULT '[]',
    findings           TEXT NOT NULL DEFAULT '',
    state              TEXT NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'open', 'discussing', 'passed', 'cancelled', 'skipped')),
    summary            JSONB,
    resolution         TEXT NOT NULL DEFAULT '',
    ended_by_user      BOOLEAN NOT NULL DEFAULT false,
    plan_change        JSONB,
    delivered_through  BIGINT NOT NULL DEFAULT 0,
    turns              INTEGER NOT NULL DEFAULT 0,
    failures           INTEGER NOT NULL DEFAULT 0,
    failed_through     BIGINT NOT NULL DEFAULT 0,
    failed_at          TIMESTAMPTZ,
    reminders          INTEGER NOT NULL DEFAULT 0,
    opened_at          TIMESTAMPTZ,
    first_reply_at     TIMESTAMPTZ,
    closed_at          TIMESTAMPTZ,
    last_reminded_at   TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT task_user_gates_summary_passed CHECK ((summary IS NOT NULL) = (state = 'passed'))
);
CREATE INDEX IF NOT EXISTS idx_task_user_gates_task ON task_user_gates (task_id);
CREATE INDEX IF NOT EXISTS idx_task_user_gates_waiting ON task_user_gates (task_id)
    WHERE state IN ('open', 'discussing');
-- A gate's discussion, oldest first: the user's messages ('message', 'end': the user ended the discussion), the
-- Planner's replies, and code's own ('opened', 'updated', 'reminder', 'cancelled', 'closed', 'resumed', 'error',
-- 'late': the user's message reached the gate as it closed and became a supplement of the task).
-- author: who wrote it in a group chat. via: 'dashboard', 'lark', 'telegram' or 'manager:<channel>'.
-- agenda_item and option: the option the user chose with a button. channel, chat and external_id: the channel
-- message a user's message is (a reply to it answers there).
CREATE TABLE IF NOT EXISTS task_user_gate_messages (
    id           BIGSERIAL PRIMARY KEY,
    step_id      TEXT NOT NULL REFERENCES task_user_gates(step_id) ON DELETE CASCADE,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('user', 'planner', 'looper')),
    kind         TEXT NOT NULL CHECK (kind IN ('message', 'end', 'reply', 'opened', 'updated', 'reminder',
                                               'cancelled', 'closed', 'resumed', 'error', 'late')),
    text         TEXT NOT NULL DEFAULT '',
    author       TEXT,
    via          TEXT NOT NULL DEFAULT '',
    agenda_item  TEXT,
    option       TEXT,
    channel      TEXT,
    chat         TEXT,
    external_id  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    post_state TEXT
    CHECK (post_state IN ('sent', 'failed', 'none')),
    post_error TEXT,
    post_attempts INT NOT NULL DEFAULT 0,
    post_retry_at TIMESTAMPTZ,
    channel_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_user_gate_messages_step ON task_user_gate_messages (step_id, id);
CREATE INDEX IF NOT EXISTS idx_task_user_gate_messages_unposted ON task_user_gate_messages (id)
    WHERE post_state IS NULL;
-- A user's message that came from a channel, found by the channel message it is (a reply to it is about its gate).
CREATE INDEX IF NOT EXISTS idx_task_user_gate_messages_external ON task_user_gate_messages (channel, external_id)
    WHERE external_id IS NOT NULL;
-- The channel messages Looper sent for a gate's discussion (a message split in several has several): a reply to
-- any of them is about that gate. chat is the target sent to (a Lark user's open_id or group, a Telegram chat).
CREATE TABLE IF NOT EXISTS task_user_gate_posts (
    id           BIGSERIAL PRIMARY KEY,
    message_id   BIGINT NOT NULL REFERENCES task_user_gate_messages(id) ON DELETE CASCADE,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    channel      TEXT CHECK (channel IN ('lark', 'lark_bot', 'telegram')),
    chat         TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    channel_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_user_gate_posts_message ON task_user_gate_posts (message_id);
CREATE INDEX IF NOT EXISTS idx_task_user_gate_posts_external ON task_user_gate_posts (channel, external_id);

-- A task's agent room (looper/agents/room.py): the messages its Planner and its steps' seats send each other, only
-- ever added. A seat is '<role>@<step key>' (worker@s2, backend_rd@s2, reviewer@s2); the sender comes from the
-- token of the run that posted it, never from what the run wrote. audience: the roles, seats or 'planner' it is for,
-- empty when it is for everyone (audience_all). cc_planner: a Reviewer's question or answer, which the Planner sees
-- too. reason: why Looper wrote a notice or an escalation. refs: [{file, work_dir} | {commit, resolved}].
-- planner_pending: it wakes the Planner, which has not had it in a [ROOM] turn yet.
CREATE TABLE IF NOT EXISTS room_messages (
    id               BIGSERIAL PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    sender_seat      TEXT NOT NULL CHECK (sender_seat <> ''),
    sender_kind      TEXT NOT NULL CHECK (sender_kind IN ('planner', 'work', 'review', 'looper')),
    sender_step      TEXT REFERENCES task_steps(id) ON DELETE SET NULL,
    sender_run       TEXT,
    via              TEXT NOT NULL DEFAULT 'agent' CHECK (via IN ('agent', 'consult', 'planner_reply', 'looper')),
    audience_all     BOOLEAN NOT NULL DEFAULT false,
    audience         TEXT[] NOT NULL DEFAULT '{}',
    cc_planner       BOOLEAN NOT NULL DEFAULT false,
    kind             TEXT NOT NULL CHECK (kind IN ('info', 'question', 'answer', 'interface', 'announce', 'notice',
                                                   'escalation')),
    reason           TEXT,
    reply_to         BIGINT REFERENCES room_messages(id) ON DELETE SET NULL,
    refs             JSONB NOT NULL DEFAULT '[]',
    body             TEXT NOT NULL CHECK (body <> ''),
    planner_pending  BOOLEAN NOT NULL DEFAULT false,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT room_messages_answer_replies CHECK (kind <> 'answer' OR reply_to IS NOT NULL),
    CONSTRAINT room_messages_audience CHECK (audience_all OR cardinality(audience) > 0),
    answers_ids BIGINT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_room_messages_task ON room_messages (task_id, id);
CREATE INDEX IF NOT EXISTS idx_room_messages_planner ON room_messages (task_id, id) WHERE planner_pending;
-- How far each seat of a task has read its room (the ids up to last_read_id); a retry of a step reads on.
CREATE TABLE IF NOT EXISTS room_reads (
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seat          TEXT NOT NULL,
    last_read_id  BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, seat)
);
-- What a run waits for in its room: a seat or a role coming online or going offline (presence, state), or a kind
-- of message (kind, from_seat). It fires a notice to the seat; 'first' ends it then, 'run_end' when the run ends.
CREATE TABLE IF NOT EXISTS room_watches (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seat        TEXT NOT NULL,
    run         TEXT NOT NULL,
    presence    TEXT,
    state       TEXT CHECK (state IN ('online', 'offline')),
    kind        TEXT CHECK (kind IN ('interface', 'announce')),
    from_seat   TEXT,
    until       TEXT NOT NULL DEFAULT 'first' CHECK (until IN ('first', 'run_end')),
    fired_at    TIMESTAMPTZ,
    ended_at    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT room_watches_what CHECK ((presence IS NULL) <> (kind IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_room_watches_open ON room_watches (task_id) WHERE ended_at IS NULL;
-- A consult: the questions for a seat no run is in that its last run's session answers, read only, in a fork of
-- that session (looper/agents/consult.py). source_run: the run whose session it resumes; source_record: its pinned
-- finished llm_records row; consult_run: its own run.
-- state: 'queued' (waiting to start, or for room in its task), 'running', then 'answered', or 'fallback' (the
-- Planner was given the questions, fallback_reason says why) or 'failed' (it ran and gave no usable answer; the
-- Planner has them too). read_only: how the run was kept from writing ('runtime': its runtime refuses writes;
-- 'snapshot': it ran in a checkout thrown away after it). denied_writes: the writes its runtime refused.
CREATE TABLE IF NOT EXISTS room_consults (
    id               BIGSERIAL PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seat             TEXT NOT NULL,
    questions        BIGINT[] NOT NULL DEFAULT '{}',
    source_run       TEXT,
    source_record    BIGINT,
    consult_run      TEXT,
    runtime          TEXT,
    state            TEXT NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'running', 'answered', 'fallback', 'failed')),
    fallback_reason  TEXT,
    read_only        TEXT CHECK (read_only IN ('runtime', 'snapshot')),
    denied_writes    INTEGER NOT NULL DEFAULT 0,
    cost_usd         DOUBLE PRECISION,
    detail           TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_room_consults_task ON room_consults (task_id, id);

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
    overlap              TEXT NOT NULL DEFAULT 'skip' CHECK (overlap IN ('skip', 'queue', 'parallel')),
    catch_up             TEXT NOT NULL DEFAULT 'once' CHECK (catch_up IN ('once', 'skip')),
    run_timeout_seconds  INTEGER CHECK (run_timeout_seconds > 0),
    run_budget_usd       DOUBLE PRECISION CHECK (run_budget_usd > 0),
    enabled              BOOLEAN NOT NULL DEFAULT false,
    next_fire_at         TIMESTAMPTZ,
    last_fired_at        TIMESTAMPTZ,
    last_success_at      TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_via          TEXT NOT NULL DEFAULT 'dashboard' CHECK (created_via IN ('dashboard', 'manager', 'import')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (enabled OR next_fire_at IS NULL),
    manager_id TEXT NOT NULL DEFAULT 'manager'
    REFERENCES managers(id) ON DELETE RESTRICT,
    template_key TEXT NULL,
    template_inputs JSONB NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(template_inputs) = 'object'),
    gate_command TEXT,
    gate_timeout_seconds INTEGER CHECK (gate_timeout_seconds > 0),
    daily_budget_usd DOUBLE PRECISION CHECK (daily_budget_usd > 0),
    notify TEXT NOT NULL DEFAULT 'failures'
    CHECK (notify IN ('failures', 'alerts', 'always', 'never')),
    notify_channel TEXT CHECK (notify_channel IN ('telegram', 'lark', 'lark_bot')),
    notify_chat TEXT,
    user_gates TEXT NOT NULL DEFAULT 'off' CHECK (user_gates IN ('off', 'on')),
    last_ok_at TIMESTAMPTZ,
    failures_alert_at TIMESTAMPTZ,
    stale_alert_at TIMESTAMPTZ,
    rule_changed_at TIMESTAMPTZ,
    max_parallel_runs INTEGER NOT NULL DEFAULT 3
    CHECK (max_parallel_runs BETWEEN 1 AND 10),
    enabled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_timeout_mode TEXT NOT NULL DEFAULT 'inherit'
        CHECK (run_timeout_mode IN ('inherit', 'seconds', 'none')),
    planner_session_id TEXT,
    planner_session_totals JSONB NOT NULL DEFAULT '{}',
    planner_session_runtime TEXT,
    planner_session_provider TEXT,
    planner_session_profile TEXT,
    planner_session_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    planner_session_since TIMESTAMPTZ,
    CONSTRAINT schedules_template_fkey FOREIGN KEY (manager_id, template_key)
        REFERENCES manager_task_templates(manager_id, key) ON DELETE RESTRICT,
    CONSTRAINT schedules_notify_target_check CHECK ((notify_channel IS NULL) = (notify_chat IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_schedules_manager ON schedules (manager_id);
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
-- How many of their Workers or Reviewers a schedule's tasks run at once, copied into each task's
-- task_concurrency_limits when it fires (its task_limits).
CREATE TABLE IF NOT EXISTS schedule_task_limits (
    schedule_id      TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('worker', 'reviewer')),
    max_concurrency  INTEGER NOT NULL CHECK (max_concurrency BETWEEN 1 AND 64),
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
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    gate_output TEXT,
    execution_snapshot JSONB,
    carried_from TEXT REFERENCES schedule_runs(id) ON DELETE SET NULL,
    firings INTEGER NOT NULL DEFAULT 1 CHECK (firings >= 1),
    run_timeout_seconds INTEGER CHECK (run_timeout_seconds > 0),
    carried_on BOOLEAN NOT NULL DEFAULT false
);
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
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'completed',
    cost_usd DOUBLE PRECISION,
    finished_at TIMESTAMPTZ,
    last_output_at TIMESTAMPTZ,
    cache_read_tokens BIGINT,
    cache_creation_tokens BIGINT,
    context_tokens INTEGER,
    compactions JSONB,
    cost_source TEXT CHECK (cost_source IN ('runtime', 'provider_price'))
);

CREATE INDEX IF NOT EXISTS idx_llm_records_agent ON llm_records(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_records_task ON llm_records(task_id, created_at DESC);

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
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    channel TEXT NOT NULL DEFAULT 'web',
    delivery_failure TEXT
    CHECK (delivery_failure IN ('no_chat', 'send_failed')),
    delivery_error TEXT
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
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    manager_id TEXT NOT NULL DEFAULT 'manager'
    REFERENCES managers(id) ON DELETE RESTRICT,
    bound_chats_only BOOLEAN NOT NULL DEFAULT false
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
-- name them and let one in: one row per channel and sender, with how often they wrote, when last and where
-- (chat_id: the group chat that message @-mentioned the bot in; NULL for a direct message); never what they
-- wrote. The channel keeps its 20 newest.
CREATE TABLE IF NOT EXISTS channel_rejected_senders (
    channel_id  TEXT NOT NULL,
    sender_id   TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    last_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, sender_id),
    chat_id TEXT
);
-- <n> of an added bot: only ever grows, never reused (a bot removed and added again gets a new id).
CREATE SEQUENCE IF NOT EXISTS channel_bot_seq START 2;
CREATE OR REPLACE FUNCTION channel_configs_manager_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'a bot''s Manager cannot change: add a new bot for the other Manager and remove this one'
        USING ERRCODE = 'check_violation';
END $$;
CREATE TRIGGER channel_configs_manager_fixed BEFORE UPDATE OF manager_id ON channel_configs
    FOR EACH ROW WHEN (OLD.manager_id IS DISTINCT FROM NEW.manager_id) EXECUTE FUNCTION channel_configs_manager_fixed();
CREATE INDEX IF NOT EXISTS idx_channel_configs_manager ON channel_configs (manager_id);
CREATE UNIQUE INDEX IF NOT EXISTS task_user_gate_posts_bot_key ON task_user_gate_posts (channel_id, chat, external_id);
CREATE INDEX IF NOT EXISTS idx_task_user_gate_posts_bot ON task_user_gate_posts (channel_id, external_id);
CREATE INDEX IF NOT EXISTS idx_task_user_gate_messages_bot ON task_user_gate_messages (channel_id, external_id)
    WHERE external_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS manager_chat_bindings (
    channel_id TEXT NOT NULL REFERENCES channel_configs(channel_id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL CHECK (btrim(chat_id) <> ''),
    manager_id TEXT NOT NULL REFERENCES managers(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_manager_chat_bindings_manager
    ON manager_chat_bindings (manager_id, channel_id, chat_id);
CREATE TABLE IF NOT EXISTS channel_seen_chats (
    channel_id TEXT NOT NULL REFERENCES channel_configs(channel_id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL CHECK (btrim(chat_id) <> ''),
    name TEXT CHECK (char_length(name) <= 200),
    count BIGINT NOT NULL DEFAULT 1 CHECK (count > 0),
    last_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, chat_id)
);
CREATE TABLE IF NOT EXISTS channel_unbound_chats (
    channel_id TEXT NOT NULL REFERENCES channel_configs(channel_id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL CHECK (btrim(chat_id) <> ''),
    name TEXT CHECK (char_length(name) <= 200),
    count BIGINT NOT NULL DEFAULT 1 CHECK (count > 0),
    last_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_channel_unbound_chats_latest
    ON channel_unbound_chats (channel_id, last_at DESC, chat_id ASC);

-- Local agents and scripts that call /api/v1 with a token of their own (Settings → API clients). Only the token's
-- SHA-256 is stored; a revoked client keeps its row so records still name it.
CREATE TABLE IF NOT EXISTS api_clients (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    token_sha256  TEXT NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_clients_active_name ON api_clients (lower(name)) WHERE revoked_at IS NULL;

ALTER TABLE tasks ADD CONSTRAINT tasks_schedule_id_fkey
    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE SET NULL;
