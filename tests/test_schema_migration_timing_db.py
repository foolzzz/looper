"""One timing log line per migration or online step that actually runs."""

import asyncio
import logging
import re
from types import SimpleNamespace

import pytest

from looper.db import connection, migrations
from looper.db.migrations import OnlineStep
from tests.db_support import DSN, fresh_connection

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

_DURATION = re.compile(r" in \d+\.\d{3}s$")
_TIMING_PREFIXES = ("migration applied:", "online schema step applied:")


def _timing_lines(records):
    return [r.message for r in records
            if r.levelname == "INFO" and r.message.startswith(_TIMING_PREFIXES)]


def _guarded_migration(name, order, *, online=()):
    """The common shape: apply performs work exactly while the marker is missing."""

    async def pending(conn):
        return not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name)

    async def apply(conn, *, sql, state) -> bool:
        if not await pending(conn):
            return False
        # Plain CREATE TABLE: a replay would raise, proving the guard suppressed it.
        await conn.execute(f"CREATE TABLE {name}_witness (id BIGSERIAL PRIMARY KEY)")
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
        return True

    return SimpleNamespace(name=name, order=order, pending_order=order, online_order=order,
                           fresh_ddl="", online=online, pending=pending, apply=apply)


def _repair_branch_migration(name, order):
    """The catalog probe stays false while apply still has a branch that does work.

    Mirrors the real migrations whose apply validates constraints or repairs markers
    after ``pending()`` comes back false (event_triage, leads_check, run_skips and the
    other constraint-repair migrations).
    """

    async def pending(conn):
        # The baseline catalog already satisfies the probe on every startup.
        return False

    async def apply(conn, *, sql, state) -> bool:
        # The pending=false repair branch: create the witness once and settle the marker.
        if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"{name}_witness"):
            return False
        await conn.execute(f"CREATE TABLE {name}_witness (id BIGSERIAL PRIMARY KEY)")
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        return True

    return SimpleNamespace(name=name, order=order, pending_order=order, online_order=order,
                           fresh_ddl="", online=(), pending=pending, apply=apply)


def _noop_migration(name, order):
    """A genuinely settled migration: pending false and apply never touches the database."""

    async def pending(conn):
        return False

    async def apply(conn, *, sql, state) -> bool:
        return False

    return SimpleNamespace(name=name, order=order, pending_order=order, online_order=order,
                           fresh_ddl="", online=(), pending=pending, apply=apply)


def _patch_registry(monkeypatch, registry, *, online_steps=(), online_orders=None):
    monkeypatch.setattr(connection, "_MIGRATIONS", registry)
    monkeypatch.setattr(connection, "_PENDING_MIGRATIONS", migrations.pending_migrations(registry))
    monkeypatch.setattr(connection, "_ONLINE_STEPS", online_steps)
    monkeypatch.setattr(connection, "_ONLINE_ORDERS", online_orders or {}, raising=False)


def test_ensure_schema_logs_one_timing_line_per_applied_item(caplog, monkeypatch):
    first = _guarded_migration("timing_fake_first", 910001)
    online_step = OnlineStep(migration="timing_fake_second", marker="timing_fake_second_online")
    second = _guarded_migration("timing_fake_second", 910002, online=(online_step,))
    _patch_registry(monkeypatch, (first, second), online_steps=(online_step,),
                    online_orders={"timing_fake_second": second.online_order})

    async def test():
        async with fresh_connection() as conn:
            caplog.set_level(logging.INFO, logger=connection.logger.name)

            await connection.ensure_schema(conn)
            timing = _timing_lines(caplog.records)
            # Exactly one line per applied item: two guarded migrations and one online step.
            assert len(timing) == 3
            assert [message.split(" in ")[0] for message in timing] == [
                "migration applied: timing_fake_first order=910001",
                "migration applied: timing_fake_second order=910002",
                "online schema step applied: timing_fake_second_online order=910002",
            ]
            assert all(_DURATION.search(message) for message in timing)
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE name = ANY($1)",
                                       ["timing_fake_first", "timing_fake_second",
                                        "timing_fake_second_online"]) == 3
            assert any(r.message == "schema ensured" and r.levelname == "INFO" for r in caplog.records)

            # A second start applies nothing: no timing lines and no replay of the DDL.
            caplog.clear()
            await connection.ensure_schema(conn)
            assert not _timing_lines(caplog.records)
            assert any(r.message == "schema ensured" and r.levelname == "INFO" for r in caplog.records)

    asyncio.run(test())


def test_ensure_schema_logs_work_done_on_a_pending_false_branch(caplog, monkeypatch):
    # A no-work migration stays silent alongside one whose apply works while pending() is false.
    repair = _repair_branch_migration("timing_fake_repair", 910003)
    noop = _noop_migration("timing_fake_noop", 910004)
    _patch_registry(monkeypatch, (repair, noop))

    async def test():
        async with fresh_connection() as conn:
            caplog.set_level(logging.INFO, logger=connection.logger.name)
            # The probe is satisfied throughout, exactly as it is for the real repair branches.
            assert not await repair.pending(conn)

            await connection.ensure_schema(conn)
            timing = _timing_lines(caplog.records)
            # Exactly one line, for the migration that actually worked; the no-op stays silent.
            assert len(timing) == 1
            assert timing[0].split(" in ")[0] == "migration applied: timing_fake_repair order=910003"
            assert _DURATION.search(timing[0])
            assert await conn.fetchval("SELECT to_regclass('timing_fake_repair_witness') IS NOT NULL")
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", "timing_fake_repair")

            # The repair is now settled: a second start runs neither branch and logs nothing.
            caplog.clear()
            await connection.ensure_schema(conn)
            assert not _timing_lines(caplog.records)

    asyncio.run(test())


# apply's bool is what the startup timing line follows. These migrations gained that
# result after the first conversion; pin both values, not only the annotation.
_APPLY_RESULT_MIGRATIONS = (
    "task_origin_session",
    "task_drafts",
    "task_work_list",
    "task_select_reads",
    "task_select_directories",
    "task_manager_role",
    "proactive_bodies",
    "main_session_egress",
    "completion_session_allowlist",
    "network_pauses",
    "chat_message_entry_kind",
    "manager_session_client_kind",
)


def _prefix_registry(migration):
    earlier = tuple(item for item in connection._MIGRATIONS if item.order < migration.order)
    return earlier, migrations.online_steps(earlier), {
        item.name: item.online_order for item in earlier if getattr(item, "online", ())}


async def _validate_online(conn, migration):
    """Settle NOT VALID checks the way the post-commit online phase does.

    A second ``apply`` still reports work while ``_unvalidated`` is true
    (``main_session_egress``, ``completion_session_allowlist``). Validation is
    what makes the next startup's ``apply`` return false.
    """
    for step in getattr(migration, "online", ()) or ():
        for table, constraint in step.validate:
            await conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")


@pytest.mark.parametrize("migration_name", _APPLY_RESULT_MIGRATIONS)
def test_apply_returns_true_only_while_pending(migration_name, monkeypatch):
    migration = next(item for item in connection._MIGRATIONS if item.name == migration_name)
    earlier, online_steps, online_orders = _prefix_registry(migration)

    async def test():
        async with fresh_connection(command_timeout=60) as conn:
            _patch_registry(monkeypatch, earlier, online_steps=online_steps, online_orders=online_orders)
            await connection.ensure_schema(conn)
            # apply reads the real online steps (the unvalidated repair looks them up).
            monkeypatch.undo()
            # The baseline CREATE TABLE already has this column, so a fresh database is
            # not pending. Dropping it is the upgrade pending() describes.
            if migration.name == "task_origin_session":
                await conn.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS origin_session")
                await conn.execute("DELETE FROM schema_migrations WHERE name = $1", migration.name)
            assert await migration.pending(conn) is True
            assert await migration.apply(conn, sql="", state="current") is True
            await _validate_online(conn, migration)
            assert await migration.apply(conn, sql="", state="current") is False

    asyncio.run(test())


# Every migration whose apply started reporting whether it did work. A fresh
# database already contains most of these upgrades, so the catalog is rewound to
# the older shape pending() recognizes. The first apply is that upgrade. Online
# validation and index builds then settle it, and the next apply must be a no-op.
_CONVERTED_MIGRATIONS = (
    "acceptance_checks",
    "acp_command",
    "action_receipts",
    "advisor_roles",
    "agent_spawns",
    "attempt_ids",
    "card_actions",
    "card_actions_intents_ledger",
    "channel_outbox",
    "channel_outbox_terminal_at",
    "chat_binding_sender_policy",
    "chat_binding_senders",
    "chat_direct_threads",
    "chat_message_entry_kind",
    "chat_scopes",
    "coco_login",
    "command_schedules",
    "completion_session_allowlist",
    "cost_holds",
    "cost_holds_unanswered",
    "cursor_login",
    "decision_snapshots",
    "dependency_conditions",
    "event_triage",
    "failure_typing",
    "gate_message_sender",
    "gate_purpose",
    "gateway_aliases",
    "headless_compaction",
    "idle_check_sending",
    "idle_check_source",
    "idle_checks",
    "intent_final_notices",
    "intent_group_reads",
    "intent_merges",
    "intent_mr_review_type",
    "intent_recognitions",
    "intent_rule_execution",
    "intent_rules",
    "journal_supplement_sender",
    "lead_taint",
    "leads_check",
    "leads_check_run_kind",
    "leads_search",
    "leads_trigram",
    "ledger",
    "llm_models_chat_dialect",
    "llm_requests",
    "llm_requests_stored_bytes",
    "llm_requests_translation",
    "main_session_egress",
    "manager_event_delivery",
    "manager_event_reasons",
    "manager_events",
    "manager_scope_projects",
    "manager_session_bounds",
    "manager_session_client_kind",
    "manager_session_context_policy",
    "manager_sessions",
    "mcp_tool_levels",
    "memory_candidates",
    "memory_closeout",
    "memory_draft_queue",
    "memory_draft_role",
    "memory_lesson_injection",
    "memory_lessons",
    "memory_retro_queue",
    "memory_retro_role",
    "message_collection",
    "message_leads",
    "message_stream",
    "message_stream_deletions",
    "metrics_alert_source",
    "mid_turn_input",
    "model_name_parameters",
    "morning_brief_kind",
    "morning_brief_send_started",
    "morning_brief_triage",
    "morning_briefs",
    "network_pauses",
    "outbound_reviews",
    "platform_subagents",
    "proactive_bodies",
    "proactive_deliveries",
    "proactive_digest",
    "processor_role",
    "profile_blocks",
    "profile_fallback",
    "profile_web_search",
    "project_activity",
    "project_advisor",
    "project_knowledge_dir",
    "provider_admission",
    "provider_enabled",
    "provider_limit_windows",
    "reminder_post_started",
    "reminders",
    "review_panel",
    "run_skips",
    "run_tree",
    "runtime_launcher",
    "scenario_session_routing",
    "schedule_deferred_by_drain",
    "schedule_equivalent_budget",
    "schedule_missed_since",
    "schedule_notification_events",
    "schedule_notify_format",
    "schedule_pause_reasons",
    "schedule_session_bounds",
    "schedule_throttle",
    "schedule_user_gates",
    "search_tokens",
    "skill_lifecycle",
    "skill_proposal_feedback",
    "skill_proposals",
    "skill_versions",
    "step_attempt_profiles",
    "step_attempts",
    "step_check_runs",
    "step_difficulty",
    "step_failure_handling",
    "subagent_room_delivery",
    "subscription_check_record",
    "subscription_lookback",
    "subscription_rules",
    "subscription_window_seqs",
    "subscriptions",
    "task_chat_group",
    "task_drafts",
    "task_feedback",
    "task_manager_role",
    "task_origin_session",
    "task_outcomes",
    "task_process_record",
    "task_reply_route",
    "task_select_directories",
    "task_select_reads",
    "task_work_list",
    "triage_handoff_reasons",
    "triage_source",
    "uncertain_deliveries",
    "verification_paths",
    "workflow_rules",
    "workflows",
)

# Rewind SQL, then a CHECK value to remove so the released constraint is pending again.
# Historical apply text rejects rows the current seeds added later; those rows go too.
_UPGRADE_FROM = {
    "acp_command": ("", ("llm_providers", "llm_providers_vendor_type_check", "acp-command")),
    "advisor_roles": ("", ("agent_setting_layers", "agent_setting_layers_role_check", "advice_check")),
    "agent_spawns": ("DROP TABLE IF EXISTS agent_spawns CASCADE", None),
    "card_actions": ("DROP TABLE IF EXISTS card_actions CASCADE", None),
    "channel_outbox": ("ALTER TABLE channel_outbox DROP COLUMN IF EXISTS claim_token", None),
    "chat_binding_sender_policy": (
        "ALTER TABLE manager_chat_bindings DROP COLUMN IF EXISTS sender_policy", None),
    "chat_binding_senders": (
        "ALTER TABLE manager_chat_bindings DROP COLUMN IF EXISTS allowed_senders", None),
    "coco_login": (
        "DELETE FROM llm_providers WHERE vendor_type = 'coco-login'",
        ("llm_providers", "llm_providers_vendor_type_check", "coco-login")),
    "command_schedules": ("ALTER TABLE schedules DROP COLUMN IF EXISTS kind", None),
    "cursor_login": (
        "DELETE FROM llm_models WHERE provider_id IN ('prov-cursor-login', 'prov-coco-login'); "
        "DELETE FROM llm_providers WHERE vendor_type IN ('cursor-login', 'coco-login', 'acp-command')",
        ("llm_providers", "llm_providers_vendor_type_check", "cursor-login")),
    "decision_snapshots": ("DROP TABLE IF EXISTS decision_snapshots CASCADE", None),
    "dependency_conditions": (
        "ALTER TABLE task_steps DROP COLUMN IF EXISTS ready_when; "
        "ALTER TABLE projects DROP COLUMN IF EXISTS dependency_conditions", None),
    "failure_typing": (
        "ALTER TABLE llm_records DROP CONSTRAINT IF EXISTS llm_records_failure_check; "
        "ALTER TABLE llm_records DROP COLUMN IF EXISTS failure_class, DROP COLUMN IF EXISTS failure_cause",
        None),
    "gate_purpose": ("ALTER TABLE task_user_gates DROP COLUMN IF EXISTS purpose", None),
    "gateway_aliases": ("DROP TABLE IF EXISTS gateway_aliases CASCADE", None),
    "headless_compaction": (
        "UPDATE agent_runtimes SET capabilities = array_remove(array_remove(capabilities, "
        "'headless_compaction'), 'mid_turn_input')",
        ("agent_runtimes", "agent_runtimes_capabilities_check", "headless_compaction")),
    "intent_merges": ('DROP TRIGGER IF EXISTS "A_intents_detach_merged" ON intents', None),
    "leads_check_run_kind": ("", ("llm_records", "llm_records_run_kind_check", "leads_check")),
    "ledger": ("DROP TABLE IF EXISTS ledger_items CASCADE", None),
    "llm_models_chat_dialect": ("ALTER TABLE llm_models DROP COLUMN IF EXISTS chat_dialect", None),
    "llm_requests": ("DROP TABLE IF EXISTS llm_requests CASCADE", None),
    "llm_requests_stored_bytes": (
        "ALTER TABLE llm_requests DROP COLUMN IF EXISTS stored_bytes; "
        "ALTER TABLE llm_request_parts DROP COLUMN IF EXISTS stored_bytes", None),
    "llm_requests_translation": ("ALTER TABLE llm_requests DROP COLUMN IF EXISTS translation", None),
    "manager_event_delivery": ("ALTER TABLE manager_events DROP COLUMN IF EXISTS delivery", None),
    "manager_event_reasons": ("", ("manager_events", "manager_events_reason_check", "no_chat")),
    "manager_events": ("DROP TABLE IF EXISTS manager_events CASCADE", None),
    "manager_scope_projects": (
        "ALTER TABLE managers DROP CONSTRAINT IF EXISTS managers_scope_tools_check; "
        "ALTER TABLE managers DROP CONSTRAINT IF EXISTS managers_scope_projects_check; "
        "ALTER TABLE managers DROP COLUMN IF EXISTS scope_project_ids", None),
    "manager_sessions": ("DROP TABLE IF EXISTS manager_sessions CASCADE", None),
    "mcp_tool_levels": ("DROP TABLE IF EXISTS mcp_tool_levels CASCADE", None),
    "memory_candidates": ("DROP TABLE IF EXISTS memory_candidates CASCADE", None),
    "message_leads": ("DROP TABLE IF EXISTS message_leads CASCADE", None),
    "message_stream": ("DROP TABLE IF EXISTS event_sources CASCADE", None),
    "message_stream_deletions": (
        "ALTER TABLE event_sources DROP COLUMN IF EXISTS retention_last_at; "
        "ALTER TABLE source_runs DROP COLUMN IF EXISTS skipped_by_deletion", None),
    "mid_turn_input": (
        "UPDATE agent_runtimes SET capabilities = array_remove(capabilities, 'mid_turn_input')",
        ("agent_runtimes", "agent_runtimes_capabilities_check", "mid_turn_input")),
    "outbound_reviews": ("DROP TABLE IF EXISTS outbound_reviews CASCADE", None),
    "proactive_deliveries": ("DROP TABLE IF EXISTS proactive_deliveries CASCADE", None),
    "processor_role": (
        "UPDATE agent_runtimes SET capabilities = array_remove(array_remove(capabilities, "
        "'headless_compaction'), 'mid_turn_input')",
        ("agent_setting_layers", "agent_setting_layers_role_check", "processor")),
    "profile_fallback": ("ALTER TABLE agent_profiles DROP COLUMN IF EXISTS fallback_profile_id", None),
    "profile_web_search": ("ALTER TABLE agent_profiles DROP COLUMN IF EXISTS web_search", None),
    "project_activity": ("DROP TABLE IF EXISTS project_activity CASCADE", None),
    "project_advisor": (
        "ALTER TABLE projects DROP COLUMN IF EXISTS advisor, "
        "DROP COLUMN IF EXISTS advisor_consult_budget_usd, DROP COLUMN IF EXISTS advisor_budget_usd, "
        "DROP COLUMN IF EXISTS advisor_consults_per_task, DROP COLUMN IF EXISTS advisor_consults_per_step, "
        "DROP COLUMN IF EXISTS advisor_fallback_profile, DROP COLUMN IF EXISTS advisor_internal_domains",
        None),
    "provider_enabled": ("ALTER TABLE llm_providers DROP COLUMN IF EXISTS enabled", None),
    "provider_limit_windows": ("DROP TABLE IF EXISTS provider_limit_windows CASCADE", None),
    "reminders": ("DROP TABLE IF EXISTS reminders CASCADE", None),
    "review_panel": (
        "ALTER TABLE task_steps DROP COLUMN IF EXISTS accept_when, DROP COLUMN IF EXISTS reviews", None),
    "run_tree": (
        "ALTER TABLE llm_records DROP CONSTRAINT IF EXISTS llm_records_run_kind_check; "
        "ALTER TABLE llm_records DROP CONSTRAINT IF EXISTS llm_records_parent_record_id_fkey; "
        "ALTER TABLE llm_records DROP COLUMN IF EXISTS parent_record_id, DROP COLUMN IF EXISTS run_kind",
        None),
    "runtime_launcher": ("ALTER TABLE agent_runtimes DROP COLUMN IF EXISTS launcher", None),
    "schedule_deferred_by_drain": (
        "ALTER TABLE schedule_runs DROP COLUMN IF EXISTS deferred_by_drain", None),
    "schedule_missed_since": ("ALTER TABLE schedule_runs DROP COLUMN IF EXISTS missed_since", None),
    "schedule_notification_events": (
        "ALTER TABLE schedule_notifications DROP COLUMN IF EXISTS manager_event_id", None),
    "schedule_notify_format": ("ALTER TABLE schedules DROP COLUMN IF EXISTS notify_format", None),
    "schedule_session_bounds": (
        "ALTER TABLE schedules DROP COLUMN IF EXISTS planner_session_context_tokens, "
        "DROP COLUMN IF EXISTS planner_session_run_cost_usd; "
        "ALTER TABLE schedule_runs DROP COLUMN IF EXISTS planner_context_tokens, "
        "DROP COLUMN IF EXISTS planner_cost_usd, DROP COLUMN IF EXISTS planner_session_action, "
        "DROP COLUMN IF EXISTS planner_session_reason, DROP COLUMN IF EXISTS planner_session_before_tokens, "
        "DROP COLUMN IF EXISTS planner_session_after_tokens, DROP COLUMN IF EXISTS failure_kind", None),
    "schedule_user_gates": ("ALTER TABLE schedules DROP COLUMN IF EXISTS user_gates", None),
    "step_difficulty": ("ALTER TABLE task_steps DROP COLUMN IF EXISTS difficulty", None),
    "subscription_lookback": ("DROP TABLE IF EXISTS subscription_daily_claims", None),
    "subscription_rules": (
        "ALTER TABLE subscriptions DROP COLUMN IF EXISTS rules, DROP COLUMN IF EXISTS config_epoch; "
        "ALTER TABLE subscription_runs DROP COLUMN IF EXISTS config, DROP COLUMN IF EXISTS config_epoch, "
        "DROP COLUMN IF EXISTS match_timeouts", None),
    "subscriptions": ("DROP TABLE IF EXISTS subscriptions CASCADE", None),
    "task_chat_group": ("ALTER TABLE tasks DROP COLUMN IF EXISTS chat_group", None),
    "task_origin_session": ("ALTER TABLE tasks DROP COLUMN IF EXISTS origin_session", None),
    "task_process_record": ("ALTER TABLE tasks DROP COLUMN IF EXISTS process_record", None),
    "task_reply_route": ("ALTER TABLE tasks DROP COLUMN IF EXISTS origin_reply_route", None),
    "uncertain_deliveries": (
        "DROP INDEX IF EXISTS idx_subscription_deliveries_uncertain; "
        "DROP INDEX IF EXISTS idx_intent_posts_uncertain; "
        "DROP INDEX IF EXISTS idx_manager_events_uncertain", None),
    "verification_paths": ("ALTER TABLE projects DROP COLUMN IF EXISTS verification_paths", None),
    "workflow_rules": ("", ("task_journal", "task_journal_kind_check", "verification_round")),
    "workflows": (
        "ALTER TABLE projects DROP COLUMN IF EXISTS default_workflow; "
        "ALTER TABLE tasks DROP COLUMN IF EXISTS workflow; "
        "DROP TABLE IF EXISTS workflows CASCADE", None),
}


def _without_check_token(definition, token):
    for needle in (f"'{token}'::text", f"'{token}'"):
        for pattern in (f", {needle}", f"{needle}, ", needle):
            if pattern in definition:
                return definition.replace(pattern, "", 1)
    raise AssertionError(f"{token} is not in {definition}")


async def _restore_older_check(conn, table, constraint, token):
    definition = await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = to_regclass($1) AND conname = $2", table, constraint)
    updated = _without_check_token(definition, token).replace(" NOT VALID", "")
    await conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
    await conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} {updated}")


# Their schema.sql block is present on a fresh database, so pending() stays false; apply's
# first True is the NOT VALID constraint repair, judged through connection._unvalidated.
_CONSTRAINT_REPAIR_MIGRATIONS = ("leads_check", "run_skips")


@pytest.mark.parametrize("migration_name", _CONVERTED_MIGRATIONS)
def test_converted_migration_apply_returns_true_then_false(migration_name, monkeypatch):
    migration = next(item for item in connection._MIGRATIONS if item.name == migration_name)
    earlier, online_steps, online_orders = _prefix_registry(migration)
    schema = migrations.schema_sql(connection.SCHEMA_PATH.read_text(encoding="utf-8"), connection._MIGRATIONS)

    async def test():
        async with fresh_connection(command_timeout=120) as conn:
            _patch_registry(monkeypatch, earlier, online_steps=online_steps, online_orders=online_orders)
            await connection.ensure_schema(conn)
            # apply and the online phase read the real registry, including this migration's steps.
            monkeypatch.undo()
            rewind, older_check = _UPGRADE_FROM.get(migration.name, ("", None))
            if rewind:
                await conn.execute(rewind)
            if older_check:
                await _restore_older_check(conn, *older_check)
            if migration.name in _UPGRADE_FROM:
                await conn.execute("DELETE FROM schema_migrations WHERE name = ANY($1::text[])",
                                   [migration.name, f"{migration.name}_online"])
            # Pin the state behind apply's bool too: without this, a fixture that had
            # already applied the migration could make "True then False" pass for the
            # wrong reason. Most migrations are catalog-pending; the two constraint-repair
            # migrations ship their block in the baseline as NOT VALID, so pending() is
            # already false and the open work apply() reports is the unvalidated
            # constraints, which is exactly the branch its apply takes.
            if migration.name in _CONSTRAINT_REPAIR_MIGRATIONS:
                assert await migration.pending(conn) is False
                assert await connection._unvalidated(conn, migration.name) is True
            else:
                assert await migration.pending(conn) is True
            assert await migration.apply(conn, sql=schema, state="current") is True
            await _validate_online(conn, migration)
            # These three keep reporting work until their own online indexes exist. The
            # full online phase would also run steps for markers another apply inserts
            # before that step's constraint exists (manager_events).
            if migration.name in {"memory_lesson_injection", "memory_lessons", "memory_retro_queue"}:
                assert not conn.is_in_transaction()
                await connection._finish_online(conn)
            assert await migration.apply(conn, sql=schema, state="current") is False
            # Settled on every path: the catalog probe is closed, and a repair migration
            # no longer has an unvalidated constraint for its apply branch to report.
            assert await migration.pending(conn) is False
            if migration.name in _CONSTRAINT_REPAIR_MIGRATIONS:
                assert await connection._unvalidated(conn, migration.name) is False

    asyncio.run(test())
