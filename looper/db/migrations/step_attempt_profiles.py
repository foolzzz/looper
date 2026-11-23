"""Index a run's attempts by profile, so a profile filter probes that run instead of scanning every attempt."""

from . import OnlineStep

name = "step_attempt_profiles"
order = 1230
pending_order = 1230

INDEX = "idx_step_attempts_run_profile"
INDEX_DEFINITION = "ON step_attempts (task_id, task_run_no, profile_id)"
fresh_ddl = f"CREATE INDEX IF NOT EXISTS {INDEX} {INDEX_DEFINITION};"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return await conn.fetchval(
        "SELECT to_regclass('step_attempts') IS NOT NULL AND to_regclass('idx_step_attempts_run_profile') IS NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        # The table is already there; the resumable runner builds the index after this transaction commits.
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)


online_order = 1230
online = (
    OnlineStep(name, "step_attempt_profiles_online", indexes=((INDEX, INDEX_DEFINITION),)),
)
