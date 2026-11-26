"""Skill proposals, their event log, and the project switch that allows them.

A confirmation is a row of ``skill_proposal_events`` that a database check ties
to the operator's dashboard, and a confirmed proposal points at it with a
composite foreign key on the event id, this proposal, the action `confirm`, the dashboard, the user and the hash. So "confirmed"
is provably the operator confirming the proposal's current content hash,
whatever code wrote the row (the same shape as the memory candidates').
"""

name = "skill_proposals"
order = 1150
pending_order = 1150

_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS skill_proposals (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('create', 'update')),
    item_id TEXT,
    base_sha256 TEXT,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    why TEXT NOT NULL CHECK (char_length(why) <= 500),
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence) = 'array'),
    proposer TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'confirmed_edited', 'rejected',
                                           'expired', 'superseded')),
    decided_at TIMESTAMPTZ,
    decision_note TEXT CHECK (char_length(decision_note) <= 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_event_id BIGINT,
    confirmed_actor TEXT CHECK (confirmed_actor = 'user'),
    confirmed_sha256 TEXT,
    -- Constants, set only with a confirmation: the event must be a confirm, through the dashboard.
    confirmed_action TEXT GENERATED ALWAYS AS (CASE WHEN confirmed_event_id IS NOT NULL THEN 'confirm' END) STORED,
    confirmed_via TEXT GENERATED ALWAYS AS (CASE WHEN confirmed_event_id IS NOT NULL THEN 'dashboard' END) STORED,
    CONSTRAINT skill_proposals_target CHECK (
        (action = 'create' AND item_id IS NULL AND base_sha256 IS NULL)
        OR (action = 'update' AND item_id IS NOT NULL AND base_sha256 IS NOT NULL)),
    CONSTRAINT skill_proposals_confirmation CHECK (
        (confirmed_event_id IS NULL) = (confirmed_actor IS NULL)
        AND (confirmed_event_id IS NULL) = (confirmed_sha256 IS NULL)
        AND (status NOT IN ('confirmed', 'confirmed_edited') OR confirmed_event_id IS NOT NULL)
        AND (confirmed_sha256 IS NULL OR confirmed_sha256 = content_sha256))
)""",
    """CREATE TABLE IF NOT EXISTS skill_proposal_events (
    id BIGSERIAL PRIMARY KEY,
    proposal_id BIGINT NOT NULL REFERENCES skill_proposals(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('propose', 'confirm', 'reject', 'edit', 'expire', 'supersede',
                                           'restore')),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'agent', 'system')),
    via TEXT NOT NULL CHECK (via IN ('dashboard', 'planner', 'manager', 'system')),
    content_sha256 TEXT,
    note TEXT CHECK (char_length(note) <= 500),
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, proposal_id, action, via, actor_kind, content_sha256),
    CONSTRAINT skill_proposal_events_user CHECK (
        action NOT IN ('confirm', 'reject', 'edit', 'restore') OR actor_kind = 'user'),
    CONSTRAINT skill_proposal_events_confirm CHECK (
        action <> 'confirm'
        OR (actor_kind = 'user' AND via = 'dashboard' AND content_sha256 IS NOT NULL))
)""",
    """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'skill_proposals_confirmed_event_fk'
          AND conrelid = to_regclass('skill_proposals')
    ) THEN
        ALTER TABLE skill_proposals
            ADD CONSTRAINT skill_proposals_confirmed_event_fk
            FOREIGN KEY (confirmed_event_id, id, confirmed_action, confirmed_via, confirmed_actor, confirmed_sha256)
            REFERENCES skill_proposal_events (id, proposal_id, action, via, actor_kind, content_sha256);
    END IF;
END $$""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_proposals_pending
    ON skill_proposals (project_id, name) WHERE status = 'pending'""",
    "CREATE INDEX IF NOT EXISTS idx_skill_proposals_project ON skill_proposals (project_id, status, id)",
    "CREATE INDEX IF NOT EXISTS idx_skill_proposal_events_proposal ON skill_proposal_events (proposal_id, id)",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS skill_proposals TEXT",
    """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'projects_skill_proposals_check'
          AND conrelid = to_regclass('projects')
    ) THEN
        ALTER TABLE projects
            ADD CONSTRAINT projects_skill_proposals_check
            CHECK (skill_proposals IN ('off', 'on'));
    END IF;
END $$""",
)
fresh_ddl = ";\n".join(_STATEMENTS) + ";"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    # A fresh database already applied fresh_ddl and records only the baseline marker. Check the objects,
    # not the marker alone: a missing table or column is pending whatever the markers say.
    return not await conn.fetchval(
        "SELECT to_regclass('skill_proposals') IS NOT NULL AND to_regclass('skill_proposal_events') IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('projects') "
        "AND attname = 'skill_proposals' AND NOT attisdropped)")


async def apply(conn, *, sql, state) -> bool:
    if not await pending(conn):
        return False
    for statement in _STATEMENTS:
        await conn.execute(statement)
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return True
