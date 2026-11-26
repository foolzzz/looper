"""Add dormant chat scope storage and preserve existing bindings once on upgrade."""

from . import OnlineStep, _widen_check, printed_check

name = "chat_scopes"
order = pending_order = online_order = 2040

_SETS = """
CREATE TABLE IF NOT EXISTS capability_sets (
    id              TEXT PRIMARY KEY CONSTRAINT capability_sets_id_check
                    CHECK (id IN ('chat_only', 'task_entry', 'readonly_qa', 'full', 'legacy')
                           OR id ~ '^set-[a-z][a-z0-9-]{1,30}$'),
    name            TEXT NOT NULL,
    builtin         BOOLEAN NOT NULL DEFAULT false,
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    deleted_at      TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS capability_set_versions (
    set_id     TEXT NOT NULL REFERENCES capability_sets(id) ON DELETE RESTRICT,
    version    INTEGER NOT NULL CHECK (version >= 1),
    items      JSONB NOT NULL,   -- checked against RFC 5.1 by code before it is written; never updated, only new versions
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (set_id, version),
    -- only full and legacy carry shell; a custom set can never hold it (D8)
    CONSTRAINT capability_set_versions_shell_check
        CHECK ((set_id IN ('full', 'legacy')) = jsonb_path_exists(items, '$[*] ? (@.kind == "shell")'))
);
INSERT INTO capability_sets (id, name, builtin, current_version) VALUES
    ('chat_only', 'Chat only', true, 1), ('full', 'Full', true, 1), ('legacy', 'Legacy', true, 1)
ON CONFLICT (id) DO NOTHING;
INSERT INTO capability_set_versions (set_id, version, items) VALUES
    ('chat_only', 1, '[{"kind": "chat"}]'),
    ('full', 1, '[{"kind": "chat"}, {"kind": "tasks", "projects": "manager", "actions": ["read", "create", "instruct", "start", "stop", "restart"], "visibility": "project", "templates_only": false}, {"kind": "schedules", "actions": ["read", "manage"]}, {"kind": "shell"}]'),
    ('legacy', 1, '[{"kind": "chat"}, {"kind": "tasks", "projects": "manager", "actions": ["read", "create", "instruct", "start", "stop", "restart"], "visibility": "project", "templates_only": false}, {"kind": "schedules", "actions": ["read", "manage"]}, {"kind": "shell"}]')
ON CONFLICT (set_id, version) DO NOTHING;

"""
_BINDING_COLUMNS = """
ALTER TABLE manager_chat_bindings
    ADD COLUMN IF NOT EXISTS default_scope TEXT REFERENCES capability_sets(id) ON DELETE RESTRICT,  -- NULL: chat_only
    ADD COLUMN IF NOT EXISTS legacy_allowed BOOLEAN NOT NULL DEFAULT false,  -- true for bindings that existed at migration
    ADD COLUMN IF NOT EXISTS suggested_scope TEXT REFERENCES capability_sets(id) ON DELETE RESTRICT;  -- display only
"""
_BINDING_CHECKS = """
ALTER TABLE manager_chat_bindings
    -- the group default applies to everyone: never full; legacy only on a pre-existing binding; suggestions never carry shell (D8)
    DROP CONSTRAINT IF EXISTS manager_chat_bindings_scope_check, ADD CONSTRAINT manager_chat_bindings_scope_check
        CHECK (default_scope IS DISTINCT FROM 'full' AND (default_scope IS DISTINCT FROM 'legacy' OR legacy_allowed)),
    DROP CONSTRAINT IF EXISTS manager_chat_bindings_suggested_check, ADD CONSTRAINT manager_chat_bindings_suggested_check
        CHECK (suggested_scope IS NULL OR suggested_scope NOT IN ('full', 'legacy'));

"""
_OVERRIDES = """
CREATE TABLE IF NOT EXISTS binding_principal_scopes (
    channel_id     TEXT NOT NULL,
    chat_id        TEXT NOT NULL,
    sender_id      TEXT NOT NULL,                       -- one row per id: an id belongs to one override only
    principal_id   TEXT NOT NULL,                       -- the ids of one principal share it (a bot's two ids)
    principal_kind TEXT NOT NULL CHECK (principal_kind IN ('person', 'bot')),
    scope_id       TEXT NOT NULL REFERENCES capability_sets(id) ON DELETE RESTRICT
                   CHECK (scope_id NOT IN ('full', 'legacy')),   -- D8
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, chat_id, sender_id),
    FOREIGN KEY (channel_id, chat_id) REFERENCES manager_chat_bindings ON DELETE CASCADE
);

"""
_SESSIONS = """
ALTER TABLE manager_sessions
    ADD COLUMN IF NOT EXISTS scope_id TEXT,
    ADD COLUMN IF NOT EXISTS scope_version INTEGER,
    ADD COLUMN IF NOT EXISTS audience TEXT,
    ADD COLUMN IF NOT EXISTS scope_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retired_reason TEXT,
    ADD COLUMN IF NOT EXISTS activity_cursor BIGINT,          -- the last chat_messages.id group activity read (RFC 8)
    -- D8: scope and audience are both NULL (the default Manager's sessions) or both set, and audience is operator
    -- exactly when the scope is full
    DROP CONSTRAINT IF EXISTS manager_sessions_scope_audience_check, ADD CONSTRAINT manager_sessions_scope_audience_check
        CHECK ((scope_id IS NULL AND audience IS NULL)
               OR (scope_id IS NOT NULL AND audience IS NOT NULL
                   AND (audience = 'operator') = (scope_id = 'full'))),
    -- the key widens: with a fingerprint it ends in '~' and the fingerprint
    DROP CONSTRAINT IF EXISTS manager_sessions_key, ADD CONSTRAINT manager_sessions_key
        CHECK (session_key = kind || ':' || channel_id || ':' || chat || coalesce('~' || scope_fingerprint, ''));

ALTER TABLE manager_sessions DROP CONSTRAINT IF EXISTS manager_sessions_audience_values, ADD CONSTRAINT manager_sessions_audience_values CHECK (audience IN ('operator', 'shared'));
"""
_MESSAGES = """
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS manager_session TEXT,  -- the Manager session that wrote the row (manager_sessions.session_key)
    ADD COLUMN IF NOT EXISTS sender_kind TEXT,
    ADD COLUMN IF NOT EXISTS sender_name TEXT;      -- the display name the channel gave; only for the fence's from
ALTER TABLE chat_messages DROP CONSTRAINT IF EXISTS chat_messages_sender_kind_check, ADD CONSTRAINT chat_messages_sender_kind_check
    CHECK (sender_kind IN ('person', 'bot')) NOT VALID;
"""
_WIDEN = _widen_check.ddl("managers", "session_routing", "managers_session_routing_check", ("scoped",))
online = (OnlineStep(name, "chat_scopes_online", validate=(
    ("chat_messages", "chat_messages_sender_kind_check"),
    ("managers", "managers_session_routing_check"),
)),)
fresh_ddl = "".join((_SETS, _BINDING_COLUMNS, _BINDING_CHECKS, _OVERRIDES, _SESSIONS, _MESSAGES, _WIDEN)) + ";\n".join(
    f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}" for table, constraint in online[0].validate) + ";"

_COLUMNS = {
    "manager_chat_bindings": ("default_scope", "legacy_allowed", "suggested_scope"),
    "manager_sessions": ("scope_id", "scope_version", "audience", "scope_fingerprint", "retired_at",
                         "retired_reason", "activity_cursor"),
    "chat_messages": ("manager_session", "sender_kind", "sender_name"),
}
_KEY_DEF = (
    "CHECK ((session_key = (((((kind || ':'::text) || channel_id) || ':'::text) || chat) "
    "|| COALESCE(('~'::text || scope_fingerprint), ''::text))))"
)


async def _definition(conn, table, constraint):
    return await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = to_regclass($1) AND conname = $2", table, constraint) or ""


async def pending(conn):
    for table in ("capability_sets", "capability_set_versions", "binding_principal_scopes"):
        if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
            return True
    for table, columns in _COLUMNS.items():
        count = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = $1 AND column_name = ANY($2::text[])", table, list(columns))
        if count != len(columns):
            return True
    if printed_check(await _definition(conn, "manager_sessions", "manager_sessions_key")) != printed_check(_KEY_DEF):
        return True
    if not await _definition(conn, "chat_messages", "chat_messages_sender_kind_check"):
        return True
    return await _widen_check.pending(conn, "managers", "session_routing", ("scoped",))


async def apply(conn, *, sql, state) -> bool:
    if not await pending(conn):
        return False
    backfill = not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'manager_chat_bindings' AND column_name = 'legacy_allowed')")
    await conn.execute(_SETS)
    await conn.execute(_BINDING_COLUMNS)
    if backfill:
        await conn.execute("""
            UPDATE manager_chat_bindings b
               SET default_scope = 'legacy', legacy_allowed = true,
                   suggested_scope = CASE WHEN m.tools = 'tasks' THEN NULL ELSE 'chat_only' END
              FROM managers m WHERE m.id = b.manager_id
        """)
    await conn.execute(_BINDING_CHECKS)
    await conn.execute(_OVERRIDES)
    await conn.execute(_SESSIONS)
    await conn.execute(_MESSAGES)
    await conn.execute(_WIDEN)
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    # The sender check is rebuilt NOT VALID; both validations must run again after commit.
    await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    return True
