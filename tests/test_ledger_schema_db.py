"""The ledger tables: what the schema itself refuses (even from raw SQL), and the upgrade of a database without them."""

import asyncpg
import pytest

from looper.db import connection
from tests import ledger_support as sup
from tests.db_support import DSN, dropped_tables, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

ITEM = "INSERT INTO ledger_items (kind, title, status) VALUES ('{kind}', 'T', 'open') RETURNING id"
EVENT = ("INSERT INTO ledger_events (item_id, item_sourced, action, actor_kind, relayed_by, via) "
         "VALUES ($1, false, $2, $3, $4, $5) RETURNING id")


def test_a_relayed_event_cannot_be_referenced_by_a_confirmation_or_a_closing(monkeypatch):
    async def check(pool):
        decision = await pool.fetchval(ITEM.format(kind="decision"))
        pending = await pool.fetchval(ITEM.format(kind="pending"))
        relayed = await pool.fetchval(EVENT, decision, "add", "relayed", "someone", "api")
        relayed_pending = await pool.fetchval(EVENT, pending, "add", "relayed", "someone", "api")
        with pytest.raises(asyncpg.ForeignKeyViolationError):  # the event exists, but not as a 'user' one
            await pool.execute("UPDATE ledger_items SET confirmed_event_id = $1, confirmed_actor = 'user' "
                               "WHERE id = $2", relayed, decision)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute("UPDATE ledger_items SET status = 'done', closed_at = now(), closed_event_id = $1, "
                               "closed_actor = 'user' WHERE id = $2", relayed_pending, pending)
        for column in ("confirmed_actor", "closed_actor"):  # and the actor cannot be claimed to be someone else
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(f"UPDATE ledger_items SET {column} = 'relayed' WHERE id = $1", pending)
        user = await pool.fetchval(EVENT, pending, "close", "user", None, "dashboard")
        await pool.execute("UPDATE ledger_items SET status = 'done', closed_at = now(), closed_event_id = $1, "
                           "closed_actor = 'user' WHERE id = $2", user, pending)

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("action", ["confirm", "close", "cancel", "supersede", "reopen", "amend"])
def test_only_add_and_note_can_be_relayed_and_the_system_only_adds(monkeypatch, action):
    async def check(pool):
        item = await pool.fetchval(ITEM.format(kind="decision"))
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.fetchval(EVENT, item, action, "relayed", "someone", "api")
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.fetchval(EVENT, item, action, "system", None, "system")
        for allowed in ("add", "note"):
            await pool.fetchval(EVENT, item, allowed, "relayed", "someone", "api")
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.fetchval(EVENT, item, "note", "system", None, "system")

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("values", [
    ("relayed", None, "api"),            # a relayed event says who relayed it
    ("user", "someone", "dashboard"),    # a user event is not relayed
    ("user", None, "cli"),               # a command-line event names its session
    ("relayed", "someone", "cli"),       # and is always the user's
    ("user", None, "system"),            # via system is the system's
])
def test_an_event_says_who_and_how(monkeypatch, values):
    async def check(pool):
        item = await pool.fetchval(ITEM.format(kind="pending"))
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.fetchval(EVENT, item, "add", values[0], values[1], values[2])

    run_db(check, monkeypatch, timeout=20)


def test_a_sourced_item_has_no_status_and_an_own_item_has_one(monkeypatch):
    async def check(pool):
        seeded = await sup.seed(pool)
        with pytest.raises(asyncpg.CheckViolationError):  # a sourced item with a status of its own
            await pool.execute("INSERT INTO ledger_items (kind, title, source_kind, task_id, status) "
                               "VALUES ('work_item', 'T', 'task', $1, 'open')", seeded["task"])
        with pytest.raises(asyncpg.CheckViolationError):  # an own item with none
            await pool.execute("INSERT INTO ledger_items (kind, title) VALUES ('pending', 'T')")
        with pytest.raises(asyncpg.CheckViolationError):  # a task's item is a work item
            await pool.execute("INSERT INTO ledger_items (kind, title, source_kind, task_id) "
                               "VALUES ('pending', 'T', 'task', $1)", seeded["task"])
        with pytest.raises(asyncpg.CheckViolationError):  # one source only
            await pool.execute("INSERT INTO ledger_items (kind, title, source_kind, task_id, gate_step_id) "
                               "VALUES ('work_item', 'T', 'task', $1, $2)", seeded["task"], seeded["gate"])
        await pool.execute("INSERT INTO ledger_items (kind, title, source_kind, task_id) "
                           "VALUES ('work_item', 'T', 'task', $1)", seeded["task"])
        with pytest.raises(asyncpg.UniqueViolationError):  # a source is registered once
            await pool.execute("INSERT INTO ledger_items (kind, title, source_kind, task_id) "
                               "VALUES ('work_item', 'T', 'task', $1)", seeded["task"])
        await pool.execute("DELETE FROM task_steps WHERE task_id = $1", seeded["task"])
        await pool.execute("DELETE FROM tasks WHERE id = $1", seeded["task"])  # the source goes, its item with it
        assert await pool.fetchval("SELECT count(*) FROM ledger_items") == 0

    run_db(check, monkeypatch, timeout=20)


def test_an_open_item_is_not_closed_and_a_closed_one_is(monkeypatch):
    async def check(pool):
        item = await pool.fetchval(ITEM.format(kind="pending"))
        with pytest.raises(asyncpg.CheckViolationError):  # done without the event that closed it
            await pool.execute("UPDATE ledger_items SET status = 'done' WHERE id = $1", item)
        with pytest.raises(asyncpg.CheckViolationError):  # a confirmation belongs to a decision
            event = await pool.fetchval(EVENT, item, "add", "user", None, "dashboard")
            await pool.execute("UPDATE ledger_items SET confirmed_event_id = $1, confirmed_actor = 'user' "
                               "WHERE id = $2", event, item)

    run_db(check, monkeypatch, timeout=20)


def test_a_database_without_the_tables_gets_them_on_upgrade(monkeypatch):
    async def check(pool):
        async with dropped_tables(pool, "ledger_events", "ledger_items"):
            await pool.execute("DELETE FROM schema_migrations WHERE name = 'ledger'")
            async with pool.acquire() as conn:
                assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        await sup.add_own(pool)
        await connection.ensure_schema(pool)  # and a second upgrade changes nothing
        assert await pool.fetchval("SELECT count(*) FROM ledger_items") == 1

    run_db(check, monkeypatch, timeout=30)


def test_the_system_adds_sourced_items_and_only_those(monkeypatch):
    async def check(pool):
        seeded = await sup.seed(pool)
        own = await pool.fetchval(ITEM.format(kind="decision"))
        sourced = await pool.fetchval("INSERT INTO ledger_items (kind, title, source_kind, task_id) "
                                      "VALUES ('work_item', 'T', 'task', $1) RETURNING id", seeded["task"])
        insert = ("INSERT INTO ledger_events (item_id, item_sourced, action, actor_kind, relayed_by, via) "
                  "VALUES ($1, $2, 'add', $3, NULL, $4)")
        with pytest.raises(asyncpg.CheckViolationError):  # the system adding to an operator's own decision
            await pool.execute(insert, own, False, "system", "system")
        with pytest.raises(asyncpg.CheckViolationError):  # a user adding a sourced item
            await pool.execute(insert, sourced, True, "user", "dashboard")
        with pytest.raises(asyncpg.ForeignKeyViolationError):  # an event cannot misstate what its item is
            await pool.execute(insert, own, True, "system", "system")
        await pool.execute(insert, sourced, True, "system", "system")
        await pool.execute(insert, own, False, "user", "dashboard")

    run_db(check, monkeypatch, timeout=20)


def test_a_null_cannot_slip_past_the_source_rule(monkeypatch):
    async def check(pool):
        seeded = await sup.seed(pool)
        with pytest.raises(asyncpg.CheckViolationError):  # a task id with no source kind and no status
            await pool.execute("INSERT INTO ledger_items (kind, title, task_id) VALUES ('work_item', 'T', $1)",
                               seeded["task"])
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("INSERT INTO ledger_items (kind, title, gate_step_id) VALUES ('pending', 'T', $1)",
                               seeded["gate"])

    run_db(check, monkeypatch, timeout=20)
