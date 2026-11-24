"""Seed the builtin MR review intent type. The rules switch stays off; a custom row with this key is refused."""

from __future__ import annotations

name = "intent_mr_review_type"
order = 1890
pending_order = 1890


def _seed():
    from ...agents.leads_model import MR_REVIEW_GUIDANCE, MR_REVIEW_NAME, RULE_INTENT_TYPE

    return RULE_INTENT_TYPE, MR_REVIEW_NAME, MR_REVIEW_GUIDANCE


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_KEY, _NAME, _GUIDANCE = _seed()
fresh_ddl = f"""
INSERT INTO intent_types (key, builtin, version, enabled)
VALUES ({_sql_literal(_KEY)}, true, 1, true)
ON CONFLICT DO NOTHING;
INSERT INTO intent_type_versions (type_key, version, name, guidance, priority, edited_via)
VALUES ({_sql_literal(_KEY)}, 1, {_sql_literal(_NAME)}, {_sql_literal(_GUIDANCE)}, 'normal', 'builtin')
ON CONFLICT DO NOTHING;
"""


async def pending(conn):
    """Pending when the seed is missing or a custom type already owns the key. A builtin row is settled."""
    if not await conn.fetchval("SELECT to_regclass('intent_types') IS NOT NULL"):
        return False
    row = await conn.fetchrow("SELECT builtin FROM intent_types WHERE key = $1", _KEY)
    if row is None:
        return True
    return not row["builtin"]


async def apply(conn, *, sql, state):
    if not await pending(conn):
        return
    row = await conn.fetchrow("SELECT builtin FROM intent_types WHERE key = $1 FOR UPDATE", _KEY)
    if row is not None:
        raise RuntimeError(
            "intent type mr_review already exists and is not the builtin seed; "
            "rename or remove that type before upgrading. Its versions were not changed.")
    await conn.execute(
        "INSERT INTO intent_types (key, builtin, version, enabled) VALUES ($1, true, 1, true)", _KEY)
    await conn.execute(
        "INSERT INTO intent_type_versions (type_key, version, name, guidance, priority, edited_via) "
        "VALUES ($1, 1, $2, $3, 'normal', 'builtin')", _KEY, _NAME, _GUIDANCE)
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
