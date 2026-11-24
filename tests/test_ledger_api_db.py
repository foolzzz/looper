"""The ledger's reads through the v1 API: what they return, while the ledger is off, and who may call them."""

import asyncio
from datetime import datetime

import pytest

from looper.agents import api_v1
from looper.db import ledger
from tests import ledger_support as sup
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("operation,body", [("ledger.list", {}), ("ledger.get", {"item": "L-1"})])
def test_a_disabled_ledger_answers_conflict(monkeypatch, tmp_path, operation, body):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            for client in (False, True):
                status, answer = await call(operation, body, client=client)
                assert status == 409 and "not enabled" in str(answer), answer

    run_db(check, monkeypatch, timeout=20)


def test_list_and_get_read_the_ledger_for_the_operator_and_a_client(monkeypatch, tmp_path):
    async def check(pool):
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('ledger.enabled', 'true')")
        seeded = await sup.seed(pool)
        await sup.add_sourced(pool, seeded, "gate")
        decision = await sup.add_own(pool, "decision", sup.RELAYED, title="Use PostgreSQL")
        done = await sup.add_own(pool)
        await ledger.apply("close", sup.USER, item_id=done["number"], db=pool)
        async with v1_api(tmp_path, pool) as call:
            for client in (False, True):
                status, answer = await call("ledger.list", {}, client=client)
                assert status == 200, answer
                result = answer["result"]
                assert [i["id"] for i in result["items"]] == [decision["id"], "L-0001"] and result["more"] == 0
                assert result["counts"] == {"open": 2, "done": 1, "cancelled": 0, "expired": 0, "superseded": 0}
                assert result["items"][0]["assurance"] == "relayed"
                status, answer = await call("ledger.list", {"status": "all", "limit": 2}, client=client)
                assert (len(answer["result"]["items"]), answer["result"]["more"]) == (2, 1)
                status, answer = await call("ledger.list", {"status": "done"}, client=client)
                assert [i["status"] for i in answer["result"]["items"]] == ["done"]
            await ledger.apply("amend", sup.USER, item_id=decision["number"], db=pool, fields={"title": "Use SQLite"})
            status, answer = await call("ledger.get", {"item": decision["id"]}, client=True)
            assert status == 200
            for operation in ("ledger.get", "ledger.list"):  # what the API returns is what it declares
                declared = next(op for op in api_v1.operations() if op.operation_id == operation)
                _, shown = await call(operation, {"item": decision["id"]} if operation == "ledger.get" else {})
                api_v1.validate_arguments(declared.result, shown["result"])
            assert answer["result"]["events"][-1]["changes"] == {"title": ["Use PostgreSQL", "Use SQLite"]}
            answer["result"]["events"] = answer["result"]["events"][:1]
            (event,) = answer["result"]["events"]
            assert (event["actor_kind"], event["via"], event["relayed_by"]) == ("relayed", "api", "client:demo")
            assert "manager_id" not in event
            status, answer = await call("ledger.get", {"item": "L-99"})
            assert status == 404
            status, answer = await call("ledger.get", {"item": "twelve"})
            assert status == 400
            status, answer = await call("ledger.list", {"status": "later"})
            assert status == 400

    run_db(check, monkeypatch, timeout=30)


async def _rows(pool):
    return tuple([await pool.fetchval(f"SELECT count(*) FROM {table}") for table in ("ledger_items", "ledger_events")])


async def _on(pool):
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ('ledger.enabled', 'true')")


@pytest.mark.parametrize("action", ["add", "confirm", "amend", "note", "close", "cancel", "reopen"])
def test_dashboard_writes_are_operator_only(monkeypatch, tmp_path, action):
    async def check(pool):
        await _on(pool)
        own = await sup.add_own(pool, "decision" if action == "confirm" else "pending", sup.RELAYED)
        if action == "reopen":
            await ledger.apply("close", sup.USER, item_id=own["number"], db=pool)
        body = {"kind": "pending", "title": "From the operator"} if action == "add" else {
            "item": own["id"], "action": action,
            **({"note": "A note"} if action == "note" else {}),
            **({"fields": {"title": "A new title"}} if action == "amend" else {})}
        operation = "ledger.add" if action == "add" else "ledger.action"
        async with v1_api(tmp_path, pool) as call:
            before = await _rows(pool)
            status, answer = await call(operation, body, client=True)
            assert status == 403, answer
            assert await _rows(pool) == before
            status, answer = await call(operation, body)
            assert status == 200, answer
            api_v1.validate_arguments(next(op.result for op in api_v1.operations() if op.operation_id == operation),
                                      answer["result"])
            for key, value in {"actor_kind": "user", "actor_id": "operator", "via": "dashboard", "source_kind": "gate",
                               "gate_step_id": "gate-1", "task_id": "task-1", "action_id": 1, "status": "done",
                               "confirmed_event_id": 1, "closed_actor": "user", "manager_id": "manager"}.items():
                before = await _rows(pool)
                status, answer = await call(operation, {**body, key: value})
                assert status == 400, (key, answer)
                assert await _rows(pool) == before
    run_db(check, monkeypatch, timeout=30)


def test_dashboard_add_and_actions_leave_user_events(monkeypatch, tmp_path):
    async def check(pool):
        await _on(pool)
        seeded = await sup.seed(pool)
        await pool.execute("UPDATE task_steps SET step_key = 'choose database' WHERE id = 'gate-1'")
        await pool.execute("UPDATE manager_actions SET channel = 'web', chat = 'demo & chat', manager_id = 'manager' "
                           "WHERE id = $1", seeded["action"])
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config) "
                           "VALUES ('channel-telegram', 'telegram', 'Example bot', true, '{}')")
        sources = [await sup.add_sourced(pool, seeded, kind) for kind in ("gate", "task", "confirmation")]
        async with v1_api(tmp_path, pool) as call:
            for kind in ("pending", "work_item"):
                status, answer = await call("ledger.add", {"kind": kind, "title": "  Reply to the proposal  ",
                    "detail": "Current input", "due": "2030-02-03", "owner": "task:task-1", "project_id": "proj",
                    "refs": [{"kind": "url", "ref": "https://example.invalid/proposal", "label": "Proposal"}],
                    "quote": "Words typed on this form"})
                assert status == 200, answer
                item = answer["result"]["item"]
                assert item["title"] == "Reply to the proposal" and item["owner"] == "task:task-1"
                assert datetime.fromisoformat(item["due_at"]) == ledger.parse_due("2030-02-03")
                assert item["project_id"] == "proj"
                bodies = [{"action": "amend", "fields": {"detail": None, "due": None, "owner": None,
                                                            "project_id": None, "title": "Changed"}},
                          {"action": "note", "note": "Read back this note"}, {"action": "close", "note": "Done"},
                          {"action": "reopen"}, {"action": "cancel"}]
                for body in bodies:
                    status, answer = await call("ledger.action", {"item": item["id"], **body})
                    assert status == 200, answer
                status, answer = await call("ledger.get", {"item": item["id"]})
                assert status == 200
                shown = answer["result"]
                assert shown["item"]["status"] == "cancelled" and shown["item"]["detail"] == ""
                assert shown["item"]["owner"] == "user" and shown["item"]["project_id"] is None
                assert shown["item"]["due_at"] is None
                assert [e["action"] for e in shown["events"]] == ["add", "amend", "note", "close", "reopen", "cancel"]
                assert all((e["actor_kind"], e["via"], e["actor_id"]) == ("user", "api", "operator")
                           for e in shown["events"])
                assert shown["events"][0]["quote"] == "Words typed on this form"
                assert all("message_id" not in e and "channel_id" not in e for e in shown["events"])
            status, answer = await call("ledger.add", {"kind": "decision", "title": "Use a relational store"})
            assert status == 200 and answer["result"]["item"]["assurance"] == "user"
            relayed = await sup.add_own(pool, "decision", sup.RELAYED)
            status, answer = await call("ledger.action", {"item": relayed["id"], "action": "confirm"})
            assert status == 200 and answer["result"]["item"]["assurance"] == "user"
            for source, href in zip(sources, ["#tasks/task-1?gate=choose+database", "#tasks/task-1",
                                             "#chat?conversation=demo+%26+chat"]):
                _, operator = await call("ledger.get", {"item": source["id"]})
                _, client = await call("ledger.get", {"item": source["id"]}, client=True)
                assert operator["result"]["item"]["source"]["href"] == href
                assert "href" not in client["result"]["item"]["source"]
            _, listed = await call("ledger.list", {}, client=True)
            assert all(not i["source"] or "href" not in i["source"] for i in listed["result"]["items"])
            status, page = await call("ledger.page", {})
            assert status == 200, page
            assert page["result"]["missing_operator_ids"] == [
                {"channel_id": "channel-telegram", "channel_type": "telegram", "name": "Example bot"}]
            api_v1.validate_arguments(next(op.result for op in api_v1.operations() if op.operation_id == "ledger.page"),
                                      page["result"])
            assert all(i["source"] is None or "href" in i["source"] for i in page["result"]["items"])
            await pool.execute("UPDATE manager_actions SET channel = 'telegram' WHERE id = $1", seeded["action"])
            _, shown = await call("ledger.get", {"item": sources[-1]["id"]})
            assert shown["result"]["item"]["source"]["href"] is None
    run_db(check, monkeypatch, timeout=40)


def test_dashboard_action_failure_preserves_the_ledger(monkeypatch, tmp_path):
    async def check(pool):
        await _on(pool)
        seeded = await sup.seed(pool)
        own = await sup.add_own(pool)
        decision = await sup.add_own(pool, "decision")
        source = await sup.add_sourced(pool, seeded, "gate")
        async with v1_api(tmp_path, pool) as call:
            failures = [("ledger.add", {"kind": "pending", "title": "x" * 201}, 400),
                        ("ledger.add", {"kind": "pending", "title": "  "}, 400),
                        ("ledger.add", {"kind": "pending", "title": "A", "detail": "x" * 4001}, 400),
                        ("ledger.add", {"kind": "pending", "title": "A", "quote": "x" * 1001}, 400)]
            for field, value in [("due", "not a date"), ("owner", ""), ("owner", "manager:absent"), ("owner", "task:absent"),
                                  ("owner", "chat:"), ("project_id", "absent")]:
                failures.append(("ledger.add", {"kind": "pending", "title": "A", field: value}, 400))
            for fields in ({}, {"title": None}, {"title": "x" * 201}, {"due": "bad"}, {"project_id": "absent"}):
                failures.append(("ledger.action", {"item": own["id"], "action": "amend", "fields": fields}, 400))
            for action, extra in [("note", {"note": " "}), ("note", {"note": "x" * 2001}),
                                  ("confirm", {"note": "not allowed"}), ("close", {"fields": {"title": "no"}})]:
                failures.append(("ledger.action", {"item": own["id"], "action": action, **extra}, 400))
            failures += [("ledger.action", {"item": "L-99999", "action": "close"}, 404),
                         ("ledger.action", {"item": decision["id"], "action": "confirm"}, 409),
                         ("ledger.action", {"item": decision["id"], "action": "close"}, 409),
                         ("ledger.action", {"item": own["id"], "action": "amend",
                                            "fields": {"title": own["title"]}}, 409)]
            for action in ("confirm", "close", "cancel", "reopen"):
                failures.append(("ledger.action", {"item": source["id"], "action": action}, 409))
            failures.append(("ledger.action", {"item": source["id"], "action": "amend",
                                                "fields": {"title": "Cannot edit a gate"}}, 409))
            for operation, body, expected in failures:
                before = await _rows(pool)
                status, answer = await call(operation, body)
                assert status == expected, (operation, body, answer)
                assert await _rows(pool) == before
            before_source = dict(await pool.fetchrow("SELECT * FROM task_user_gates WHERE step_id = 'gate-1'"))
            status, answer = await call("ledger.action", {"item": source["id"], "action": "amend",
                "fields": {"due": "2030-01-01T00:00:00+00:00", "owner": "chat:proposal"}})
            assert status == 200, answer
            assert dict(await pool.fetchrow("SELECT * FROM task_user_gates WHERE step_id = 'gate-1'")) == before_source
            status, _ = await call("ledger.action", {"item": own["id"], "action": "close"})
            assert status == 200
            before = await _rows(pool)
            status, _ = await call("ledger.action", {"item": own["id"], "action": "close"})
            assert status == 409 and await _rows(pool) == before
            for body in ({"cursor": "broken"}, {"project_id": "absent"}, {"section": "unknown"}):
                status, _ = await call("ledger.page", body)
                assert status == 400
    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("off", ["false", None])
def test_disabled_dashboard_writes_do_not_create_rows(monkeypatch, tmp_path, off):
    async def check(pool):
        if off is not None:
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ('ledger.enabled', $1)", off)
        async with v1_api(tmp_path, pool) as call:
            for operation, body in [("ledger.page", {}), ("ledger.add", {"kind": "pending", "title": "No write"}),
                                     ("ledger.action", {"item": "L-1", "action": "close"})]:
                for client in (True, False):
                    status, answer = await call(operation, body, client=client)
                    assert status == (403 if client else 409), answer
            assert await _rows(pool) == (0, 0)
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key = 'ledger.enabled'") == off
    run_db(check, monkeypatch, timeout=25)


def test_dashboard_duplicate_close_has_one_winner(monkeypatch, tmp_path):
    async def check(pool):
        await _on(pool)
        item = await sup.add_own(pool)
        async with v1_api(tmp_path, pool) as call:
            results = await asyncio.gather(*(call("ledger.action", {"item": item["id"], "action": "close"})
                                             for _ in range(2)))
            assert sorted(status for status, _ in results) == [200, 409], results
            assert await pool.fetchval("SELECT count(*) FROM ledger_events WHERE action = 'close'") == 1
    run_db(check, monkeypatch, timeout=25)
