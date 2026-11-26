"""The dependency_conditions migration, in its released execution order."""

name = "dependency_conditions"
order = 450
pending_order = 470


async def pending(conn):
    from .. import connection as legacy

    return await legacy._dependency_conditions_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._dependency_conditions_pending(conn):
        did_work = True
        await conn.execute("ALTER TABLE task_steps ADD COLUMN ready_when JSONB")
        await conn.execute(
            "ALTER TABLE projects ADD COLUMN dependency_conditions TEXT "
            "CHECK (dependency_conditions IN ('off', 'on'))"
        )
        await conn.execute(
            "ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check"
        )
        # Freeze this upgrade's constraint in the same order as the fresh schema.
        await conn.execute("""ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check CHECK (kind IN (
                'step_accepted', 'step_failed', 'step_retry', 'decision', 'supplement',
                'session_switch', 'handoff', 'progress', 'profile_choice', 'profile_fallback', 'concurrency_change',
                'role_defined', 'gate_opened', 'gate_updated', 'gate_cancelled', 'gate_closed',
                'wait_opened', 'wait_extended', 'wait_closed', 'condition_blocked'))""")
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)",
            legacy._DEPENDENCY_CONDITIONS,
        )
    return did_work
