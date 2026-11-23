"""The leads_check migration, in its released execution order."""

from . import OnlineStep

name = "leads_check"
order = 530
pending_order = 290


async def pending(conn):
    from .. import connection as legacy

    return await legacy._leads_check_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._leads_check_pending(conn):
        await conn.execute(legacy._leads_check_ddl())
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
            legacy._LEADS_CHECK,
        )
        # Its constraints are NOT VALID again: the online step validates them after this commits.
        await conn.execute(
            "DELETE FROM schema_migrations WHERE name = $1", legacy._LEADS_CHECK_ONLINE
        )
    elif state == "empty":
        # schema.sql ran the block, so its constraints are NOT VALID here too: on tables just created, validating
        # them now scans nothing, and a fresh database keeps recording only its baseline.
        for table, constraint in legacy._leads_check_step().validate:
            await conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")
    elif await legacy._leads_check_unvalidated(conn):
        # The block ran outside a migration (schema.sql applied by hand): validated after commit, as an upgrade.
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
            legacy._LEADS_CHECK,
        )
        await conn.execute(
            "DELETE FROM schema_migrations WHERE name = $1", legacy._LEADS_CHECK_ONLINE
        )


online_order = 60
online = (
    OnlineStep(
        migration="leads_check",
        marker="leads_check_online",
        validate=(
            ("agent_setting_layers", "agent_setting_layers_role_check"),
            ("subscription_runs", "subscription_runs_check_status_check"),
            ("schedule_notifications", "schedule_notifications_kind_check"),
            ("message_leads", "message_leads_state_check"),
            ("message_leads", "message_leads_check_verdict_check"),
            ("message_leads", "message_leads_check_reason_check"),
            ("message_leads", "message_leads_check_record_id_fkey"),
            ("message_leads", "message_leads_dropped"),
            ("intents", "intents_state_check"),
            ("intents", "intents_check_verdict_check"),
            ("intents", "intents_check_reason_check"),
            ("intents", "intents_check_record_id_fkey"),
            ("intents", "intents_dropped"),
        ),
        indexes=(),
    ),
)
