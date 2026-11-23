"""Durable account admission and its transition history."""

name = "provider_admission"
order = 1400
pending_order = 1400
fresh_ddl = """
CREATE TABLE IF NOT EXISTS provider_admission (
    provider_id TEXT PRIMARY KEY REFERENCES llm_providers(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('open', 'frozen', 'probing', 'degraded')),
    reason TEXT CHECK (reason IN ('quota', 'provider_fault')),
    until TIMESTAMPTZ,
    until_known BOOLEAN NOT NULL DEFAULT true,
    backoff_step INTEGER NOT NULL DEFAULT 0 CHECK (backoff_step >= 0),
    record_id BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    since TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((state = 'open') = (reason IS NULL)),
    CHECK (state NOT IN ('frozen', 'degraded') OR until IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS provider_admission_events (
    id BIGSERIAL PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES llm_providers(id) ON DELETE CASCADE,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT,
    until TIMESTAMPTZ,
    record_id BIGINT REFERENCES llm_records(id) ON DELETE SET NULL,
    actor TEXT NOT NULL CHECK (actor IN ('code', 'operator')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def pending(conn):
    # Inspect both objects even after a recorded migration: partial restoration
    # must not leave admission without its state or history table.
    return not await conn.fetchval(
        "SELECT to_regclass('provider_admission') IS NOT NULL "
        "AND to_regclass('provider_admission_events') IS NOT NULL"
    )


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute(
            "INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING",
            name,
        )
