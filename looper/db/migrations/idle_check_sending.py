"""Claim an idle-check send before posting, so recovery never repeats an uncertain send."""

from . import OnlineStep, _widen_check

name = "idle_check_sending"
order = 1480
pending_order = 1480
online_order = 1480
online = (OnlineStep(name, "idle_check_sending_online", validate=(
    ("idle_checks", "idle_checks_state_check"),
)),)

_ADD = ("sending",)
_DDL = _widen_check.ddl("idle_checks", "state", "idle_checks_state_check", _ADD)
fresh_ddl = _DDL + "ALTER TABLE idle_checks VALIDATE CONSTRAINT idle_checks_state_check;\n"


async def pending(conn) -> bool:
    return await _widen_check.pending(conn, "idle_checks", "state", _ADD)


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
