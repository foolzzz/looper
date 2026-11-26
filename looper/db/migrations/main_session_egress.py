"""Let ``manager_events`` record a Manager reply withheld from a chat that is not the operator's.

``kind`` gains ``autonomous`` (what Claude said on its own, after a turn) and ``reason`` gains
``withheld_not_operator``; both checks are rebuilt from the values they have now, see ``_widen_check``.
"""

from . import OnlineStep, _widen_check

name = "main_session_egress"
order = pending_order = online_order = 2130
online = (OnlineStep(name, "main_session_egress_online", validate=(
    ("manager_events", "manager_events_kind_check"),
    ("manager_events", "manager_events_reason_check"),
)),)

_ADD = (("kind", "manager_events_kind_check", ("autonomous",)),
        ("reason", "manager_events_reason_check", ("withheld_not_operator",)))
fresh_ddl = "".join(_widen_check.ddl("manager_events", column, constraint, add) for column, constraint, add in _ADD)
fresh_ddl += "".join(f"ALTER TABLE manager_events VALIDATE CONSTRAINT {constraint};\n" for _, constraint, _ in _ADD)


async def pending(conn) -> bool:
    return any([await _widen_check.pending(conn, "manager_events", column, add) for column, _, add in _ADD])


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await pending(conn):
        await conn.execute("".join(_widen_check.ddl("manager_events", column, constraint, add)
                                   for column, constraint, add in _ADD))
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    elif await legacy._unvalidated(conn, name):
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
