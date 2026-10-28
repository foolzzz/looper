"""Upgrade completed schema-3 releases to schema 4."""

import json

BRIDGE_FINGERPRINT = "b2bca629d4d68b756f42bc6d4567b9883985343c120f2543a0f9cda9da6732a3"
_SNAPSHOT_STRUCTURE = "242146a4f16ef11f710e6b8ed7d97c0d0437daaa18666a4c64ffedd64eeb73f0"
# v2.13.165–174 and v2.13.175–184 differ only by execution_snapshot.
# Replaying the older release keeps that column but rewrites the fingerprint.
BRIDGE_CATALOGS = {
    BRIDGE_FINGERPRINT: {
        "5f65ac1439de35108b830a598a22d86e6fb8d2d0bab383038d5ae9a52dcef5b4",
        _SNAPSHOT_STRUCTURE,
    },
    "0c0f43ae1efc09fc486d6dd773f36bca737547b3660d4b6925896b8f306ef6fe": {_SNAPSHOT_STRUCTURE},
}
BASELINE = "post_bridge_baseline"
# SQL and Python ledger entries of the pinned bridge; file_state_import belongs
# to serve, not schema initialization, and is deliberately not required here.
BRIDGE_MIGRATIONS = """
profiles_roles_renamed profile_runtime_effort_levels role_configs_max_concurrency_removed
worker_timeout_default_86400 task_journal_kinds_m7c task_journal_kinds_progress task_journal_supplements
task_journal_kinds_planner_tools task_journal_kinds_task_roles task_journal_kinds_user_gates llm_profiles_removed
agent_runtimes_unused_custom_removed distill_lessons_modes library_catalog_installs agent_setting_layers
agent_setting_layers_agent managers_default_name task_steps_kind_wait task_journal_kinds_waits
task_waits_outcome_ready task_user_gate_messages_kind_late task_user_gate_messages_posted_before_channels
task_user_gate_posts_lark_bot agent_profiles_agent_teams_on library_items_kind_mcp tasks_project_backfill
schedules_notify_channel_lark_bot schedules_notify_alerts schedules_created_via_import schedules_overlap_parallel
schedules_run_timeout_mode schedule_runs_status_s2 schedule_planner_session api_sessions_context_tokens_backfill
llm_records_status_backfill layer_effort_on_profiles concurrency_per_task llm_model_registry_v3
""".split()


async def final_constraints(conn):
    """Fresh and upgraded databases have the same final constraints."""
    await conn.execute("""
        ALTER TABLE agent_setting_layers DROP COLUMN IF EXISTS effort;
        ALTER TABLE task_user_gate_posts DROP CONSTRAINT IF EXISTS task_user_gate_posts_channel_chat_external_id_key;
    """)
    await conn.executemany("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
                          [(name,) for name in ("layer_effort_on_profiles", "concurrency_per_task",
                                                "llm_model_registry_v3", BASELINE)])


async def upgrade_from_bridge(conn):
    """Called only after locked bridge classification, inside ensure_schema's transaction."""
    from ..agents.agent_settings import DEFAULT_HARNESS_PROMPT, _BUILTIN_HARNESS_BEFORE_RENAME
    from ..agents.channels.allowlist import SENDER_ID_FORMATS, sender_ids
    from .tasks import _CONFIG_UNWRAP_LIMIT

    bad = await conn.fetchval("SELECT id FROM llm_records WHERE jsonb_typeof(metadata) NOT IN ('object','null') LIMIT 1")
    if bad is not None:
        raise ValueError(f"bridge llm_records row {bad}: metadata must be an object")
    for row in await conn.fetch("SELECT id, config FROM tasks"):
        # Keep the driver's JSON text decoding and the bridge's eight-level limit.
        value = row["config"]
        try:
            for _ in range(_CONFIG_UNWRAP_LIMIT):
                if not isinstance(value, str):
                    break
                value = json.loads(value)
            else:
                raise ValueError("config exceeds the eight-level decoding limit")
            if not isinstance(value, dict):
                raise ValueError("config must decode to an object within eight levels")
        except ValueError as exc:
            raise ValueError(f"bridge task {row['id']}: {exc}") from exc
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
            raise ValueError(f"bridge channel {row['channel_id']}: config must be an object")
        original = dict(config)
        fmt = SENDER_ID_FORMATS.get(row["channel_type"])
        if fmt and config.get(fmt.field) is not None:
            try:
                config[fmt.field] = list(sender_ids(row["channel_type"], config[fmt.field]))
            except ValueError as exc:
                raise ValueError(f"bridge channel {row['channel_id']}: {exc}") from exc
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
        WHERE NOT metadata ? 'profile' AND jsonb_typeof(metadata->'agent') = 'object';
        UPDATE llm_records SET metadata = coalesce(nullif(metadata, 'null'::jsonb), '{}'::jsonb)
            || '{"accounting_basis":"subscription_equivalent"}'::jsonb
        WHERE coalesce(metadata->'profile'->'provider'->>'auth', metadata->'agent'->'provider'->>'auth') IS NULL
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
        bad = await conn.fetchval(f"SELECT id FROM {table} WHERE {condition} LIMIT 1")
        if bad is not None:
            raise ValueError(f"bridge {table} row {bad}: bot identity cannot be recovered")


async def structure(conn):
    # PG18 also records NOT NULL in pg_constraint; columns.is_nullable already
    # captures it, so exclude the redundant rows to keep witnesses portable.
    queries = {
        "columns": "SELECT table_name,column_name,udt_name,character_maximum_length,numeric_precision,numeric_scale,datetime_precision,collation_name,is_nullable,column_default FROM information_schema.columns WHERE table_schema=current_schema() ORDER BY 1,2",
        "constraints": "SELECT conrelid::regclass::text,conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE connamespace=quote_ident(current_schema())::regnamespace AND contype <> 'n' ORDER BY 1,2",
        "indexes": "SELECT i.tablename,i.indexname,i.indexdef,x.indisvalid,x.indisready FROM pg_indexes i JOIN pg_class c ON c.relname=i.indexname AND c.relnamespace=quote_ident(current_schema())::regnamespace JOIN pg_index x ON x.indexrelid=c.oid WHERE i.schemaname=current_schema() ORDER BY 1,2",
        "functions": "SELECT proname,pg_get_functiondef(oid) FROM pg_proc WHERE pronamespace=quote_ident(current_schema())::regnamespace ORDER BY 1,2",
        "triggers": "SELECT tgname,pg_get_triggerdef(oid),tgenabled::text FROM pg_trigger WHERE tgrelid IN (SELECT oid FROM pg_class WHERE relnamespace=quote_ident(current_schema())::regnamespace) AND NOT tgisinternal ORDER BY 1,2",
        "sequences": "SELECT sequencename,data_type,start_value,min_value,max_value,increment_by,cycle FROM pg_sequences WHERE schemaname=current_schema() ORDER BY 1",
    }
    schema = await conn.fetchval("SELECT current_schema()")
    quoted = '"' + schema.replace('"', '""') + '".'
    # Sort normalized identities in Python's code point order, independently of
    # the database collation used by SQL ORDER BY.
    return {kind: sorted(
        [tuple(value.replace(quoted, "").replace(schema + ".", "") if isinstance(value, str) else value for value in row)
         for row in await conn.fetch(sql)], key=lambda row: row[:2]) for kind, sql in queries.items()}
