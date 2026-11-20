"""``db.ledger.apply``: who may do what, the states it moves an item through, and how a sourced item reads its source."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents.ops import service
from looper.db import ledger
from tests import ledger_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

ACTORS = {"user": sup.USER, "relayed": sup.RELAYED, "system": sup.SYSTEM}
# For each action: the actor kinds that may do it (RFC 4.4). The system's only act is adding a sourced item.
ALLOWED = {"note": {"user", "relayed"}, "confirm": {"user"}, "close": {"user"}, "cancel": {"user"},
           "supersede": {"user"}, "reopen": {"user"}, "amend": {"user"}}


async def target(pool, action):
    """A fresh own item that ``action`` applies to, and the arguments it takes."""
    if action == "confirm":
        return (await sup.add_own(pool, "decision", sup.RELAYED, title="Use PostgreSQL"))["number"], {}
    if action == "supersede":
        newer = await sup.add_own(pool, "decision", sup.USER, title="Use SQLite")
        return (await sup.add_own(pool, "decision", sup.USER, title="Use files"))["number"], {"superseded_by": newer["number"]}
    if action == "reopen":
        item = await sup.add_own(pool)
        await ledger.apply("cancel", sup.USER, item_id=item["number"], db=pool)
        return item["number"], {}
    if action == "amend":
        return (await sup.add_own(pool))["number"], {"fields": {"title": "Reply by Monday"}}
    return (await sup.add_own(pool))["number"], {"note": "a note"}


@pytest.mark.parametrize("action", sorted(ALLOWED))
def test_the_permission_table(monkeypatch, action):
    async def check(pool):
        for kind, actor in ACTORS.items():
            item_id, arguments = await target(pool, action)
            before = await pool.fetchval("SELECT count(*) FROM ledger_events")
            if kind in ALLOWED[action]:
                await ledger.apply(action, actor, item_id=item_id, db=pool, **arguments)
                assert await pool.fetchval("SELECT count(*) FROM ledger_events") == before + 1
            else:
                with pytest.raises(ledger.LedgerRefused) as refused:
                    await ledger.apply(action, actor, item_id=item_id, db=pool, **arguments)
                assert refused.value.code == "forbidden"
                assert await pool.fetchval("SELECT count(*) FROM ledger_events") == before

    run_db(check, monkeypatch, timeout=60)


def test_adding_is_split_between_own_and_sourced_items(monkeypatch):
    async def check(pool):
        seeded = await sup.seed(pool)
        for actor in (sup.USER, sup.RELAYED):
            with pytest.raises(ledger.LedgerRefused) as refused:  # a user or a relay cannot register a source
                await ledger.apply("add", actor, db=pool, new=ledger.NewItem(
                    kind="work_item", title="T", source_kind="task", task_id=seeded["task"]))
            assert refused.value.code == "forbidden"
        with pytest.raises(ledger.LedgerRefused) as refused:  # the system adds nothing of its own
            await ledger.apply("add", sup.SYSTEM, new=ledger.NewItem(kind="pending", title="T"), db=pool)
        assert refused.value.code == "forbidden"
        sourced = await sup.add_sourced(pool, seeded, "task")
        again = await sup.add_sourced(pool, seeded, "task")
        assert (sourced["number"], sourced["status"], sourced["assurance"]) == (again["number"], "open", "system")
        assert await pool.fetchval("SELECT count(*) FROM ledger_events") == 1  # registering twice is one event

    run_db(check, monkeypatch, timeout=30)


def test_what_the_user_decides_is_confirmed_and_what_is_relayed_waits(monkeypatch):
    async def check(pool):
        said = await sup.add_own(pool, "decision", sup.USER)
        relayed = await sup.add_own(pool, "decision", sup.RELAYED)
        assert (said["assurance"], relayed["assurance"]) == ("user", "relayed")
        with pytest.raises(ledger.LedgerRefused) as refused:
            await ledger.apply("confirm", sup.RELAYED, item_id=relayed["number"], db=pool)
        assert refused.value.code == "forbidden"
        confirmed = await ledger.apply("confirm", sup.CLI, item_id=relayed["number"], db=pool, note="yes")
        assert confirmed["assurance"] == "user"
        for item in (said, relayed):  # confirming twice is a conflict, not a second confirmation
            with pytest.raises(ledger.LedgerRefused) as refused:
                await ledger.apply("confirm", sup.USER, item_id=item["number"], db=pool)
            assert refused.value.code == "conflict"
        events = await ledger.list_events(relayed["number"], db=pool)
        assert [(e["action"], e["actor_kind"], e["via"], e["session_label"]) for e in events] == [
            ("add", "relayed", "api", None), ("confirm", "user", "cli", "test session")]

    run_db(check, monkeypatch, timeout=30)


def test_an_item_moves_through_its_states(monkeypatch):
    async def check(pool):
        item = (await sup.add_own(pool))["number"]
        assert (await ledger.apply("close", sup.USER, item_id=item, db=pool))["status"] == "done"
        for action in ("close", "cancel"):
            with pytest.raises(ledger.LedgerRefused) as refused:
                await ledger.apply(action, sup.USER, item_id=item, db=pool)
            assert refused.value.code == "conflict"
        assert (await ledger.apply("reopen", sup.USER, item_id=item, db=pool))["status"] == "open"
        with pytest.raises(ledger.LedgerRefused):
            await ledger.apply("reopen", sup.USER, item_id=item, db=pool)
        assert (await ledger.apply("cancel", sup.USER, item_id=item, db=pool))["status"] == "cancelled"
        old = (await sup.add_own(pool, "decision"))["number"]
        new = (await sup.add_own(pool, "decision", title="Newer"))["number"]
        with pytest.raises(ledger.LedgerRefused) as refused:  # a decision is not a thing to complete
            await ledger.apply("close", sup.USER, item_id=old, db=pool)
        assert refused.value.code == "conflict"
        replaced = await ledger.apply("supersede", sup.USER, item_id=old, superseded_by=new, db=pool)
        assert (replaced["status"], replaced["superseded_by"]) == ("superseded", ledger.format_id(new))
        assert (await ledger.apply("reopen", sup.USER, item_id=old, db=pool))["superseded_by"] is None
        with pytest.raises(ledger.LedgerRefused) as refused:
            await ledger.apply("supersede", sup.USER, item_id=old, superseded_by=old, db=pool)
        assert refused.value.code == "invalid"
        with pytest.raises(ledger.LedgerRefused) as refused:
            await ledger.apply("close", sup.USER, item_id=9999, db=pool)
        assert refused.value.code == "not_found"

    run_db(check, monkeypatch, timeout=30)


def test_amend_records_what_changed_and_refuses_what_cannot(monkeypatch):
    async def check(pool):
        seeded = await sup.seed(pool)
        item = (await sup.add_own(pool))["number"]
        due = datetime(2030, 1, 2, tzinfo=timezone.utc)
        amended = await ledger.apply("amend", sup.USER, item_id=item, db=pool, fields={
            "title": "Reply by Monday", "due_at": due, "owner": "manager:manager", "project_id": seeded["project"]})
        assert (amended["title"], amended["due_at"], amended["owner"], amended["project_id"]) == (
            "Reply by Monday", due, "manager:manager", "proj")
        (_, event) = await ledger.list_events(item, db=pool)
        assert event["changes"] is not None
        assert {k: v for k, v in event["changes"].items() if k != "due_at"} == {
            "title": ["Reply by Friday", "Reply by Monday"], "owner": ["user", "manager:manager"],
            "project_id": [None, "proj"]}
        for fields, code in (({}, "invalid"), ({"status": "done"}, "invalid"), ({"title": "  "}, "invalid"),
                             ({"title": "Reply by Monday"}, "conflict"), ({"owner": "task"}, "invalid"), ({"owner": "user:x"}, "invalid"),
                             ({"project_id": "missing"}, "invalid")):
            with pytest.raises(ledger.LedgerRefused) as refused:
                await ledger.apply("amend", sup.USER, item_id=item, db=pool, fields=fields)
            assert refused.value.code == code, fields
        sourced = (await sup.add_sourced(pool, seeded, "gate"))["number"]
        assert (await ledger.apply("amend", sup.USER, item_id=sourced, db=pool,
                                   fields={"due_at": due}))["due_at"] == due
        with pytest.raises(ledger.LedgerRefused) as refused:  # a gate's title is the gate's
            await ledger.apply("amend", sup.USER, item_id=sourced, db=pool, fields={"title": "Mine"})
        assert refused.value.code == "invalid"

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("source,action", [(source, action) for source in ("gate", "task", "confirmation")
                                           for action in ("close", "cancel", "reopen", "supersede")])
def test_a_sourced_item_is_changed_at_its_source(monkeypatch, source, action):
    async def check(pool):
        item = (await sup.add_sourced(pool, await sup.seed(pool), source))["number"]
        with pytest.raises(ledger.LedgerRefused) as refused:
            await ledger.apply(action, sup.USER, item_id=item, db=pool, superseded_by=item)
        assert refused.value.code == "conflict" and source in str(refused.value)
        noted = await ledger.apply("note", sup.RELAYED, item_id=item, db=pool, note="seen")
        assert noted["status"] == "open"

    run_db(check, monkeypatch, timeout=30)


def test_a_sourced_item_reads_its_sources_state(monkeypatch):
    async def check(pool):
        seeded = await sup.seed(pool)
        gate, task, confirmation = [(await sup.add_sourced(pool, seeded, s))["number"]
                                    for s in ("gate", "task", "confirmation")]

        async def states():
            return {i["id"]: i["status"] for i in (await ledger.list_items(status=None, db=pool))[0]}

        def named(*items):
            return {ledger.format_id(i) for i in items}

        async def check_filters():
            """Every status filter returns exactly the items that show that status."""
            shown = await states()
            for status in ledger.STATUSES:
                found = {i["id"] for i in (await ledger.list_items(status=status, db=pool))[0]}
                assert found == {n for n, s in shown.items() if s == status}, status
            assert sum((await ledger.counts(db=pool)).values()) == len(shown)

        assert await states() == dict.fromkeys(named(gate, task, confirmation), "open")
        for raw, status in (("discussing", "open"), ("passed", "done"), ("cancelled", "cancelled"),
                            ("skipped", "cancelled")):
            await pool.execute("UPDATE task_user_gates SET state = $1, summary = CASE WHEN $1 = 'passed' "
                               "THEN '{}'::jsonb END WHERE step_id = 'gate-1'", raw)
            assert (await states())[ledger.format_id(gate)] == status, raw
            await check_filters()
        for raw, status in (("running", "open"), ("waiting_user", "open"), ("failed", "open"), ("completed", "done"),
                            ("cancelled", "cancelled")):
            await pool.execute("UPDATE tasks SET status = $1 WHERE id = 'task-1'", raw)
            assert (await states())[ledger.format_id(task)] == status, raw
            await check_filters()
        # a confirmation: answered ok, answered with an error, or past its time
        await pool.execute("INSERT INTO manager_actions (tool, risk, status, confirmation_id) "
                           "VALUES ('looper_task_control', 'high', 'ok', 'c-1')")
        assert (await states())[ledger.format_id(confirmation)] == "done"
        await pool.execute("DELETE FROM manager_actions WHERE status = 'ok'")
        await pool.execute("INSERT INTO manager_actions (tool, risk, status, confirmation_id) "
                           "VALUES ('looper_task_control', 'high', 'error', 'c-1')")
        assert (await states())[ledger.format_id(confirmation)] == "cancelled"
        await pool.execute("DELETE FROM manager_actions WHERE status = 'error'")
        await pool.execute("UPDATE manager_actions SET created_at = $1",
                           datetime.now(timezone.utc) - timedelta(seconds=ledger.CONFIRMATION_SECONDS + 5))
        assert (await states())[ledger.format_id(confirmation)] == "expired"
        await check_filters()
        shown = {i["id"]: i for i in (await ledger.list_items(status=None, db=pool))[0]}
        assert shown[ledger.format_id(gate)]["title"] == "Pick a database"  # a sourced item shows its source's title
        assert shown[ledger.format_id(task)]["project_id"] == "proj"

    run_db(check, monkeypatch, timeout=60)


def test_list_filters_and_paging(monkeypatch):
    async def check(pool):
        seeded = await sup.seed(pool)
        for number in range(3):
            await sup.add_own(pool, "pending", title=f"Item {number}", project_id=seeded["project"] if number else None,
                              owner_kind="manager" if number == 2 else "user",
                              owner_ref="manager" if number == 2 else None)
        await sup.add_own(pool, "decision", title="Decided")
        items, more = await ledger.list_items(limit=2, db=pool)
        assert ([i["title"] for i in items], more) == (["Decided", "Item 2"], 2)
        assert [i["title"] for i in (await ledger.list_items(kind="decision", db=pool))[0]] == ["Decided"]
        assert [i["title"] for i in (await ledger.list_items(owner="manager", db=pool))[0]] == ["Item 2"]
        assert {i["title"] for i in (await ledger.list_items(project_id="proj", db=pool))[0]} == {"Item 1", "Item 2"}
        for bad in ({"status": "later"}, {"kind": "idea"}, {"limit": 0}, {"limit": 51}, {"owner": "robot"}):
            with pytest.raises(ledger.LedgerRefused):
                await ledger.list_items(db=pool, **bad)

    run_db(check, monkeypatch, timeout=30)


def test_refs_and_ids():
    assert [ledger.parse_id(text) for text in ("L-0012", "l-12", "12", " L-12 ")] == [12] * 4
    for bad in ("L-", "0", "L-0", "twelve", "L-1x", ""):
        with pytest.raises(ledger.LedgerRefused):
            ledger.parse_id(bad)
    assert ledger.format_id(12) == "L-0012" and ledger.format_id(12345) == "L-12345"
    assert ledger.check_refs([{"kind": "url", "ref": "https://example.com", "label": "x"}])
    for bad in ([{"kind": "email", "ref": "x"}], [{"kind": "url", "ref": ""}], [{"kind": "url", "ref": "x" * 501}],
                [{"kind": "url", "ref": "x"}] * 11, "x", [{"kind": "url", "ref": "x", "extra": 1}]):
        with pytest.raises(ledger.LedgerRefused):
            ledger.check_refs(bad)


def test_the_confirmation_window_is_the_managers():
    assert ledger.CONFIRMATION_SECONDS == service.CONFIRMATION_SECONDS


def test_an_actor_is_consistent():
    for kind, via, extra in (("system", "user", {}), ("user", "system", {}), ("relayed", "api", {}),
                             ("user", "cli", {}), ("relayed", "cli", {"relayed_by": "x", "session_label": "s"}),
                             ("user", "api", {"relayed_by": "x"}), ("owner", "api", {})):
        with pytest.raises(ValueError):
            ledger.Actor(kind, via, **extra)


def test_a_gate_item_has_its_tasks_project(monkeypatch):
    async def check(pool):
        seeded = await sup.seed(pool)
        gate = await sup.add_sourced(pool, seeded, "gate")
        assert gate["project_id"] == "proj"
        assert [i["id"] for i in (await ledger.list_items(project_id="proj", db=pool))[0]] == [gate["id"]]

    run_db(check, monkeypatch, timeout=20)


def test_amend_events_come_back_as_objects(monkeypatch):
    async def check(pool):
        item = (await sup.add_own(pool))["number"]
        await ledger.apply("amend", sup.USER, item_id=item, db=pool, fields={"title": "Reply by Monday"})
        events = await ledger.list_events(item, db=pool)
        assert [e["changes"] for e in events] == [None, {"title": ["Reply by Friday", "Reply by Monday"]}]

    run_db(check, monkeypatch, timeout=20)


def test_two_decisions_superseding_each_other_do_not_deadlock(monkeypatch):
    async def check(pool):
        a = (await sup.add_own(pool, "decision", title="A"))["number"]
        b = (await sup.add_own(pool, "decision", title="B"))["number"]
        # One transaction holds A, as a supersede of A would. Another supersedes B by A. Each then needs the
        # other's row (the foreign key to the superseding decision), the cycle that used to deadlock.
        async with pool.acquire() as holder:
            tx = holder.transaction()
            await tx.start()
            await holder.fetch("SELECT id FROM ledger_items WHERE id = $1 FOR UPDATE", a)
            other = asyncio.ensure_future(ledger.apply("supersede", sup.USER, item_id=b, superseded_by=a, db=pool))
            await asyncio.sleep(0.5)
            await holder.fetch("SELECT id FROM ledger_items WHERE id = $1 FOR KEY SHARE", b)
            await tx.commit()
        assert (await other)["status"] == "superseded"

    run_db(check, monkeypatch, timeout=30, max_size=4)
