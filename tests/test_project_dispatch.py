"""A project move waits for Planner workspace preparation and resumes deferred dispatch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from looper.agents import scheduler
from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan


def _planner(tmp_path):
    agent = PlannerAgent(task_id="task-dispatch", runtime_root=tmp_path)
    agent._execution = SimpleNamespace(isolate_steps=True)
    step = PlanStep("step-work", 0, "Write a report")
    plan = TaskPlan("plan-dispatch", "goal", "Write a report", str(tmp_path),
                    steps=[step], status="executing", project_id="project-dispatch")
    agent._plan_round = AsyncMock(return_value=([step], None))
    agent._dispatch_step = AsyncMock()
    return agent, plan, step


def test_planner_defers_filesystem_preparation_until_project_quiescence_ends(tmp_path):
    async def run():
        agent, plan, step = _planner(tmp_path)
        async with scheduler.quiesce_project(plan.project_id):
            dispatch = asyncio.create_task(agent._dispatch_ready(plan))
            await asyncio.sleep(0)
            assert not dispatch.done()
            agent._plan_round.assert_not_awaited()
            agent._dispatch_step.assert_not_awaited()
        await dispatch
        agent._plan_round.assert_awaited_once_with(plan, [step])
        agent._dispatch_step.assert_awaited_once_with(plan, step, base=None)
        assert plan.project_id not in scheduler._project_firings

    asyncio.run(asyncio.wait_for(run(), 5))


def test_quiescence_drains_planner_filesystem_preparation_before_moving(tmp_path):
    async def run():
        agent, plan, step = _planner(tmp_path)
        preparing, finish_preparing, quiesced = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def prepare(plan_, steps):
            preparing.set()
            await finish_preparing.wait()
            return steps, None

        agent._plan_round.side_effect = prepare
        dispatch = asyncio.create_task(agent._dispatch_ready(plan))
        await preparing.wait()

        async def move():
            async with scheduler.quiesce_project(plan.project_id):
                quiesced.set()
                agent._dispatch_step.assert_awaited_once_with(plan, step, base=None)

        moving = asyncio.create_task(move())
        await asyncio.sleep(0)
        assert not quiesced.is_set()
        finish_preparing.set()
        await asyncio.gather(dispatch, moving)
        assert quiesced.is_set() and plan.project_id not in scheduler._project_firings

    asyncio.run(asyncio.wait_for(run(), 5))


def test_admitted_router_can_dispatch_while_quiescence_waits_for_it(tmp_path):
    async def run():
        agent, plan, step = _planner(tmp_path)
        quiesced = asyncio.Event()

        async def move():
            async with scheduler.quiesce_project(plan.project_id):
                quiesced.set()

        with scheduler._project_firing(plan.project_id) as allowed:
            assert allowed
            moving = asyncio.create_task(move())
            await asyncio.sleep(0)
            assert scheduler._project_firings[plan.project_id].deleting and not quiesced.is_set()
            # This must remain in the Router's admitted asyncio task: a child task is a new entrant.
            await agent._dispatch_ready(plan)
            agent._plan_round.assert_awaited_once_with(plan, [step])
            agent._dispatch_step.assert_awaited_once_with(plan, step, base=None)
            assert not quiesced.is_set()
        await moving
        assert quiesced.is_set() and plan.project_id not in scheduler._project_firings

    asyncio.run(asyncio.wait_for(run(), 5))


def test_child_dispatch_does_not_inherit_router_admission(tmp_path):
    async def run():
        agent, plan, step = _planner(tmp_path)
        quiesced, finish_move = asyncio.Event(), asyncio.Event()

        async def move():
            async with scheduler.quiesce_project(plan.project_id):
                quiesced.set()
                await finish_move.wait()

        with scheduler._project_firing(plan.project_id) as allowed:
            assert allowed
            moving = asyncio.create_task(move())
            await asyncio.sleep(0)
            dispatch = asyncio.create_task(agent._dispatch_ready(plan))
            await asyncio.sleep(0)
            assert not dispatch.done() and not quiesced.is_set()
            agent._plan_round.assert_not_awaited()
        await quiesced.wait()
        agent._plan_round.assert_not_awaited()
        finish_move.set()
        await asyncio.gather(moving, dispatch)
        agent._plan_round.assert_awaited_once_with(plan, [step])
        agent._dispatch_step.assert_awaited_once_with(plan, step, base=None)
        assert plan.project_id not in scheduler._project_firings

    asyncio.run(asyncio.wait_for(run(), 5))
