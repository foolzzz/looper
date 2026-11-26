"""Register private retrospectives without rewriting historical runs or the schema baseline."""

from . import OnlineStep, _widen_check

name = "memory_retro_role"
order = 1980
pending_order = 1980
_CHECKS = (
    ("llm_records", "run_kind", "llm_records_run_kind_check"),
    ("agent_setting_layers", "role", "agent_setting_layers_role_check"),
)


async def statements(conn):
    return [_widen_check.ddl(table, column, constraint, ("memory_retro",))
            for table, column, constraint in _CHECKS
            if await _widen_check.pending(conn, table, column, ("memory_retro",))]


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


online_order = 1980
online = (OnlineStep(name, "memory_retro_role_online", validate=tuple(
    (table, constraint) for table, _, constraint in _CHECKS
)),)
fresh_ddl = "\n".join(
    _widen_check.ddl(table, column, constraint, ("memory_retro",)) +
    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint};"
    for table, column, constraint in _CHECKS
)
