"""The ledger's reads through the v1 API: what they return, while the ledger is off, and who may call them."""

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
