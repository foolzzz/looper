"""Let ``manager_events.reason`` name a deferred or capped triage handoff (``delivery_deferred``, ``daily_cap``).

The check is rebuilt from the values it has now, plus these two; see ``_widen_check``.
"""

from . import OnlineStep, _widen_check

name = "triage_handoff_reasons"
order = 1310  # after triage_source
pending_order = 1310
online_order = 1310
online = (OnlineStep(name, "triage_handoff_reasons_online", validate=(
    ("manager_events", "manager_events_reason_check"),
)),)

_ADD = ("delivery_deferred", "daily_cap")
_DDL = _widen_check.ddl("manager_events", "reason", "manager_events_reason_check", _ADD)
fresh_ddl = _DDL + "ALTER TABLE manager_events VALIDATE CONSTRAINT manager_events_reason_check;\n"


async def pending(conn) -> bool:
    return await _widen_check.pending(conn, "manager_events", "reason", _ADD)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await pending(conn):
        did_work = True
        await conn.execute(_DDL)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    elif await legacy._unvalidated(conn, name):
        did_work = True
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    return did_work
