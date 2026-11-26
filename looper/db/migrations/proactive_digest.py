"""Allow subscription digest urgency without scanning deliveries under the schema lock."""

from . import OnlineStep

name = "proactive_digest"
order = 595  # after proactive_deliveries creates the table
pending_order = 223
online_order = 0
online = (OnlineStep(name, "proactive_digest_online", validate=(
    ("proactive_deliveries", "proactive_deliveries_urgency_check"),
)),)

_DDL = (
    "ALTER TABLE proactive_deliveries DROP CONSTRAINT proactive_deliveries_urgency_check, "
    "ADD CONSTRAINT proactive_deliveries_urgency_check "
    "CHECK (urgency IN ('normal', 'digest', 'blocking', 'user_set')) NOT VALID"
)
fresh_ddl = _DDL + ";\nALTER TABLE proactive_deliveries VALIDATE CONSTRAINT proactive_deliveries_urgency_check;"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('proactive_deliveries') "
        "AND conname = 'proactive_deliveries_urgency_check' "
        "AND strpos(pg_get_constraintdef(oid), $1) = 0)", "'digest'")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(_DDL)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work
