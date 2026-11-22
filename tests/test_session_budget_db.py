"""The Manager's session budget setting through the v1 API: off by default, the operator's alone, stored per field,
and a stored value that is not a budget is reported and left off."""

import pytest

from looper.agents import api_v1, session_budget
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OFF = {"turn_budget_usd": None, "recommended_usd": 4.0, "problem": None}


def test_the_operator_reads_and_sets_it_and_null_turns_it_off(monkeypatch, tmp_path):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("session_budget.get")
            assert status == 200 and answer["result"] == OFF, answer
            assert await session_budget.turn_budget_usd(pool=pool) is None
            status, answer = await call("session_budget.update", {"turn_budget_usd": 4})
            assert status == 200 and answer["result"]["turn_budget_usd"] == 4.0, answer
            assert await session_budget.turn_budget_usd(pool=pool) == 4.0
            declared = next(op for op in api_v1.operations() if op.operation_id == "session_budget.update")
            api_v1.validate_arguments(declared.result, answer["result"])
            status, answer = await call("session_budget.update", {"turn_budget_usd": None})
            assert status == 200 and answer["result"] == OFF
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1",
                                       session_budget.TURN_BUDGET_KEY) == 0

    run_db(check, monkeypatch, timeout=60)


@pytest.mark.parametrize("body", [{"turn_budget_usd": 0}, {"turn_budget_usd": -2}, {}, {"other": 1}])
def test_a_refused_body_stores_nothing(monkeypatch, tmp_path, body):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("session_budget.update", body)
            assert status == 400, answer
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1",
                                   session_budget.TURN_BUDGET_KEY) == 0

    run_db(check, monkeypatch, timeout=60)


@pytest.mark.parametrize("operation,body", [("session_budget.get", {}),
                                            ("session_budget.update", {"turn_budget_usd": 4})])
def test_an_api_client_can_neither_read_nor_set_it(monkeypatch, tmp_path, operation, body):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call(operation, body, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden", answer
        assert await session_budget.turn_budget_usd(pool=pool) is None

    run_db(check, monkeypatch, timeout=60)


def test_a_stored_value_that_is_not_a_budget_is_reported_and_off(monkeypatch):
    async def check(pool):
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'four dollars')",
                           session_budget.TURN_BUDGET_KEY)
        assert await session_budget.turn_budget_usd(pool=pool) is None
        shown = await session_budget.view(pool=pool)
        assert shown["turn_budget_usd"] is None and "not a positive number" in shown["problem"]

    run_db(check, monkeypatch, timeout=30)
