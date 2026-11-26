"""Why a schedule is paused, kept apart from the enabled switch."""

from . import OnlineStep, printed_check

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
# PostgreSQL 16 prints these for the validated checks. ``printed_check`` also accepts the NOT VALID form.
_REASON_DEF = (
    "CHECK ((pause_reason = ANY (ARRAY['operator'::text, 'unanswered'::text, "
    "'budget'::text, 'quota'::text])))"
)
_PAIR_DEF = "CHECK (((pause_reason IS NULL) = (paused_at IS NULL)))"
fresh_ddl = ";\n".join((_COLUMNS, _REASON, _PAIR, *(
    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}" for table, constraint in online[0].validate))) + ";"


async def _definition(conn, constraint: str) -> str:
    text = await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = to_regclass('schedules') AND conname = $1", constraint)
    return text or ""


def _matches(definition: str, expected: str) -> bool:
    return printed_check(definition) == printed_check(expected)


async def pending(conn):
    """The three columns and both checks, compared to the full predicate. The marker is not consulted: a dropped
    or rewritten check is pending again even when ``schema_migrations`` still names this migration."""
    columns = await conn.fetchval(
        "SELECT count(*) FROM pg_attribute WHERE attrelid = to_regclass('schedules') "
        "AND attname IN ('pause_reason', 'paused_at', 'unanswered_snooze_until') "
        "AND attnum > 0 AND NOT attisdropped")
    if columns != 3:
        return True
    return not (_matches(await _definition(conn, "schedules_pause_reason_check"), _REASON_DEF)
                and _matches(await _definition(conn, "schedules_pause_pair_check"), _PAIR_DEF))


async def _ensure(conn, constraint: str, ddl: str, expected: str) -> bool:
    if _matches(await _definition(conn, constraint), expected):
        return False
    await conn.execute(f"ALTER TABLE schedules DROP CONSTRAINT IF EXISTS {constraint}")
    await conn.execute(ddl)
    return True


async def apply(conn, *, sql, state) -> bool:
    if not await pending(conn):
        return False
    await conn.execute(_COLUMNS)
    rebuilt = await _ensure(conn, "schedules_pause_reason_check", _REASON, _REASON_DEF)
    rebuilt = await _ensure(conn, "schedules_pause_pair_check", _PAIR, _PAIR_DEF) or rebuilt
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    if rebuilt:
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    return True
