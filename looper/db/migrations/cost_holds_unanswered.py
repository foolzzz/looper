"""A schedule-level unanswered hold carries a push count and no money columns.

``asked_at`` is when the question was actually delivered. The pause clock starts there, so a send that timed out
does not pause the schedule.
"""

from . import OnlineStep, _widen_check, printed_check

name = "cost_holds_unanswered"
order = 1701
pending_order = 1701
online_order = 1701
_SCOPE = ("schedule",)
_WIDEN = _widen_check.ddl("cost_holds", "scope", "cost_holds_scope_check", _SCOPE)
_SCOPE_DDL = (
    "ALTER TABLE cost_holds ADD CONSTRAINT cost_holds_scope_check CHECK "
    "(scope IN ('task', 'schedule_day', 'role_day', 'global_day', 'schedule')) NOT VALID"
)
_NULLABLE = (
    "ALTER TABLE cost_holds ALTER COLUMN currency DROP NOT NULL, "
    "ALTER COLUMN limit_usd DROP NOT NULL, ALTER COLUMN spent_usd DROP NOT NULL"
)
_PUSHES = "ALTER TABLE cost_holds ADD COLUMN IF NOT EXISTS pushes INTEGER CHECK (pushes > 0)"
_ASKED = "ALTER TABLE cost_holds ADD COLUMN IF NOT EXISTS asked_at TIMESTAMPTZ"
_SHAPE = (
    "ALTER TABLE cost_holds ADD CONSTRAINT cost_holds_reason_shape CHECK ("
    "(reason = 'unanswered') = (pushes IS NOT NULL) AND "
    "(reason = 'unanswered') = (limit_usd IS NULL) AND "
    "(limit_usd IS NULL) = (currency IS NULL) AND "
    "(limit_usd IS NULL) = (spent_usd IS NULL)) NOT VALID"
)
online = (OnlineStep(name, "cost_holds_unanswered_online", validate=(
    ("cost_holds", "cost_holds_scope_check"),
    ("cost_holds", "cost_holds_reason_shape"),
)),)
fresh_ddl = _WIDEN + ";\n".join((_NULLABLE, _PUSHES, _ASKED, _SHAPE)) + ";\n" + ";\n".join(
    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}" for table, constraint in online[0].validate) + ";"


async def _definition(conn, constraint: str) -> str:
    text = await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = to_regclass('cost_holds') AND conname = $1", constraint)
    return text or ""


# PostgreSQL 16 prints these for the validated checks. ``printed_check`` also accepts the NOT VALID form.
_SCOPE_DEF = (
    "CHECK ((scope = ANY (ARRAY['task'::text, 'schedule_day'::text, 'role_day'::text, "
    "'global_day'::text, 'schedule'::text])))"
)
_SHAPE_DEF = (
    "CHECK ((((reason = 'unanswered'::text) = (pushes IS NOT NULL)) AND "
    "((reason = 'unanswered'::text) = (limit_usd IS NULL)) AND "
    "((limit_usd IS NULL) = (currency IS NULL)) AND "
    "((limit_usd IS NULL) = (spent_usd IS NULL))))"
)


def _matches(definition: str, expected: str) -> bool:
    return printed_check(definition) == printed_check(expected)


async def pending(conn):
    """Scope, nullability, ``pushes``, ``asked_at`` and the shape check, each against its predicate.

    The marker is not consulted. A missing scope check is pending: widening looks only for a check that
    exists and lacks ``schedule``.
    """
    if not await conn.fetchval("SELECT to_regclass('cost_holds') IS NOT NULL"):
        return True
    nullable = await conn.fetchval(
        "SELECT count(*) = 3 FROM pg_attribute WHERE attrelid = to_regclass('cost_holds') "
        "AND attname IN ('currency', 'limit_usd', 'spent_usd') AND NOT attnotnull "
        "AND attnum > 0 AND NOT attisdropped")
    columns = await conn.fetchval(
        "SELECT count(*) FROM pg_attribute WHERE attrelid = to_regclass('cost_holds') "
        "AND attname IN ('pushes', 'asked_at') AND attnum > 0 AND NOT attisdropped")
    return not (nullable and columns == 2
                and _matches(await _definition(conn, "cost_holds_scope_check"), _SCOPE_DEF)
                and _matches(await _definition(conn, "cost_holds_reason_shape"), _SHAPE_DEF))


async def apply(conn, *, sql, state) -> bool:
    if not await pending(conn):
        return False
    rebuilt = False
    if not _matches(await _definition(conn, "cost_holds_scope_check"), _SCOPE_DEF):
        await conn.execute("ALTER TABLE cost_holds DROP CONSTRAINT IF EXISTS cost_holds_scope_check")
        await conn.execute(_SCOPE_DDL)
        rebuilt = True
    await conn.execute(_NULLABLE)
    await conn.execute(_PUSHES)
    await conn.execute(_ASKED)
    if not _matches(await _definition(conn, "cost_holds_reason_shape"), _SHAPE_DEF):
        await conn.execute("ALTER TABLE cost_holds DROP CONSTRAINT IF EXISTS cost_holds_reason_shape")
        await conn.execute(_SHAPE)
        rebuilt = True
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    if rebuilt:
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    return True
