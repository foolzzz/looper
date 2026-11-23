"""Schedule throttle columns and skipped_busy; validate large-table checks online."""

from . import OnlineStep

name = "schedule_throttle"
order = 1000
pending_order = 1000
online_order = 1000
online = (OnlineStep(name, "schedule_throttle_online", validate=(
    ("schedules", "schedules_active_hours_check"),
    ("schedule_runs", "schedule_runs_status_check"),
)),)

_DDL = (
    "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS active_hours TEXT, "
    "ADD COLUMN IF NOT EXISTS idle_only BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE schedules ADD CONSTRAINT schedules_active_hours_check CHECK ("
    "active_hours ~ '^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$' "
    "AND split_part(active_hours, '-', 1) <> split_part(active_hours, '-', 2)) NOT VALID",
    "ALTER TABLE schedule_runs DROP CONSTRAINT schedule_runs_status_check, "
    "ADD CONSTRAINT schedule_runs_status_check CHECK (status IN ("
    "'queued', 'gating', 'running', 'completed', 'failed', 'stopped', 'cancelled', 'timed_out', "
    "'gate_failed', 'skipped_overlap', 'skipped_missed', 'skipped_gate', 'skipped_budget', "
    "'succeeded', 'interrupted', 'skipped_busy')) NOT VALID",
)
fresh_ddl = ";\n".join((*_DDL, *(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}"
                                  for table, constraint in online[0].validate))) + ";"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('schedules') "
        "AND attname = 'idle_only' AND NOT attisdropped)")


async def apply(conn, *, sql, state):
    if await pending(conn):
        for ddl in _DDL:
            await conn.execute(ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
