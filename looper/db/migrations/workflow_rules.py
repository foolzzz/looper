"""The workflow_rules migration, in its released execution order."""

name = "workflow_rules"
order = 480
pending_order = 500


async def pending(conn):
    from .. import connection as legacy

    return await legacy._workflow_rules_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._workflow_rules_pending(conn):
        did_work = True
        constraint = legacy.re.search(
            r"CONSTRAINT task_journal_kind_check (CHECK \(kind IN \(.*?\)\))",
            sql,
            legacy.re.S,
        ).group(1)
        await conn.execute(
            "ALTER TABLE task_journal DROP CONSTRAINT task_journal_kind_check"
        )
        await conn.execute(
            "ALTER TABLE task_journal ADD CONSTRAINT task_journal_kind_check "
            + constraint
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._WORKFLOW_RULES
        )
    return did_work
