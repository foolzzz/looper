"""Async connection pool and schema bootstrap."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

from looper.agents.model_names import MODEL_NAME_PATTERN

from . import migrations
from .migrations import OnlineStep as _OnlineStep
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


class PoolNotInitializedError(RuntimeError):
    """No database pool has been opened in this process."""


def get_pool() -> asyncpg.Pool:
    if _fenced:
        raise RuntimeError(_FENCED_MESSAGE)
    if _pool is None:
        raise PoolNotInitializedError("database pool not initialised — call init_pool() first")
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
_MID_TURN_INPUT = "mid_turn_input"
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
_DECISION_SNAPSHOTS = "decision_snapshots"
_STEP_DIFFICULTY = "step_difficulty"
_ADVISOR_ROLES = "advisor_roles"
_PROJECT_ADVISOR = "project_advisor"
_PROJECT_ACTIVITY = "project_activity"
_CHANNEL_OUTBOX = "channel_outbox"
_LEDGER = "ledger"
_MANAGER_SESSIONS = "manager_sessions"
_MESSAGE_LEADS = "message_leads"
_INTENT_MERGES = "intent_merges"
_LEADS_CHECK = "leads_check"
_LEADS_CHECK_ONLINE = "leads_check_online"
_RUN_SKIPS = "run_skips"
_RUN_SKIPS_ONLINE = "run_skips_online"
_LLM_REQUESTS = "llm_requests"
_LLM_REQUESTS_TRANSLATION = "llm_requests_translation"
_LLM_MODELS_CHAT_DIALECT = "llm_models_chat_dialect"
_MODEL_NAME_PARAMETERS = "model_name_parameters"
_LLM_REQUESTS_STORED_BYTES = "llm_requests_stored_bytes"
_OUTBOUND_REVIEWS = "outbound_reviews"
_CARD_ACTIONS = "card_actions"

_MEMORY_CANDIDATES = "memory_candidates"
_PROVIDER_LIMIT_WINDOWS = "provider_limit_windows"
_UNCERTAIN_DELIVERIES = "uncertain_deliveries"
# The partial indexes of uncertain deliveries, as schema.sql's last lines create them.
_UNCERTAIN_DELIVERY_INDEXES = (
    ("idx_subscription_deliveries_uncertain", "ON subscription_deliveries (updated_at) WHERE state = 'uncertain'"),
    ("idx_intent_posts_uncertain", "ON intent_posts (updated_at) WHERE state = 'uncertain'"),
    ("idx_manager_events_uncertain", "ON manager_events (created_at) WHERE delivery = 'uncertain'"),
)
_REMINDERS = "reminders"
_MCP_TOOL_LEVELS = "mcp_tool_levels"
_FAILURE_TYPING = "failure_typing"

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
_MANAGER_SESSIONS_BLOCK = re.compile(r"-- manager sessions begin\n(.*?)-- manager sessions end", re.S)
_AGENT_SPAWNS_BLOCK = re.compile(r"-- agent spawns begin\n(.*?)-- agent spawns end", re.S)
_DECISION_SNAPSHOTS_BLOCK = re.compile(r"-- decision snapshots begin\n(.*?)-- decision snapshots end", re.S)


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


def _decision_snapshots_ddl() -> str:
    """The decision snapshot table as schema.sql defines it, so a database that predates it gets the same text."""
    found = _DECISION_SNAPSHOTS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its decision snapshots block")
    return found.group(1)


_MCP_TOOL_LEVELS_BLOCK = re.compile(r"-- mcp tool levels begin\n(.*?)-- mcp tool levels end", re.S)


def _mcp_tool_levels_ddl() -> str:
    """The MCP tool declarations as schema.sql defines them, so a database that predates them gets the same text."""
    found = _MCP_TOOL_LEVELS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its mcp tool levels block")
    return found.group(1)


_OUTBOUND_REVIEWS_BLOCK = re.compile(r"-- outbound reviews begin\n(.*?)-- outbound reviews end", re.S)
_MEMORY_CANDIDATES_BLOCK = re.compile(r"-- memory candidates begin\n(.*?)-- memory candidates end", re.S)


def _memory_candidates_ddl() -> str:
    """The memory candidate tables and their triggers as schema.sql defines them (idempotent), so a database that
    predates them gets the same text."""
    found = _MEMORY_CANDIDATES_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its memory candidates block")
    return found.group(1)


# The checks that list the classifier: (table, constraint).
_OUTBOUND_CHECKS = (("llm_records", "llm_records_run_kind_check"),
                    ("agent_setting_layers", "agent_setting_layers_role_check"))


async def _lacks_outbound_check(conn: asyncpg.Connection, table: str, constraint: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass($1) AND conname = $2 "
        "AND strpos(pg_get_constraintdef(oid), $3) = 0)", table, constraint, "'outbound_check'")


async def _outbound_reviews_ddl(conn: asyncpg.Connection) -> list[str]:
    """The review table as schema.sql defines it (idempotent), and the classifier in each check that lists roles or
    run kinds without it. Each check comes back NOT VALID, so nothing is scanned while the schema transaction holds
    their tables' locks, and is validated in ``_ONLINE_STEPS``."""
    from .llm_records import RUN_KINDS

    found = _OUTBOUND_REVIEWS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its outbound reviews block")
    kinds = ", ".join(f"'{kind}'" for kind in RUN_KINDS)
    widen = {
        "llm_records_run_kind_check": "ALTER TABLE llm_records DROP CONSTRAINT llm_records_run_kind_check, "
        f"ADD CONSTRAINT llm_records_run_kind_check CHECK (run_kind IN ({kinds})) NOT VALID",
        "agent_setting_layers_role_check": "ALTER TABLE agent_setting_layers DROP CONSTRAINT "
        "agent_setting_layers_role_check, ADD CONSTRAINT agent_setting_layers_role_check CHECK (role IN ('manager', "
        "'planner', 'worker', 'reviewer', 'processor', 'advisor', 'advice_check', 'leads_check', 'outbound_check')) "
        "NOT VALID",
    }
    return [found.group(1)] + [widen[constraint] for table, constraint in _OUTBOUND_CHECKS
                               if await _lacks_outbound_check(conn, table, constraint)]


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


def _failure_typing_ddl() -> tuple[str, ...]:
    """The failure columns of llm_records, as schema.sql defines them, added without a scan or a rewrite (as the run
    tree's are): every older row is NULL, so the check holds; it comes NOT VALID and is validated, and the index
    built, in ``_ONLINE_STEPS``."""
    from ..agents.failures import check_sql

    return (
        "ALTER TABLE llm_records ADD COLUMN failure_class TEXT, ADD COLUMN failure_cause TEXT",
        f"ALTER TABLE llm_records ADD CONSTRAINT llm_records_failure_check CHECK ({check_sql()}) NOT VALID",
    )


_INDEX_PROBE = "looper_index_probe"


async def _index_kept(conn: asyncpg.Connection, name: str, definition: str, *, unique: bool = False) -> bool:
    """Whether the index ``name`` is there, valid and the same as ``definition`` (``ON <table> ...``) would build.
    The comparison is PostgreSQL's own text of each (``pg_get_indexdef`` from ``USING`` on, uniqueness too): the
    definition is built on an empty copy of its table in a transaction that is rolled back, so nothing is scanned
    and no lock blocks the table's writers."""
    existing = await conn.fetchrow("SELECT indisvalid, pg_get_indexdef(indexrelid) AS definition FROM pg_index "
                                   "WHERE indexrelid = to_regclass($1)", name)
    if existing is None or not existing["indisvalid"]:
        return False
    table = re.match(r"ON\s+(\w+)\s", definition)
    if table is None:
        raise ValueError(f"an online index definition starts with ON <table>: {definition!r}")
    probe = conn.transaction()
    await probe.start()
    try:
        await conn.execute(f"CREATE TEMP TABLE {_INDEX_PROBE} (LIKE {table.group(1)})")
        await conn.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX {_INDEX_PROBE}_index "
                           f"ON {_INDEX_PROBE} {definition[table.end():]}")
        wanted = await conn.fetchval("SELECT pg_get_indexdef(to_regclass($1))", f"{_INDEX_PROBE}_index")
    finally:
        await probe.rollback()

    def shape(text: str) -> tuple[bool, str]:
        return text.startswith("CREATE UNIQUE INDEX"), text.split(" USING ", 1)[1]

    return shape(existing["definition"]) == shape(wanted)


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
            indexes = [(name, definition, False) for name, definition in step.indexes]
            indexes.extend((name, definition, True) for name, definition in step.unique_indexes)
            for name, definition, unique in indexes:
                # Kept only when it is valid and is the index the step builds: one a stopped build left INVALID,
                # or an older definition under the same name, is dropped and built again.
                if await _index_kept(conn, name, definition, unique=unique):
                    continue
                await conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                await conn.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}")
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
_RUN_SKIPS_BLOCK = re.compile(r"-- run skips begin\n(.*?)-- run skips end", re.S)


def _run_skips_ddl() -> str:
    """A subscription run's skipped window and why, as schema.sql defines it."""
    found = _RUN_SKIPS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its run skips block")
    return found.group(1)


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


_CARD_ACTIONS_BLOCK = re.compile(r"-- card actions begin\n(.*?)-- card actions end", re.S)


def _card_actions_ddl() -> str:
    """The card buttons table as schema.sql defines it, so a database that predates it gets the same text."""
    found = _CARD_ACTIONS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its card actions block")
    return found.group(1)


async def _card_actions_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _CARD_ACTIONS):
        return False
    return not await conn.fetchval("SELECT to_regclass('card_actions') IS NOT NULL")


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


_REMINDERS_BLOCK = re.compile(r"-- reminders begin\n(.*?)-- reminders end", re.S)


def _reminders_ddl() -> str:
    """The reminders table as schema.sql defines it, so a database that predates it gets the same text."""
    found = _REMINDERS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its reminders block")
    return found.group(1)


def _proactive_deliveries_ddl() -> str:
    found = _PROACTIVE_DELIVERIES_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its proactive deliveries block")
    return found.group(1)


def _manager_sessions_ddl() -> str:
    """The Manager session table as schema.sql defines it, so a database that predates it gets the same text."""
    found = _MANAGER_SESSIONS_BLOCK.search(SCHEMA_PATH.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("schema.sql lost its manager sessions block")
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


_MID_TURN_INPUT_DDL = """
ALTER TABLE agent_runtimes DROP CONSTRAINT agent_runtimes_capabilities_check,
    ADD CONSTRAINT agent_runtimes_capabilities_check CHECK (capabilities <@ ARRAY['run', 'resident_session', 'resume',
        'structured_output', 'claude_plugins', 'mcp', 'add_dirs', 'compaction_events', 'autonomous_turns', 'cost_report',
        'read_tools', 'steer', 'no_tools', 'headless_compaction', 'mid_turn_input']) NOT VALID;
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


async def _uncertain_deliveries_pending(conn: asyncpg.Connection) -> bool:
    """A database without the partial indexes of uncertain deliveries: the migration only records itself, and its
    online step builds them after the commit (a new database has them from schema.sql)."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _UNCERTAIN_DELIVERIES):
        return False
    return not await conn.fetchval("SELECT bool_and(to_regclass(name) IS NOT NULL) FROM unnest($1::text[]) AS name",
                                   [name for name, _ in _UNCERTAIN_DELIVERY_INDEXES])


async def _headless_compaction_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _HEADLESS_COMPACTION):
        return False
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('agent_runtimes') "
        "AND conname = 'agent_runtimes_capabilities_check' "
        "AND strpos(pg_get_constraintdef(oid), $1) = 0)", "'headless_compaction'")


async def _mid_turn_input_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _MID_TURN_INPUT):
        return False
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('agent_runtimes') "
        "AND conname = 'agent_runtimes_capabilities_check' "
        "AND strpos(pg_get_constraintdef(oid), $1) = 0)", "'mid_turn_input'")


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


async def _failure_typing_pending(conn: asyncpg.Connection) -> bool:
    return await _column_pending(conn, _FAILURE_TYPING, "llm_records", "failure_class")


async def _step_difficulty_pending(conn: asyncpg.Connection) -> bool:
    return await _column_pending(conn, _STEP_DIFFICULTY, "task_steps", "difficulty")


async def _agent_spawns_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _AGENT_SPAWNS):
        return False
    return not await conn.fetchval("SELECT to_regclass('agent_spawns') IS NOT NULL")


async def _decision_snapshots_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _DECISION_SNAPSHOTS):
        return False
    return not await conn.fetchval("SELECT to_regclass('decision_snapshots') IS NOT NULL")


async def _mcp_tool_levels_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _MCP_TOOL_LEVELS):
        return False
    return not await conn.fetchval("SELECT to_regclass('mcp_tool_levels') IS NOT NULL")


async def _provider_limit_windows_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)", _PROVIDER_LIMIT_WINDOWS):
        return False
    return not await conn.fetchval("SELECT to_regclass('provider_limit_windows') IS NOT NULL")


def _provider_limit_windows_ddl() -> str:
    found = re.search(r"-- provider limit windows begin\n(.*?)-- provider limit windows end",
                      SCHEMA_PATH.read_text(encoding="utf-8"), re.S)
    if found is None:
        raise RuntimeError("schema.sql lost its provider limit windows block")
    return found.group(1)


async def _outbound_reviews_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _OUTBOUND_REVIEWS):
        return False
    # Fresh baselines have the table and the widened checks; an older migration may have rewritten a check since.
    if not await conn.fetchval("SELECT to_regclass('outbound_reviews') IS NOT NULL"):
        return True
    for table, constraint in _OUTBOUND_CHECKS:
        if await _lacks_outbound_check(conn, table, constraint):
            return True
    return False


async def _memory_candidates_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _MEMORY_CANDIDATES):
        return False
    return not await conn.fetchval("SELECT to_regclass('memory_candidates') IS NOT NULL")


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


async def _channel_outbox_pending(conn: asyncpg.Connection) -> bool:
    return not await conn.fetchval("""
        SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('channel_outbox')
                       AND attname='claim_token' AND NOT attisdropped)
        """)


def _channel_outbox_ddl() -> str:
    found = re.search(r"-- channel outbox begin\n(.*?)-- channel outbox end",
                      SCHEMA_PATH.read_text(encoding="utf-8"), re.S)
    if found is None:
        raise RuntimeError("schema.sql lost its channel outbox block")
    return found.group(1)


async def _ledger_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _LEDGER):
        return False
    return not await conn.fetchval("SELECT to_regclass('ledger_items') IS NOT NULL")


async def _reminders_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _REMINDERS):
        return False
    return not await conn.fetchval("SELECT to_regclass('reminders') IS NOT NULL")


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


async def _run_skips_pending(conn: asyncpg.Connection) -> bool:
    """Whether the subscription runs lack the skip's reason, its constraints, or ``skipped`` among their outcomes, the
    subscriptions their skip alert, or the notifications its kinds. Judged by the catalog: a database that already
    has them is left alone (one whose leads check block ran again since, without the kinds, is not)."""
    if not await conn.fetchval("SELECT to_regclass('subscription_runs') IS NOT NULL"):
        return False
    return not await conn.fetchval(
        "SELECT (SELECT count(*) FROM pg_attribute WHERE attnum > 0 AND NOT attisdropped AND "
        "((attrelid = 'subscription_runs'::regclass AND attname = 'skip_reason') "
        "OR (attrelid = 'subscriptions'::regclass AND attname = 'skip_alert_at'))) = 2 "
        "AND (SELECT count(*) FROM pg_constraint WHERE conrelid = 'subscription_runs'::regclass "
        "AND conname IN ('subscription_runs_skip_reason_check', 'subscription_runs_skipped')) = 2 "
        "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'subscription_runs'::regclass "
        "AND conname = 'subscription_runs_outcome_check' AND strpos(pg_get_constraintdef(oid), $1) > 0) "
        "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'schedule_notifications'::regclass "
        "AND conname = 'schedule_notifications_kind_check' AND strpos(pg_get_constraintdef(oid), $2) > 0)",
        "'skipped'", "'skip_recovered'")


def _online_step(migration: str) -> _OnlineStep:
    return next(step for step in _ONLINE_STEPS if step.migration == migration)


async def _unvalidated(conn: asyncpg.Connection, migration: str) -> bool:
    """Whether a constraint ``migration`` adds NOT VALID is still not validated."""
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = ANY($1::text[]) AND NOT convalidated "
        "AND connamespace = current_schema()::regnamespace)",
        [constraint for _, constraint in _online_step(migration).validate]))


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


async def _manager_sessions_pending(conn: asyncpg.Connection) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", _MANAGER_SESSIONS):
        return False
    return not await conn.fetchval("SELECT to_regclass('manager_sessions') IS NOT NULL")


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
    sql = migrations.schema_sql(SCHEMA_PATH.read_text(encoding="utf-8"), _MIGRATIONS)
    async with _connection(pool) as conn, conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK})")
        state = await _classify_schema(conn, sql)
        if state == "empty":
            await conn.execute(sql)
        elif state == "bridge":
            await upgrade_from_bridge(conn)
        for migration in _MIGRATIONS:
            await migration.apply(conn, sql=sql, state=state)
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
    sql = migrations.schema_sql(SCHEMA_PATH.read_text(encoding="utf-8"), _MIGRATIONS)
    async with conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK})")
        state = await _classify_schema(conn, sql)
        if state == "bridge":
            return True
        if state == "current":
            for migration in _PENDING_MIGRATIONS:
                if await migration.pending(conn):
                    return True
        return False


# Discover after the legacy helpers are defined. New migrations need no edits here.
_MIGRATIONS = migrations.discover()
_PENDING_MIGRATIONS = migrations.pending_migrations(_MIGRATIONS)
_ONLINE_STEPS = migrations.online_steps(_MIGRATIONS)
