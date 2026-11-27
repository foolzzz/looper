"""Per-project network allowlist of the task Manager's sandbox, with an audit trail of its changes.

Two new tables, so existing rows are untouched and no online step is needed. A project without a row has the
default policy (no domains, revision 0).
"""

name = "task_manager_sandbox_policy"
order = 2160
pending_order = 2160
fresh_ddl = """\
CREATE TABLE IF NOT EXISTS task_manager_sandbox_policies (
    project_id             TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    network_allowed_domains JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(network_allowed_domains) = 'array'),
    revision               BIGINT NOT NULL CHECK (revision >= 0),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by             TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_manager_sandbox_policy_audit (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT NOT NULL,
    field      TEXT NOT NULL,
    old        JSONB NOT NULL,
    new        JSONB NOT NULL,
    revision   BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_manager_sandbox_policy_audit_project
    ON task_manager_sandbox_policy_audit (project_id, id DESC);
"""


async def pending(conn) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval(
        "SELECT to_regclass('task_manager_sandbox_policies') IS NOT NULL "
        "AND to_regclass('task_manager_sandbox_policy_audit') IS NOT NULL")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work
