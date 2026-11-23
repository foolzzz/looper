"""Why a schedule is paused, kept apart from the enabled switch."""

from . import OnlineStep

name = "schedule_pause_reasons"
order = 1450
pending_order = 1450
online_order = 1450
online = (OnlineStep(name, "schedule_pause_reasons_online", validate=(
    ("schedules", "schedules_pause_reason_check"),
    ("schedules", "schedules_pause_pair_check"),
)),)

_DDL = (
    "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS pause_reason TEXT, "
    "ADD COLUMN IF NOT EXISTS paused_at TIMESTAMPTZ, "
    "ADD COLUMN IF NOT EXISTS unanswered_snooze_until TIMESTAMPTZ",
    "ALTER TABLE schedules ADD CONSTRAINT schedules_pause_reason_check CHECK "
    "(pause_reason IN ('operator', 'unanswered', 'budget', 'quota')) NOT VALID",
    "ALTER TABLE schedules ADD CONSTRAINT schedules_pause_pair_check CHECK "
    "((pause_reason IS NULL) = (paused_at IS NULL)) NOT VALID",
)
fresh_ddl = ";\n".join((*_DDL, *(
    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}" for table, constraint in online[0].validate))) + ";"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    columns = await conn.fetchval(
        "SELECT count(*) FROM pg_attribute WHERE attrelid = to_regclass('schedules') "
        "AND attname IN ('pause_reason', 'paused_at', 'unanswered_snooze_until') "
        "AND attnum > 0 AND NOT attisdropped")
    constraints = await conn.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = to_regclass('schedules') "
        "AND conname IN ('schedules_pause_reason_check', 'schedules_pause_pair_check')")
    return not (columns == 3 and constraints == 2)


async def apply(conn, *, sql, state):
    if await pending(conn):
        for ddl in _DDL:
            await conn.execute(ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
