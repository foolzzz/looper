"""Persist drafting waits and serialize bounded draft spend across restarts."""

name = "memory_draft_queue"
order = 1840
pending_order = 1840
fresh_ddl = """
-- Persist waits across restarts. Removing either candidate removes only the wait; the seed can be claimed again.
CREATE TABLE IF NOT EXISTS memory_draft_waits (
    candidate_id          BIGINT PRIMARY KEY REFERENCES memory_candidates(id) ON DELETE CASCADE,
    blocking_candidate_id BIGINT NOT NULL REFERENCES memory_candidates(id) ON DELETE CASCADE,
    reason                TEXT NOT NULL CHECK (reason IN ('target_busy')),
    CHECK (candidate_id <> blocking_candidate_id)
);
-- A single bounded call owns the daily spend allowance. No FK: deleting its seed must not release live spend.
CREATE TABLE IF NOT EXISTS memory_draft_lease (
    id           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    token        UUID NOT NULL,
    candidate_id BIGINT NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL
);
"""


async def pending(conn):
    return not await conn.fetchval("SELECT to_regclass('memory_draft_waits') IS NOT NULL "
                                   "AND to_regclass('memory_draft_lease') IS NOT NULL")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    return did_work
