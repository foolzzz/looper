"""Event triage tables and widened checks, after historical migrations."""

from . import OnlineStep

name = "event_triage"
order = 1000
pending_order = 1000
fresh_ddl = """\
-- A self-contained additive migration; existing tables get NOT VALID checks, validated after commit.
ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS subscriptions_output_kind_check;
ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_output_kind_check
    CHECK (output_kind IN ('push', 'leads', 'intents', 'triage')) NOT VALID;
ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS window_seqs BIGINT[];

ALTER TABLE manager_events ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE manager_events DROP CONSTRAINT IF EXISTS manager_events_kind_check;
ALTER TABLE manager_events ADD CONSTRAINT manager_events_kind_check
    CHECK (kind IN ('task_end', 'triage')) NOT VALID;
ALTER TABLE manager_events DROP CONSTRAINT IF EXISTS manager_events_task;
ALTER TABLE manager_events ADD CONSTRAINT manager_events_task
    CHECK ((kind = 'task_end') = (task_id IS NOT NULL)) NOT VALID;
ALTER TABLE manager_events DROP CONSTRAINT IF EXISTS manager_events_reason_check;
ALTER TABLE manager_events ADD CONSTRAINT manager_events_reason_check
    CHECK (reason IN ('off', 'group_chat', 'manager_off', 'turn_failed', 'turn_timeout', 'turn_stopped',
                      'empty_reply', 'send_failed', 'queue_deadline', 'restart', 'no_chat', 'target_changed',
                      'manager_silent')) NOT VALID;

CREATE TABLE IF NOT EXISTS triage_events (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- shown as E-123
    source_id        TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
    schedule_id      TEXT REFERENCES schedules(id) ON DELETE SET NULL,
    run_id           TEXT REFERENCES subscription_runs(run_id) ON DELETE SET NULL,
    record_id        BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    category         TEXT NOT NULL CHECK (category IN ('important', 'digest', 'ignore')),
    title            TEXT NOT NULL DEFAULT '' CHECK (char_length(title) <= 60),
    summary          TEXT NOT NULL DEFAULT '' CHECK (char_length(summary) <= 400),
    reason           TEXT NOT NULL CHECK (btrim(reason) <> '' AND char_length(reason) <= 100),
    reason_kind      TEXT NOT NULL CHECK (reason_kind IN ('model', 'uncited', 'raised')),
    confidence       TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    flagged          BOOLEAN NOT NULL DEFAULT false,
    first_sent_at    TIMESTAMPTZ NOT NULL,
    last_sent_at     TIMESTAMPTZ NOT NULL,
    manager_event_id BIGINT REFERENCES manager_events(id) ON DELETE SET NULL,
    briefed_run_id   TEXT REFERENCES schedule_runs(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT triage_events_manager CHECK (manager_event_id IS NULL OR category = 'important'),
    CONSTRAINT triage_events_briefed CHECK (briefed_run_id IS NULL OR category IN ('digest', 'important')),
    CONSTRAINT triage_events_uncited CHECK (reason_kind <> 'uncited' OR category = 'ignore'),
    CONSTRAINT triage_events_ignored CHECK (category <> 'ignore' OR (title = '' AND summary = '')),
    CONSTRAINT triage_events_sent CHECK (first_sent_at <= last_sent_at)
);
CREATE TABLE IF NOT EXISTS triage_event_sources (
    event_id  BIGINT NOT NULL REFERENCES triage_events(id) ON DELETE CASCADE,
    event_seq BIGINT NOT NULL REFERENCES source_events(seq) ON DELETE CASCADE,
    PRIMARY KEY (event_id, event_seq)
);
CREATE INDEX IF NOT EXISTS idx_triage_event_sources_message ON triage_event_sources (event_seq);
CREATE INDEX IF NOT EXISTS idx_triage_events_run ON triage_events (run_id);
CREATE INDEX IF NOT EXISTS idx_triage_events_schedule ON triage_events (schedule_id);
CREATE INDEX IF NOT EXISTS idx_triage_events_record ON triage_events (record_id);
CREATE INDEX IF NOT EXISTS idx_triage_events_manager ON triage_events (manager_event_id);
CREATE INDEX IF NOT EXISTS idx_triage_events_briefed ON triage_events (briefed_run_id);
CREATE INDEX IF NOT EXISTS idx_triage_events_source ON triage_events (source_id, last_sent_at DESC, id);

-- Deleting any source also removes the event's derived text, even when other sources remain.
CREATE OR REPLACE FUNCTION triage_drop_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM triage_events WHERE id = OLD.event_id;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS triage_event_sources_drop ON triage_event_sources;
CREATE TRIGGER triage_event_sources_drop AFTER DELETE ON triage_event_sources
    FOR EACH ROW EXECUTE FUNCTION triage_drop_event();
"""

online_order = 1000
online = (OnlineStep(name, "event_triage_online", validate=(
    ("subscriptions", "subscriptions_output_kind_check"),
    ("manager_events", "manager_events_kind_check"),
    ("manager_events", "manager_events_task"),
    ("manager_events", "manager_events_reason_check"),
)),)


async def pending(conn) -> bool:
    """Admit an unmarked pre-triage catalog; recorded migrations never repair missing objects."""
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval(
        "SELECT to_regclass('triage_events') IS NOT NULL AND to_regclass('triage_event_sources') IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('subscription_runs') "
        "AND attname = 'window_seqs' AND attnum > 0 AND NOT attisdropped) "
        "AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('manager_events') "
        "AND attname = 'task_id' AND NOT attnotnull AND attnum > 0 AND NOT attisdropped) "
        "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('manager_events') "
        "AND conname = 'manager_events_task') "
        "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('manager_events') "
        "AND conname = 'manager_events_kind_check' AND strpos(pg_get_constraintdef(oid), $1) > 0) "
        "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('manager_events') "
        "AND conname = 'manager_events_reason_check' AND strpos(pg_get_constraintdef(oid), $2) > 0) "
        "AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('subscriptions') "
        "AND conname = 'subscriptions_output_kind_check' AND strpos(pg_get_constraintdef(oid), $1) > 0)",
        "'triage'", "'manager_silent'")


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    elif state == "empty":
        for table, constraint in online[0].validate:
            await conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")
    elif await legacy._unvalidated(conn, name):
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
