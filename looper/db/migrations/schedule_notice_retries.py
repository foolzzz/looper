"""One episode of one alert kind of one schedule.

``schedule_notifications`` stays an append-only outbox (sent, failed, dashboard) and does not claim a send.
``proactive_deliveries`` records a decision and does not resend. Completion notices stay on ``manager_events``
and are not rows here. This table is the claim for an alert or its recovery: pending, then sending, then sent,
uncertain, gave_up or superseded.
"""

name = "schedule_notice_retries"
order = 2190
pending_order = 2190
fresh_ddl = """\
CREATE TABLE IF NOT EXISTS schedule_notice_retries (
    id              BIGSERIAL PRIMARY KEY,
    schedule_id     TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    -- The alert family. A completion notice is not a row of this table.
    kind            TEXT NOT NULL CHECK (kind IN ('failures', 'stale', 'deadline', 'overlap', 'check', 'skip')),
    -- Increases when that kind's failure period opens. A later period does not reuse an earlier episode.
    episode         INTEGER NOT NULL CHECK (episode >= 1),
    role            TEXT NOT NULL CHECK (role IN ('alert', 'recovery')),
    run_id          TEXT REFERENCES schedule_runs(id) ON DELETE SET NULL,
    -- sending is the claim held across the send. A restart settles it to uncertain.
    state           TEXT NOT NULL CHECK (state IN (
                        'pending', 'sending', 'sent', 'uncertain', 'gave_up', 'superseded')),
    retries         INTEGER NOT NULL DEFAULT 0 CHECK (retries >= 0),
    next_attempt_at TIMESTAMPTZ,
    error           TEXT,
    failures        INTEGER,
    text            TEXT NOT NULL,
    channel         TEXT,
    chat            TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (schedule_id, kind, episode, role)
);
CREATE INDEX IF NOT EXISTS idx_schedule_notice_retries_due
    ON schedule_notice_retries (next_attempt_at) WHERE state = 'pending';
"""


async def pending(conn) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return await conn.fetchval("SELECT to_regclass('schedule_notice_retries') IS NULL")


async def apply(conn, *, sql, state) -> bool:
    if not await pending(conn):
        return False
    await conn.execute(fresh_ddl)
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return True
