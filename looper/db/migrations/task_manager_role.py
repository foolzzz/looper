"""Register the Task Manager's independent role and per-task profile overrides."""

from . import OnlineStep, _widen_check

name = "task_manager_role"
order = pending_order = online_order = 2090
_CHECKS = (
    ("agent_setting_layers", "role", "agent_setting_layers_role_check"),
    ("task_profile_overrides", "role", "task_profile_overrides_role_check"),
)


async def statements(conn):
    return [_widen_check.ddl(table, column, constraint, ("task_manager",))
            for table, column, constraint in _CHECKS
            if await _widen_check.pending(conn, table, column, ("task_manager",))]


async def pending(conn):
    return bool(await statements(conn))


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if ddl := await statements(conn):
        did_work = True
        for statement in ddl:
            await conn.execute(statement)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    return did_work


online = (OnlineStep(name, "task_manager_role_online", validate=tuple(
    (table, constraint) for table, _, constraint in _CHECKS)),)
fresh_ddl = "\n".join(
    _widen_check.ddl(table, column, constraint, ("task_manager",)) +
    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint};"
    for table, column, constraint in _CHECKS)
