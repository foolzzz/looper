"""A schedule-level unanswered hold carries a push count and no money columns."""

from . import OnlineStep, _widen_check

name = "cost_holds_unanswered"
order = 1451
pending_order = 1451
online_order = 1451
_SCOPE = ("schedule",)
_WIDEN = _widen_check.ddl("cost_holds", "scope", "cost_holds_scope_check", _SCOPE)
_COLUMNS = (
    "ALTER TABLE cost_holds ALTER COLUMN currency DROP NOT NULL, "
    "ALTER COLUMN limit_usd DROP NOT NULL, ALTER COLUMN spent_usd DROP NOT NULL",
    "ALTER TABLE cost_holds ADD COLUMN IF NOT EXISTS pushes INTEGER CHECK (pushes > 0)",
    "ALTER TABLE cost_holds ADD CONSTRAINT cost_holds_reason_shape CHECK ("
    "(reason = 'unanswered') = (pushes IS NOT NULL) AND "
    "(reason = 'unanswered') = (limit_usd IS NULL) AND "
    "(limit_usd IS NULL) = (currency IS NULL) AND "
    "(limit_usd IS NULL) = (spent_usd IS NULL)) NOT VALID",
)
online = (OnlineStep(name, "cost_holds_unanswered_online", validate=(
    ("cost_holds", "cost_holds_scope_check"),
    ("cost_holds", "cost_holds_reason_shape"),
)),)
fresh_ddl = _WIDEN + ";\n".join(_COLUMNS) + ";\n" + ";\n".join(
    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}" for table, constraint in online[0].validate) + ";"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    if not await conn.fetchval("SELECT to_regclass('cost_holds') IS NOT NULL"):
        return True
    if await _widen_check.pending(conn, "cost_holds", "scope", _SCOPE):
        return True
    nullable = await conn.fetchval(
        "SELECT count(*) = 3 FROM pg_attribute WHERE attrelid = to_regclass('cost_holds') "
        "AND attname IN ('currency', 'limit_usd', 'spent_usd') AND NOT attnotnull "
        "AND attnum > 0 AND NOT attisdropped")
    pushes = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('cost_holds') "
        "AND attname = 'pushes' AND attnum > 0 AND NOT attisdropped)")
    shape = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('cost_holds') "
        "AND conname = 'cost_holds_reason_shape')")
    return not (nullable and pushes and shape)


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(_WIDEN)
        for ddl in _COLUMNS:
            await conn.execute(ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
