"""``idle_checks``: one row per idle check of the default Manager (assistant phase 3 RFC, 4.7).

A new table, so it adds nothing to rows that exist and needs no online step.
"""

name = "idle_checks"
order = 1200
pending_order = 1200
fresh_ddl = """\
CREATE TABLE IF NOT EXISTS idle_checks (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    manager_id  TEXT NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
    local_date  DATE NOT NULL,
    items       JSONB NOT NULL CHECK (jsonb_typeof(items) = 'array'
                                      AND jsonb_array_length(items) BETWEEN 1 AND 10),
    state       TEXT NOT NULL DEFAULT 'running'
                CHECK (state IN ('running', 'silent', 'sent', 'uncertain', 'deferred', 'failed', 'preempted')),
    reason      TEXT CHECK (reason IN ('turn_failed', 'turn_timeout', 'invalid_reply', 'send_rejected',
                                       'delivery_failed', 'restart', 'manager_off')),
    channel_id  TEXT NOT NULL,
    chat        TEXT NOT NULL,
    reply       TEXT CHECK (char_length(reply) <= 2000),
    record_id   BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT idle_checks_finished CHECK ((state = 'running') = (finished_at IS NULL)),
    CONSTRAINT idle_checks_reason CHECK ((state = 'failed') = (reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_idle_checks_recent ON idle_checks (manager_id, created_at DESC);
"""


async def pending(conn) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("SELECT to_regclass('idle_checks') IS NOT NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
