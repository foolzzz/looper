"""Manager replies precede session maintenance, with real recording and persistence."""

import json

import pytest

from looper.agents import session_budget
from looper.agents.runtimes import Capability
from looper.db import projects, tasks
from tests.db_support import DSN, run_db
from tests.manager_budget_support import manager

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("compact,capable,action", [
    ("success", True, "compacted"), ("large", True, "rotated"),
    ("raise", True, "rotated"), ("missing", True, "rotated"), ("success", False, "rotated"),
])
def test_context_bounds_after_the_reply(monkeypatch, tmp_path, compact, capable, action):
    async def check(pool):
        await session_budget.update({"turn_budget_usd": 4})
        async with manager(pool, tmp_path, monkeypatch, compact=compact) as chat:
            if not capable:
                runtime = chat.manager._claude_runtime
                monkeypatch.setattr(type(runtime), "capabilities", runtime.capabilities - {Capability.HEADLESS_COMPACTION})
            replies = []
            send = chat.manager._send_reply

            async def sent(*args, **kwargs):
                assert chat.manager._turn_lock.locked()
                replies.append("sent")
                await send(*args, **kwargs)

            monkeypatch.setattr(chat.manager, "_send_reply", sent)
            assert await chat.ask() == ["answer"]
            assert replies == ["sent"]
            row = await pool.fetchrow("SELECT context_tokens, last_turn_cost_usd FROM agent_sessions WHERE agent_id='manager'")
            assert tuple(row) == (100_001, 0.1)
            if action == "compacted":
                record = await pool.fetchrow("SELECT metadata FROM llm_records WHERE input_text='/compact'")
                assert json.loads(record["metadata"])["session_bound"] == {
                    "action": action, "reason": "context", "before_tokens": 100_001, "after_tokens": 1200}
                assert chat.manager._claude.started and chat.manager._pending_switch is None
            else:
                assert not chat.manager._claude.started
                assert chat.manager._pending_switch.reason.value == "session_bound"
                await chat.ask("continue")
                record = await pool.fetchrow("SELECT metadata FROM llm_records WHERE input_text LIKE '%continue' ORDER BY id DESC LIMIT 1")
                assert json.loads(record["metadata"])["session_bound"]["action"] == "rotated"

    run_db(check, monkeypatch, timeout=45)


@pytest.mark.parametrize("context,cost,rotated", [(None, 3, False), (60_000, 2.1, True), (10_000, 3, False)])
def test_unknown_context_and_shared_cost_rule(monkeypatch, tmp_path, context, cost, rotated):
    async def check(pool):
        await session_budget.update({"turn_budget_usd": 4})
        async with manager(pool, tmp_path, monkeypatch, context=context, cost=cost) as chat:
            assert await chat.ask() == ["answer"]
            assert (chat.manager._pending_switch is not None) is rotated
            if rotated:
                assert "cost" in chat.manager._pending_switch.describe()
    run_db(check, monkeypatch, timeout=30)


def test_rotation_handoff_retrieves_only_latest_three_results(monkeypatch, tmp_path):
    async def check(pool):
        await session_budget.update({"turn_budget_usd": 4})
        project = await projects.create_project(name="synthetic", pool=pool)
        for index in range(4):
            await tasks.create_task(task_id=f"task-{index}", requirement="r", project_id=project["id"],
                                    title=f"result-{index}", status="completed", pool=pool)
            await pool.execute("UPDATE tasks SET completed_at=now()+$2*interval '1 second', result_summary=$3 WHERE id=$1",
                               f"task-{index}", index, "x" * 250)
        async with manager(pool, tmp_path, monkeypatch, compact="missing") as chat:
            await chat.ask()
            await chat.ask("continue")
            inputs = [json.loads(line) for line in (tmp_path / "inputs.jsonl").read_text().splitlines()]
            assert any(text.startswith("[HANDOFF]") for text in inputs)
            brief = next(text for text in inputs if text.startswith("[RESUME]"))
            recent = brief.split("Recent task results", 1)[1].split("Recent conversations", 1)[0]
            assert "result-0" not in recent
            assert recent.index("result-3") < recent.index("result-2") < recent.index("result-1")
            assert "x" * 201 not in recent
    run_db(check, monkeypatch, timeout=45)
