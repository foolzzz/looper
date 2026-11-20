"""Async connection pool and schema bootstrap."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from looper.agents.model_names import MODEL_MAX_CHARS, MODEL_NAME_PATTERN

from .timing import record_query

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_fenced = False
_FENCED_MESSAGE = "the database pool is fenced: this control plane lost its lock"

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SEEDS_PATH = Path(__file__).with_name("seeds.sql")


async def _check_connection_fence(conn: asyncpg.Connection) -> None:
    # asyncpg < 0.32 can finish connecting after Pool.terminate(). setup runs
    # before every checkout; raising here also makes asyncpg close the connection.
    if _fenced:
        raise RuntimeError(_FENCED_MESSAGE)


async def _init_connection(conn: asyncpg.Connection) -> None:
    conn.add_query_logger(record_query)


async def init_pool(dsn: str, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    """The pool for ``dsn``, the ops file's ``database.url`` (``looper.ops_config``)."""
    global _pool
    if _fenced:
        raise RuntimeError(_FENCED_MESSAGE)
    pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size,
                                     init=_init_connection, setup=_check_connection_fence)
    # Loss can happen while create_pool is connecting, before _pool exists.
    if _fenced:
        pool.terminate()
        raise RuntimeError(_FENCED_MESSAGE)
    _pool = pool
    logger.info("database pool created (%s connections)", max_size)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("database pool closed")


def fence() -> None:
    """Permanently revoke every pooled connection in this process, without awaiting anything."""
    global _fenced
    _fenced = True
    if _pool is not None:
        _pool.terminate()


def get_pool() -> asyncpg.Pool:
    if _fenced:
        raise RuntimeError(_FENCED_MESSAGE)
    if _pool is None:
        raise RuntimeError("database pool not initialised — call init_pool() first")
    return _pool


@asynccontextmanager
async def transaction(*, on_commit: Callable[[], None] | None = None) -> AsyncIterator[asyncpg.Connection]:
    """Commit on exit; notify synchronously before releasing the pooled connection."""
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            yield conn
        if on_commit is not None:
            on_commit()


# The schema.sql text last applied. Schema 3 uses this stable release marker
# for admission; schema 4 uses its version and post_bridge_baseline marker.
_RECORD_FINGERPRINT = """
INSERT INTO schema_fingerprint (id, sha256) VALUES (TRUE, '{sha256}')
ON CONFLICT (id) DO UPDATE SET sha256 = EXCLUDED.sha256, applied_at = now()
WHERE schema_fingerprint.sha256 <> EXCLUDED.sha256;
"""


def schema_fingerprint(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


# Serializes schema runs on one database (any schema in it): the script runs as one transaction, which takes
# this lock first, so two processes starting together cannot both create a table or migrate.
_SCHEMA_LOCK = 0x6C6F6F70  # "loop"
# Session lock, distinct from the schema transaction's lock: online DDL must
# stay in autocommit, including while another process builds an index.
_ONLINE_LOCK = 0x6C6F6F706F6E6C  # "looponl", database-wide across schemas
_ONLINE_LOCK_WAIT_SECONDS = 5.0

REGISTRY_SCHEMA_VERSION = 4
_SCHEDULE_USER_GATES = "schedule_user_gates"
_SCHEDULE_NOTIFY_FORMAT = "schedule_notify_format"
_SCHEDULE_SESSION_BOUNDS = "schedule_session_bounds"
_RUNTIME_LAUNCHER = "runtime_launcher"
_PROFILE_FALLBACK = "profile_fallback"
_PROFILE_WEB_SEARCH = "profile_web_search"
_CURSOR_LOGIN = "cursor_login"
_COCO_LOGIN = "coco_login"
_DEPENDENCY_CONDITIONS = "dependency_conditions"
_WORKFLOWS = "workflows"
_REVIEW_PANEL = "review_panel"
_WORKFLOW_RULES = "workflow_rules"
_GATE_PURPOSE = "gate_purpose"
_VERIFICATION_PATHS = "verification_paths"
_COMMAND_SCHEDULES = "command_schedules"
_TASK_REPLY_ROUTE = "task_reply_route"
_TASK_PROCESS_RECORD = "task_process_record"
_CHAT_BINDING_SENDERS = "chat_binding_senders"
_CHAT_BINDING_SENDER_POLICY = "chat_binding_sender_policy"
_PROVIDER_ENABLED = "provider_enabled"
_MANAGER_SCOPE_PROJECTS = "manager_scope_projects"
_SCHEDULE_MISSED_SINCE = "schedule_missed_since"
_SCHEDULE_DEFERRED_BY_DRAIN = "schedule_deferred_by_drain"
_TASK_CHAT_GROUP = "task_chat_group"
_PROCESSOR_ROLE = "processor_role"
_HEADLESS_COMPACTION = "headless_compaction"
_GATEWAY_ALIASES = "gateway_aliases"
_MESSAGE_STREAM = "message_stream"
_MESSAGE_STREAM_DELETIONS = "message_stream_deletions"
_SUBSCRIPTIONS = "subscriptions"
_SUBSCRIPTION_RULES = "subscription_rules"
_SUBSCRIPTION_LOOKBACK = "subscription_lookback"
_MANAGER_EVENTS = "manager_events"
_PROACTIVE_DELIVERIES = "proactive_deliveries"
_SCHEDULE_NOTIFICATION_EVENTS = "schedule_notification_events"
_MANAGER_EVENT_REASONS_MIGRATION = "manager_event_reasons"
_MANAGER_EVENT_DELIVERY = "manager_event_delivery"
_SCHEDULE_NOTIFICATION_EVENTS_ONLINE = "schedule_notification_events_online"
# The reasons a completion event may record (schema.sql's manager events block lists the same).
_MANAGER_EVENT_REASONS = ("off", "group_chat", "manager_off", "turn_failed", "turn_timeout", "turn_stopped",
                          "empty_reply", "send_failed", "queue_deadline", "restart", "no_chat", "target_changed")
_RUN_TREE = "run_tree"
_AGENT_SPAWNS = "agent_spawns"
_STEP_DIFFICULTY = "step_difficulty"
_ADVISOR_ROLES = "advisor_roles"
_PROJECT_ADVISOR = "project_advisor"
_PROJECT_ACTIVITY = "project_activity"
_LEDGER = "ledger"
_MESSAGE_LEADS = "message_leads"
_INTENT_MERGES = "intent_merges"
_LEADS_CHECK = "leads_check"
_LEADS_CHECK_ONLINE = "leads_check_online"
_LLM_REQUESTS = "llm_requests"
_LLM_REQUESTS_TRANSLATION = "llm_requests_translation"
_LLM_MODELS_CHAT_DIALECT = "llm_models_chat_dialect"
_MODEL_NAME_PARAMETERS = "model_name_parameters"
_LLM_REQUESTS_STORED_BYTES = "llm_requests_stored_bytes"

_LLM_REQUESTS_DDL = """
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
"""


_MANAGER_SCOPE_PROJECTS_DDL: tuple[str, ...] = (
    "ALTER TABLE managers DROP CONSTRAINT managers_task_scope_check, "
    "ADD CONSTRAINT managers_task_scope_check CHECK (task_scope IN ('all', 'own', 'projects'))",
    "ALTER TABLE managers ADD COLUMN scope_project_ids TEXT[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE managers ADD CONSTRAINT managers_scope_projects_check CHECK (cardinality(scope_project_ids) <= 20 "
    "AND (task_scope = 'projects' OR cardinality(scope_project_ids) = 0))",
    "ALTER TABLE managers ADD CONSTRAINT managers_scope_tools_check CHECK (task_scope <> 'projects' OR tools = 'tasks')",
)

_MESSAGE_STREAM_BLOCK = re.compile(r"-- message stream begin\n(.*?)-- message stream end", re.S)
_SOURCE_DELETIONS_BLOCK = re.compile(r"-- source deletions begin\n(.*?)-- source deletions end", re.S)
_SUBSCRIPTIONS_BLOCK = re.compile(r"-- subscriptions begin\n(.*?)-- subscriptions end", re.S)
_PROACTIVE_DELIVERIES_BLOCK = re.compile(r"-- proactive deliveries begin\n(.*?)-- proactive deliveries end", re.S)
_MANAGER_EVENTS_BLOCK = re.compile(r"-- manager events begin\n(.*?)-- manager events end", re.S)
_AGENT_SPAWNS_BLOCK = re.compile(r"-- agent spawns begin\n(.*?)-- agent spawns end", re.S)


def _message_stream_ddl() -> str:
    """The message stream tables as schema.sql defines them, so a database that predates them gets the same text."""
    found = _MESSAGE_STREAM_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its message stream block")
    return found.group(1)


def _source_deletions_ddl() -> str:
    """Only the additive tombstone tables: replaying source_events indexes holds a writer-blocking lock."""
    found = _SOURCE_DELETIONS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its source deletions block")
    return found.group(1)


def _agent_spawns_ddl() -> str:
    """The spawn and advice tables as schema.sql defines them, so a database that predates them gets the same text."""
    found = _AGENT_SPAWNS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its agent spawns block")
    return found.group(1)


def _run_tree_ddl() -> tuple[str, ...]:
    """The run tree columns of llm_records, as schema.sql defines them, added without a scan or a rewrite: llm_records
    is large, and these statements hold its ACCESS EXCLUSIVE lock until the schema transaction commits. No older row
    is updated: its run_kind stays NULL, and readers take its kind from its agent id (``llm_records.RUN_KIND``). The
    constraints come NOT VALID (every older row is NULL, so they hold) and the index comes later, both in
    ``_ONLINE_STEPS``."""
    from .llm_records import RUN_KINDS

    kinds = ", ".join(f"'{kind}'" for kind in RUN_KINDS)
    return (
        "ALTER TABLE llm_records ADD COLUMN parent_record_id BIGINT, ADD COLUMN run_kind TEXT",
        f"ALTER TABLE llm_records ADD CONSTRAINT llm_records_run_kind_check CHECK (run_kind IN ({kinds})) NOT VALID, "
        "ADD CONSTRAINT llm_records_parent_record_id_fkey FOREIGN KEY (parent_record_id) REFERENCES llm_records(id) "
        "ON DELETE SET NULL NOT VALID",
    )


@dataclass(frozen=True)
class _OnlineStep:
    """What a migration finishes after the schema transaction commits, taking no lock that stops reads or writes: it
    validates its NOT VALID constraints (SHARE UPDATE EXCLUSIVE) and builds its indexes concurrently (an index a
    stopped build left invalid is dropped first). It runs once ``migration``'s marker is there and until its own
    ``marker`` is: a start stopped halfway resumes it, and a finished one never runs it again."""

    migration: str
    marker: str
    validate: tuple[tuple[str, str], ...] = ()  # (table, constraint)
    indexes: tuple[tuple[str, str], ...] = ()  # (name, what follows the name in CREATE INDEX)


_ONLINE_STEPS = (
    _OnlineStep(_RUN_TREE, "run_tree_online",
                validate=(("llm_records", "llm_records_run_kind_check"),
                          ("llm_records", "llm_records_parent_record_id_fkey")),
                indexes=(("idx_llm_records_parent",
                          "ON llm_records(parent_record_id) WHERE parent_record_id IS NOT NULL"),)),
    _OnlineStep(_STEP_DIFFICULTY, "step_difficulty_online",
                validate=(("task_steps", "task_steps_difficulty_check"),)),
    # The outbox can be large: its new foreign key is validated, and its index (for ON DELETE SET NULL) built,
    # without blocking writes.
    _OnlineStep(_SCHEDULE_NOTIFICATION_EVENTS, _SCHEDULE_NOTIFICATION_EVENTS_ONLINE,
                validate=(("schedule_notifications", "schedule_notifications_manager_event_id_fkey"),),
                indexes=(("idx_schedule_notifications_manager_event",
                          "ON schedule_notifications (manager_event_id) WHERE manager_event_id IS NOT NULL"),)),
    _OnlineStep(_MANAGER_EVENT_REASONS_MIGRATION, "manager_event_reasons_online",
                validate=(("manager_events", "manager_events_reason_check"),)),
    _OnlineStep(_MANAGER_EVENT_DELIVERY, "manager_event_delivery_online",
                validate=(("manager_events", "manager_events_delivery_check"),)),
    # The leads check block adds these NOT VALID (a fresh database validates them as it is created).
    _OnlineStep(_LEADS_CHECK, _LEADS_CHECK_ONLINE,
                validate=(("agent_setting_layers", "agent_setting_layers_role_check"),
                          ("subscription_runs", "subscription_runs_check_status_check"),
                          ("schedule_notifications", "schedule_notifications_kind_check"),
                          *((table, f"{table}_{name}") for table in ("message_leads", "intents")
                            for name in ("state_check", "check_verdict_check", "check_reason_check",
                                         "check_record_id_fkey", "dropped")))),
    _OnlineStep(_HEADLESS_COMPACTION, "headless_compaction_online",
                validate=(("agent_runtimes", "agent_runtimes_capabilities_check"),)),
)


async def _finish_online(conn: asyncpg.Connection) -> None:
    """Serialize online DDL without holding a transaction while waiting.

    After five seconds of contention, defer to the owner or the next start;
    markers stay absent until a step finishes. A caller's open transaction also
    defers these steps: even waiting for this lock could block the owner's CIC.
    """
    if conn.is_in_transaction():
        logger.warning("deferring online schema steps until a start outside a transaction")
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ONLINE_LOCK_WAIT_SECONDS
    while not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _ONLINE_LOCK):
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning("online schema lock busy after %.1fs; deferring steps to the owner or next start",
                           _ONLINE_LOCK_WAIT_SECONDS)
            return
        # A blocking advisory-lock SELECT would leave an active transaction
        # whose snapshot a concurrent index build could itself be waiting for.
        await asyncio.sleep(min(0.1, remaining))
    try:
        # The previous owner may have finished while we waited.
        done = {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}
        for step in _ONLINE_STEPS:
            if step.migration not in done or step.marker in done:
                continue
            for table, constraint in step.validate:
                await conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")
            for name, definition in step.indexes:
                await conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                await conn.execute(f"CREATE INDEX CONCURRENTLY {name} {definition}")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", step.marker)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _ONLINE_LOCK)


def _subscriptions_ddl() -> str:
    """The subscription tables as schema.sql defines them, after the widened ``schedules.kind`` check."""
    found = _SUBSCRIPTIONS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its subscriptions block")
    return ("ALTER TABLE schedules DROP CONSTRAINT IF EXISTS schedules_kind_check, "
            "ADD CONSTRAINT schedules_kind_check CHECK (kind IN ('agent', 'command', 'subscription'));\n"
            + found.group(1))


_LEDGER_BLOCK = re.compile(r"-- ledger begin\n(.*?)-- ledger end", re.S)
_MESSAGE_LEADS_BLOCK = re.compile(r"-- message leads begin\n(.*?)-- message leads end", re.S)


_INTENT_MERGES_BLOCK = re.compile(r"-- intent merges begin\n(.*?)-- intent merges end", re.S)


def _intent_merges_ddl() -> str:
    """How a merged intent survives the intent it was merged into, as schema.sql defines it."""
    found = _INTENT_MERGES_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its intent merges block")
    return found.group(1)


_LEADS_CHECK_BLOCK = re.compile(r"-- leads check begin\n(.*?)-- leads check end", re.S)


def _leads_check_ddl() -> str:
    """The second model's check of leads and intents, as schema.sql defines it."""
    found = _LEADS_CHECK_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its leads check block")
    return found.group(1)


def _message_leads_ddl() -> str:
    """The leads and intents tables as schema.sql defines them, so a database that predates them gets the same text."""
    found = _MESSAGE_LEADS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its message leads block")
    return found.group(1)


def _ledger_ddl() -> str:
    """The ledger tables as schema.sql defines them, so a database that predates them gets the same text."""
    found = _LEDGER_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its ledger block")
    return found.group(1)


def _manager_events_ddl() -> str:
    """The completion event table as schema.sql defines it, so a database that predates it gets the same text."""
    found = _MANAGER_EVENTS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its manager events block")
    return found.group(1)


def _proactive_deliveries_ddl() -> str:
    found = _PROACTIVE_DELIVERIES_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its proactive deliveries block")
    return found.group(1)


_GATEWAY_ALIASES_DDL = """
CREATE TABLE IF NOT EXISTS gateway_aliases (
    alias TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES llm_models(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_PROJECT_ACTIVITY_DDL = """
CREATE TABLE IF NOT EXISTS project_activity (
    seq BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_project_activity_project ON project_activity (project_id, seq);
"""

_COMMAND_SCHEDULES_DDL: tuple[str, ...] = (
    """ALTER TABLE schedules
        ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'agent' CHECK (kind IN ('agent', 'command')),
        ADD COLUMN IF NOT EXISTS command TEXT,
        ADD COLUMN IF NOT EXISTS env JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(env) = 'object'),
        ADD COLUMN IF NOT EXISTS env_file TEXT,
        ADD COLUMN IF NOT EXISTS success_deadline TEXT CHECK (success_deadline ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
        ADD COLUMN IF NOT EXISTS alert_failures_after INTEGER CHECK (alert_failures_after BETWEEN 1 AND 1000),
        ADD COLUMN IF NOT EXISTS alert_stale_after_seconds INTEGER CHECK (alert_stale_after_seconds BETWEEN 300 AND 2592000),
        ADD COLUMN IF NOT EXISTS alert_overlap_after INTEGER CHECK (alert_overlap_after BETWEEN 0 AND 10000),
        ADD COLUMN IF NOT EXISTS consecutive_overlaps INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_overlaps >= 0),
        ADD COLUMN IF NOT EXISTS deadline_alert_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS overlap_alert_at TIMESTAMPTZ""",
    """ALTER TABLE schedules ADD CONSTRAINT schedules_kind_fields_check CHECK (
        (kind = 'command') = (command IS NOT NULL)
        AND (kind = 'command' OR (env = '{}'::jsonb AND env_file IS NULL))
        AND (kind = 'agent' OR (gate_command IS NULL AND template_key IS NULL)))""",
    """ALTER TABLE schedule_runs
        ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS exit_code INTEGER,
        ADD COLUMN IF NOT EXISTS exit_signal INTEGER,
        ADD COLUMN IF NOT EXISTS log_path TEXT,
        ADD COLUMN IF NOT EXISTS log_bytes BIGINT,
        ADD COLUMN IF NOT EXISTS log_truncated BOOLEAN NOT NULL DEFAULT false,
        ADD COLUMN IF NOT EXISTS log_tail TEXT,
        ADD COLUMN IF NOT EXISTS runner_pid INTEGER,
        ADD COLUMN IF NOT EXISTS runner_started TEXT""",
    """ALTER TABLE schedule_runs DROP CONSTRAINT schedule_runs_status_check,
        ADD CONSTRAINT schedule_runs_status_check CHECK (status IN (
            'queued', 'gating', 'running', 'completed', 'failed', 'stopped', 'cancelled', 'timed_out',
            'gate_failed', 'skipped_overlap', 'skipped_missed', 'skipped_gate', 'skipped_budget',
            'succeeded', 'interrupted'))""",
    """ALTER TABLE schedule_notifications DROP CONSTRAINT schedule_notifications_kind_check,
        ADD CONSTRAINT schedule_notifications_kind_check CHECK (kind IN (
            'run', 'failures_alert', 'failures_recovered', 'stale_alert', 'stale_recovered',
            'deadline_alert', 'deadline_recovered', 'overlap_alert', 'overlap_recovered'))""",
    "CREATE INDEX IF NOT EXISTS idx_schedule_runs_finished ON schedule_runs (finished_at) WHERE finished_at IS NOT NULL",
)


# The processor role: its layers, its schedule's choice of profile, and the no_tools capability. No runtime row
# changes here: startup refreshes the built-in Claude Code runtime's capabilities (``register_found_runtimes``), and a
# runtime the operator registered gets it when probed again.
_PROCESSOR_ROLE_DDL = """
ALTER TABLE agent_runtimes DROP CONSTRAINT agent_runtimes_capabilities_check,
    ADD CONSTRAINT agent_runtimes_capabilities_check CHECK (capabilities <@ ARRAY['run', 'resident_session', 'resume',
        'structured_output', 'claude_plugins', 'mcp', 'add_dirs', 'compaction_events', 'autonomous_turns', 'cost_report',
        'read_tools', 'steer', 'no_tools']);
ALTER TABLE agent_setting_layers DROP CONSTRAINT agent_setting_layers_role_check,
    ADD CONSTRAINT agent_setting_layers_role_check CHECK (role IN ('manager', 'planner', 'worker', 'reviewer', 'processor'));
ALTER TABLE schedule_profile_overrides DROP CONSTRAINT schedule_profile_overrides_role_check,
    ADD CONSTRAINT schedule_profile_overrides_role_check CHECK (role IN ('planner', 'worker', 'reviewer', 'processor'));
"""


_HEADLESS_COMPACTION_DDL = """
ALTER TABLE agent_runtimes DROP CONSTRAINT agent_runtimes_capabilities_check,
    ADD CONSTRAINT agent_runtimes_capabilities_check CHECK (capabilities <@ ARRAY['run', 'resident_session', 'resume',
        'structured_output', 'claude_plugins', 'mcp', 'add_dirs', 'compaction_events', 'autonomous_turns', 'cost_report',
        'read_tools', 'steer', 'no_tools', 'headless_compaction']) NOT VALID;
"""


# The advisor and its judge: their role layers. No row changes.
_ADVISOR_ROLES_DDL = """
ALTER TABLE agent_setting_layers DROP CONSTRAINT agent_setting_layers_role_check,
    ADD CONSTRAINT agent_setting_layers_role_check CHECK (role IN ('manager', 'planner', 'worker', 'reviewer', 'processor',
        'advisor', 'advice_check'));
"""


# The advisor's project switch and limits, as schema.sql defines them.
_PROJECT_ADVISOR_DDL = (
    "ALTER TABLE projects ADD COLUMN advisor TEXT CHECK (advisor IN ('off', 'on')), "
    "ADD COLUMN advisor_consult_budget_usd DOUBLE PRECISION "
    "CHECK (advisor_consult_budget_usd >= 0.01 AND advisor_consult_budget_usd <= 5), "
    "ADD COLUMN advisor_budget_usd DOUBLE PRECISION CHECK (advisor_budget_usd >= 0.01 AND advisor_budget_usd <= 50), "
    "ADD COLUMN advisor_consults_per_task INTEGER "
    "CHECK (advisor_consults_per_task >= 0 AND advisor_consults_per_task <= 100), "
    "ADD COLUMN advisor_consults_per_step INTEGER "
    "CHECK (advisor_consults_per_step >= 0 AND advisor_consults_per_step <= 10), "
    "ADD COLUMN advisor_fallback_profile TEXT CHECK (length(advisor_fallback_profile) BETWEEN 1 AND 200), "
    "ADD COLUMN advisor_internal_domains TEXT[] CHECK (cardinality(advisor_internal_domains) <= 20)"
)


async def _project_advisor_pending(conn: asyncpg.Connection) -> bool:
    return await _column_pending(conn, _PROJECT_ADVISOR, "projects", "advisor")


async def _advisor_roles_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _ADVISOR_ROLES):
        return False
    # Fresh baselines already admit the advisor's roles; only the released CHECK needs widening.
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('agent_setting_layers') "
        "AND conname = 'agent_setting_layers_role_check' "
        "AND strpos(pg_get_constraintdef(oid), $1) = 0)", "'advice_check'")


async def _processor_role_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _PROCESSOR_ROLE):
        return False
    # Fresh baselines already admit the processor; only the released CHECK needs widening.
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('agent_setting_layers') "
        "AND conname = 'agent_setting_layers_role_check' "
        "AND strpos(pg_get_constraintdef(oid), $1) = 0)", "'processor'")


async def _headless_compaction_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _HEADLESS_COMPACTION):
        return False
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('agent_runtimes') "
        "AND conname = 'agent_runtimes_capabilities_check' "
        "AND strpos(pg_get_constraintdef(oid), $1) = 0)", "'headless_compaction'")


_CURSOR_LOGIN_DDL = """
ALTER TABLE llm_providers
    DROP CONSTRAINT llm_providers_vendor_type_check,
    DROP CONSTRAINT llm_providers_check1,
    ADD CONSTRAINT llm_providers_vendor_type_check CHECK (vendor_type IN ('glm-bigmodel', 'glm-zai', 'deepseek', 'kimi-moonshot-cn', 'kimi-moonshot',
                          'cpa', 'opencode', 'sub2api', 'claude-subscription', 'codex-login', 'grok-login', 'cursor-login', 'custom')),
    ADD CONSTRAINT llm_providers_check1 CHECK (
        (vendor_type = 'claude-subscription' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'claude-code')
        OR (vendor_type = 'codex-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'codex')
        OR (vendor_type = 'grok-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'grok')
        OR (vendor_type = 'cursor-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'cursor-agent')
        OR (vendor_type NOT IN ('claude-subscription', 'codex-login', 'grok-login', 'cursor-login')
            AND auth = 'api_key' AND oauth_runtime_type IS NULL)
    );
"""


_COCO_LOGIN_DDL = """
ALTER TABLE llm_providers
    DROP CONSTRAINT llm_providers_vendor_type_check,
    DROP CONSTRAINT llm_providers_check1,
    ADD CONSTRAINT llm_providers_vendor_type_check CHECK (vendor_type IN ('glm-bigmodel', 'glm-zai', 'deepseek', 'kimi-moonshot-cn', 'kimi-moonshot',
                          'cpa', 'opencode', 'sub2api', 'claude-subscription', 'codex-login', 'grok-login', 'cursor-login', 'coco-login', 'custom')),
    ADD CONSTRAINT llm_providers_check1 CHECK (
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
        OR (vendor_type NOT IN ('claude-subscription', 'codex-login', 'grok-login', 'cursor-login', 'coco-login')
            AND auth = 'api_key' AND oauth_runtime_type IS NULL)
    );
"""


async def _coco_login_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _COCO_LOGIN):
        return False
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('llm_providers') "
        "AND conname = 'llm_providers_vendor_type_check' "
        "AND strpos(pg_get_constraintdef(oid), $1) = 0)", "'coco-login'")


async def _cursor_login_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _CURSOR_LOGIN):
        return False
    # Fresh baselines already admit Cursor; only the released CHECK needs widening.
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('llm_providers') "
        "AND conname = 'llm_providers_vendor_type_check' "
        "AND strpos(pg_get_constraintdef(oid), $1) = 0)", "'cursor-login'")


async def _runtime_launcher_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _RUNTIME_LAUNCHER):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('agent_runtimes') AND attname = 'launcher' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _profile_fallback_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _PROFILE_FALLBACK):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('agent_profiles') "
        "AND attname = 'fallback_profile_id' AND attnum > 0 AND NOT attisdropped)")


async def _profile_web_search_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _PROFILE_WEB_SEARCH):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('agent_profiles') "
        "AND attname = 'web_search' AND attnum > 0 AND NOT attisdropped)")


async def _schedule_user_gates_pending(conn: asyncpg.Connection) -> bool:
    """An existing baseline still lacks ``schedules.user_gates``.

    The current ``schema.sql`` already creates the column, so an empty database
    records only ``post_bridge_baseline``. The marker is written only when this
    upgrade runs; once recorded, a missing column is not repaired.
    """
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _SCHEDULE_USER_GATES):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('schedules') AND attname = 'user_gates' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _schedule_notify_format_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _SCHEDULE_NOTIFY_FORMAT):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('schedules') AND attname = 'notify_format' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _llm_requests_pending(conn: asyncpg.Connection) -> bool:
    """Apply this additive upgrade once; fresh schemas already contain the tables."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _LLM_REQUESTS):
        return False
    return not await conn.fetchval("SELECT to_regclass('llm_requests') IS NOT NULL")


async def _llm_requests_translation_pending(conn: asyncpg.Connection) -> bool:
    """Older recorder tables need this nullable column; marked upgrades never repair DDL."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _LLM_REQUESTS_TRANSLATION):
        return False
    return await conn.fetchval(
        "SELECT to_regclass('llm_requests') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('llm_requests') AND attname = 'translation' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _llm_models_chat_dialect_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _LLM_MODELS_CHAT_DIALECT):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('llm_models') AND attname = 'chat_dialect' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _llm_requests_stored_bytes_pending(conn: asyncpg.Connection) -> bool:
    """Backfill recorded byte counts once; current schemas already have both columns."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _LLM_REQUESTS_STORED_BYTES):
        return False
    return await conn.fetchval("""SELECT EXISTS (
        SELECT 1 FROM unnest(ARRAY['llm_requests', 'llm_request_parts']) AS name
        WHERE to_regclass(name) IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(name)
            AND attname = 'stored_bytes' AND attnum > 0 AND NOT attisdropped))""")


async def _gateway_aliases_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _GATEWAY_ALIASES):
        return False
    return not await conn.fetchval("SELECT to_regclass('gateway_aliases') IS NOT NULL")


async def _message_stream_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _MESSAGE_STREAM):
        return False
    return not await conn.fetchval("SELECT to_regclass('event_sources') IS NOT NULL")


async def _message_stream_deletions_pending(conn: asyncpg.Connection) -> bool:
    return (await _column_pending(conn, _MESSAGE_STREAM_DELETIONS, "event_sources", "retention_last_at")
            or await _column_pending(conn, _MESSAGE_STREAM_DELETIONS, "source_runs", "skipped_by_deletion"))


async def _column_pending(conn: asyncpg.Connection, marker: str, table: str, column: str) -> bool:
    """An unmarked database whose ``table`` lacks ``column`` needs its additive upgrade; recorded upgrades never
    repair DDL."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", marker):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass($1) AND attname = $2 "
        "AND attnum > 0 AND NOT attisdropped)", table, column)


async def _run_tree_pending(conn: asyncpg.Connection) -> bool:
    return await _column_pending(conn, _RUN_TREE, "llm_records", "run_kind")


async def _step_difficulty_pending(conn: asyncpg.Connection) -> bool:
    return await _column_pending(conn, _STEP_DIFFICULTY, "task_steps", "difficulty")


async def _agent_spawns_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _AGENT_SPAWNS):
        return False
    return not await conn.fetchval("SELECT to_regclass('agent_spawns') IS NOT NULL")


async def _subscriptions_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _SUBSCRIPTIONS):
        return False
    return not await conn.fetchval("SELECT to_regclass('subscriptions') IS NOT NULL")


async def _subscription_rules_pending(conn: asyncpg.Connection) -> bool:
    """A database whose ``subscriptions`` predates the selection rules; a fresh one already has the column."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _SUBSCRIPTION_RULES):
        return False
    return bool(await conn.fetchval(
        "SELECT to_regclass('subscriptions') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('subscriptions') AND attname = 'rules' AND attnum > 0 AND NOT attisdropped)"))


_SUBSCRIPTION_RULES_DDL = """
ALTER TABLE subscriptions ADD COLUMN rules JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(rules) = 'array');
ALTER TABLE subscriptions ADD COLUMN config_epoch BIGINT NOT NULL DEFAULT 0;
ALTER TABLE subscription_runs ADD COLUMN config JSONB, ADD COLUMN config_epoch BIGINT,
    ADD COLUMN match_timeouts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS subscriptions_render_check,
    ADD CONSTRAINT subscriptions_render_check CHECK (render IN ('lines_v1', 'lines_v2'));
"""


async def _subscription_lookback_pending(conn: asyncpg.Connection) -> bool:
    """Unmarked databases may already have some daily objects; recorded upgrades never repair DDL."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _SUBSCRIPTION_LOOKBACK):
        return False
    return bool(await conn.fetchval("""
        SELECT to_regclass('subscriptions') IS NOT NULL AND (
            to_regclass('subscription_daily_claims') IS NULL OR EXISTS (
                SELECT 1 FROM (VALUES ('subscriptions', 'window'), ('subscriptions', 'delivery_policy'),
                                     ('subscription_runs', 'window_mode'), ('subscription_runs', 'daily_date'))
                    AS required(table_name, column_name)
                WHERE NOT EXISTS (SELECT 1 FROM pg_attribute
                    WHERE attrelid = to_regclass(required.table_name) AND attname = required.column_name
                    AND attnum > 0 AND NOT attisdropped)))
    """))


_SUBSCRIPTION_LOOKBACK_DDL = """
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS "window" TEXT NOT NULL DEFAULT 'cursor' CHECK ("window" IN ('cursor', 'lookback')),
    ADD COLUMN IF NOT EXISTS delivery_policy TEXT NOT NULL DEFAULT 'per_run' CHECK (delivery_policy IN ('per_run', 'daily'));
ALTER TABLE subscription_runs
    ADD COLUMN IF NOT EXISTS window_mode TEXT NOT NULL DEFAULT 'cursor' CHECK (window_mode IN ('cursor', 'lookback')),
    ADD COLUMN IF NOT EXISTS daily_date DATE;
CREATE TABLE IF NOT EXISTS subscription_daily_claims (
    schedule_id TEXT NOT NULL REFERENCES subscriptions(schedule_id) ON DELETE CASCADE,
    local_date DATE NOT NULL,
    PRIMARY KEY (schedule_id, local_date)
);
"""


async def _ledger_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _LEDGER):
        return False
    return not await conn.fetchval("SELECT to_regclass('ledger_items') IS NOT NULL")


async def _intent_merges_pending(conn: asyncpg.Connection) -> bool:
    """Whether the intents table lacks the AFTER DELETE detach trigger ``A_intents_detach_merged``, still has the
    earlier BEFORE trigger, or has a merge key that cascades. Judged by the catalog, not the marker, so a database that
    ran an earlier version of this block is upgraded too."""
    if not await conn.fetchval("SELECT to_regclass('intents') IS NOT NULL"):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'intents'::regclass "
        "AND tgname = 'A_intents_detach_merged') "
        "AND NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'intents'::regclass "
        "AND tgname = 'intents_detach_merged') "
        "AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'intents_merged_into_fkey' "
        "AND conrelid = 'intents'::regclass AND confdeltype = 'c')")


async def _leads_check_pending(conn: asyncpg.Connection) -> bool:
    """Whether the leads and intents lack the check's ``dropped`` state and columns, the subscriptions its switch and
    alert, the runs their check status, the notifications its kinds, or the role layers its role. Judged by the
    catalog, as the intent merges block is: a database whose leads block predates it is upgraded, and one that
    already has it is left alone."""
    if not await conn.fetchval("SELECT to_regclass('message_leads') IS NOT NULL AND to_regclass('intents') IS NOT NULL"):
        return False
    return not await conn.fetchval(
        "SELECT (SELECT count(*) FROM pg_constraint WHERE conname = ANY($1::text[]) "
        "AND connamespace = current_schema()::regnamespace) = cardinality($1::text[]) "
        "AND (SELECT count(*) FROM pg_attribute WHERE attnum > 0 AND NOT attisdropped AND "
        "((attrelid = 'subscriptions'::regclass AND attname IN ('check_items', 'check_alert_at')) "
        "OR (attrelid = 'subscription_runs'::regclass AND attname = 'check_status'))) = 3 "
        "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'agent_setting_layers'::regclass "
        "AND conname = 'agent_setting_layers_role_check' AND strpos(pg_get_constraintdef(oid), $2) > 0) "
        "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'schedule_notifications'::regclass "
        "AND conname = 'schedule_notifications_kind_check' AND strpos(pg_get_constraintdef(oid), $3) > 0)",
        ["message_leads_dropped", "intents_dropped", "subscription_runs_check_status_check",
         "message_leads_check_record_id_fkey", "intents_check_record_id_fkey"], "'leads_check'", "'check_alert'")


def _leads_check_step() -> _OnlineStep:
    return next(step for step in _ONLINE_STEPS if step.migration == _LEADS_CHECK)


async def _leads_check_unvalidated(conn: asyncpg.Connection) -> bool:
    """Whether a constraint the leads check block adds NOT VALID is still not validated."""
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = ANY($1::text[]) AND NOT convalidated "
        "AND connamespace = current_schema()::regnamespace)",
        [constraint for _, constraint in _leads_check_step().validate]))


async def _message_leads_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _MESSAGE_LEADS):
        return False
    return not await conn.fetchval("SELECT to_regclass('message_leads') IS NOT NULL")


async def _manager_events_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _MANAGER_EVENTS):
        return False
    return not await conn.fetchval("SELECT to_regclass('manager_events') IS NOT NULL")


async def _proactive_deliveries_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _PROACTIVE_DELIVERIES):
        return False
    return not await conn.fetchval("SELECT to_regclass('proactive_deliveries') IS NOT NULL")


async def _manager_event_reasons_pending(conn: asyncpg.Connection) -> bool:
    """A database whose completion events cannot record every reason (``no_chat``, ``target_changed``)."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _MANAGER_EVENT_REASONS_MIGRATION):
        return False
    # This schema's table only: another schema of the database has a constraint of the same name.
    definition = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                     "WHERE conname = 'manager_events_reason_check' "
                                     "AND conrelid = to_regclass('manager_events')")
    return definition is not None and any(f"'{reason}'" not in definition for reason in _MANAGER_EVENT_REASONS)


async def _manager_event_delivery_pending(conn: asyncpg.Connection) -> bool:
    """A database whose completion events predate the typed delivery of their messages."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _MANAGER_EVENT_DELIVERY):
        return False
    return await conn.fetchval("SELECT to_regclass('manager_events') IS NOT NULL") and not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('manager_events') "
        "AND attname = 'delivery' AND attnum > 0 AND NOT attisdropped)")


async def _schedule_notification_events_pending(conn: asyncpg.Connection) -> bool:
    """A database whose completion events predate ``schedule_notifications.manager_event_id`` (the block above
    adds it to one that gets the table now, and schema.sql to a new one)."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _SCHEDULE_NOTIFICATION_EVENTS):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('schedule_notifications') "
        "AND attname = 'manager_event_id' AND attnum > 0 AND NOT attisdropped)")


async def _project_activity_pending(conn: asyncpg.Connection) -> bool:
    """Only older databases need this additive table; fresh schemas already contain it."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _PROJECT_ACTIVITY):
        return False
    return not await conn.fetchval("SELECT to_regclass('project_activity') IS NOT NULL")


async def _command_schedules_pending(conn: asyncpg.Connection) -> bool:
    """An unmarked baseline lacks ``schedules.kind``; recorded upgrades never repair DDL."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _COMMAND_SCHEDULES):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('schedules') AND attname = 'kind' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _schedule_session_bounds_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _SCHEDULE_SESSION_BOUNDS):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('schedules') "
        "AND attname = 'planner_session_context_tokens' AND attnum > 0 AND NOT attisdropped)")


async def _task_reply_route_pending(conn: asyncpg.Connection) -> bool:
    """An unmarked task baseline lacks the original reply route; recorded upgrades never repair DDL."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _TASK_REPLY_ROUTE):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('tasks') AND attname = 'origin_reply_route' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _task_process_record_pending(conn: asyncpg.Connection) -> bool:
    """An unmarked baseline lacks the task process record; recorded upgrades never repair DDL."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _TASK_PROCESS_RECORD):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('tasks') AND attname = 'process_record' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _chat_binding_senders_pending(conn: asyncpg.Connection) -> bool:
    """Unmarked bindings without extra senders need the additive upgrade."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _CHAT_BINDING_SENDERS):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('manager_chat_bindings') AND attname = 'allowed_senders' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _chat_binding_sender_policy_pending(conn: asyncpg.Connection) -> bool:
    """Unmarked bindings without a sender policy need the additive upgrade."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _CHAT_BINDING_SENDER_POLICY):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('manager_chat_bindings') AND attname = 'sender_policy' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _provider_enabled_pending(conn: asyncpg.Connection) -> bool:
    """Recorded upgrades never repair DDL; fresh schemas already contain the column."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _PROVIDER_ENABLED):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('llm_providers') AND attname = 'enabled' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _manager_scope_projects_pending(conn: asyncpg.Connection) -> bool:
    """Recorded upgrades never repair DDL; fresh schemas already contain the column."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _MANAGER_SCOPE_PROJECTS):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('managers') AND attname = 'scope_project_ids' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _schedule_missed_since_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _SCHEDULE_MISSED_SINCE):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('schedule_runs') AND attname = 'missed_since' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _schedule_deferred_by_drain_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _SCHEDULE_DEFERRED_BY_DRAIN):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('schedule_runs') AND attname = 'deferred_by_drain' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _task_chat_group_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _TASK_CHAT_GROUP):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('tasks') AND attname = 'chat_group' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _dependency_conditions_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _DEPENDENCY_CONDITIONS):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('task_steps') AND attname = 'ready_when' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _workflows_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _WORKFLOWS):
        return False
    return not await conn.fetchval("SELECT to_regclass('workflows') IS NOT NULL")


async def _review_panel_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _REVIEW_PANEL):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('task_steps') AND attname = 'accept_when' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _workflow_rules_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _WORKFLOW_RULES):
        return False
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('task_journal') "
        "AND conname = 'task_journal_kind_check' "
        "AND (strpos(pg_get_constraintdef(oid), $1) = 0 OR strpos(pg_get_constraintdef(oid), $2) = 0))",
        "'verification_round'", "'escalation'")


async def _gate_purpose_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _GATE_PURPOSE):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('task_user_gates') AND attname = 'purpose' "
        "AND attnum > 0 AND NOT attisdropped)")


async def _verification_paths_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _VERIFICATION_PATHS):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('projects') AND attname = 'verification_paths' "
        "AND attnum > 0 AND NOT attisdropped)")


class RegistrySchemaVersionError(RuntimeError):
    """The registry cannot be opened safely by this binary."""


async def check_registry_schema_version(conn: asyncpg.Connection, *, supported_version: int) -> int | None:
    """Read-only forward guard, also usable by the old-layout patch release.

    Call under the schema lock before executing any initialization DDL. A
    missing table is an unversioned database; an empty version table is an
    incomplete initialization, never permission to initialize over it.
    """
    if not await conn.fetchval("SELECT to_regclass('registry_schema_version') IS NOT NULL"):
        return None
    version = await conn.fetchval("SELECT version FROM registry_schema_version WHERE id = TRUE")
    if version is None:
        raise RegistrySchemaVersionError("registry schema version is missing; initialization refused")
    if version > supported_version:
        raise RegistrySchemaVersionError(
            f"registry schema version {version} exceeds supported version {supported_version}; startup refused")
    return version


async def _classify_schema(conn: asyncpg.Connection, sql: str) -> str:
    """Read-only admission and bridge preconditions, always under _SCHEMA_LOCK."""
    from .bridge_upgrade import BASELINE, BRIDGE_ERA_FINGERPRINTS, BRIDGE_PRECONDITIONS, BRIDGE_ROLE_PROMPTS

    older = ("unsupported Looper database; upgrade to v2.13.165 or a later 2.13 release first, "
             "start it once, then upgrade to this version")
    names = re.findall(r"(?im)^\s*CREATE TABLE IF NOT EXISTS\s+(\w+)", sql)
    # The version marker must be recognized even if schema.sql is incomplete.
    names.extend(("agents", "agent_configs", "task_agent_overrides", "project_skills", "schema_fingerprint", "llm_profiles",
                  "registry_schema_version"))
    # Do not initialize shadow tables over a visible installation in another namespace.
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM unnest($1::text[]) t(name) "
            "JOIN pg_class c ON c.oid = to_regclass(t.name) "
            "WHERE c.relnamespace <> quote_ident(current_schema())::regnamespace)", names):
        raise RegistrySchemaVersionError(older + "; Looper tables span namespaces")
    tables = {r["relname"] for r in await conn.fetch(
        "SELECT relname FROM pg_class WHERE relnamespace = quote_ident(current_schema())::regnamespace "
        "AND relname = ANY($1::text[])", names)}
    if not tables:
        return "empty"
    version = (await conn.fetchval("SELECT version FROM registry_schema_version WHERE id = TRUE")
               if "registry_schema_version" in tables else None)
    if "registry_schema_version" in tables and version is None:
        raise RegistrySchemaVersionError(older + "; registry schema version is missing; initialization refused")
    await check_registry_schema_version(conn, supported_version=REGISTRY_SCHEMA_VERSION)
    if version == REGISTRY_SCHEMA_VERSION:
        if "schema_migrations" not in tables or not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", BASELINE
        ):
            raise RegistrySchemaVersionError(f"registry schema version {version} is missing the {BASELINE} marker; "
                                             "startup refused")
        return "current"
    if version != 3:
        raise RegistrySchemaVersionError(older)
    fingerprint = (await conn.fetchval("SELECT sha256 FROM schema_fingerprint WHERE id = TRUE")
                   if "schema_fingerprint" in tables else None)
    if fingerprint not in BRIDGE_ERA_FINGERPRINTS:
        raise RegistrySchemaVersionError(older)
    # Check only the names the conversion needs, before serve makes a backup
    # and again before ensure_schema executes any DDL. No catalog definitions.
    columns = {(r["table_name"], r["column_name"]) for r in await conn.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
        [table for table, _ in BRIDGE_PRECONDITIONS])}
    for table, required in BRIDGE_PRECONDITIONS:
        if table not in tables:
            raise RegistrySchemaVersionError(f"bridge upgrade requires table {table}; startup refused")
        for column in required:
            if (table, column) not in columns:
                raise RegistrySchemaVersionError(f"bridge upgrade requires column {table}.{column}; startup refused")
    if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name='file_state_import')"):
        raise RegistrySchemaVersionError(older + "; bridge file-state import has not completed")
    prompts = {r["role"]: hashlib.sha256(r["default_prompt"].encode()).hexdigest()
               for r in await conn.fetch("SELECT role, default_prompt FROM role_configs WHERE role = ANY($1::text[])",
                                         list(BRIDGE_ROLE_PROMPTS))}
    if prompts != BRIDGE_ROLE_PROMPTS:
        raise RegistrySchemaVersionError(older + "; bridge role-prompt seeding has not completed")
    return "bridge"


async def _model_name_parameters_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)",
                           _MODEL_NAME_PARAMETERS):
        return False
    # Fresh baselines already install the shared grammar and record only BASELINE.
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('llm_models') "
        "AND conname IN ('llm_models_name_check', 'llm_models_background_model_check') "
        "AND strpos(pg_get_constraintdef(oid), $1) = 0)", MODEL_NAME_PATTERN)


async def ensure_schema(pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    from .bridge_upgrade import BASELINE, upgrade_from_bridge

    pool = pool or get_pool()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with _connection(pool) as conn, conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK})")
        state = await _classify_schema(conn, sql)
        if state == "empty":
            await conn.execute(sql)
        elif state == "bridge":
            await upgrade_from_bridge(conn)
        if await _model_name_parameters_pending(conn):
            # Use the application's grammar rather than maintaining a second
            # regex in SQL. NOT VALID preserves historical malformed names on
            # upgrade; all new inserts/updates still enforce the new grammar.
            pattern = MODEL_NAME_PATTERN.replace("'", "''")
            for field in ("name", "background_model"):
                constraint = f"llm_models_{field}_check"
                check = f"length({field}) BETWEEN 1 AND {MODEL_MAX_CHARS} AND {field} ~ '{pattern}'"
                await conn.execute(f"ALTER TABLE llm_models DROP CONSTRAINT {constraint}, "
                    f"ADD CONSTRAINT {constraint} CHECK ({check}) NOT VALID")
                # Clean upgrades must have the same validated catalog as fresh databases.
                if state == "empty" or not await conn.fetchval(
                    f"SELECT EXISTS (SELECT 1 FROM llm_models WHERE NOT ({check}))"
                ):
                    await conn.execute(f"ALTER TABLE llm_models VALIDATE CONSTRAINT {constraint}")
            if state != "empty":
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _MODEL_NAME_PARAMETERS)
        # One additive upgrade for a baseline that predates schedules.user_gates,
        # including the bridge. An empty database already has the column from
        # schema.sql and records only post_bridge_baseline. Once this marker is
        # recorded, reopening never tries to repair a missing column.
        if await _schedule_user_gates_pending(conn):
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS user_gates "
                               "TEXT NOT NULL DEFAULT 'off' CHECK (user_gates IN ('off', 'on'))")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SCHEDULE_USER_GATES)
        if await _profile_fallback_pending(conn):
            await conn.execute("ALTER TABLE agent_profiles ADD COLUMN fallback_profile_id TEXT "
                               "REFERENCES agent_profiles(id) ON DELETE RESTRICT")
            # Use the fresh schema's canonical check so upgraded catalogs match it.
            constraint = re.search(
                r"CONSTRAINT task_journal_kind_check (CHECK \(kind IN \(.*?\)\))", sql, re.S,
            ).group(1)
            await conn.execute("ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check")
            await conn.execute("ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check "
                               + constraint)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _PROFILE_FALLBACK)
        if await _profile_web_search_pending(conn):
            await conn.execute("ALTER TABLE agent_profiles ADD COLUMN web_search BOOLEAN NOT NULL DEFAULT true")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _PROFILE_WEB_SEARCH)
        if await _cursor_login_pending(conn):
            await conn.execute(_CURSOR_LOGIN_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _CURSOR_LOGIN)
        if await _coco_login_pending(conn):
            await conn.execute(_COCO_LOGIN_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _COCO_LOGIN)
        if await _runtime_launcher_pending(conn):
            await conn.execute("ALTER TABLE agent_runtimes ADD COLUMN IF NOT EXISTS launcher "
                               "TEXT NOT NULL DEFAULT 'native' CHECK (launcher IN ('native', 'ttadk'))")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _RUNTIME_LAUNCHER)
        if await _command_schedules_pending(conn):
            for ddl in _COMMAND_SCHEDULES_DDL:
                await conn.execute(ddl)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _COMMAND_SCHEDULES)
        if await _schedule_notify_format_pending(conn):
            await conn.execute("ALTER TABLE schedules ADD COLUMN notify_format TEXT NOT NULL DEFAULT 'report' "
                               "CHECK (notify_format IN ('report', 'deliverable'))")
            await conn.execute("ALTER TABLE schedules ADD CONSTRAINT schedules_command_notify_format_check "
                               "CHECK (kind <> 'command' OR notify_format = 'report')")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SCHEDULE_NOTIFY_FORMAT)
        if await _task_process_record_pending(conn):
            await conn.execute("ALTER TABLE tasks ADD COLUMN process_record JSONB "
                               "CHECK (jsonb_typeof(process_record) = 'object')")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _TASK_PROCESS_RECORD)
        if await _task_reply_route_pending(conn):
            await conn.execute("ALTER TABLE tasks ADD COLUMN origin_reply_route JSONB "
                               "CHECK (jsonb_typeof(origin_reply_route) = 'object')")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _TASK_REPLY_ROUTE)
        if await _manager_scope_projects_pending(conn):
            for ddl in _MANAGER_SCOPE_PROJECTS_DDL:
                await conn.execute(ddl)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _MANAGER_SCOPE_PROJECTS)
        if await _schedule_missed_since_pending(conn):
            await conn.execute("ALTER TABLE schedule_runs ADD COLUMN missed_since TIMESTAMPTZ")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SCHEDULE_MISSED_SINCE)
        if await _schedule_deferred_by_drain_pending(conn):
            await conn.execute("ALTER TABLE schedule_runs ADD COLUMN deferred_by_drain BOOLEAN NOT NULL DEFAULT false")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SCHEDULE_DEFERRED_BY_DRAIN)
        if await _task_chat_group_pending(conn):
            await conn.execute("ALTER TABLE tasks ADD COLUMN chat_group BOOLEAN NOT NULL DEFAULT false")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _TASK_CHAT_GROUP)
        if await _processor_role_pending(conn):
            await conn.execute(_PROCESSOR_ROLE_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _PROCESSOR_ROLE)
        if await _headless_compaction_pending(conn):
            await conn.execute(_HEADLESS_COMPACTION_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _HEADLESS_COMPACTION)
        if await _advisor_roles_pending(conn):
            await conn.execute(_ADVISOR_ROLES_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _ADVISOR_ROLES)
        if await _project_advisor_pending(conn):
            await conn.execute(_PROJECT_ADVISOR_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _PROJECT_ADVISOR)
        if await _llm_requests_pending(conn):
            await conn.execute(_LLM_REQUESTS_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _LLM_REQUESTS)
        if await _llm_requests_translation_pending(conn):
            await conn.execute("ALTER TABLE llm_requests ADD COLUMN IF NOT EXISTS translation JSONB")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _LLM_REQUESTS_TRANSLATION)
        if await _llm_models_chat_dialect_pending(conn):
            await conn.execute("ALTER TABLE llm_models ADD COLUMN chat_dialect TEXT "
                               "CHECK (chat_dialect IS NULL OR protocol = 'openai-chat')")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _LLM_MODELS_CHAT_DIALECT)
        if await _llm_requests_stored_bytes_pending(conn):
            for table in ("llm_requests", "llm_request_parts"):
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS stored_bytes BIGINT")
                await conn.execute(f"UPDATE {table} AS row SET stored_bytes = octet_length(row::text) "
                                   "WHERE stored_bytes IS NULL")
                await conn.execute(f"ALTER TABLE {table} ALTER COLUMN stored_bytes SET DEFAULT 0, "
                                   "ALTER COLUMN stored_bytes SET NOT NULL")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _LLM_REQUESTS_STORED_BYTES)
        if await _gateway_aliases_pending(conn):
            await conn.execute(_GATEWAY_ALIASES_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _GATEWAY_ALIASES)
        if await _message_stream_pending(conn):
            await conn.execute(_message_stream_ddl())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _MESSAGE_STREAM)
        if await _message_stream_deletions_pending(conn):
            await conn.execute("ALTER TABLE event_sources ADD COLUMN IF NOT EXISTS retention_last_at TIMESTAMPTZ")
            await conn.execute("ALTER TABLE source_runs ADD COLUMN IF NOT EXISTS skipped_by_deletion "
                               "BIGINT NOT NULL DEFAULT 0")
            await conn.execute(_source_deletions_ddl())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _MESSAGE_STREAM_DELETIONS)
        if await _subscriptions_pending(conn):
            await conn.execute(_subscriptions_ddl())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SUBSCRIPTIONS)
        if await _subscription_rules_pending(conn):
            await conn.execute(_SUBSCRIPTION_RULES_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SUBSCRIPTION_RULES)
        if await _subscription_lookback_pending(conn):
            await conn.execute(_SUBSCRIPTION_LOOKBACK_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SUBSCRIPTION_LOOKBACK)
        if await _ledger_pending(conn):
            await conn.execute(_ledger_ddl())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _LEDGER)
        if await _proactive_deliveries_pending(conn):
            await conn.execute(_proactive_deliveries_ddl())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _PROACTIVE_DELIVERIES)
        if await _manager_events_pending(conn):
            await conn.execute(_manager_events_ddl())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _MANAGER_EVENTS)
            # The block added the outbox's column too: its index is built online, as for a later database.
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SCHEDULE_NOTIFICATION_EVENTS)
        if await _schedule_notification_events_pending(conn):
            await conn.execute("ALTER TABLE schedule_notifications ADD COLUMN IF NOT EXISTS manager_event_id BIGINT, "
                               "ADD CONSTRAINT schedule_notifications_manager_event_id_fkey FOREIGN KEY "
                               "(manager_event_id) REFERENCES manager_events(id) ON DELETE SET NULL NOT VALID")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SCHEDULE_NOTIFICATION_EVENTS)
        if await _manager_event_reasons_pending(conn):
            reasons = ", ".join(f"'{reason}'" for reason in _MANAGER_EVENT_REASONS)
            # NOT VALID: no scan under this transaction's lock; the online step validates it after the commit.
            await conn.execute("ALTER TABLE manager_events DROP CONSTRAINT manager_events_reason_check, ADD CONSTRAINT "
                               f"manager_events_reason_check CHECK (reason IN ({reasons})) NOT VALID")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _MANAGER_EVENT_REASONS_MIGRATION)
        if await _manager_event_delivery_pending(conn):
            await conn.execute(
                "ALTER TABLE manager_events ADD COLUMN IF NOT EXISTS delivery TEXT, ADD COLUMN IF NOT EXISTS "
                "delivery_attempts INTEGER NOT NULL DEFAULT 0, ADD COLUMN IF NOT EXISTS delivery_at TIMESTAMPTZ, "
                "ADD COLUMN IF NOT EXISTS delivery_error TEXT, ADD COLUMN IF NOT EXISTS delivery_claim UUID, "
                "ADD CONSTRAINT manager_events_delivery_check CHECK (delivery IN ('pending', 'sending', 'sent', "
                "'failed', 'uncertain')) NOT VALID")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _MANAGER_EVENT_DELIVERY)
        if await _run_tree_pending(conn):
            for ddl in _run_tree_ddl():
                await conn.execute(ddl)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _RUN_TREE)
        if await _step_difficulty_pending(conn):
            await conn.execute("ALTER TABLE task_steps ADD COLUMN difficulty TEXT")
            await conn.execute("ALTER TABLE task_steps ADD CONSTRAINT task_steps_difficulty_check "
                               "CHECK (difficulty IN ('routine', 'hard', 'critical')) NOT VALID")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _STEP_DIFFICULTY)
        if await _agent_spawns_pending(conn):
            await conn.execute(_agent_spawns_ddl())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _AGENT_SPAWNS)
        if await _project_activity_pending(conn):
            await conn.execute(_PROJECT_ACTIVITY_DDL)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _PROJECT_ACTIVITY)
        if await _chat_binding_senders_pending(conn):
            await conn.execute("ALTER TABLE manager_chat_bindings ADD COLUMN allowed_senders TEXT[] NOT NULL "
                               "DEFAULT '{}' CONSTRAINT manager_chat_bindings_allowed_senders_check "
                               "CHECK (cardinality(allowed_senders) <= 100)")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _CHAT_BINDING_SENDERS)
        if await _chat_binding_sender_policy_pending(conn):
            await conn.execute("ALTER TABLE manager_chat_bindings ADD COLUMN sender_policy TEXT NOT NULL "
                               "DEFAULT 'listed' CHECK (sender_policy IN ('listed', 'chat'))")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _CHAT_BINDING_SENDER_POLICY)
        if await _provider_enabled_pending(conn):
            await conn.execute("ALTER TABLE llm_providers ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT true")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _PROVIDER_ENABLED)
        if await _dependency_conditions_pending(conn):
            await conn.execute("ALTER TABLE task_steps ADD COLUMN ready_when JSONB")
            await conn.execute("ALTER TABLE projects ADD COLUMN dependency_conditions TEXT "
                               "CHECK (dependency_conditions IN ('off', 'on'))")
            await conn.execute("ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check")
            # Freeze this upgrade's constraint in the same order as the fresh schema.
            await conn.execute("""ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check CHECK (kind IN (
                'step_accepted', 'step_failed', 'step_retry', 'decision', 'supplement',
                'session_switch', 'handoff', 'progress', 'profile_choice', 'profile_fallback', 'concurrency_change',
                'role_defined', 'gate_opened', 'gate_updated', 'gate_cancelled', 'gate_closed',
                'wait_opened', 'wait_extended', 'wait_closed', 'condition_blocked'))""")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _DEPENDENCY_CONDITIONS)
        if await _workflows_pending(conn):
            await conn.execute("""CREATE TABLE workflows (
                key TEXT PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_-]{0,31}$' AND key <> 'none'),
                name TEXT NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 60),
                description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 300),
                definition JSONB NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
                CREATE UNIQUE INDEX workflows_name_key ON workflows (lower(btrim(name)));
                ALTER TABLE projects ADD COLUMN default_workflow TEXT REFERENCES workflows(key) ON DELETE SET NULL;
                ALTER TABLE tasks ADD COLUMN workflow JSONB;
                ALTER TABLE task_roles DROP CONSTRAINT task_roles_via_check;
                ALTER TABLE task_roles ADD CONSTRAINT task_roles_via_check
                    CHECK (via IN ('planner', 'user', 'template', 'workflow'));""")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _WORKFLOWS)
        if await _review_panel_pending(conn):
            await conn.execute("ALTER TABLE task_steps ADD COLUMN accept_when JSONB, ADD COLUMN reviews TEXT")
            await conn.execute("ALTER TABLE task_steps DROP CONSTRAINT task_steps_kind_check")
            await conn.execute("ALTER TABLE task_steps ADD CONSTRAINT task_steps_kind_check "
                               "CHECK (kind IN ('work', 'gate', 'wait', 'review'))")
            constraint = re.search(
                r"CONSTRAINT task_journal_kind_check (CHECK \(kind IN \(.*?\)\))", sql, re.S,
            ).group(1)
            await conn.execute("ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check")
            await conn.execute("ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check " + constraint)
            # Released workflow presets allowed panels without distinct runtimes. Bring those presets
            # under the panel invariant before readers validate them; existing task snapshots stay fixed.
            await conn.execute("UPDATE workflows SET definition = jsonb_set(definition, "
                               "'{distinct_reviewer_runtime}', 'true'::jsonb) "
                               "WHERE jsonb_typeof(definition->'review_panel') = 'object' "
                               "AND definition->'distinct_reviewer_runtime' IS DISTINCT FROM 'true'::jsonb")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _REVIEW_PANEL)
        if await _workflow_rules_pending(conn):
            constraint = re.search(
                r"CONSTRAINT task_journal_kind_check (CHECK \(kind IN \(.*?\)\))", sql, re.S,
            ).group(1)
            await conn.execute("ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check")
            await conn.execute("ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check " + constraint)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _WORKFLOW_RULES)
        if await _gate_purpose_pending(conn):
            await conn.execute("ALTER TABLE task_user_gates ADD COLUMN purpose TEXT NOT NULL DEFAULT 'discussion' "
                               "CHECK (purpose IN ('discussion', 'exhaustion', 'verification_exhaustion'))")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _GATE_PURPOSE)
        if await _verification_paths_pending(conn):
            await conn.execute("ALTER TABLE projects ADD COLUMN verification_paths TEXT[] "
                               "CHECK (cardinality(verification_paths) <= 20)")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _VERIFICATION_PATHS)
        # After every table its keys point at.
        if await _message_leads_pending(conn):
            await conn.execute(_message_leads_ddl())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _MESSAGE_LEADS)
        if await _intent_merges_pending(conn):
            await conn.execute(_intent_merges_ddl())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
                               _INTENT_MERGES)
        if await _leads_check_pending(conn):
            await conn.execute(_leads_check_ddl())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
                               _LEADS_CHECK)
            # Its constraints are NOT VALID again: the online step validates them after this commits.
            await conn.execute("DELETE FROM schema_migrations WHERE name = $1", _LEADS_CHECK_ONLINE)
        elif state == "empty":
            # schema.sql ran the block, so its constraints are NOT VALID here too: on tables just created, validating
            # them now scans nothing, and a fresh database keeps recording only its baseline.
            for table, constraint in _leads_check_step().validate:
                await conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")
        elif await _leads_check_unvalidated(conn):
            # The block ran outside a migration (schema.sql applied by hand): validated after commit, as an upgrade.
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
                               _LEADS_CHECK)
            await conn.execute("DELETE FROM schema_migrations WHERE name = $1", _LEADS_CHECK_ONLINE)
        if await _schedule_session_bounds_pending(conn):
            await conn.execute("""
                ALTER TABLE schedules
                  ADD COLUMN planner_session_context_tokens BIGINT CHECK (planner_session_context_tokens >= 0),
                  ADD COLUMN planner_session_run_cost_usd DOUBLE PRECISION CHECK (planner_session_run_cost_usd >= 0);
                ALTER TABLE schedule_runs
                  ADD COLUMN planner_context_tokens BIGINT CHECK (planner_context_tokens >= 0),
                  ADD COLUMN planner_cost_usd DOUBLE PRECISION CHECK (planner_cost_usd >= 0),
                  ADD COLUMN planner_session_action TEXT CHECK (planner_session_action IN ('compacted', 'rotated')),
                  ADD COLUMN planner_session_reason TEXT CHECK (planner_session_reason IN ('context', 'cost')),
                  ADD COLUMN planner_session_before_tokens BIGINT CHECK (planner_session_before_tokens >= 0),
                  ADD COLUMN planner_session_after_tokens BIGINT CHECK (planner_session_after_tokens >= 0),
                  ADD COLUMN failure_kind TEXT CHECK (failure_kind IN ('task_budget'));
            """)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", _SCHEDULE_SESSION_BOUNDS)
        # Current databases never replay initialization DDL. Missing builtins
        # are inserted without changing any saved choice or creation time.
        await conn.execute(SEEDS_PATH.read_text(encoding="utf-8"))
        if state != "current":
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", BASELINE)
        await conn.execute("INSERT INTO registry_schema_version (id, version) VALUES (TRUE, $1) "
                           "ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version "
                           "WHERE registry_schema_version.version <> EXCLUDED.version", REGISTRY_SCHEMA_VERSION)
        # The stable current marker is sufficient even when its optional text
        # fingerprint was pruned. Opening a current database never repairs DDL.
        if state != "current" or await conn.fetchval("SELECT to_regclass('schema_fingerprint') IS NOT NULL"):
            await conn.execute(_RECORD_FINGERPRINT.format(sha256=schema_fingerprint(sql)))
    async with _connection(pool) as conn:
        await _finish_online(conn)
    logger.info("schema ensured")


@asynccontextmanager
async def _connection(pool: asyncpg.Pool | asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    if isinstance(pool, asyncpg.Pool):
        async with pool.acquire() as conn:
            yield conn
    else:
        yield pool


async def schema_change_pending(conn: asyncpg.Connection) -> bool:
    """Read-only admission and backup decision, before ensure_schema rechecks under its write lock."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK})")
        state = await _classify_schema(conn, sql)
        return state == "bridge" or (state == "current" and (
            await _schedule_user_gates_pending(conn) or await _schedule_notify_format_pending(conn)
            or await _schedule_session_bounds_pending(conn)
            or await _cursor_login_pending(conn)
            or await _model_name_parameters_pending(conn)
            or await _coco_login_pending(conn)
            or await _runtime_launcher_pending(conn) or await _profile_fallback_pending(conn)
            or await _profile_web_search_pending(conn)
            or await _command_schedules_pending(conn) or await _task_reply_route_pending(conn)
            or await _task_process_record_pending(conn)
            or await _schedule_missed_since_pending(conn) or await _schedule_deferred_by_drain_pending(conn)
            or await _project_activity_pending(conn) or await _message_stream_pending(conn)
            or await _subscriptions_pending(conn) or await _message_stream_deletions_pending(conn)
            or await _subscription_rules_pending(conn) or await _subscription_lookback_pending(conn)
            or await _ledger_pending(conn)
            or await _proactive_deliveries_pending(conn)
            or await _manager_events_pending(conn) or await _schedule_notification_events_pending(conn)
            or await _manager_event_reasons_pending(conn) or await _manager_event_delivery_pending(conn)
            or await _message_leads_pending(conn)
            or await _intent_merges_pending(conn) or await _leads_check_pending(conn)
            or await _run_tree_pending(conn) or await _step_difficulty_pending(conn)
            or await _agent_spawns_pending(conn) or await _advisor_roles_pending(conn)
            or await _project_advisor_pending(conn)
            or await _llm_requests_pending(conn)
            or await _llm_requests_translation_pending(conn) or await _llm_models_chat_dialect_pending(conn)
            or await _llm_requests_stored_bytes_pending(conn)
            or await _chat_binding_senders_pending(conn) or await _chat_binding_sender_policy_pending(conn)
            or await _manager_scope_projects_pending(conn) or await _provider_enabled_pending(conn)
            or await _dependency_conditions_pending(conn) or await _workflows_pending(conn)
            or await _review_panel_pending(conn)
            or await _workflow_rules_pending(conn) or await _gate_purpose_pending(conn)
            or await _verification_paths_pending(conn) or await _task_chat_group_pending(conn)
            or await _processor_role_pending(conn) or await _headless_compaction_pending(conn)))
