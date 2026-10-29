"""Upgrade completed schema-3 releases to schema 4."""

import json
import logging

logger = logging.getLogger(__name__)

# schema_fingerprint(schema.sql) at v2.13.165–174 and v2.13.175–192,
# including the schema.sql replaced on main. These are text markers, never catalog hashes.
BRIDGE_ERA_FINGERPRINTS = (
    "b2bca629d4d68b756f42bc6d4567b9883985343c120f2543a0f9cda9da6732a3",
    "0c0f43ae1efc09fc486d6dd773f36bca737547b3660d4b6925896b8f306ef6fe",
)
BASELINE = "post_bridge_baseline"


# Only names read or written by upgrade_from_bridge; optional historic columns,
# constraints, indexes, functions and objects owned by extensions are irrelevant.
BRIDGE_PRECONDITIONS = (
    ("tasks", ("id", "config")),
    ("llm_records", ("id", "metadata")),
    ("projects", ("task_workspaces",)),
    ("agent_sessions", ("session_id", "runtime_type", "provider_id")),
    ("schedules", ("planner_session_id", "planner_session_runtime", "planner_session_provider")),
    ("channel_configs", ("channel_id", "channel_type", "config")),
    ("agent_setting_layers", ("harness_prompt",)),
    ("task_user_gate_posts", ("id", "channel", "channel_id")),
    ("task_user_gate_messages", ("id", "channel", "channel_id", "external_id")),
)


async def upgrade_from_bridge(conn):
    """Called only after locked bridge classification, inside ensure_schema's transaction."""
    from ..agents.agent_settings import DEFAULT_HARNESS_PROMPT, _BUILTIN_HARNESS_BEFORE_RENAME
    from ..agents.channels.allowlist import SENDER_ID_FORMATS, sender_ids
    from .tasks import _CONFIG_UNWRAP_LIMIT, _read_config

    # Both admitted schema-3 releases already have the final registry, layers
    # and channel storage. Only the earlier release lacks execution snapshots.
    await conn.execute("""
        ALTER TABLE schedule_runs ADD COLUMN IF NOT EXISTS execution_snapshot JSONB;
        ALTER TABLE task_user_gate_posts DROP CONSTRAINT IF EXISTS task_user_gate_posts_channel_chat_external_id_key;
    """)

    for row in await conn.fetch(
        "SELECT id FROM llm_records WHERE jsonb_typeof(metadata) NOT IN ('object','null')"
    ):
        logger.warning("bridge llm_records row %s: keeping non-object metadata unchanged", row["id"])
    for row in await conn.fetch("SELECT id, config FROM tasks"):
        # Bound inspection of old task-page string layers. Preserve precisely
        # what the bridge's eight-iteration reader sees, including its {} fallback.
        value = row["config"]
        depth = 0
        for depth in range(64):
            if not isinstance(value, str):
                break
            try:
                value = json.loads(value)
            except ValueError:
                break
        if not isinstance(value, dict) or depth >= _CONFIG_UNWRAP_LIMIT:
            logger.warning("bridge task %s: config is not an object within the bridge reading limit; keeping {}",
                           row["id"])
        value = _read_config(row["config"])
        await conn.execute("UPDATE tasks SET config = $2::jsonb WHERE id = $1 AND config IS DISTINCT FROM $2::jsonb",
                           row["id"], json.dumps(value))
    await conn.execute("UPDATE projects SET task_workspaces = 'in_place' WHERE task_workspaces IS NULL")
    for table, session, runtime, provider in (
        ("agent_sessions", "session_id", "runtime_type", "provider_id"),
        ("schedules", "planner_session_id", "planner_session_runtime", "planner_session_provider"),
    ):
        await conn.execute(f"UPDATE {table} SET {runtime} = coalesce({runtime}, 'claude-code'), "
                           f"{provider} = coalesce({provider}, 'prov-claude-subscription') "
                           f"WHERE {session} IS NOT NULL AND {session} <> '' "
                           f"AND ({runtime} IS NULL OR {provider} IS NULL) "
                           f"AND ({runtime} IS NULL OR {runtime} = 'claude-code') "
                           f"AND ({provider} IS NULL OR {provider} = 'prov-claude-subscription')")
    for row in await conn.fetch("SELECT channel_id, channel_type, config FROM channel_configs"):
        config = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
        if not isinstance(config, dict):
            logger.warning("bridge channel %s: keeping non-object config unchanged", row["channel_id"])
            continue
        original = dict(config)
        fmt = SENDER_ID_FORMATS.get(row["channel_type"])
        if fmt and config.get(fmt.field) is not None:
            try:
                config[fmt.field] = list(sender_ids(row["channel_type"], config[fmt.field]))
            except ValueError as exc:
                logger.warning("bridge channel %s: keeping invalid sender allowlist unchanged: %s",
                               row["channel_id"], exc)
                continue
        if row["channel_type"] == "lark":
            config.pop("app_id_env", None)
            config.pop("app_secret_env", None)
            config.setdefault("profile", "")
        if config != original:
            await conn.execute("UPDATE channel_configs SET config = $2::jsonb WHERE channel_id = $1",
                               row["channel_id"], json.dumps(config))
    await conn.execute("UPDATE agent_setting_layers SET harness_prompt = $1 WHERE harness_prompt = $2",
                       DEFAULT_HARNESS_PROMPT, _BUILTIN_HARNESS_BEFORE_RENAME)
    await conn.execute("""
        UPDATE llm_records SET metadata = metadata || jsonb_build_object('profile', metadata->'agent')
        WHERE jsonb_typeof(metadata) = 'object'
            AND NOT metadata ? 'profile' AND jsonb_typeof(metadata->'agent') = 'object';
        UPDATE llm_records SET metadata = coalesce(nullif(metadata, 'null'::jsonb), '{}'::jsonb)
            || '{"accounting_basis":"subscription_equivalent"}'::jsonb
        WHERE (metadata IS NULL OR jsonb_typeof(metadata) IN ('object', 'null'))
            AND coalesce(metadata->'profile'->'provider'->>'auth', metadata->'agent'->'provider'->>'auth') IS NULL
            AND NOT coalesce(metadata, '{}'::jsonb) ? 'accounting_basis';
    """)
    for table in ("task_user_gate_posts", "task_user_gate_messages"):
        # Only a builtin channel type proves a missing bot id.
        await conn.execute(f"UPDATE {table} SET channel_id = 'channel-' || channel WHERE channel_id IS NULL "
                           "AND channel IN ('lark', 'lark_bot', 'telegram')")
    # The old binary wrote NULL for custom bot posts; only an extant bot proves its type.
    # A deleted bot's channel stays NULL: channel_id still identifies its historical replies.
    await conn.execute("UPDATE task_user_gate_posts p SET channel = c.channel_type FROM channel_configs c "
                       "WHERE p.channel IS NULL AND p.channel_id = c.channel_id")
    for table, condition in (
        ("task_user_gate_posts", "channel_id IS NULL"),
        ("task_user_gate_messages", "channel_id IS NULL AND external_id IS NOT NULL AND coalesce(channel, '') <> 'web'"),
    ):
        for row in await conn.fetch(f"SELECT id FROM {table} WHERE {condition}"):
            logger.warning("bridge %s row %s: bot identity cannot be recovered; keeping historical row",
                           table, row["id"])
