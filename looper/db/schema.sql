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
    launcher      TEXT NOT NULL DEFAULT 'native' CHECK (launcher IN ('native', 'ttadk')),
    executable    TEXT NOT NULL DEFAULT '',
    version       TEXT,
    capabilities  TEXT[] NOT NULL DEFAULT '{}'
        CHECK (capabilities <@ ARRAY['run', 'resident_session', 'resume', 'structured_output', 'claude_plugins', 'mcp',
                                     'add_dirs', 'compaction_events', 'autonomous_turns', 'cost_report',
                                     'read_tools', 'steer', 'no_tools', 'headless_compaction', 'mid_turn_input']),
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
    CONSTRAINT llm_providers_vendor_type_check CHECK (vendor_type IN ('glm-bigmodel', 'glm-zai', 'deepseek', 'kimi-moonshot-cn', 'kimi-moonshot',
                          'cpa', 'opencode', 'sub2api', 'claude-subscription', 'codex-login', 'grok-login', 'cursor-login', 'coco-login', 'acp-command', 'custom'))
    CONSTRAINT llm_providers_check1 CHECK (
        (vendor_type = 'claude-subscription' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'claude-code')
        OR (vendor_type = 'codex-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'codex')
        OR (vendor_type = 'grok-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'grok')
        OR (vendor_type = 'cursor-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'cursor-agent')
        OR (vendor_type = 'coco-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'coco')
        OR (vendor_type = 'acp-command' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'acp')
        OR (vendor_type NOT IN ('claude-subscription', 'codex-login', 'grok-login', 'cursor-login', 'coco-login', 'acp-command')
            AND auth = 'api_key' AND oauth_runtime_type IS NULL)
    ),
    -- false: its Agent Profiles do not run (refused at launch); nothing else changes.
    enabled BOOLEAN NOT NULL DEFAULT true
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
    -- ensure_schema installs the shared model_names grammar on both names.
    name                      TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    context_window            INTEGER CHECK (context_window BETWEEN 1 AND 100000000),
    input_usd_per_mtok        DOUBLE PRECISION CHECK (input_usd_per_mtok BETWEEN 0 AND 10000),
    output_usd_per_mtok       DOUBLE PRECISION CHECK (output_usd_per_mtok BETWEEN 0 AND 10000),
    cache_read_usd_per_mtok   DOUBLE PRECISION CHECK (cache_read_usd_per_mtok BETWEEN 0 AND 10000),
    cache_write_usd_per_mtok  DOUBLE PRECISION CHECK (cache_write_usd_per_mtok BETWEEN 0 AND 10000),
    efforts                   TEXT[] CHECK (llm_model_efforts_valid(efforts)),
    background_model          TEXT CHECK (length(background_model) BETWEEN 1 AND 200),
    chat_dialect              TEXT CHECK (chat_dialect IS NULL OR protocol = 'openai-chat'),
    catalog_order             INTEGER NOT NULL DEFAULT 0 CHECK (catalog_order >= 0),
    builtin                   BOOLEAN NOT NULL DEFAULT false,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (provider_id, protocol) REFERENCES llm_provider_endpoints(provider_id, protocol) ON DELETE RESTRICT,
    UNIQUE (provider_id, name, protocol)
);
CREATE TABLE IF NOT EXISTS gateway_aliases (
    alias TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES llm_models(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
    web_search BOOLEAN NOT NULL DEFAULT true,
    custom_prompt TEXT NOT NULL DEFAULT '',
    fallback_profile_id TEXT REFERENCES agent_profiles(id) ON DELETE RESTRICT,
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

-- Operator-owned presets; tasks hold independent definition snapshots.
CREATE TABLE IF NOT EXISTS workflows (
    key TEXT PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_-]{0,31}$' AND key <> 'none'),
    name TEXT NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 60),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 300),
    definition JSONB NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS workflows_name_key ON workflows (lower(btrim(name)));

-- Every task belongs to one project; a project may give its tasks a default
-- directory (work_dir, '' = none).
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    default_workflow TEXT REFERENCES workflows(key) ON DELETE SET NULL,
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
    verification_paths TEXT[] CHECK (cardinality(verification_paths) <= 20),
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
    dependency_conditions TEXT CHECK (dependency_conditions IN ('off', 'on')),
    -- The advisor (the advisor RFC, section 5.4): off unless on; each consult's budget, the task's consults and
    -- their total, and a step's consults (no limit when NULL); a fallback profile (unused for now); the project's
    -- internal domains.
    advisor TEXT CHECK (advisor IN ('off', 'on')),
    advisor_consult_budget_usd DOUBLE PRECISION
    CHECK (advisor_consult_budget_usd >= 0.01 AND advisor_consult_budget_usd <= 5),
    advisor_budget_usd DOUBLE PRECISION CHECK (advisor_budget_usd >= 0.01 AND advisor_budget_usd <= 50),
    advisor_consults_per_task INTEGER CHECK (advisor_consults_per_task >= 0 AND advisor_consults_per_task <= 100),
    advisor_consults_per_step INTEGER CHECK (advisor_consults_per_step >= 0 AND advisor_consults_per_step <= 10),
    advisor_fallback_profile TEXT CHECK (length(advisor_fallback_profile) BETWEEN 1 AND 200),
    advisor_internal_domains TEXT[] CHECK (cardinality(advisor_internal_domains) <= 20),
    distill_lessons TEXT NOT NULL DEFAULT 'off' CONSTRAINT projects_distill_lessons_check
        CHECK (distill_lessons IN ('off', 'draft', 'write'))
);

-- Project operations have their own history; task activities keep their task foreign key.
CREATE TABLE IF NOT EXISTS project_activity (
    seq BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_project_activity_project ON project_activity (project_id, seq);

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
    -- 'all' every task, 'own' only tasks it created, 'projects' tasks in scope_project_ids (Tasks tools only).
    task_scope    TEXT NOT NULL DEFAULT 'own' CONSTRAINT managers_task_scope_check CHECK (task_scope IN ('all', 'own', 'projects')),
    task_policy   TEXT NOT NULL DEFAULT 'free'
                  CONSTRAINT managers_task_policy_check CHECK (task_policy IN ('free', 'templates_only')),
    enabled       BOOLEAN NOT NULL DEFAULT true,
    idle_timeout_seconds INTEGER NOT NULL DEFAULT 1800
                  CONSTRAINT managers_idle_timeout_seconds_check
                  CHECK (idle_timeout_seconds = 0 OR idle_timeout_seconds BETWEEN 60 AND 86400),
    -- Bumped in the same transaction by every write that changes what the Manager may do (tools, task_scope,
    -- scope_project_ids (including project deletion), task_policy, enabled, a template saved or removed):
    -- ManagerOps compares it at each call.
    revision      BIGINT NOT NULL DEFAULT 0,
    -- Deleted on the dashboard: the row stays so its chat threads keep their owner (they never fall to the default
    -- Manager) and its id is never reused; a deleted row is disabled and hidden from every view.
    deleted_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    scope_project_ids TEXT[] NOT NULL DEFAULT '{}',
    CONSTRAINT managers_scope_projects_check CHECK (cardinality(scope_project_ids) <= 20
        AND (task_scope = 'projects' OR cardinality(scope_project_ids) = 0)),
    CONSTRAINT managers_scope_tools_check CHECK (task_scope <> 'projects' OR tools = 'tasks'),
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
    process_record    JSONB CHECK (jsonb_typeof(process_record) = 'object'),
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
    workflow JSONB,
    origin_session TEXT CONSTRAINT tasks_origin_session_check CHECK (origin_session IS NULL OR origin_session ~ '^(group|dm):channel-[a-z_]+(-[1-9][0-9]*)?:.+$'),
    origin_thread TEXT,
    origin_reply_route JSONB CHECK (jsonb_typeof(origin_reply_route) = 'object'),
    schedule_id TEXT,
    budget_usd DOUBLE PRECISION CHECK (budget_usd > 0),
    planner_session_label TEXT,
    chat_id TEXT,
    chat_mention TEXT,
    original_user_text TEXT,
    title TEXT,
    client_id TEXT,
    continues_schedule_session BOOLEAN NOT NULL DEFAULT false,
    chat_kind TEXT CHECK (chat_kind IN ('user', 'chat')),
    chat_group BOOLEAN NOT NULL DEFAULT false
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
    ready_when JSONB,
    accept_when JSONB,
    reviews TEXT,
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
    kind TEXT NOT NULL DEFAULT 'work' CHECK (kind IN ('work', 'gate', 'wait', 'review')),
    -- How hard the Planner says the step is; only critical does anything yet (the advisor's checkpoint).
    difficulty TEXT CHECK (difficulty IN ('routine', 'hard', 'critical')),
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
                    'session_switch', 'handoff', 'progress', 'profile_choice', 'profile_fallback', 'concurrency_change',
                    'role_defined', 'gate_opened', 'gate_updated', 'gate_cancelled', 'gate_closed',
                    'wait_opened', 'wait_extended', 'wait_closed', 'condition_blocked',
                    'review_round', 'late_review', 'review_adjudicated',
                    'verification_round', 'escalation')),
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

-- Each role's own settings: its display name, role prompt and timeout (manager, planner, worker, reviewer,
-- processor).
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
-- the first two; the processor (runs with no tools) has all four. A NULL field is not set at its layer (it inherits); env is an
-- ordered list of {"key", "value"}, where a null value unsets a key an earlier
-- layer set (never at the system layer). profile_id is the Agent Profile the
-- layer's roles run (its model and effort with it). Code owns the validation and the resolution
-- (looper/agents/agent_settings.py). One row per layer and place.
CREATE TABLE IF NOT EXISTS agent_setting_layers (
    scope           TEXT NOT NULL CHECK (scope IN ('system', 'role', 'project', 'role_project')),
    role            TEXT CHECK (role IN ('manager', 'planner', 'worker', 'reviewer', 'processor', 'advisor',
                                         'advice_check', 'leads_check', 'outbound_check')),
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
    CONSTRAINT task_roles_via_check CHECK (via IN ('planner', 'user', 'template', 'workflow')),
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
CREATE INDEX IF NOT EXISTS idx_tasks_origin_session ON tasks (origin_session,created_at DESC) WHERE origin_session IS NOT NULL;
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
    purpose            TEXT NOT NULL DEFAULT 'discussion'
                       CHECK (purpose IN ('discussion', 'exhaustion', 'verification_exhaustion')),
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
    notify_format TEXT NOT NULL DEFAULT 'report' CHECK (notify_format IN ('report', 'deliverable')),
    CONSTRAINT schedules_command_notify_format_check CHECK (kind <> 'command' OR notify_format = 'report'),
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
    planner_session_context_tokens BIGINT CHECK (planner_session_context_tokens >= 0),
    planner_session_run_cost_usd DOUBLE PRECISION CHECK (planner_session_run_cost_usd >= 0),
    kind TEXT NOT NULL DEFAULT 'agent' CHECK (kind IN ('agent', 'command', 'subscription')),
    command TEXT,
    env JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(env) = 'object'),
    env_file TEXT,
    success_deadline TEXT CHECK (success_deadline ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    alert_failures_after INTEGER CHECK (alert_failures_after BETWEEN 1 AND 1000),
    alert_stale_after_seconds INTEGER CHECK (alert_stale_after_seconds BETWEEN 300 AND 2592000),
    alert_overlap_after INTEGER CHECK (alert_overlap_after BETWEEN 0 AND 10000),
    consecutive_overlaps INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_overlaps >= 0),
    deadline_alert_at TIMESTAMPTZ,
    overlap_alert_at TIMESTAMPTZ,
    CONSTRAINT schedules_template_fkey FOREIGN KEY (manager_id, template_key)
        REFERENCES manager_task_templates(manager_id, key) ON DELETE RESTRICT,
    CONSTRAINT schedules_notify_target_check CHECK ((notify_channel IS NULL) = (notify_chat IS NULL)),
    CONSTRAINT schedules_kind_fields_check CHECK (
        (kind = 'command') = (command IS NOT NULL)
        AND (kind = 'command' OR (env = '{}'::jsonb AND env_file IS NULL))
        AND (kind = 'agent' OR (gate_command IS NULL AND template_key IS NULL)))
);
CREATE INDEX IF NOT EXISTS idx_schedules_manager ON schedules (manager_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_project_name ON schedules (project_id, lower(name));
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules (next_fire_at) WHERE enabled;
-- The Agent Profile a schedule's tasks run for some of their roles, copied into each task's
-- task_profile_overrides when it fires, or its processor runs with (over the processor's layers); a profile a
-- schedule chose cannot be deleted.
CREATE TABLE IF NOT EXISTS schedule_profile_overrides (
    schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('planner', 'worker', 'reviewer', 'processor')),
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
    missed_since  TIMESTAMPTZ,
    status        TEXT NOT NULL CHECK (status IN (
                      'queued', 'gating', 'running', 'completed', 'failed', 'stopped', 'cancelled', 'timed_out',
                      'gate_failed', 'skipped_overlap', 'skipped_missed', 'skipped_gate', 'skipped_budget',
                      'succeeded', 'interrupted')),
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
    carried_on BOOLEAN NOT NULL DEFAULT false,
    started_at TIMESTAMPTZ,
    deferred_by_drain BOOLEAN NOT NULL DEFAULT false,
    exit_code INTEGER,
    exit_signal INTEGER,
    log_path TEXT,
    log_bytes BIGINT,
    log_truncated BOOLEAN NOT NULL DEFAULT false,
    log_tail TEXT,
    runner_pid INTEGER,
    runner_started TEXT,
    planner_context_tokens BIGINT CHECK (planner_context_tokens >= 0),
    planner_cost_usd DOUBLE PRECISION CHECK (planner_cost_usd >= 0),
    planner_session_action TEXT CHECK (planner_session_action IN ('compacted', 'rotated')),
    planner_session_reason TEXT CHECK (planner_session_reason IN ('context', 'cost')),
    planner_session_before_tokens BIGINT CHECK (planner_session_before_tokens >= 0),
    planner_session_after_tokens BIGINT CHECK (planner_session_after_tokens >= 0),
    failure_kind TEXT CHECK (failure_kind IN ('task_budget'))
);
CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule ON schedule_runs (schedule_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_schedule_runs_open ON schedule_runs (schedule_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS idx_schedule_runs_finished ON schedule_runs (finished_at) WHERE finished_at IS NOT NULL;
-- Every message about a schedule, sent or not: a run's outcome, an alert or its recovery. 'dashboard' when the
-- schedule names no chat (or notify is 'never' for an alert), 'failed' with the reason when sending failed.
CREATE TABLE IF NOT EXISTS schedule_notifications (
    id          TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    run_id      TEXT REFERENCES schedule_runs(id) ON DELETE SET NULL,
    -- 'check_alert' and 'check_recovered' are the leads check block's.
    kind        TEXT NOT NULL CHECK (kind IN ('run', 'failures_alert', 'failures_recovered', 'stale_alert',
                                              'stale_recovered',
                                              'deadline_alert', 'deadline_recovered', 'overlap_alert', 'overlap_recovered')),
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
    cost_source TEXT CHECK (cost_source IN ('runtime', 'provider_price')),
    -- The run that started this one (an advisor's or a judge's run: the Planner's turn that asked), and what kind
    -- of run it is (llm_records.RUN_KINDS; NULL on rows from before it, whose readers take it from their agent id).
    parent_record_id BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    run_kind TEXT CHECK (run_kind IN ('manager', 'planner', 'worker', 'reviewer', 'processor', 'consult', 'advisor',
                                      'advice_check', 'outbound_check')),
    -- Why a failed run failed (failures.FailureClass and FailureCause, each cause with its own class), recorded while
    -- engine.failure_typing is on; NULL for every other run.
    failure_class TEXT,
    failure_cause TEXT,
    CONSTRAINT llm_records_failure_check CHECK ((failure_class IS NULL AND failure_cause IS NULL) OR
        (failure_class IS NOT NULL AND failure_cause IS NOT NULL AND
        (failure_class, failure_cause) IN (('quota', 'usage_limit'), ('quota', 'rate_limit'),
            ('transient', 'connection_failed'), ('transient', 'provider_overloaded'), ('transient', 'start_timeout'),
            ('environment', 'runtime_missing'), ('environment', 'login'), ('environment', 'tls'),
            ('environment', 'profile'), ('judgment', 'timeout'), ('judgment', 'context'), ('judgment', 'budget'),
            ('judgment', 'report'), ('judgment', 'unknown'))))
);

CREATE INDEX IF NOT EXISTS idx_llm_records_agent ON llm_records(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_records_task ON llm_records(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_records_parent ON llm_records(parent_record_id) WHERE parent_record_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_records_failures ON llm_records (created_at) WHERE failure_class IS NOT NULL;

-- provider limit windows begin
CREATE TABLE IF NOT EXISTS provider_limit_windows (
    provider_id TEXT NOT NULL REFERENCES llm_providers(id) ON DELETE CASCADE,
    window_name TEXT NOT NULL CHECK (window_name ~ '^[a-z0-9_]{1,40}$'),
    status TEXT NOT NULL CHECK (status IN ('allowed', 'warning', 'rejected')),
    utilization DOUBLE PRECISION CHECK (utilization BETWEEN 0 AND 1),
    resets_at TIMESTAMPTZ,
    window_minutes INTEGER CHECK (window_minutes > 0),
    source TEXT NOT NULL CHECK (source IN ('claude_stream', 'codex_app_server', 'api_headers', 'proxy')),
    record_id BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (provider_id, window_name)
);
-- provider limit windows end
CREATE INDEX IF NOT EXISTS idx_llm_records_provider_day
    ON llm_records ((metadata->'profile'->'provider'->>'id'), created_at);

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
    allowed_senders TEXT[] NOT NULL DEFAULT '{}' CONSTRAINT manager_chat_bindings_allowed_senders_check CHECK (cardinality(allowed_senders) <= 100),
    sender_policy TEXT NOT NULL DEFAULT 'listed' CHECK (sender_policy IN ('listed', 'chat')),
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

CREATE TABLE IF NOT EXISTS llm_request_routes (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    task_id         TEXT,
    step_id         TEXT,
    role            TEXT,
    runtime_type    TEXT NOT NULL,
    provider_id     TEXT,
    protocol        TEXT,
    model           TEXT,
    upstream_host   TEXT NOT NULL,
    upstream_path   TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL,
    last_request_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_request_routes_agent ON llm_request_routes (agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS llm_request_parts (
    route_id TEXT NOT NULL REFERENCES llm_request_routes(id) ON DELETE CASCADE,
    sha256   TEXT NOT NULL,
    body     JSONB NOT NULL,
    bytes    INTEGER NOT NULL,
    stored_bytes BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (route_id, sha256)
);

CREATE TABLE IF NOT EXISTS llm_requests (
    id                BIGSERIAL PRIMARY KEY,
    route_id          TEXT NOT NULL REFERENCES llm_request_routes(id) ON DELETE CASCADE,
    proxy_instance    TEXT NOT NULL,
    proxy_seq         BIGINT NOT NULL,
    record_id         BIGINT REFERENCES llm_records(id) ON DELETE CASCADE,
    after_record_id   BIGINT,
    agent_id          TEXT NOT NULL,
    task_id           TEXT,
    started_at        TIMESTAMPTZ NOT NULL,
    method            TEXT NOT NULL,
    path              TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('model', 'count_tokens', 'other')),
    model             TEXT,
    stream            BOOLEAN,
    status            INTEGER,
    outcome           TEXT NOT NULL CHECK (outcome IN ('completed', 'upstream_error', 'client_closed', 'proxy_error')),
    error             TEXT,
    ttfb_ms           INTEGER,
    duration_ms       INTEGER,
    request_headers   JSONB NOT NULL,
    response_headers  JSONB,
    request_body      JSONB,
    request_bytes     INTEGER NOT NULL,
    request_sha256    TEXT NOT NULL,
    response_body     JSONB,
    response_raw      TEXT,
    response_bytes    BIGINT,
    input_tokens      BIGINT,
    output_tokens     BIGINT,
    cache_read_tokens BIGINT,
    cache_write_tokens BIGINT,
    usage             JSONB,
    translation       JSONB,
    record_gap        TEXT CHECK (record_gap IN ('request_too_large', 'response_too_large', 'undecodable_encoding', 'body_encoding_unparsed',
                                                 'unparsable_request', 'unparsable_stream', 'rejected_by_database')),
    body_sanitized    BOOLEAN NOT NULL DEFAULT FALSE,
    stored_bytes      BIGINT NOT NULL DEFAULT 0,
    UNIQUE (proxy_instance, proxy_seq)
);
CREATE INDEX IF NOT EXISTS idx_llm_requests_record ON llm_requests (record_id, started_at);
CREATE INDEX IF NOT EXISTS idx_llm_requests_after ON llm_requests (after_record_id) WHERE record_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_llm_requests_route ON llm_requests (route_id, started_at);
CREATE INDEX IF NOT EXISTS idx_llm_requests_agent ON llm_requests (agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_requests_task ON llm_requests (task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_requests_started ON llm_requests (started_at);

-- Message stream (message-stream RFC): the sources Looper reads messages from, their chats and people, the events
-- they yield and the runs that ingest them. Default off: no row, no behavior. connection.py applies this block
-- to databases that predate it, so keep it between the markers and idempotent.
-- message stream begin
CREATE TABLE IF NOT EXISTS event_sources (
    id                      TEXT PRIMARY KEY,
    kind                    TEXT NOT NULL CHECK (kind IN ('lark_user')),
    name                    TEXT NOT NULL CHECK (btrim(name) <> ''),
    lark_profile            TEXT NOT NULL DEFAULT '',
    user_open_id            TEXT,
    ingest_mode             TEXT NOT NULL DEFAULT 'off' CHECK (ingest_mode IN ('off', 'bridge', 'collect')),
    bridge_dir              TEXT NOT NULL DEFAULT '',
    bridge_start_from       TEXT NOT NULL DEFAULT 'end' CHECK (bridge_start_from IN ('end', 'beginning')),
    cron                    TEXT NOT NULL DEFAULT '*/10 * * * *',
    timezone                TEXT NOT NULL DEFAULT 'UTC',
    watermark_at            TIMESTAMPTZ,
    sweep_after             TEXT,
    sweep_cycle_runs        INTEGER NOT NULL DEFAULT 18 CHECK (sweep_cycle_runs BETWEEN 1 AND 288),
    committed_seq           BIGINT NOT NULL DEFAULT 0,
    auth_state              TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (auth_state IN ('unknown', 'ready', 'needs_refresh', 'expired', 'missing')),
    auth_checked_at         TIMESTAMPTZ,
    refresh_expires_at      TIMESTAMPTZ,
    last_run_at             TIMESTAMPTZ,
    last_ok_at              TIMESTAMPTZ,
    last_listing_at         TIMESTAMPTZ,
    retention_last_at       TIMESTAMPTZ,
    body_retention_days     INTEGER CHECK (body_retention_days > 0),
    app_body_retention_days INTEGER DEFAULT 30 CHECK (app_body_retention_days > 0),
    stale_alert_at          TIMESTAMPTZ,
    auth_alert_at           TIMESTAMPTZ,
    partial_alert_at        TIMESTAMPTZ,
    alert_channel           TEXT,
    alert_chat              TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ingest_mode <> 'bridge' OR bridge_dir <> '')
);
CREATE TABLE IF NOT EXISTS source_chats (
    source_id        TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
    chat_id          TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('p2p', 'group', 'topic', 'unknown')),
    name             TEXT NOT NULL DEFAULT '',
    inclusion        TEXT NOT NULL DEFAULT 'included' CHECK (inclusion IN ('included', 'excluded')),
    exclusion_reason TEXT CHECK (exclusion_reason IN ('operator', 'looper_channel')),
    state            TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'left', 'failing')),
    cursor_time      TIMESTAMPTZ,
    cursor_ids       TEXT[] NOT NULL DEFAULT '{}',
    last_pulled_at   TIMESTAMPTZ,
    last_message_at  TIMESTAMPTZ,
    failures         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    last_error_code  INTEGER,
    bridge_offset    BIGINT,
    bridge_size      BIGINT,
    bridge_inode     TEXT,
    bridge_last_id   TEXT,
    bridge_mtime     TIMESTAMPTZ,
    -- The file's first read is not finished: what is still to read is part of that import, not live news.
    bridge_importing BOOLEAN NOT NULL DEFAULT false,
    excluded_at      TIMESTAMPTZ,
    purged_at        TIMESTAMPTZ,
    PRIMARY KEY (source_id, chat_id),
    CHECK ((inclusion = 'excluded') = (exclusion_reason IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS source_threads (
    source_id      TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    chat_id        TEXT NOT NULL,
    cursor_time    TIMESTAMPTZ,
    cursor_ids     TEXT[] NOT NULL DEFAULT '{}',
    last_pulled_at TIMESTAMPTZ,
    failures       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    PRIMARY KEY (source_id, thread_id),
    FOREIGN KEY (source_id, chat_id) REFERENCES source_chats(source_id, chat_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS source_people (
    source_id    TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
    open_id      TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    name_source  TEXT NOT NULL CHECK (name_source IN ('contact', 'mention', 'none')),
    refreshed_at TIMESTAMPTZ,
    PRIMARY KEY (source_id, open_id)
);
CREATE TABLE IF NOT EXISTS source_runs (
    id                BIGSERIAL PRIMARY KEY,
    source_id         TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
    mode              TEXT NOT NULL CHECK (mode IN ('bridge', 'collect', 'listing', 'import')),
    trigger           TEXT NOT NULL CHECK (trigger IN ('scheduled', 'catch_up', 'manual')),
    status            TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'interrupted', 'search_failed',
                                                      'auth_failed', 'failed', 'skipped_busy')),
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    window_from       TIMESTAMPTZ,
    window_to         TIMESTAMPTZ,
    watermark_before  TIMESTAMPTZ,
    watermark_after   TIMESTAMPTZ,
    slices            INTEGER,
    search_hits       INTEGER,
    unindexed         INTEGER,
    chats_pulled      INTEGER,
    chats_failed      INTEGER,
    threads_pulled    INTEGER,
    threads_failed    INTEGER,
    sweep_chats       INTEGER,
    sweep_failed      INTEGER,
    sweep_found       INTEGER,
    events_new        INTEGER NOT NULL DEFAULT 0,
    events_seen       INTEGER NOT NULL DEFAULT 0,
    events_skipped    INTEGER NOT NULL DEFAULT 0,
    skipped_by_deletion BIGINT NOT NULL DEFAULT 0,
    files_rescanned   INTEGER,
    error             TEXT,
    error_code        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs (source_id, id DESC);
CREATE TABLE IF NOT EXISTS source_events (
    seq             BIGSERIAL PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
    external_id     TEXT NOT NULL,
    chat_id         TEXT NOT NULL,
    thread_id       TEXT,
    root_id         TEXT,
    parent_id       TEXT,
    sender_id       TEXT NOT NULL DEFAULT '',
    sender_kind     TEXT NOT NULL CHECK (sender_kind IN ('user', 'app', 'other')),
    sent_at         TIMESTAMPTZ NOT NULL,
    msg_type        TEXT NOT NULL,
    mentions_me     BOOLEAN NOT NULL,
    mention_ids     TEXT[] NOT NULL DEFAULT '{}',
    text            TEXT NOT NULL,
    body            JSONB,
    body_bytes      INTEGER NOT NULL,
    body_dropped_at TIMESTAMPTZ,
    origin          TEXT NOT NULL CHECK (origin IN ('live', 'history', 'import')),
    run_id          BIGINT REFERENCES source_runs(id) ON DELETE SET NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_id, external_id),
    FOREIGN KEY (source_id, chat_id) REFERENCES source_chats(source_id, chat_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_events_seq ON source_events (source_id, seq);
CREATE INDEX IF NOT EXISTS idx_source_events_chat ON source_events (source_id, chat_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_source_events_sent ON source_events (source_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_source_events_run ON source_events (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_source_events_body ON source_events (sent_at) WHERE body IS NOT NULL;
-- source deletions begin
-- Durable predicates contain identifiers and bounds, never message content or chat names.
CREATE TABLE IF NOT EXISTS source_deletions (
    id          BIGSERIAL PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
    scope       TEXT NOT NULL CHECK (scope IN ('message', 'range', 'chat')),
    message_id  TEXT,
    chat_id     TEXT,
    start_at    TIMESTAMPTZ,
    end_at      TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((scope = 'message' AND message_id IS NOT NULL AND chat_id IS NULL AND start_at IS NULL AND end_at IS NULL)
        OR (scope = 'range' AND message_id IS NULL AND start_at IS NOT NULL AND end_at IS NOT NULL AND end_at > start_at)
        OR (scope = 'chat' AND chat_id IS NOT NULL AND message_id IS NULL AND start_at IS NULL AND end_at IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_source_deletions_source ON source_deletions (source_id, scope);
CREATE INDEX IF NOT EXISTS idx_source_deletions_message ON source_deletions (source_id, message_id) WHERE scope = 'message';
CREATE INDEX IF NOT EXISTS idx_source_deletions_chat ON source_deletions (source_id, chat_id) WHERE scope = 'chat';
-- source deletions end
-- message stream end

-- Subscriptions (message-stream RFC, section 7): the schedule kind that turns the events a source ingested after a
-- cursor into one tool-less processor run and, once its target is confirmed, a push. Default off: no row, no
-- behavior. connection.py applies this block (with the widened schedules.kind check) to databases that predate it, so
-- keep it between the markers and idempotent.
-- subscriptions begin
CREATE TABLE IF NOT EXISTS subscriptions (
    schedule_id         TEXT PRIMARY KEY REFERENCES schedules(id) ON DELETE CASCADE,
    source_id           TEXT NOT NULL REFERENCES event_sources(id),
    chat_kinds          TEXT[] NOT NULL CHECK (cardinality(chat_kinds) > 0 AND chat_kinds <@ ARRAY['p2p', 'group', 'topic', 'unknown']),
    include_chats       TEXT[] NOT NULL DEFAULT '{}',
    exclude_chats       TEXT[] NOT NULL DEFAULT '{}',
    sender_kinds        TEXT[] NOT NULL CHECK (cardinality(sender_kinds) > 0 AND sender_kinds <@ ARRAY['user', 'app']),
    exclude_mentions_me BOOLEAN NOT NULL DEFAULT false,
    include_history     BOOLEAN NOT NULL DEFAULT false,
    max_age_minutes     INTEGER NOT NULL CHECK (max_age_minutes BETWEEN 5 AND 10080),
    max_events          INTEGER NOT NULL CHECK (max_events BETWEEN 1 AND 2000),
    render              TEXT NOT NULL DEFAULT 'lines_v1' CHECK (render IN ('lines_v1', 'lines_v2')),
    text_max_chars      INTEGER NOT NULL CHECK (text_max_chars BETWEEN 20 AND 4000),
    quiet_word          TEXT NOT NULL DEFAULT 'SKIP' CHECK (quiet_word ~ '^[^[:space:]]{1,40}$'),
    output_header       TEXT NOT NULL DEFAULT '' CHECK (length(output_header) <= 200),
    output_kind         TEXT NOT NULL DEFAULT 'push' CHECK (output_kind IN ('push')),
    cursor_seq          BIGINT NOT NULL DEFAULT 0 CHECK (cursor_seq >= 0),
    cursor_at           TIMESTAMPTZ,
    target_confirmed_at TIMESTAMPTZ,
    -- What the operator was shown when they confirmed (``agents/subscriptions.py`` ``config_version``): a push needs it
    -- to still be what the schedule is.
    target_confirmed_version TEXT,
    -- Selection rules (agents/subscription_rules.py): empty, the subscription's own filter above applies.
    rules               JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(rules) = 'array'),
    -- Counts every change to what a push depends on (the stream, the target, the rule, the zone, whether it runs, a
    -- confirmation): a run pins it, and a push needs it unchanged, so a change and its undoing both stop the run.
    config_epoch        BIGINT NOT NULL DEFAULT 0,
    "window"            TEXT NOT NULL DEFAULT 'cursor' CHECK ("window" IN ('cursor', 'lookback')),
    delivery_policy     TEXT NOT NULL DEFAULT 'per_run' CHECK (delivery_policy IN ('per_run', 'daily'))
);
-- Reserve before the processor starts; release failures or interruptions only when nothing was sent or uncertain.
CREATE TABLE IF NOT EXISTS subscription_daily_claims (
    schedule_id TEXT NOT NULL REFERENCES subscriptions(schedule_id) ON DELETE CASCADE,
    local_date  DATE NOT NULL,
    PRIMARY KEY (schedule_id, local_date)
);
CREATE TABLE IF NOT EXISTS subscription_runs (
    run_id            TEXT PRIMARY KEY REFERENCES schedule_runs(id) ON DELETE CASCADE,
    from_seq          BIGINT NOT NULL,
    to_seq            BIGINT NOT NULL,
    events_in_window  INTEGER NOT NULL DEFAULT 0,
    events_selected   INTEGER NOT NULL DEFAULT 0,
    events_too_old    INTEGER NOT NULL DEFAULT 0,
    events_over_cap   INTEGER NOT NULL DEFAULT 0,
    record_id         BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    outcome           TEXT NOT NULL DEFAULT 'pending'
                      CHECK (outcome IN ('pending', 'delivering', 'delivered', 'uncertain', 'quiet', 'dashboard_only',
                                         'output_invalid', 'failed')),
    output_text       TEXT,
    output_sha256     TEXT,
    notification_id   TEXT REFERENCES schedule_notifications(id) ON DELETE SET NULL,
    -- Where the run may push and under which configuration: set when it started with the target confirmed, NULL
    -- otherwise (its output stays on the dashboard).
    target_channel    TEXT,
    target_chat       TEXT,
    config_version    TEXT,
    window_mode       TEXT NOT NULL DEFAULT 'cursor' CHECK (window_mode IN ('cursor', 'lookback')),
    daily_date        DATE,  -- the claimed date at planning, unaffected by later timezone changes
    -- The subscription's configuration and epoch as the run started: its window is selected with these, not with what
    -- the subscription is by then.
    config            JSONB,
    config_epoch      BIGINT,
    -- Windows whose text_match pattern run was stopped at its deadline: no pattern took anything in them.
    match_timeouts    INTEGER NOT NULL DEFAULT 0
);
-- One message of a run's push: pending, then sending (written before the channel is asked), then sent, failed (the
-- channel said no: asked again) or uncertain (it was being sent when the process died, or the channel's answer was
-- lost: never sent again).
CREATE TABLE IF NOT EXISTS subscription_deliveries (
    run_id       TEXT NOT NULL REFERENCES subscription_runs(run_id) ON DELETE CASCADE,
    segment      INTEGER NOT NULL CHECK (segment >= 1),
    text         TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'sending', 'sent', 'failed', 'uncertain')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    external_ids TEXT[] NOT NULL DEFAULT '{}',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, segment)
);
-- subscriptions end

-- ledger begin
-- The Manager's ledger (completion-ledger RFC, section 4): what waits for the user, what was decided, who does what.
-- An own item keeps its state here; a sourced item (a gate, a confirmation, a task) has no status and reads its
-- source's. ledger_events only ever grows. Confirming or closing an own item points at a 'user' event, and the
-- composite keys below make a 'relayed' event impossible to point at, even from raw SQL.
CREATE TABLE IF NOT EXISTS ledger_items (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('pending', 'decision', 'work_item')),
    title         TEXT NOT NULL CHECK (btrim(title) <> '' AND char_length(title) <= 200),
    detail        TEXT NOT NULL DEFAULT '' CHECK (char_length(detail) <= 4000),
    source_kind   TEXT CHECK (source_kind IN ('gate', 'confirmation', 'task')),
    gate_step_id  TEXT REFERENCES task_user_gates(step_id) ON DELETE CASCADE,
    action_id     BIGINT REFERENCES manager_actions(id) ON DELETE CASCADE,
    task_id       TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    status        TEXT CHECK (status IN ('open', 'done', 'cancelled', 'superseded')),
    owner_kind    TEXT NOT NULL DEFAULT 'user' CHECK (owner_kind IN ('user', 'manager', 'task', 'chat')),
    owner_ref     TEXT,
    project_id    TEXT REFERENCES projects(id) ON DELETE SET NULL,
    due_at        TIMESTAMPTZ,
    refs          JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(refs) = 'array'),
    superseded_by BIGINT REFERENCES ledger_items(id) ON DELETE SET NULL,
    confirmed_event_id BIGINT,
    confirmed_actor    TEXT CHECK (confirmed_actor = 'user'),
    closed_event_id    BIGINT,
    closed_actor       TEXT CHECK (closed_actor = 'user'),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at     TIMESTAMPTZ,
    sourced       BOOLEAN GENERATED ALWAYS AS (source_kind IS NOT NULL) STORED,
    UNIQUE (id, sourced),
    CONSTRAINT ledger_items_source CHECK (COALESCE(
        (source_kind IS NULL AND gate_step_id IS NULL AND action_id IS NULL AND task_id IS NULL
         AND status IS NOT NULL)
        OR (source_kind = 'gate' AND kind = 'pending' AND gate_step_id IS NOT NULL
            AND action_id IS NULL AND task_id IS NULL AND status IS NULL)
        OR (source_kind = 'confirmation' AND kind = 'pending' AND action_id IS NOT NULL
            AND gate_step_id IS NULL AND task_id IS NULL AND status IS NULL)
        OR (source_kind = 'task' AND kind = 'work_item' AND task_id IS NOT NULL
            AND gate_step_id IS NULL AND action_id IS NULL AND status IS NULL), false)),
    CONSTRAINT ledger_items_owner CHECK ((owner_kind = 'user') = (owner_ref IS NULL)),
    CONSTRAINT ledger_items_confirmed CHECK ((confirmed_event_id IS NULL) = (confirmed_actor IS NULL)
                                             AND (confirmed_event_id IS NULL OR kind = 'decision')),
    -- an own item is closed exactly when it points at a closing event; a sourced item never does
    CONSTRAINT ledger_items_closed CHECK ((closed_event_id IS NULL) = (closed_actor IS NULL)
                                          AND (status IS NOT NULL OR closed_event_id IS NULL)
                                          AND (status IS NULL OR (status = 'open') = (closed_event_id IS NULL))),
    CONSTRAINT ledger_items_superseded CHECK (superseded_by IS NULL OR (status = 'superseded' AND kind = 'decision'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_items_gate ON ledger_items (gate_step_id) WHERE gate_step_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_items_action ON ledger_items (action_id) WHERE action_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_items_task ON ledger_items (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ledger_items_open ON ledger_items (kind, due_at) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS ledger_events (
    id            BIGSERIAL PRIMARY KEY,
    item_id       BIGINT NOT NULL REFERENCES ledger_items(id) ON DELETE CASCADE,
    item_sourced  BOOLEAN NOT NULL,   -- whether the item is sourced; the key below keeps it true to the item
    action        TEXT NOT NULL CHECK (action IN ('add', 'note', 'amend', 'confirm', 'close', 'cancel',
                                                  'supersede', 'reopen')),
    actor_kind    TEXT NOT NULL CHECK (actor_kind IN ('user', 'relayed', 'system')),
    actor_id      TEXT,
    relayed_by    TEXT,
    via           TEXT NOT NULL CHECK (via IN ('dashboard', 'lark', 'telegram', 'cli', 'api', 'system')),
    manager_id    TEXT REFERENCES managers(id) ON DELETE RESTRICT,
    channel_id    TEXT,
    chat          TEXT,
    message_id    TEXT,
    session_label TEXT,
    quote         TEXT CHECK (char_length(quote) <= 1000),
    note          TEXT CHECK (char_length(note) <= 2000),
    changes       JSONB CHECK (changes IS NULL OR jsonb_typeof(changes) = 'object'),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, actor_kind),
    FOREIGN KEY (item_id, item_sourced) REFERENCES ledger_items (id, sourced),
    -- the system adds sourced items and only those; the user and a relayed message add own items
    CONSTRAINT ledger_events_add CHECK (action <> 'add' OR (actor_kind = 'system') = item_sourced),
    CONSTRAINT ledger_events_actor CHECK (action IN ('add', 'note') OR actor_kind = 'user'),
    CONSTRAINT ledger_events_system CHECK ((actor_kind = 'system') = (via = 'system')
                                           AND (actor_kind <> 'system' OR action = 'add')),
    CONSTRAINT ledger_events_relayed CHECK ((actor_kind = 'relayed') = (relayed_by IS NOT NULL)),
    CONSTRAINT ledger_events_cli CHECK ((via = 'cli') = (session_label IS NOT NULL)),
    CONSTRAINT ledger_events_cli_user CHECK (via <> 'cli' OR actor_kind = 'user'),
    CONSTRAINT ledger_events_quote CHECK (quote IS NULL OR actor_kind IN ('user', 'relayed'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_events_item ON ledger_events (item_id, id);

DO $ledger$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'ledger_items'::regclass
                      AND conname = 'ledger_items_confirm_fk') THEN
        ALTER TABLE ledger_items ADD CONSTRAINT ledger_items_confirm_fk
            FOREIGN KEY (confirmed_event_id, confirmed_actor) REFERENCES ledger_events (id, actor_kind);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'ledger_items'::regclass
                      AND conname = 'ledger_items_close_fk') THEN
        ALTER TABLE ledger_items ADD CONSTRAINT ledger_items_close_fk
            FOREIGN KEY (closed_event_id, closed_actor) REFERENCES ledger_events (id, actor_kind);
    END IF;
END $ledger$;
-- ledger end
-- Manager completion events (manager completion ledger RFC, C1): one row per task end that has a chat to tell.
-- connection.py applies this block to databases that predate it, so keep it between the markers and idempotent.
-- manager events begin
CREATE TABLE IF NOT EXISTS manager_events (
    id              BIGSERIAL PRIMARY KEY,
    manager_id      TEXT NOT NULL REFERENCES managers(id) ON DELETE RESTRICT,
    kind            TEXT NOT NULL CHECK (kind IN ('task_end')),
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    origin          TEXT NOT NULL CHECK (origin IN ('chat', 'schedule')),
    schedule_run_id TEXT REFERENCES schedule_runs(id) ON DELETE SET NULL,
    target_kind     TEXT NOT NULL CHECK (target_kind IN ('web', 'dm', 'group')),
    channel_id      TEXT NOT NULL,
    chat            TEXT NOT NULL,
    session_key     TEXT,
    dedupe_key      TEXT NOT NULL UNIQUE,
    payload         JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    fallback        JSONB NOT NULL CHECK (jsonb_typeof(fallback) = 'array'),
    state           TEXT NOT NULL CHECK (state IN ('pending', 'running', 'delivered', 'fallback', 'skipped')),
    reason          TEXT CHECK (reason IN ('off', 'group_chat', 'manager_off', 'turn_failed', 'turn_timeout',
                                           'turn_stopped', 'empty_reply', 'send_failed', 'queue_deadline',
                                           'restart', 'no_chat', 'target_changed')),
    batch           UUID,
    record_id       BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    due_at          TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    sent_at         TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    CONSTRAINT manager_events_reason CHECK ((state IN ('skipped', 'fallback')) = (reason IS NOT NULL)),
    CONSTRAINT manager_events_turn_target CHECK (state NOT IN ('pending', 'running', 'delivered')
                                                 OR target_kind IN ('web', 'dm')),
    CONSTRAINT manager_events_batch CHECK (state NOT IN ('running', 'delivered') OR batch IS NOT NULL),
    CONSTRAINT manager_events_due CHECK (state <> 'pending' OR due_at IS NOT NULL),
    CONSTRAINT manager_events_schedule CHECK (origin = 'schedule' OR schedule_run_id IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_manager_events_open
    ON manager_events (manager_id, channel_id, chat, id) WHERE state IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_manager_events_task ON manager_events (task_id);
CREATE INDEX IF NOT EXISTS idx_manager_events_created ON manager_events (created_at);
CREATE INDEX IF NOT EXISTS idx_manager_events_schedule_run ON manager_events (schedule_run_id) WHERE schedule_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_manager_events_record ON manager_events (record_id) WHERE record_id IS NOT NULL;
-- The delivery of what tells the event (the completion turn's reply, or the code notice it falls back to), as
-- subscription_deliveries: written `sending` (committed) before the channel is asked; `sent`; `failed` (the
-- channel said it did not send it, or it never got that far: tried again from delivery_at); `uncertain` (it may
-- have been sent: never again). NULL: no delivery yet (a skipped event's notice is sent inline and only sets sent_at).
ALTER TABLE manager_events ADD COLUMN IF NOT EXISTS delivery TEXT
    CHECK (delivery IN ('pending', 'sending', 'sent', 'failed', 'uncertain'));
ALTER TABLE manager_events ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE manager_events ADD COLUMN IF NOT EXISTS delivery_at TIMESTAMPTZ;
ALTER TABLE manager_events ADD COLUMN IF NOT EXISTS delivery_error TEXT;
-- The claim (one send) the delivery was taken by: only that send's answer settles it, and a success it reports late
-- turns an `uncertain` it was settled as meanwhile into `sent` (it is never sent again either way).
ALTER TABLE manager_events ADD COLUMN IF NOT EXISTS delivery_claim UUID;
-- The completion event that told a schedule run's message (the Manager's reply, or its fallback); NULL otherwise.
ALTER TABLE schedule_notifications ADD COLUMN IF NOT EXISTS manager_event_id BIGINT
    REFERENCES manager_events(id) ON DELETE SET NULL;
-- manager events end
-- The default Manager's group and other-sender direct-chat sessions (the main one stays in agent_sessions).
-- manager sessions begin
CREATE TABLE IF NOT EXISTS manager_sessions (
    manager_id      TEXT NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
    session_key     TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('group', 'dm')),
    channel_id      TEXT NOT NULL,
    chat            TEXT NOT NULL,
    session_id      TEXT,
    session_totals  JSONB NOT NULL DEFAULT '{}',
    runtime_type    TEXT,
    profile_id      TEXT,
    provider_id     TEXT,
    clean_start     BOOLEAN NOT NULL DEFAULT false,
    pending_handoff TEXT,
    context_tokens  INTEGER,
    last_turn_cost_usd DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_id, session_key),
    CONSTRAINT manager_sessions_key CHECK (session_key = kind || ':' || channel_id || ':' || chat)
);
CREATE INDEX IF NOT EXISTS idx_manager_sessions_active ON manager_sessions (manager_id, last_active_at DESC);
-- manager sessions end
-- A database that gets the column above later builds this index online (connection.py ``_ONLINE_STEPS``).
CREATE INDEX IF NOT EXISTS idx_schedule_notifications_manager_event ON schedule_notifications (manager_event_id)
    WHERE manager_event_id IS NOT NULL;

-- agent spawns begin
-- A run another run starts (the advisor RFC, section 5): one row per consult of the advisor, its state, what it was
-- given and what came back. The Planner's turn that asked is parent_record_id; the last advisor round is
-- child_record_id (every round's record names the parent in llm_records.parent_record_id). result holds typed
-- outcomes only: rounds, check hits by category and position (never what they matched), and why it failed.
CREATE TABLE IF NOT EXISTS agent_spawns (
    id               BIGSERIAL PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id          TEXT REFERENCES task_steps(id) ON DELETE SET NULL,
    parent_record_id BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    child_record_id  BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('advisor')),
    brief            JSONB NOT NULL CHECK (jsonb_typeof(brief) = 'object'),
    inputs           JSONB NOT NULL CHECK (jsonb_typeof(inputs) = 'object'),
    budget_usd       NUMERIC(10, 4) NOT NULL CHECK (budget_usd > 0),
    spent_usd        NUMERIC(10, 6) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
    state            TEXT NOT NULL CHECK (state IN ('drafting', 'linting', 'asking', 'needs_info',
                                                    'answered', 'blocked', 'failed', 'over_budget')),
    returns          INTEGER NOT NULL DEFAULT 0 CHECK (returns BETWEEN 0 AND 2),
    rounds           INTEGER NOT NULL DEFAULT 0 CHECK (rounds BETWEEN 0 AND 3),
    result           JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(result) = 'object'),
    detail           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    CONSTRAINT agent_spawns_finished CHECK ((state IN ('answered', 'blocked', 'failed', 'over_budget'))
                                            = (finished_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_agent_spawns_task ON agent_spawns (task_id, id);
CREATE INDEX IF NOT EXISTS idx_agent_spawns_step ON agent_spawns (step_id) WHERE step_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_spawns_open ON agent_spawns (task_id) WHERE finished_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_agent_spawns_parent ON agent_spawns (parent_record_id) WHERE parent_record_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_spawns_child ON agent_spawns (child_record_id) WHERE child_record_id IS NOT NULL;

-- An advisor consult's question and advice, how the Planner adopted it, and whether the attempt it was for passed
-- (unlocked). attempt is the step's attempt the advice is for (task_steps.retries + 1 at a checkpoint, + 2 after a
-- failed attempt); attempt_id is left for step attempts (M1) to fill and constrain.
CREATE TABLE IF NOT EXISTS advisor_advice (
    spawn_id        BIGINT PRIMARY KEY REFERENCES agent_spawns(id) ON DELETE CASCADE,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id         TEXT REFERENCES task_steps(id) ON DELETE SET NULL,
    attempt         INTEGER NOT NULL CHECK (attempt >= 1),
    attempt_id      BIGINT,
    source          TEXT NOT NULL CHECK (source IN ('ladder', 'checkpoint', 'optional')),
    kind            TEXT NOT NULL CHECK (kind IN ('diagnose', 'design_choice', 'plan_review', 'unblock')),
    question        JSONB NOT NULL CHECK (jsonb_typeof(question) = 'object'),
    advice          JSONB CHECK (advice IS NULL OR jsonb_typeof(advice) = 'object'),
    adoption        TEXT CHECK (adoption IN ('adopted', 'partial', 'rejected')),
    adoption_detail JSONB CHECK (adoption_detail IS NULL OR jsonb_typeof(adoption_detail) = 'object'),
    unlocked        BOOLEAN,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    adopted_at      TIMESTAMPTZ,
    unlocked_at     TIMESTAMPTZ,
    CONSTRAINT advisor_advice_adoption CHECK (adoption IS NULL OR advice IS NOT NULL),
    CONSTRAINT advisor_advice_adopted_at CHECK ((adoption IS NULL) = (adopted_at IS NULL)),
    CONSTRAINT advisor_advice_unlocked CHECK ((unlocked IS NULL) = (unlocked_at IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_advisor_advice_task ON advisor_advice (task_id);
CREATE INDEX IF NOT EXISTS idx_advisor_advice_step ON advisor_advice (step_id, attempt) WHERE step_id IS NOT NULL;
-- agent spawns end

-- decision snapshots begin
-- What a task's engine saw when it decided (the decision snapshot RFC), written only while the system setting
-- decision_snapshots.enabled is true: a dispatch pass (kind dispatch: the steps as readiness reads them, the step
-- workspace round's inputs, which steps were ready and which started) or the Planner's decision on a step's result
-- (kind step_decision: what code computed for the turn, the turn's run record in digest, the decision and its
-- reason as the activity line keeps it). No message text. record_id is the turn's llm_records row, without a foreign
-- key: the snapshot keeps the digest it needs. Going with its task; housekeeping deletes rows after 30 days.
-- connection.py applies this block to databases that predate it, so keep it between the markers.
CREATE TABLE IF NOT EXISTS decision_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('dispatch', 'step_decision')),
    step_id       TEXT,
    record_id     BIGINT,
    inputs        JSONB NOT NULL CHECK (jsonb_typeof(inputs) = 'object'),
    inputs_sha256 TEXT NOT NULL,
    decision      JSONB NOT NULL CHECK (jsonb_typeof(decision) = 'object'),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT decision_snapshots_step CHECK ((kind = 'step_decision') = (step_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_task ON decision_snapshots (task_id, kind, id);
CREATE INDEX IF NOT EXISTS idx_decision_snapshots_created ON decision_snapshots (created_at);
-- decision snapshots end

-- Message leads and intents (message-leads-intents RFC, section 6): sourced notes and typed intents the processor
-- extracts from source_events, the per-chat extraction switch, intent types with their append-only history, chat →
-- project bindings, the alert-bot allowlist and the messages Looper sent about intents. Default off: no rows, no
-- behavior. connection.py applies this block to databases that predate it, so keep it between the markers and
-- idempotent.
-- message leads begin
ALTER TABLE source_chats ADD COLUMN IF NOT EXISTS extraction TEXT NOT NULL DEFAULT 'included';
ALTER TABLE source_chats ADD COLUMN IF NOT EXISTS extraction_excluded_at TIMESTAMPTZ;
ALTER TABLE source_chats DROP CONSTRAINT IF EXISTS source_chats_extraction_check;
ALTER TABLE source_chats ADD CONSTRAINT source_chats_extraction_check CHECK (extraction IN ('included', 'excluded')
    AND ((extraction = 'excluded') = (extraction_excluded_at IS NOT NULL)));
ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS subscriptions_output_kind_check;
ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_output_kind_check
    CHECK (output_kind IN ('push', 'leads', 'intents'));
ALTER TABLE subscription_runs DROP CONSTRAINT IF EXISTS subscription_runs_outcome_check;
ALTER TABLE subscription_runs ADD CONSTRAINT subscription_runs_outcome_check
    CHECK (outcome IN ('pending', 'delivering', 'delivered', 'uncertain', 'quiet', 'dashboard_only', 'output_invalid',
                       'failed', 'extracted'));
ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS extract_counts JSONB;
ALTER TABLE subscription_runs DROP CONSTRAINT IF EXISTS subscription_runs_extract_counts_check;
ALTER TABLE subscription_runs ADD CONSTRAINT subscription_runs_extract_counts_check
    CHECK (extract_counts IS NULL OR jsonb_typeof(extract_counts) = 'object');

CREATE TABLE IF NOT EXISTS message_leads (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,      -- shown as lead-123
    source_id      TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('decision', 'commitment', 'request', 'status', 'issue', 'fact')),
    summary        TEXT NOT NULL CHECK (btrim(summary) <> '' AND char_length(summary) <= 200),
    detail         TEXT NOT NULL DEFAULT '' CHECK (char_length(detail) <= 600),
    confidence     TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    -- 'expired' is not stored: an active lead past valid_until shows as expired (agents/leads_model.shown_state).
    -- 'dropped' and the check's columns are the leads check block's.
    state          TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'superseded', 'promoted', 'inaccurate')),
    valid_until    TIMESTAMPTZ NOT NULL,
    superseded_by  BIGINT REFERENCES message_leads(id) ON DELETE SET NULL,
    promoted_kind  TEXT CHECK (promoted_kind IN ('vault_proposal')),     -- M6a writes it; nothing does before
    promoted_ref   TEXT,
    marked_via     TEXT CHECK (marked_via IN ('dashboard', 'cli')),      -- who marked it inaccurate
    marked_note    TEXT CHECK (char_length(marked_note) <= 500),
    first_sent_at  TIMESTAMPTZ NOT NULL,   -- the earliest and latest sent_at of its sources, kept by its writers
    last_sent_at   TIMESTAMPTZ NOT NULL,
    run_id         TEXT REFERENCES schedule_runs(id) ON DELETE SET NULL,   -- NULL for a backfill
    record_id      BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    backfill       BOOLEAN NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT message_leads_superseded CHECK (superseded_by IS NULL OR state = 'superseded'),
    CONSTRAINT message_leads_promoted CHECK ((state = 'promoted') = (promoted_kind IS NOT NULL)),
    CONSTRAINT message_leads_marked CHECK ((state = 'inaccurate') = (marked_via IS NOT NULL)),
    CONSTRAINT message_leads_sent CHECK (first_sent_at <= last_sent_at)
);
CREATE TABLE IF NOT EXISTS message_lead_sources (
    lead_id    BIGINT NOT NULL REFERENCES message_leads(id) ON DELETE CASCADE,
    event_seq  BIGINT NOT NULL REFERENCES source_events(seq) ON DELETE CASCADE,
    PRIMARY KEY (lead_id, event_seq)
);
-- Who a lead concerns: the senders and the @-mentioned of its sources (from the events), and the participants the
-- processor named (about).
CREATE TABLE IF NOT EXISTS message_lead_people (
    lead_id  BIGINT NOT NULL REFERENCES message_leads(id) ON DELETE CASCADE,
    open_id  TEXT NOT NULL CHECK (open_id <> ''),
    role     TEXT NOT NULL CHECK (role IN ('sender', 'mentioned', 'about')),
    PRIMARY KEY (lead_id, open_id, role)
);
CREATE INDEX IF NOT EXISTS idx_message_leads_list ON message_leads (source_id, state, last_sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_leads_valid ON message_leads (valid_until) WHERE state = 'active';
CREATE INDEX IF NOT EXISTS idx_message_lead_sources_event ON message_lead_sources (event_seq);
CREATE INDEX IF NOT EXISTS idx_message_lead_people_person ON message_lead_people (open_id);

-- Intent types: the current version of each, and every version saved (only ever added; a rollback adds a copy).
CREATE TABLE IF NOT EXISTS intent_types (
    key          TEXT PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_]{0,31}$'),
    builtin      BOOLEAN NOT NULL DEFAULT false,
    version      INTEGER NOT NULL CHECK (version >= 1),   -- the current row of intent_type_versions
    enabled      BOOLEAN NOT NULL DEFAULT true,
    archived_at  TIMESTAMPTZ,                             -- a custom type that was deleted
    CONSTRAINT intent_types_builtin_kept CHECK (NOT (builtin AND archived_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS intent_type_versions (
    type_key          TEXT NOT NULL REFERENCES intent_types(key) ON DELETE CASCADE,
    version           INTEGER NOT NULL CHECK (version >= 1),
    name              TEXT NOT NULL CHECK (btrim(name) <> '' AND char_length(name) <= 30),
    guidance          TEXT NOT NULL CHECK (char_length(btrim(guidance)) BETWEEN 1 AND 2000),   -- model-visible
    priority          TEXT NOT NULL CHECK (priority IN ('urgent', 'normal', 'quiet')),
    workflow_key      TEXT,                                -- the type's default workflows.key; NULL: the project's
    rolled_back_from  INTEGER,
    edited_via        TEXT NOT NULL CHECK (edited_via IN ('builtin', 'dashboard', 'api')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (type_key, version)
);
-- A type's template (workflow preset) in one project.
CREATE TABLE IF NOT EXISTS intent_type_projects (
    type_key     TEXT NOT NULL REFERENCES intent_types(key) ON DELETE CASCADE,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workflow_key TEXT,                     -- a workflows.key; code checks it still exists when it builds a task
    PRIMARY KEY (type_key, project_id)
);
-- A chat's project, set by the operator on the Intents page; an intent copies it when it is made.
CREATE TABLE IF NOT EXISTS message_chat_projects (
    source_id  TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    PRIMARY KEY (source_id, chat_id),
    FOREIGN KEY (source_id, chat_id) REFERENCES source_chats(source_id, chat_id) ON DELETE CASCADE
);
-- The alert bots (by app id) whose messages the intents run reads, only for alert_triage.
CREATE TABLE IF NOT EXISTS intent_alert_senders (
    source_id  TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
    app_id     TEXT NOT NULL CHECK (btrim(app_id) <> ''),
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, app_id)
);
CREATE TABLE IF NOT EXISTS intents (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,          -- shown as I-0042
    source_id     TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
    schedule_id   TEXT REFERENCES schedules(id) ON DELETE SET NULL,      -- the intents subscription that made it
    type_key      TEXT NOT NULL REFERENCES intent_types(key),
    type_version  INTEGER NOT NULL,
    title         TEXT NOT NULL CHECK (btrim(title) <> '' AND char_length(title) <= 60),
    summary       TEXT NOT NULL DEFAULT '' CHECK (char_length(summary) <= 400),
    confidence    TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    state         TEXT NOT NULL DEFAULT 'pending'          -- 'dropped' and the check's columns: the leads check block's
                  CHECK (state IN ('pending', 'tasked', 'rejected', 'expired', 'merged')),
    chat_id       TEXT NOT NULL,                       -- the chat of its first source
    project_id    TEXT REFERENCES projects(id) ON DELETE SET NULL,
    workflow_key  TEXT,                                -- a workflows.key, checked when it builds a task
    merged_into   BIGINT,                              -- its key and its detaching are the intent merges block's
    task_id       TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    flagged       BOOLEAN NOT NULL DEFAULT false,      -- one of its sources is in its run's instructions_seen
    expires_at    TIMESTAMPTZ,                         -- 24 hours after creation or latest successful push/progress; NULL falls back to creation + 24 hours
    handled_at    TIMESTAMPTZ,
    handled_via   TEXT CHECK (handled_via IN ('dashboard', 'lark', 'telegram', 'system')),
    reject_reason TEXT CHECK (char_length(reject_reason) <= 200),
    run_id        TEXT REFERENCES schedule_runs(id) ON DELETE SET NULL,
    record_id     BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT intents_merged CHECK ((state = 'merged') = (merged_into IS NOT NULL)),
    CONSTRAINT intents_handled CHECK ((state IN ('pending', 'merged')) = (handled_at IS NULL)
                                      AND (handled_at IS NULL) = (handled_via IS NULL)),
    CONSTRAINT intents_rejected CHECK (reject_reason IS NULL OR state = 'rejected')
);
CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id  BIGINT NOT NULL REFERENCES intents(id) ON DELETE CASCADE,
    event_seq  BIGINT NOT NULL REFERENCES source_events(seq) ON DELETE CASCADE,
    PRIMARY KEY (intent_id, event_seq)
);
-- One message Looper sent about an intent, claimed (sending, committed) before the channel is asked and never sent
-- again unless the channel answered that it did not send it, as subscription_deliveries. A progress post belongs to
-- the merged intent that brought the news and replies to its target's push; final is the fallback of an edit.
CREATE TABLE IF NOT EXISTS intent_posts (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    intent_id    BIGINT NOT NULL REFERENCES intents(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('push', 'progress', 'final')),
    channel_id   TEXT NOT NULL,
    chat         TEXT NOT NULL,
    text         TEXT NOT NULL,
    candidates   TEXT[] NOT NULL DEFAULT '{}',         -- the project ids it listed, in order: 「建 I-0042 2」 is the 2nd
    state        TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'sending', 'sent', 'failed', 'uncertain')),
    external_id  TEXT,                                 -- the channel's message id, for quotes and the final edit
    edited_at    TIMESTAMPTZ,                          -- a push changed to its intent's final state
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_intents_list ON intents (source_id, state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intents_recent ON intents (source_id, type_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intents_expiry ON intents (expires_at) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS idx_intent_sources_event ON intent_sources (event_seq);
CREATE INDEX IF NOT EXISTS idx_intent_posts_intent ON intent_posts (intent_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intent_posts_external ON intent_posts (channel_id, external_id)
    WHERE external_id IS NOT NULL;

-- A deleted source message takes the leads and intents made from it: their text may restate it.
CREATE OR REPLACE FUNCTION message_leads_drop_lead() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM message_leads WHERE id = OLD.lead_id;
    RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION message_leads_drop_intent() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM intents WHERE id = OLD.intent_id;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS message_lead_sources_drop ON message_lead_sources;
CREATE TRIGGER message_lead_sources_drop AFTER DELETE ON message_lead_sources
    FOR EACH ROW EXECUTE FUNCTION message_leads_drop_lead();
DROP TRIGGER IF EXISTS intent_sources_drop ON intent_sources;
CREATE TRIGGER intent_sources_drop AFTER DELETE ON intent_sources
    FOR EACH ROW EXECUTE FUNCTION message_leads_drop_intent();

-- The built-in intent types, version 1 (the recommended text: a version equal to it is "recommended").
INSERT INTO intent_types (key, builtin, version) VALUES
    ('dev_task', true, 1), ('bug_triage', true, 1), ('alert_triage', true, 1)
ON CONFLICT DO NOTHING;
INSERT INTO intent_type_versions (type_key, version, name, guidance, priority, edited_via) VALUES
    ('dev_task', 1, '开发任务',
     '有人请用户做、或用户答应做一项开发工作（新功能、改动、修复、评审、上线），说得出要做什么。闲聊里的设想、别人自己在做的事不算。',
     'normal', 'builtin'),
    ('bug_triage', 1, 'bug 排查',
     '有人报告用户负责或相关的系统出了问题，需要用户看：报错、行为异常、数据不对，最好带现象或链接。已经解决的不算。',
     'normal', 'builtin'),
    ('alert_triage', 1, '监控异常排查',
     '白名单里的告警机器人发出、需要人处理的告警：触发、持续或恶化。恢复通知、测试告警、群里已经有人认领的不算。',
     'urgent', 'builtin')
ON CONFLICT DO NOTHING;
-- message leads end

-- An intent merged into another is its own matter, made from its own messages: deleting the intent it was merged
-- into (its messages deleted, its chat purged, any code path) makes it pending again instead of deleting it. The key
-- takes no action of its own, so a path that skipped the trigger would fail rather than cascade. The trigger runs
-- AFTER each deleted row (a BEFORE trigger could not update a row the same statement deletes, as in a chain of merges
-- deleted at once) and its name sorts before the key's own RI_ConstraintTrigger_* triggers, which PostgreSQL fires in
-- name order, so the merged rows are detached before the key is checked. connection.py applies this block to
-- databases whose leads block predates it, so keep it between the markers and idempotent.
-- intent merges begin
ALTER TABLE intents DROP CONSTRAINT IF EXISTS intents_merged_into_fkey;
ALTER TABLE intents ADD CONSTRAINT intents_merged_into_fkey FOREIGN KEY (merged_into) REFERENCES intents(id);
CREATE OR REPLACE FUNCTION intents_detach_merged() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE intents SET state = 'pending', merged_into = NULL, updated_at = now() WHERE merged_into = OLD.id;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS intents_detach_merged ON intents;
DROP TRIGGER IF EXISTS "A_intents_detach_merged" ON intents;
CREATE TRIGGER "A_intents_detach_merged" AFTER DELETE ON intents
    FOR EACH ROW EXECUTE FUNCTION intents_detach_merged();
-- intent merges end

-- A second model's check of what an extraction found (message-leads-intents RFC, section 10.1): its typed verdict on
-- each lead and intent (keep, fix, drop), why, and the check's run; a dropped item stays, in state 'dropped', so a
-- wrong drop can be measured (its sources still take it with them). A dropped intent is handled by the system and
-- never pushed. Each extraction run records what its check did (check_status), a subscription whose checks keep
-- failing is alerted once (check_alert_at), and the checker has a role layer of its own. The subscription's switch is
-- off by default. connection.py applies this block to databases that predate it, so keep it between the markers and
-- idempotent. Columns come without constraints and every constraint NOT VALID, so nothing is scanned under the
-- migration's locks; connection._ONLINE_STEPS validates them after it commits (a fresh database, whose tables are
-- empty, validates them in its own transaction).
-- leads check begin
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS check_items BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS check_alert_at TIMESTAMPTZ;
ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS check_status TEXT;
ALTER TABLE message_leads ADD COLUMN IF NOT EXISTS check_verdict TEXT;
ALTER TABLE message_leads ADD COLUMN IF NOT EXISTS check_reason TEXT;
ALTER TABLE message_leads ADD COLUMN IF NOT EXISTS check_record_id BIGINT;
ALTER TABLE intents ADD COLUMN IF NOT EXISTS check_verdict TEXT;
ALTER TABLE intents ADD COLUMN IF NOT EXISTS check_reason TEXT;
ALTER TABLE intents ADD COLUMN IF NOT EXISTS check_record_id BIGINT;
ALTER TABLE agent_setting_layers DROP CONSTRAINT IF EXISTS agent_setting_layers_role_check;
ALTER TABLE agent_setting_layers ADD CONSTRAINT agent_setting_layers_role_check
    CHECK (role IN ('manager', 'planner', 'worker', 'reviewer', 'processor', 'advisor', 'advice_check', 'leads_check',
                    'outbound_check')) NOT VALID;
ALTER TABLE subscription_runs DROP CONSTRAINT IF EXISTS subscription_runs_check_status_check;
ALTER TABLE subscription_runs ADD CONSTRAINT subscription_runs_check_status_check
    CHECK (check_status IN ('skipped', 'checked', 'failed')) NOT VALID;
ALTER TABLE schedule_notifications DROP CONSTRAINT IF EXISTS schedule_notifications_kind_check;
ALTER TABLE schedule_notifications ADD CONSTRAINT schedule_notifications_kind_check
    CHECK (kind IN ('run', 'failures_alert', 'failures_recovered', 'stale_alert', 'stale_recovered', 'deadline_alert',
                'deadline_recovered', 'overlap_alert', 'overlap_recovered', 'check_alert', 'check_recovered')) NOT VALID;
ALTER TABLE message_leads DROP CONSTRAINT IF EXISTS message_leads_state_check;
ALTER TABLE message_leads ADD CONSTRAINT message_leads_state_check CHECK (state IN ('active', 'superseded', 'promoted', 'inaccurate', 'dropped')) NOT VALID;
ALTER TABLE message_leads DROP CONSTRAINT IF EXISTS message_leads_check_verdict_check;
ALTER TABLE message_leads ADD CONSTRAINT message_leads_check_verdict_check CHECK (check_verdict IN ('keep', 'fix', 'drop')) NOT VALID;
ALTER TABLE message_leads DROP CONSTRAINT IF EXISTS message_leads_check_reason_check;
ALTER TABLE message_leads ADD CONSTRAINT message_leads_check_reason_check CHECK (char_length(check_reason) <= 200) NOT VALID;
ALTER TABLE message_leads DROP CONSTRAINT IF EXISTS message_leads_check_record_id_fkey;
ALTER TABLE message_leads ADD CONSTRAINT message_leads_check_record_id_fkey FOREIGN KEY (check_record_id)
    REFERENCES llm_records(id) ON DELETE SET NULL NOT VALID;
ALTER TABLE message_leads DROP CONSTRAINT IF EXISTS message_leads_dropped;
ALTER TABLE message_leads ADD CONSTRAINT message_leads_dropped
    CHECK ((state = 'dropped') = (check_verdict IS NOT DISTINCT FROM 'drop')) NOT VALID;
ALTER TABLE intents DROP CONSTRAINT IF EXISTS intents_state_check;
ALTER TABLE intents ADD CONSTRAINT intents_state_check CHECK (state IN ('pending', 'tasked', 'rejected', 'expired', 'merged', 'dropped')) NOT VALID;
ALTER TABLE intents DROP CONSTRAINT IF EXISTS intents_check_verdict_check;
ALTER TABLE intents ADD CONSTRAINT intents_check_verdict_check CHECK (check_verdict IN ('keep', 'fix', 'drop')) NOT VALID;
ALTER TABLE intents DROP CONSTRAINT IF EXISTS intents_check_reason_check;
ALTER TABLE intents ADD CONSTRAINT intents_check_reason_check CHECK (char_length(check_reason) <= 200) NOT VALID;
ALTER TABLE intents DROP CONSTRAINT IF EXISTS intents_check_record_id_fkey;
ALTER TABLE intents ADD CONSTRAINT intents_check_record_id_fkey FOREIGN KEY (check_record_id)
    REFERENCES llm_records(id) ON DELETE SET NULL NOT VALID;
ALTER TABLE intents DROP CONSTRAINT IF EXISTS intents_dropped;
ALTER TABLE intents ADD CONSTRAINT intents_dropped
    CHECK ((state = 'dropped') = (check_verdict IS NOT DISTINCT FROM 'drop')) NOT VALID;
-- leads check end

-- One row per proactive message. Metrics write the receipt directly; the delivery
-- layer will own the single decision point. Its policy fields stay unset until
-- that layer is connected. Keep this block as the one named migration.
-- proactive deliveries begin
CREATE TABLE IF NOT EXISTS proactive_deliveries (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL CHECK (source IN ('task_notice', 'completion', 'progress', 'gate', 'gate_reminder',
                                                'subscription', 'intent', 'schedule', 'advisor_alert', 'autonomous',
                                                'user_reminder')),
    source_ref  TEXT,                 -- 任务、关卡、订阅运行、意图或定时任务运行的 id；管家自己说的话为空
    manager_id  TEXT,
    channel_id  TEXT NOT NULL,        -- 'web' 或机器人的渠道 id
    chat        TEXT NOT NULL,        -- 飞书、Telegram 的会话 id；控制台的会话 id
    external_id TEXT,                 -- 发送方知道时：平台消息 id
    outcome     TEXT CHECK (outcome IN ('sent', 'rejected', 'unknown')),
    dedupe_key  TEXT,
    urgency     TEXT CHECK (urgency IN ('normal', 'blocking', 'user_set')),
    chat_kind   TEXT NOT NULL CHECK (chat_kind IN ('direct', 'group')),
    task_id     TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    step_id     TEXT,
    schedule_id TEXT REFERENCES schedules(id) ON DELETE SET NULL,
    decision    TEXT CHECK (decision IN ('send', 'merge', 'defer', 'digest', 'drop')),
    reason      TEXT CHECK (reason IN ('silent', 'urgent', 'outside_active_hours', 'recent_reply',
                                      'quiet_hours', 'daily_cap', 'within_limits', 'off')),
    counted     BOOLEAN,
    local_date  DATE,
    digest_date DATE,
    decided_at  TIMESTAMPTZ,
    sent_at     TIMESTAMPTZ,
    replied_at  TIMESTAMPTZ,
    reply_match TEXT CHECK (reply_match IN ('quoted', 'next_message')),
    CONSTRAINT proactive_deliveries_receipt CHECK ((sent_at IS NULL) = (outcome IS NULL)),
    CONSTRAINT proactive_deliveries_reply CHECK ((replied_at IS NULL) = (reply_match IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_proactive_deliveries_open
    ON proactive_deliveries (channel_id, chat, sent_at DESC) WHERE replied_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_proactive_deliveries_sent ON proactive_deliveries (sent_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_proactive_deliveries_dedupe
    ON proactive_deliveries (dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_proactive_deliveries_counted
    ON proactive_deliveries (channel_id, chat, local_date) WHERE counted;
CREATE INDEX IF NOT EXISTS idx_proactive_deliveries_digest
    ON proactive_deliveries (digest_date) WHERE decision = 'digest';
CREATE INDEX IF NOT EXISTS idx_proactive_deliveries_task ON proactive_deliveries (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_proactive_deliveries_schedule
    ON proactive_deliveries (schedule_id) WHERE schedule_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_proactive_deliveries_decided ON proactive_deliveries (decided_at);
-- proactive deliveries end

-- channel outbox begin
CREATE TABLE IF NOT EXISTS channel_outbox (
    id              BIGSERIAL PRIMARY KEY,
    channel_id      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 50),
    kind            TEXT NOT NULL CHECK (kind IN ('send', 'reply')),
    chat            TEXT NOT NULL,
    reply_to        TEXT,
    body            JSONB NOT NULL CHECK (jsonb_typeof(body) = 'object'),  -- {"text": ...} or {"markdown": ...}
    source          TEXT NOT NULL CHECK (source IN ('answer', 'notice', 'progress', 'post', 'notify')),
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'sending', 'sent', 'failed', 'uncertain')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_class     TEXT CHECK (error_class IN ('rate_limited', 'rejected', 'transient', 'unknown')),
    error           TEXT CHECK (char_length(error) <= 500),
    external_id     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ,
    first_possible_at TIMESTAMPTZ,
    claim_token     UUID,
    UNIQUE (channel_id, idempotency_key),
    CONSTRAINT channel_outbox_sent CHECK ((state = 'sent') = (sent_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_channel_outbox_due
    ON channel_outbox (channel_id, chat, id) WHERE state IN ('pending', 'sending');
CREATE INDEX IF NOT EXISTS idx_channel_outbox_created ON channel_outbox (created_at);
ALTER TABLE channel_outbox ADD COLUMN IF NOT EXISTS first_possible_at TIMESTAMPTZ;
ALTER TABLE channel_outbox ADD COLUMN IF NOT EXISTS claim_token UUID;
-- Earlier versions did not record attempts separately from claims. Be conservative.
UPDATE channel_outbox SET first_possible_at=created_at WHERE attempts>0 AND first_possible_at IS NULL;
-- channel outbox end

-- A window a subscription run did not use: the run is 'skipped' with why (skip_reason: a prompt hook blocked it, or
-- the processor's reply could not be read when asked twice), and its cursor moves past it, since the same window
-- would fail again with every run after it. A subscription whose runs keep skipping is alerted once (skip_alert_at,
-- the notification kinds 'skip_alert' and 'skip_recovered'). connection.py applies this block to databases that
-- predate it, so keep it between the markers and idempotent. Its columns come without a constraint and every
-- constraint NOT VALID; connection._ONLINE_STEPS validates them after the migration commits (a fresh database
-- validates them in its own transaction).
-- run skips begin
ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS skip_reason TEXT;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS skip_alert_at TIMESTAMPTZ;
ALTER TABLE schedule_notifications DROP CONSTRAINT IF EXISTS schedule_notifications_kind_check;
ALTER TABLE schedule_notifications ADD CONSTRAINT schedule_notifications_kind_check
    CHECK (kind IN ('run', 'failures_alert', 'failures_recovered', 'stale_alert', 'stale_recovered', 'deadline_alert',
                'deadline_recovered', 'overlap_alert', 'overlap_recovered', 'check_alert', 'check_recovered',
                'skip_alert', 'skip_recovered')) NOT VALID;
ALTER TABLE subscription_runs DROP CONSTRAINT IF EXISTS subscription_runs_outcome_check;
ALTER TABLE subscription_runs ADD CONSTRAINT subscription_runs_outcome_check
    CHECK (outcome IN ('pending', 'delivering', 'delivered', 'uncertain', 'quiet', 'dashboard_only', 'output_invalid',
                       'failed', 'extracted', 'skipped')) NOT VALID;
ALTER TABLE subscription_runs DROP CONSTRAINT IF EXISTS subscription_runs_skip_reason_check;
ALTER TABLE subscription_runs ADD CONSTRAINT subscription_runs_skip_reason_check
    CHECK (skip_reason IN ('blocked_by_hook', 'invalid_reply')) NOT VALID;
ALTER TABLE subscription_runs DROP CONSTRAINT IF EXISTS subscription_runs_skipped;
ALTER TABLE subscription_runs ADD CONSTRAINT subscription_runs_skipped
    CHECK ((outcome = 'skipped') = (skip_reason IS NOT NULL)) NOT VALID;
-- run skips end

-- One review of an outbound action (the outbound-review RFC, section 9): who asked for it and where, the action as
-- code describes it (its tool, level and target, a redacted summary), the verdict and why, and the classifier's run
-- (record_id; none when code blocked it without one), model, cost and latency. A row is written before the action
-- is allowed; failure says why a review blocked without a verdict. Rows go with their task. connection.py applies
-- this block to databases that predate it, so keep it between the markers and idempotent.
-- outbound reviews begin
CREATE TABLE IF NOT EXISTS outbound_reviews (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_id          TEXT NOT NULL,
    parent_record_id  BIGINT,
    record_id         BIGINT,
    task_id           TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    manager_action_id BIGINT,
    channel           TEXT,
    chat              TEXT,
    source            TEXT NOT NULL CHECK (source IN ('tool', 'hook', 'post')),
    tool              TEXT NOT NULL,
    action            TEXT,
    level             TEXT NOT NULL CHECK (level IN ('inside', 'operator', 'origin', 'machine', 'external', 'unknown')),
    target_kind       TEXT NOT NULL,
    target            TEXT NOT NULL,
    summary           TEXT NOT NULL CHECK (length(summary) <= 500),
    verdict           TEXT NOT NULL CHECK (verdict IN ('allow', 'block')),
    failure           TEXT CHECK (failure IN ('not_operator', 'timeout', 'parse', 'runtime', 'rate_limited',
                                              'unavailable', 'budget', 'too_long', 'refused', 'protected')),
    reason            TEXT NOT NULL CHECK (length(reason) <= 300),
    signals           TEXT[] NOT NULL DEFAULT '{}',
    profile_id        TEXT,
    model             TEXT,
    cost_usd          NUMERIC(10, 6),
    latency_ms        INTEGER,
    CONSTRAINT outbound_reviews_failure_blocks CHECK (failure IS NULL OR verdict = 'block')
);
CREATE INDEX IF NOT EXISTS idx_outbound_reviews_created ON outbound_reviews (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_outbound_reviews_task ON outbound_reviews (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_outbound_reviews_blocked ON outbound_reviews (created_at DESC) WHERE verdict = 'block';
-- outbound reviews end
-- One-time reminders (reminders RFC): set by the user in their own turns, delivered at due_at by code (no model
-- call) to the user's direct chat with the bot channel_id (chat: their sender id there). pending → sending (claimed,
-- committed before the channel is asked) → delivered, failed (the channel refused it three times, or the bot's
-- operator changed) or uncertain (it may have been sent: never again); pending → missed (Looper was not delivering
-- when it was due, and it is not the latest such one within 36 hours) or cancelled (by the user).
-- connection.py applies this block to databases that predate it, so keep it between the markers and idempotent.
-- reminders begin
CREATE TABLE IF NOT EXISTS reminders (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    text               TEXT NOT NULL CHECK (btrim(text) <> '' AND char_length(text) <= 500),
    due_at             TIMESTAMPTZ NOT NULL,
    timezone           TEXT NOT NULL CHECK (btrim(timezone) <> ''),
    channel_id         TEXT NOT NULL CHECK (channel_id ~ '^channel-(telegram|lark_bot|lark)(-[1-9][0-9]*)?$'),
    chat               TEXT NOT NULL CHECK (btrim(chat) <> ''),
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'sending', 'delivered', 'failed', 'uncertain', 'missed',
                                         'cancelled')),
    attempts           INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    detail             TEXT CHECK (char_length(detail) <= 500),
    external_id        TEXT,
    created_channel    TEXT NOT NULL CHECK (created_channel IN ('web', 'telegram', 'lark', 'lark_bot')),
    created_message_id TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at         TIMESTAMPTZ,
    CONSTRAINT reminders_settled CHECK ((status IN ('pending', 'sending')) = (settled_at IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (due_at) WHERE status IN ('pending', 'sending');
-- reminders end

-- The buttons of a card a chat got (assistant card actions RFC): one row per card, keyed by the opaque token that is
-- all its buttons carry; what they act on (a Manager's confirmation, a user gate), a digest of the parameters they are
-- bound to, the chat and the channel message it was sent as, who may press them and until when, and what became of
-- them. A confirmation typed in the chat and its
-- button resolve the same row (handled_via). Written only while cards.actions.enabled is on. connection.py applies
-- this block to databases that predate it, so keep it between the markers and idempotent.
-- card actions begin
CREATE TABLE IF NOT EXISTS card_actions (
    token       TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('confirmation', 'gate')),
    subject     TEXT NOT NULL,
    digest      TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    manager_id  TEXT,
    channel_id  TEXT,
    chat        TEXT,
    message_id  TEXT,
    allowed     TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    handled_at  TIMESTAMPTZ,
    handled_by  TEXT,
    handled_via TEXT CHECK (handled_via IN ('button', 'typed', 'lapsed')),
    choice      TEXT,
    outcome     TEXT CHECK (outcome IN ('running', 'ok', 'error', 'cancelled', 'lapsed', 'answered', 'stale')),
    CONSTRAINT card_actions_handled CHECK ((handled_at IS NULL) = (handled_via IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_card_actions_open ON card_actions (kind, manager_id, created_at)
    WHERE handled_at IS NULL;
-- card actions end

-- The operator's declarations of an MCP server's tools (the outbound-review RFC, section 6.2): a tool declared
-- 'inside' acts only inside Looper and this machine, and the review lets its calls run unchecked; every other tool of
-- a server other than Looper's own is reviewed. Declarations go with their server. connection.py applies this block
-- to databases that predate it, so keep it between the markers and idempotent.
-- mcp tool levels begin
CREATE TABLE IF NOT EXISTS mcp_tool_levels (
    item_id    TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
    tool       TEXT NOT NULL CHECK (tool ~ '^[A-Za-z0-9_.-]{1,128}$'),
    level      TEXT NOT NULL CHECK (level IN ('inside')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, tool)
);
-- mcp tool levels end

-- memory candidates begin
-- What Looper may remember (memory-write-path RFC, section 4; memory-tiers RFC, section 4.1). A 'vault' row is staged
-- until the operator confirms its exact content, then written to the knowledge vault; a 'looper' row is a lesson that
-- takes effect in this database only (the tiers RFC; nothing writes one yet). Code owns the transitions
-- (agents/memory_model.TRANSITIONS); these constraints keep what each state needs.
CREATE TABLE IF NOT EXISTS memory_candidates (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,           -- shown as M-0042
    kind               TEXT NOT NULL CHECK (kind IN ('preference', 'feedback', 'decision', 'lesson', 'fact',
                                                     'commitment', 'person', 'project')),
    source_kind        TEXT NOT NULL CHECK (source_kind IN ('manager_turn', 'task', 'lead')),   -- its first source
    -- The least trusted class among its sources (operator > system > agent > external), set by code.
    source_class       TEXT NOT NULL CHECK (source_class IN ('operator', 'agent', 'external', 'system')),
    destination        TEXT NOT NULL DEFAULT 'vault' CHECK (destination IN ('vault', 'looper')),
    state              TEXT NOT NULL DEFAULT 'drafting' CHECK (state IN (
                           'drafting', 'pending', 'confirmed', 'writing', 'written', 'rejected', 'expired',
                           'conflict', 'blocked', 'dropped',
                           'held', 'in_effect', 'revoked', 'superseded')),   -- the last four: 'looper' only
    reason             TEXT CHECK (reason IN (
                           'duplicate', 'rejected_before', 'same_as', 'invalid', 'secret', 'draft_failed',
                           'base_changed', 'anchor_missing', 'target_changed',
                           'vault_problem', 'not_git', 'detached_head', 'branch_mismatch', 'operation_in_progress',
                           'dirty_target', 'hook_rejected', 'git_failed', 'git_timeout', 'commit_mismatch',
                           'worktree_failed', 'interrupted',
                           'overwrite_gate', 'external_source', 'shadow', 'project_cap', 'flagged_wrong')),
    statement          TEXT NOT NULL CHECK (char_length(statement) <= 300),
    why                TEXT NOT NULL DEFAULT '' CHECK (char_length(why) <= 300),
    keywords           TEXT[] NOT NULL DEFAULT '{}' CHECK (cardinality(keywords) <= 6),
    fingerprint        TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),   -- kind + normalized statement
    project_id         TEXT REFERENCES projects(id) ON DELETE SET NULL,   -- a 'looper' row's goes with its project
    supersedes         BIGINT REFERENCES memory_candidates(id) ON DELETE SET NULL,
    valid_until        TIMESTAMPTZ,                      -- 'looper' only
    manager_id         TEXT REFERENCES managers(id) ON DELETE SET NULL,   -- the Manager that proposed it
    untrusted          BOOLEAN NOT NULL DEFAULT false,   -- its seed's text came from messages (a lead)
    flagged            BOOLEAN NOT NULL DEFAULT false,   -- its drafting listed instructions_seen
    hard_rule          BOOLEAN NOT NULL DEFAULT false,
    revision           INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),   -- the current revision; 0: none yet
    title              TEXT CHECK (char_length(title) <= 60),
    note_path          TEXT,                             -- the note it creates or replaces, vault-relative
    content_hash       TEXT CHECK (content_hash ~ '^[0-9a-f]{64}$'),   -- the current revision's
    confirmed_event_id BIGINT,
    confirmed_actor    TEXT CHECK (confirmed_actor = 'user'),
    confirmed_hash     TEXT,
    merged_into        BIGINT REFERENCES memory_candidates(id) ON DELETE SET NULL,
    attempts           INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),   -- writes of the current confirmation
    commit_sha         TEXT CHECK (commit_sha ~ '^[0-9a-f]{40,64}$'),
    expires_at         TIMESTAMPTZ,                      -- pending only
    purged_at          TIMESTAMPTZ,                      -- a source was deleted after it was written
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    written_at         TIMESTAMPTZ,
    CONSTRAINT memory_candidates_text CHECK (purged_at IS NOT NULL OR btrim(statement) <> ''),
    CONSTRAINT memory_candidates_content CHECK (destination = 'looper' OR state IN ('drafting', 'dropped')
                                                OR (revision >= 1 AND content_hash IS NOT NULL)),
    -- Two lifecycles, never mixed: a 'looper' row is a project's lesson with a validity, and has no revision,
    -- confirmation, note or commit.
    CONSTRAINT memory_candidates_tier CHECK (CASE destination
        WHEN 'vault' THEN state NOT IN ('held', 'in_effect', 'revoked', 'superseded') AND valid_until IS NULL
        ELSE state IN ('held', 'in_effect', 'revoked', 'superseded', 'expired', 'rejected')
             AND kind = 'lesson' AND project_id IS NOT NULL AND valid_until IS NOT NULL
             AND revision = 0 AND note_path IS NULL END),
    -- Nothing from outside or from the system takes effect on its own.
    CONSTRAINT memory_candidates_effect CHECK (state <> 'in_effect' OR source_class = 'agent'),
    CONSTRAINT memory_candidates_supersedes CHECK (supersedes IS NULL OR supersedes <> id),
    -- Confirmed exactly while a confirmation of the current content holds; the foreign key below makes it the
    -- operator's (MATCH SIMPLE, so the three columns go together).
    CONSTRAINT memory_candidates_confirmed CHECK (
        (confirmed_event_id IS NULL) = (confirmed_actor IS NULL)
        AND (confirmed_event_id IS NULL) = (confirmed_hash IS NULL)
        AND (state IN ('confirmed', 'writing', 'written', 'blocked')) = (confirmed_event_id IS NOT NULL)
        AND (confirmed_hash IS NULL OR confirmed_hash = content_hash)),
    CONSTRAINT memory_candidates_written CHECK ((state = 'written') = (commit_sha IS NOT NULL)
                                                AND (state = 'written') = (written_at IS NOT NULL)),
    CONSTRAINT memory_candidates_reason CHECK ((state IN ('dropped', 'conflict', 'blocked', 'held')) = (reason IS NOT NULL)),
    CONSTRAINT memory_candidates_merged CHECK (merged_into IS NULL OR (state = 'dropped' AND reason = 'same_as')),
    CONSTRAINT memory_candidates_expiry CHECK ((state = 'pending') = (expires_at IS NOT NULL)),
    CONSTRAINT memory_candidates_purged CHECK (purged_at IS NULL OR state = 'written')
);

-- One per change of content: the typed changes, with the paths and base sha256s code resolved.
CREATE TABLE IF NOT EXISTS memory_candidate_revisions (
    candidate_id  BIGINT NOT NULL REFERENCES memory_candidates(id) ON DELETE CASCADE,
    revision      INTEGER NOT NULL CHECK (revision >= 1),
    made_by       TEXT NOT NULL CHECK (made_by IN ('draft', 'redraft', 'operator')),
    changes       JSONB NOT NULL CHECK (jsonb_typeof(changes) = 'array'),   -- '[]' once purged
    content_hash  TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    title         TEXT NOT NULL CHECK (btrim(title) <> '' AND char_length(title) <= 60),
    diff_stats    JSONB NOT NULL CHECK (jsonb_typeof(diff_stats) = 'object'),   -- per path: added, removed lines
    large_removal BOOLEAN NOT NULL DEFAULT false,
    record_id     BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,    -- its drafting run
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (candidate_id, revision)
);

-- Only ever added. A confirmation is one of these, by the operator, naming the hash it confirmed.
CREATE TABLE IF NOT EXISTS memory_candidate_events (
    id            BIGSERIAL PRIMARY KEY,
    candidate_id  BIGINT NOT NULL REFERENCES memory_candidates(id) ON DELETE CASCADE,
    action        TEXT NOT NULL CHECK (action IN (
                      'propose', 'draft', 'edit', 'push', 'confirm', 'reject', 'expire', 'revive', 'claim', 'write',
                      'conflict', 'block', 'retry', 'recover', 'redraft', 'drop', 'merge',
                      'activate', 'hold', 'revoke', 'renew', 'reinforce', 'supersede', 'promote')),
    actor_kind    TEXT NOT NULL CHECK (actor_kind IN ('user', 'manager', 'planner', 'system')),
    via           TEXT NOT NULL CHECK (via IN ('dashboard', 'lark', 'telegram', 'cli', 'system')),
    revision      INTEGER,
    content_hash  TEXT CHECK (content_hash ~ '^[0-9a-f]{64}$'),   -- confirm, edit: confirmed or written; push: shown
    post_id       BIGINT,               -- a confirmation from a DM reply: the push it answered
    channel_id    TEXT,
    chat          TEXT,
    message_id    TEXT,                 -- a DM reply: the message that said it
    note          TEXT CHECK (char_length(note) <= 300),   -- a reject reason; a fixed failure text
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, actor_kind, content_hash),
    CONSTRAINT memory_events_user CHECK (action NOT IN ('confirm', 'reject', 'edit', 'revive', 'retry', 'revoke',
                                                        'renew', 'promote') OR actor_kind = 'user'),
    CONSTRAINT memory_events_confirm CHECK (action <> 'confirm'
                                            OR (content_hash IS NOT NULL AND via IN ('dashboard', 'lark', 'telegram'))),
    CONSTRAINT memory_events_activate CHECK (action <> 'activate' OR actor_kind = 'system'
                                             OR (actor_kind = 'user' AND via IN ('dashboard', 'lark', 'telegram'))),
    CONSTRAINT memory_events_system CHECK (action NOT IN ('hold', 'recover') OR actor_kind = 'system')
);

-- The evidence of a candidate: one row per source it was made or merged from.
CREATE TABLE IF NOT EXISTS memory_candidate_sources (
    id                 BIGSERIAL PRIMARY KEY,
    candidate_id       BIGINT NOT NULL REFERENCES memory_candidates(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL CHECK (kind IN ('manager_turn', 'journal', 'lead')),
    source_class       TEXT GENERATED ALWAYS AS (CASE kind WHEN 'manager_turn' THEN 'operator'
                           WHEN 'journal' THEN 'agent' WHEN 'lead' THEN 'external' END) STORED,
    manager_action_id  BIGINT REFERENCES manager_actions(id) ON DELETE CASCADE,   -- the propose call
    journal_id         BIGINT REFERENCES task_journal(id) ON DELETE CASCADE,
    lead_id            BIGINT REFERENCES message_leads(id) ON DELETE CASCADE,
    quote              TEXT CHECK (char_length(quote) <= 500),                    -- manager_turn: checked by code
    CONSTRAINT memory_sources_ref CHECK (
        (kind = 'manager_turn') = (manager_action_id IS NOT NULL)
        AND (kind = 'journal') = (journal_id IS NOT NULL)
        AND (kind = 'lead') = (lead_id IS NOT NULL)
        AND (quote IS NULL OR kind = 'manager_turn'))
);

-- One message Looper sent about a candidate, claimed before the channel is asked (as intent_posts).
CREATE TABLE IF NOT EXISTS memory_posts (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidate_id  BIGINT NOT NULL REFERENCES memory_candidates(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('push', 'final')),
    revision      INTEGER NOT NULL,
    content_hash  TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),   -- the revision it showed
    complete      BOOLEAN NOT NULL,     -- it showed every changed line: a DM「记」may confirm it
    channel_id    TEXT NOT NULL,
    chat          TEXT NOT NULL,
    text          TEXT NOT NULL,        -- '' once purged
    state         TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'sending', 'sent', 'failed', 'uncertain')),
    external_id   TEXT,
    edited_at     TIMESTAMPTZ,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What is left after the operator deletes a candidate: no text, no source.
CREATE TABLE IF NOT EXISTS memory_tombstones (
    fingerprint  TEXT PRIMARY KEY CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    kind         TEXT NOT NULL,
    destination  TEXT NOT NULL CHECK (destination IN ('vault', 'looper')),
    deleted_via  TEXT NOT NULL CHECK (deleted_via IN ('dashboard', 'cli')),
    deleted_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_candidates_open ON memory_candidates (state, updated_at DESC)
    WHERE state NOT IN ('written', 'rejected', 'dropped');
CREATE INDEX IF NOT EXISTS idx_memory_candidates_expiry ON memory_candidates (expires_at) WHERE state = 'pending';
-- One open candidate per note: a second seed for it revises the first.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_candidates_note ON memory_candidates (note_path)
    WHERE note_path IS NOT NULL AND state IN ('pending', 'confirmed', 'writing', 'conflict', 'blocked');
CREATE INDEX IF NOT EXISTS idx_memory_candidates_drafting ON memory_candidates (id) WHERE state = 'drafting';
CREATE INDEX IF NOT EXISTS idx_memory_candidates_fingerprint ON memory_candidates (fingerprint);
CREATE INDEX IF NOT EXISTS idx_memory_events_candidate ON memory_candidate_events (candidate_id, id);
CREATE INDEX IF NOT EXISTS idx_memory_sources_candidate ON memory_candidate_sources (candidate_id);
CREATE INDEX IF NOT EXISTS idx_memory_sources_action ON memory_candidate_sources (manager_action_id)
    WHERE manager_action_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_sources_journal ON memory_candidate_sources (journal_id)
    WHERE journal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_sources_lead ON memory_candidate_sources (lead_id) WHERE lead_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_posts_candidate ON memory_posts (candidate_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_posts_external ON memory_posts (channel_id, external_id)
    WHERE external_id IS NOT NULL;

DO $memory$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'memory_candidates'::regclass
                      AND conname = 'memory_candidates_confirm_fk') THEN
        ALTER TABLE memory_candidates ADD CONSTRAINT memory_candidates_confirm_fk
            FOREIGN KEY (confirmed_event_id, confirmed_actor, confirmed_hash)
            REFERENCES memory_candidate_events (id, actor_kind, content_hash);
    END IF;
END $memory$;

-- A deleted source takes a candidate that was not written yet (its text may restate the source); a written one keeps
-- only its paths, hashes, commit and times. A candidate deleted itself takes its sources, and this finds no row.
CREATE OR REPLACE FUNCTION memory_candidates_source_deleted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM memory_candidates WHERE id = OLD.candidate_id AND state <> 'written';
    UPDATE memory_candidates SET statement = '', why = '', keywords = '{}', purged_at = now(), updated_at = now()
        WHERE id = OLD.candidate_id AND purged_at IS NULL;
    UPDATE memory_candidate_revisions SET changes = '[]' WHERE candidate_id = OLD.candidate_id;
    UPDATE memory_posts SET text = '', updated_at = now() WHERE candidate_id = OLD.candidate_id;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS memory_candidate_sources_drop ON memory_candidate_sources;
CREATE TRIGGER memory_candidate_sources_drop AFTER DELETE ON memory_candidate_sources
    FOR EACH ROW EXECUTE FUNCTION memory_candidates_source_deleted();

-- A 'looper' row must have its project: a deleted project takes its lessons (a 'vault' row only loses the link).
CREATE OR REPLACE FUNCTION memory_candidates_project_deleted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM memory_candidates WHERE project_id = OLD.id AND destination = 'looper';
    RETURN OLD;
END $$;
DROP TRIGGER IF EXISTS projects_memory_lessons_drop ON projects;
CREATE TRIGGER projects_memory_lessons_drop BEFORE DELETE ON projects
    FOR EACH ROW EXECUTE FUNCTION memory_candidates_project_deleted();
-- memory candidates end

-- The uncertain deliveries the "needs you" summary lists (agents/attention.py): a partial index each, so it reads
-- only those rows, never the tables' history. A database that predates them builds them online
-- (connection.py ``_ONLINE_STEPS``).
CREATE INDEX IF NOT EXISTS idx_subscription_deliveries_uncertain ON subscription_deliveries (updated_at)
    WHERE state = 'uncertain';
CREATE INDEX IF NOT EXISTS idx_intent_posts_uncertain ON intent_posts (updated_at) WHERE state = 'uncertain';
CREATE INDEX IF NOT EXISTS idx_manager_events_uncertain ON manager_events (created_at) WHERE delivery = 'uncertain';
