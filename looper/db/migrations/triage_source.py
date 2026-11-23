"""Let ``proactive_deliveries.source`` name an event triage message.

The check is rebuilt from the values it has now (other migrations widen it too: the morning brief, idle checks), plus
``triage``; see ``_widen_check``.
"""

from . import OnlineStep, _widen_check

name = "triage_source"
order = 1300  # after every widening of the check that is released or open
pending_order = 1300
online_order = 1300
online = (OnlineStep(name, "triage_source_online", validate=(
    ("proactive_deliveries", "proactive_deliveries_source_check"),
)),)

_ADD = ("triage",)
_DDL = _widen_check.ddl("proactive_deliveries", "source", "proactive_deliveries_source_check", _ADD)
fresh_ddl = _DDL + "ALTER TABLE proactive_deliveries VALIDATE CONSTRAINT proactive_deliveries_source_check;\n"


async def pending(conn) -> bool:
    return await _widen_check.pending(conn, "proactive_deliveries", "source", _ADD)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await pending(conn):
        await conn.execute(_DDL)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    elif await legacy._unvalidated(conn, name):
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
