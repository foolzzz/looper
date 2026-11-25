"""The review_panel migration, in its released execution order."""

name = "review_panel"
order = 470
pending_order = 490


async def pending(conn):
    from .. import connection as legacy

    return await legacy._review_panel_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._review_panel_pending(conn):
        did_work = True
        await conn.execute(
            "ALTER TABLE task_steps ADD COLUMN accept_when JSONB, ADD COLUMN reviews TEXT"
        )
        await conn.execute(
            "ALTER TABLE task_steps DROP CONSTRAINT task_steps_kind_check"
        )
        await conn.execute(
            "ALTER TABLE task_steps ADD CONSTRAINT task_steps_kind_check "
            "CHECK (kind IN ('work', 'gate', 'wait', 'review'))"
        )
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
        # Released workflow presets allowed panels without distinct runtimes. Bring those presets
        # under the panel invariant before readers validate them; existing task snapshots stay fixed.
        await conn.execute(
            "UPDATE workflows SET definition = jsonb_set(definition, "
            "'{distinct_reviewer_runtime}', 'true'::jsonb) "
            "WHERE jsonb_typeof(definition->'review_panel') = 'object' "
            "AND definition->'distinct_reviewer_runtime' IS DISTINCT FROM 'true'::jsonb"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._REVIEW_PANEL
        )
    return did_work
