"""Allow quality alerts without narrowing sources added by other migrations."""

from . import OnlineStep, _widen_check

name = "metrics_alert_source"
order = 1420
pending_order = 1420
online_order = 1420
_TABLE = "proactive_deliveries"
_CHECK = "proactive_deliveries_source_check"
_ADD = ("metrics_alert",)
_DDL = _widen_check.ddl(_TABLE, "source", _CHECK, _ADD)
fresh_ddl = _DDL + f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_CHECK};\n"
online = (OnlineStep(name, "metrics_alert_source_online", validate=((_TABLE, _CHECK),)),)


async def pending(conn):
    return await _widen_check.pending(conn, _TABLE, "source", _ADD)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection

    if await pending(conn):
        did_work = True
        await conn.execute(_DDL)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name=$1", online[0].marker)
    elif await connection._unvalidated(conn, name):
        did_work = True
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name=$1", online[0].marker)
    return did_work
