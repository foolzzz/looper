"""A step's start: its first attempt's dispatch stamps it, a later attempt keeps it, and the Planner saves it."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from looper.agents.bus import MessageBus
from looper.agents.message import MessageType
from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan


def test_the_first_attempts_dispatch_stamps_the_start_and_a_retry_keeps_it(agent_db: Any) -> None:
    async def run() -> tuple[datetime, datetime | None, datetime | None, Any, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            bus = MessageBus(runtime_root=Path(tmp))
            bus.subscribe_topic(MessageType.WORK_ITEM, lambda e: True, consumes=True)
            planner = PlannerAgent(task_id="t1", runtime_root=Path(tmp))
            planner._bus = bus
            plan = TaskPlan(plan_id="p1", goal_id="g1", requirement="r", work_dir=tmp, status="executing")
            step = PlanStep(step_id="step-1", index=0, instruction="do it", key="s1")
            plan.steps = [step]
            before = datetime.now(timezone.utc)
            await planner._dispatch_step(plan, step)
            first = step.started_at
            saved_first = agent_db.store.steps["t1"]["step-1"]["started_at"]
            step.status, step.retries = "deciding", 1  # its attempt failed; the Planner retries it
            await planner._dispatch_step(plan, step)
            await bus.shutdown()
            return before, first, step.started_at, saved_first, agent_db.store.steps["t1"]["step-1"]["started_at"]

    before, first, after_retry, saved_first, saved_after = asyncio.run(run())
    assert first is not None and before <= first <= datetime.now(timezone.utc)
    assert after_retry == first
    assert saved_first == first and saved_after == first
