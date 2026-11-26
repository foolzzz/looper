"""Give leads-check calls their own run kind without rewriting historical rows."""

from . import OnlineStep
from ..llm_records import RUN_KINDS

name = "leads_check_run_kind"
order = 700
pending_order = 700
_CHECK = "llm_records_run_kind_check"
_KINDS = ", ".join(f"'{kind}'" for kind in RUN_KINDS)
_DDL = (f"ALTER TABLE llm_records DROP CONSTRAINT {_CHECK}, ADD CONSTRAINT "
        f"{_CHECK} CHECK (run_kind IN ({_KINDS}))")
fresh_ddl = _DDL + ";"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('llm_records') "
        "AND conname = 'llm_records_run_kind_check' AND strpos(pg_get_constraintdef(oid), $1) = 0)",
        "'leads_check'",
    )


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        # Validate after commit; the large record table is not scanned under ACCESS EXCLUSIVE.
        did_work = True
        await conn.execute(_DDL + " NOT VALID")
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work


online_order = 160
online = (OnlineStep(name, "leads_check_run_kind_online", validate=(("llm_records", _CHECK),)),)
