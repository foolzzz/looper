"""The lead_taint migration: ``tasks.untrusted_inputs`` and ``agent_sessions.leads_session_id``.

What outside content went into a task (the outbound-review RFC, section 6.3): ``leads`` when a Manager session that
had found leads created it or added to it, ``intent`` when an intent built it; only added, never removed. And the
Manager's session in which a leads search found leads. Constant defaults, so adding them rewrites no row.
"""

name = "lead_taint"
order = 670
pending_order = 600
_TAINT = ("untrusted_inputs TEXT[] NOT NULL DEFAULT '{}' "
          "CHECK (untrusted_inputs <@ ARRAY['leads', 'intent']::text[])")
fresh_ddl = (f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {_TAINT};\n"
             "ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS leads_session_id TEXT;\n")


async def pending(conn):
    from .. import connection

    return await connection._column_pending(conn, name, "tasks", "untrusted_inputs")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(f"ALTER TABLE tasks ADD COLUMN {_TAINT}")
        await conn.execute("ALTER TABLE agent_sessions ADD COLUMN leads_session_id TEXT")
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work
