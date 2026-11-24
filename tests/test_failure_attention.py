"""System holds appear independently of project gates, without changing the off summary."""
import asyncio
from unittest.mock import AsyncMock


def test_environment_and_breaker_need_the_operator_but_timed_waits_do_not(monkeypatch):
    from looper.agents import attention, failures
    from looper.db import step_failures

    async def body():
        monkeypatch.setattr(failures, "typing_mode", AsyncMock(return_value=failures.FailureTyping.ACT))
        monkeypatch.setattr(step_failures, "list_holds", AsyncMock(return_value=[
            {"task_id": "t", "step_id": "s", "step_key": "work", "failure_id": 1, "kind": kind,
             "cause": "login", "created_at": None, "streak": 3} for kind in ("environment", "quota", "transient", "breaker")]))
        items, count = await attention._failure_holds(None)
        assert count == 2
        assert [item["facts"]["kind"] for item in items] == ["environment", "breaker"]
        assert all(item["href"] == "#tasks/t" for item in items)
        monkeypatch.setattr(failures, "typing_mode", AsyncMock(return_value=failures.FailureTyping.OFF))
        assert await attention._failure_holds(None) is attention.NotRead.OFF
    asyncio.run(body())
