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


@pytest.mark.parametrize("budget,compact", [(None, "missing"), (4, "success"), (4, "missing")])
def test_routed_budget_keeps_measurements_and_maintenance_in_its_session(monkeypatch, tmp_path, budget, compact):
    from looper.agents import session_routing
    from looper.agents.session_routing import SessionKey, SessionKind
    from looper.db import agent_sessions, manager_sessions
    from tests.test_manager_session_routing_db import GROUP_A, LARK, OTHER, _lark

    async def check(pool):
        await session_budget.update({"turn_budget_usd": budget})
        monkeypatch.setattr(session_routing, "_routing_enabled", True)
        async with manager(pool, tmp_path, monkeypatch, compact=compact) as chat:
            route = SessionKey(SessionKind.GROUP, LARK.agent_id, GROUP_A)
            session = chat.manager._router.session(route)
            context = chat.manager._current.set(session)
            try:
                await chat.manager._handle_user_message(_lark("hello", GROUP_A, OTHER, group=True))
                saved = await manager_sessions.get("manager", session.key)
                assert (saved["context_tokens"], saved["last_turn_cost_usd"]) == (
                    (100_001, .1) if budget else (None, None))
                assert await agent_sessions.get_session("manager") is None
                if budget and compact == "missing":
                    assert not session.claude.started
                    await chat.manager._handle_user_message(_lark("continue", GROUP_A, OTHER, group=True))
                    inputs = [json.loads(line) for line in
                              (chat.manager._session_work_dir() / "inputs.jsonl").read_text().splitlines()]
                    assert any(text.startswith("[HANDOFF]") for text in inputs)
                    assert any(text.startswith("[RESUME]") and "GROUP SESSION" in text for text in inputs)
                if budget:
                    records = await pool.fetch("SELECT metadata FROM llm_records WHERE agent_id='manager'")
                    assert all(json.loads(row["metadata"])["cwd"] == str(chat.manager._session_work_dir())
                               for row in records)
                assert not (tmp_path / "inputs.jsonl").exists()
                assert not session.turn_lock.locked()
                assert not chat.manager._sessions["main"].claude.started
            finally:
                chat.manager._current.reset(context)

    run_db(check, monkeypatch, timeout=45)
