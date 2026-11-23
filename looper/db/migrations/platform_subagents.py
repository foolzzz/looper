"""Persist explorer reservations and the run tree without enabling an entry point."""

from . import OnlineStep

name = "platform_subagents"
order = 650
pending_order = 650

# Explorer service outcomes also include local start_failure and runtime causes;
# keep those explicit alongside the shared classifier's declared pairs.
_FAILURE = """(failure_class IS NULL AND failure_cause IS NULL) OR
    (failure_class IS NOT NULL AND failure_cause IS NOT NULL AND (
       (failure_class = 'quota' AND failure_cause IN ('usage_limit', 'rate_limit')) OR
       (failure_class = 'transient' AND failure_cause IN ('connection_failed', 'provider_overloaded', 'start_timeout')) OR
       (failure_class = 'environment' AND failure_cause IN ('runtime_missing', 'login', 'tls', 'profile', 'start_failure', 'runtime')) OR
       (failure_class = 'judgment' AND failure_cause IN ('timeout', 'context', 'budget', 'report', 'unknown'))))"""

_DDL = """
ALTER TABLE agent_spawns
    ADD COLUMN idempotency_key TEXT,
    ADD COLUMN spawner TEXT,
    ADD COLUMN profile_id TEXT,
    ADD COLUMN delivery TEXT,
    ADD COLUMN delivered_at TIMESTAMPTZ,
    ADD COLUMN cancel_reason TEXT,
    ADD COLUMN failure_class TEXT,
    ADD COLUMN failure_cause TEXT,
    ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN child_process JSONB,
    ADD COLUMN parent_spend_known BOOLEAN;
ALTER TABLE agent_spawns
    DROP CONSTRAINT agent_spawns_kind_check,
    DROP CONSTRAINT agent_spawns_state_check,
    DROP CONSTRAINT agent_spawns_finished,
    ADD CONSTRAINT agent_spawns_kind_check CHECK (kind IN ('advisor', 'explorer')) NOT VALID,
    ADD CONSTRAINT agent_spawns_state_check CHECK (state IN ('drafting', 'linting', 'asking', 'needs_info',
        'answered', 'blocked', 'failed', 'over_budget', 'queued', 'finishing', 'cancelling', 'cancelled')) NOT VALID,
    ADD CONSTRAINT agent_spawns_finished CHECK ((state IN ('answered', 'blocked', 'failed', 'over_budget', 'cancelled'))
        = (finished_at IS NOT NULL)) NOT VALID,
    ADD CONSTRAINT agent_spawns_idempotency_key_check CHECK (length(idempotency_key) BETWEEN 1 AND 200) NOT VALID,
    ADD CONSTRAINT agent_spawns_delivery_check CHECK (delivery IN ('tool', 'room', 'turn')) NOT VALID,
    ADD CONSTRAINT agent_spawns_cancel_reason_check CHECK (cancel_reason IN
        ('requested', 'parent_ended', 'step_dropped', 'task_ended', 'operator', 'server')) NOT VALID,
    ADD CONSTRAINT agent_spawns_cancel_reason CHECK ((state IN ('cancelling', 'cancelled'))
        = (cancel_reason IS NOT NULL)) NOT VALID,
    ADD CONSTRAINT agent_spawns_attempt_check CHECK (attempt BETWEEN 1 AND 2) NOT VALID,
    ADD CONSTRAINT agent_spawns_child_process_check CHECK (child_process IS NULL OR
        jsonb_typeof(child_process) = 'object') NOT VALID,
    ADD CONSTRAINT agent_spawns_failure_check CHECK (FAILURE_CHECK) NOT VALID;
ALTER TABLE projects
    ADD COLUMN subagents TEXT,
    ADD COLUMN subagent_budget_usd DOUBLE PRECISION,
    ADD COLUMN subagent_task_budget_usd DOUBLE PRECISION,
    ADD COLUMN subagents_per_run INTEGER,
    ADD COLUMN subagents_per_task INTEGER,
    ADD CONSTRAINT projects_subagents_check CHECK (subagents IN ('off', 'on')) NOT VALID,
    ADD CONSTRAINT projects_subagent_budget_usd_check CHECK (subagent_budget_usd BETWEEN 0.01 AND 5) NOT VALID,
    ADD CONSTRAINT projects_subagent_task_budget_usd_check CHECK (subagent_task_budget_usd BETWEEN 0.01 AND 50) NOT VALID,
    ADD CONSTRAINT projects_subagents_per_run_check CHECK (subagents_per_run BETWEEN 0 AND 20) NOT VALID,
    ADD CONSTRAINT projects_subagents_per_task_check CHECK (subagents_per_task BETWEEN 0 AND 100) NOT VALID;
ALTER TABLE llm_records DROP CONSTRAINT llm_records_run_kind_check,
    ADD CONSTRAINT llm_records_run_kind_check CHECK (run_kind IN ('manager', 'planner', 'worker', 'reviewer',
        'processor', 'consult', 'advisor', 'advice_check', 'outbound_check', 'explorer')) NOT VALID;
ALTER TABLE agent_setting_layers DROP CONSTRAINT agent_setting_layers_role_check,
    ADD CONSTRAINT agent_setting_layers_role_check CHECK (role IN ('manager', 'planner', 'worker', 'reviewer',
        'processor', 'advisor', 'advice_check', 'leads_check', 'outbound_check', 'explorer')) NOT VALID;
ALTER TABLE task_concurrency_limits DROP CONSTRAINT task_concurrency_limits_role_check,
    ADD CONSTRAINT task_concurrency_limits_role_check CHECK (role IN ('worker', 'reviewer', 'subagent')) NOT VALID;
""".replace("FAILURE_CHECK", _FAILURE)

async def pending(conn):
    if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE name=$1)", name):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='agent_spawns' AND column_name='idempotency_key')")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(_DDL)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", name)


online_order = 140
online = (OnlineStep(
    migration=name, marker="platform_subagents_online",
    validate=tuple(("agent_spawns", constraint) for constraint in (
        "agent_spawns_kind_check", "agent_spawns_state_check", "agent_spawns_finished",
        "agent_spawns_idempotency_key_check", "agent_spawns_delivery_check", "agent_spawns_cancel_reason_check",
        "agent_spawns_cancel_reason", "agent_spawns_attempt_check", "agent_spawns_child_process_check",
        "agent_spawns_failure_check")) + tuple(("projects", constraint) for constraint in (
        "projects_subagents_check", "projects_subagent_budget_usd_check", "projects_subagent_task_budget_usd_check",
        "projects_subagents_per_run_check", "projects_subagents_per_task_check")) + (
        ("llm_records", "llm_records_run_kind_check"),
        ("agent_setting_layers", "agent_setting_layers_role_check"),
        ("task_concurrency_limits", "task_concurrency_limits_role_check")),
    unique_indexes=(("idx_agent_spawns_key", "ON agent_spawns (parent_record_id, idempotency_key) "
                     "WHERE idempotency_key IS NOT NULL"),),
),)

# New empty tables need neither a concurrent build nor migration markers. The
# upgrade path validates and builds after commit through the resumable runner.
fresh_ddl = _DDL + "\n" + "\n".join(
    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint};"
    for step in online for table, constraint in step.validate
) + "\nCREATE UNIQUE INDEX idx_agent_spawns_key ON agent_spawns " \
    "(parent_record_id, idempotency_key) WHERE idempotency_key IS NOT NULL;\n"
