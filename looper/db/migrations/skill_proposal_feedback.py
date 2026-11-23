"""Deliver proposal decisions and validation refusals once to the project's next Planner."""

name = "skill_proposal_feedback"
order = 1160
pending_order = 1160
fresh_ddl = """
ALTER TABLE skill_proposal_events
    ADD COLUMN IF NOT EXISTS told_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS name TEXT,
    ADD COLUMN IF NOT EXISTS refusal TEXT CHECK (refusal IN
        ('shape', 'name', 'description', 'content', 'secret', 'limit', 'why', 'evidence', 'off', 'unknown_skill', 'stale')),
    ADD COLUMN IF NOT EXISTS record_id BIGINT REFERENCES llm_records(id) ON DELETE SET NULL;
ALTER TABLE skill_proposal_events ALTER COLUMN proposal_id DROP NOT NULL;
ALTER TABLE skill_proposal_events DROP CONSTRAINT IF EXISTS skill_proposal_events_action_check;
ALTER TABLE skill_proposal_events ADD CONSTRAINT skill_proposal_events_action_check
    CHECK (action IN ('propose', 'confirm', 'reject', 'edit', 'expire', 'supersede', 'restore', 'refuse'));
ALTER TABLE skill_proposal_events DROP CONSTRAINT IF EXISTS skill_proposal_events_refusal;
ALTER TABLE skill_proposal_events ADD CONSTRAINT skill_proposal_events_refusal CHECK (
    (action = 'refuse' AND proposal_id IS NULL AND project_id IS NOT NULL AND name IS NOT NULL
        AND refusal IS NOT NULL AND actor_kind = 'agent' AND via IN ('planner', 'manager'))
    OR (action <> 'refuse' AND proposal_id IS NOT NULL AND refusal IS NULL));
CREATE INDEX IF NOT EXISTS idx_skill_proposal_events_feedback
    ON skill_proposal_events (project_id, id) WHERE action = 'refuse' AND told_at IS NULL;
"""


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('skill_proposal_events') "
        "AND attname = 'told_at' AND NOT attisdropped)")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
