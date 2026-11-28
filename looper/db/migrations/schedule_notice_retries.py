"""One retry row per schedule notice that was handed to a channel and not confirmed sent.

``schedule_notifications`` stays an append-only outbox (sent, failed, dashboard) and does not claim a send.
``proactive_deliveries`` records a decision and does not resend. ``manager_events`` claims completion notices,
which are a different key. This table is the claim: pending, then sending, then sent, uncertain or gave_up.
"""

name = "schedule_notice_retries"
order = 2190
pending_order = 2190
fresh_ddl = """\
CREATE TABLE IF NOT EXISTS schedule_notice_retries (
    id              BIGSERIAL PRIMARY KEY,
    schedule_id     TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN (
                        'run', 'failures_alert', 'failures_recovered', 'stale_alert', 'stale_recovered',
                        'deadline_alert', 'deadline_recovered', 'overlap_alert', 'overlap_recovered',
                        'check_alert', 'check_recovered', 'skip_alert', 'skip_recovered')),
    run_id          TEXT REFERENCES schedule_runs(id) ON DELETE SET NULL,
    -- '' when run_id is null, so one stale alert (no run) still dedupes. A run notice is never re-armed.
    run_key         TEXT NOT NULL DEFAULT '',
    -- sending is the claim held across the send, not a fourth outcome. A restart settles it to uncertain.
    state           TEXT NOT NULL CHECK (state IN ('pending', 'sending', 'sent', 'uncertain', 'gave_up')),
    retries         INTEGER NOT NULL DEFAULT 0 CHECK (retries >= 0),
    next_attempt_at TIMESTAMPTZ,
    error           TEXT,
    failures        INTEGER,
    text            TEXT NOT NULL,
    channel         TEXT,
    chat            TEXT,
    -- Failure alerts named by a recovery that was sent or may have been sent. Null until then.
    reported_at     TIMESTAMPTZ,
    -- Failure-alert rows a recovery's text names, so a later retry of that recovery reports the same ones.
    summary_of      BIGINT[],
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (schedule_id, kind, run_key)
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
