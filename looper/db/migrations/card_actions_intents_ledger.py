"""Intent and ledger card kinds without changing the frozen baseline."""

from . import OnlineStep

name = "card_actions_intents_ledger"
order = 1010
pending_order = 1010
fresh_ddl = """\
ALTER TABLE card_actions DROP CONSTRAINT card_actions_kind_check,
    ADD CONSTRAINT card_actions_kind_check
    CHECK (kind IN ('confirmation', 'gate', 'intent', 'ledger')) NOT VALID;
"""
online_order = 1010
online = (OnlineStep(name, "card_actions_intents_ledger_online",
                     validate=(("card_actions", "card_actions_kind_check"),)),)


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass('card_actions') "
        "AND conname = 'card_actions_kind_check' AND strpos(pg_get_constraintdef(oid), $1) > 0 "
        "AND strpos(pg_get_constraintdef(oid), $2) > 0)", "'intent'", "'ledger'")


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    elif state == "empty":
        did_work = True
        for table, constraint in online[0].validate:
            await conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")
    elif await legacy._unvalidated(conn, name):
        did_work = True
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name = $1", online[0].marker)
    return did_work
