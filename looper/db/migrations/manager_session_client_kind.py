"""Let ``manager_sessions`` hold an API client's own Manager session.

``kind`` gains ``client`` (key ``client:api:<client id>``). The check is rebuilt from the values it has now; see
``_widen_check``.
"""

from . import OnlineStep, _widen_check

name = "manager_session_client_kind"
order = pending_order = online_order = 2150
online = (OnlineStep(name, "manager_session_client_kind_online", validate=(
    ("manager_sessions", "manager_sessions_kind_check"),
)),)

_ADD = ("client",)
_DDL = _widen_check.ddl("manager_sessions", "kind", "manager_sessions_kind_check", _ADD)
fresh_ddl = _DDL + "ALTER TABLE manager_sessions VALIDATE CONSTRAINT manager_sessions_kind_check;\n"


async def pending(conn) -> bool:
    return await _widen_check.pending(conn, "manager_sessions", "kind", _ADD)


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
