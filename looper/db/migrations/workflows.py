"""The workflows migration, in its released execution order."""

name = "workflows"
order = 460
pending_order = 480


async def pending(conn):
    from .. import connection as legacy

    return await legacy._workflows_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._workflows_pending(conn):
        await conn.execute("""CREATE TABLE workflows (
                key TEXT PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_-]{0,31}$' AND key <> 'none'),
                name TEXT NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 60),
                description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 300),
                definition JSONB NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
                CREATE UNIQUE INDEX workflows_name_key ON workflows (lower(btrim(name)));
                ALTER TABLE projects ADD COLUMN default_workflow TEXT REFERENCES workflows(key) ON DELETE SET NULL;
                ALTER TABLE tasks ADD COLUMN workflow JSONB;
                ALTER TABLE task_roles DROP CONSTRAINT task_roles_via_check;
                ALTER TABLE task_roles ADD CONSTRAINT task_roles_via_check
                    CHECK (via IN ('planner', 'user', 'template', 'workflow'));""")
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._WORKFLOWS
        )
