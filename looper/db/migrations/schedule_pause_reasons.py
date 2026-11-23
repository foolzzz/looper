"""Why a schedule is paused, kept apart from the enabled switch."""

from . import OnlineStep

name = "schedule_pause_reasons"
order = 1700
pending_order = 1700
online_order = 1700
online = (OnlineStep(name, "schedule_pause_reasons_online", validate=(
    ("schedules", "schedules_pause_reason_check"),
    ("schedules", "schedules_pause_pair_check"),
)),)

_COLUMNS = (
    "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS pause_reason TEXT, "
    "ADD COLUMN IF NOT EXISTS paused_at TIMESTAMPTZ, "
    "ADD COLUMN IF NOT EXISTS unanswered_snooze_until TIMESTAMPTZ"
)
_REASON = (
    "ALTER TABLE schedules ADD CONSTRAINT schedules_pause_reason_check CHECK "
    "(pause_reason IN ('operator', 'unanswered', 'budget', 'quota')) NOT VALID"
)
_PAIR = (
    "ALTER TABLE schedules ADD CONSTRAINT schedules_pause_pair_check CHECK "
    "((pause_reason IS NULL) = (paused_at IS NULL)) NOT VALID"
)
_REASON_VALUES = ("operator", "unanswered", "budget", "quota")
fresh_ddl = ";\n".join((_COLUMNS, _REASON, _PAIR, *(
    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}" for table, constraint in online[0].validate))) + ";"


async def _definition(conn, constraint: str) -> str:
    text = await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = to_regclass('schedules') AND conname = $1", constraint)
    return text or ""


def _reason_ok(definition: str) -> bool:
    return all(f"'{value}'" in definition for value in _REASON_VALUES)


def _pair_ok(definition: str) -> bool:
    return "pause_reason" in definition and "paused_at" in definition


async def pending(conn):
    """The three columns and both checks are in place. The marker is not consulted: a dropped check is pending
    again even when ``schema_migrations`` still names this migration."""
    columns = await conn.fetchval(
        "SELECT count(*) FROM pg_attribute WHERE attrelid = to_regclass('schedules') "
        "AND attname IN ('pause_reason', 'paused_at', 'unanswered_snooze_until') "
        "AND attnum > 0 AND NOT attisdropped")
    if columns != 3:
        return True
    return not (_reason_ok(await _definition(conn, "schedules_pause_reason_check"))
                and _pair_ok(await _definition(conn, "schedules_pause_pair_check")))


async def _ensure(conn, constraint: str, ddl: str, ok) -> bool:
    if ok(await _definition(conn, constraint)):
        return False
    await conn.execute(f"ALTER TABLE schedules DROP CONSTRAINT IF EXISTS {constraint}")
    await conn.execute(ddl)
    return True


async def apply(conn, *, sql, state):
    if not await pending(conn):
        return
    await conn.execute(_COLUMNS)
    rebuilt = await _ensure(conn, "schedules_pause_reason_check", _REASON, _reason_ok)
    rebuilt = await _ensure(conn, "schedules_pause_pair_check", _PAIR, _pair_ok) or rebuilt
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    if rebuilt:
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
