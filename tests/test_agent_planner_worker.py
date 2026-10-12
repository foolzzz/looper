"""Tests for the Planner Agent and Worker Pool."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from loopx.agents.bus import MessageBus
from loopx.agents.message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    planner_address,
    worker_address,
)
from loopx.agents.tiers.planner import PlanStep, PlannerAgent, TaskPlan
from loopx.agents.tiers.worker import WorkerConfig, WorkerPool, WorkerProcess


# --- TaskPlan unit tests ---


class TestTaskPlan:
    def test_ready_steps_no_deps(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", project_root="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="do A"),
            PlanStep(step_id="s2", index=1, instruction="do B"),
        ]
        ready = plan.ready_steps()
        assert len(ready) == 2

    def test_ready_steps_with_deps(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", project_root="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="do A"),
            PlanStep(step_id="s2", index=1, instruction="do B", depends_on=["s1"]),
        ]
        ready = plan.ready_steps()
        assert len(ready) == 1
        assert ready[0].step_id == "s1"

    def test_ready_steps_dep_completed(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", project_root="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="do A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="do B", depends_on=["s1"]),
        ]
        ready = plan.ready_steps()
        assert len(ready) == 1
        assert ready[0].step_id == "s2"

    def test_is_complete(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", project_root="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="B", status="verified"),
        ]
        assert plan.is_complete

    def test_completed_not_treated_as_done(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", project_root="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="B", status="completed"),
        ]
        assert not plan.is_complete

    def test_is_not_complete(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", project_root="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="B", status="dispatched"),
        ]
        assert not plan.is_complete

    def test_has_failed(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", project_root="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="B", status="failed"),
        ]
        assert plan.has_failed

    def test_progress_summary(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", project_root="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="B", status="dispatched"),
            PlanStep(step_id="s3", index=2, instruction="C", status="failed"),
        ]
        summary = plan.progress_summary()
        assert "1/3 完成" in summary
        assert "1 执行中" in summary
        assert "1 失败" in summary

    def test_to_dict(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", project_root="/tmp"
        )
        plan.steps = [PlanStep(step_id="s1", index=0, instruction="do A")]
        d = plan.to_dict()
        assert d["plan_id"] == "p1"
        assert len(d["steps"]) == 1
        assert d["steps"][0]["step_id"] == "s1"


# --- PlannerAgent tests ---


class TestPlannerAgent:
    def test_decompose_task(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                planner = PlannerAgent(task_id="test-1", runtime_root=Path(tmp))
                payload = {"requirement": "add login page"}
                steps = planner._decompose_task(payload)
                assert len(steps) == 1
                assert steps[0].instruction == "add login page"

        asyncio.run(_run())

    def test_agent_id_includes_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            planner = PlannerAgent(task_id="abc-123", runtime_root=Path(tmp))
            assert planner.agent_id == "planner-abc-123"
            assert planner.agent_address == planner_address("abc-123")

    def test_plan_and_dispatch(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                planner = PlannerAgent(task_id="test-pd", runtime_root=Path(tmp))
                await planner.start(bus)

                addr = planner.agent_address
                envelope = MessageEnvelope.create(
                    source=TASK_ROUTER_ADDRESS,
                    target=addr,
                    message_type=MessageType.TASK_REQUEST,
                    payload={
                        "goal_id": "g1",
                        "requirement": "add logout button",
                        "project_root": "/tmp",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await asyncio.sleep(0.1)

                # Check: planner should have published TASK_ACCEPTED + GOAL_STATUS to task-router
                router_q = bus.get_queue("task-router")
                messages: list[MessageEnvelope] = []
                while not router_q.empty():
                    messages.append(await router_q.get())

                types = [m.message_type for m in messages]
                assert MessageType.TASK_ACCEPTED in types
                assert MessageType.GOAL_STATUS in types

                # Check plan was persisted
                plan_dir = Path(tmp) / ".local" / "planner" / "planner-test-pd"
                plan_files = list(plan_dir.glob("*.json"))
                assert len(plan_files) >= 1

                await planner.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_handle_work_result_simple_auto_verify(self) -> None:
        """Simple tasks should auto-verify (strategy C)."""

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                planner = PlannerAgent(task_id="test-av", runtime_root=Path(tmp))
                await planner.start(bus)

                addr = planner.agent_address
                envelope = MessageEnvelope.create(
                    source=TASK_ROUTER_ADDRESS,
                    target=addr,
                    message_type=MessageType.TASK_REQUEST,
                    payload={
                        "goal_id": "g1",
                        "requirement": "simple fix",
                        "project_root": "/tmp",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await asyncio.sleep(0.1)

                plan = planner._plan
                assert plan is not None
                step = plan.steps[0]
                assert step.complexity == "simple"

                result_env = MessageEnvelope.create(
                    source=worker_address("w1"),
                    target=addr,
                    message_type=MessageType.WORK_RESULT,
                    payload={
                        "task_id": step.step_id,
                        "plan_id": plan.plan_id,
                        "status": "completed",
                        "changed_files": {"test.py": "added"},
                    },
                    correlation_id=plan.correlation_id,
                )
                await bus.publish(result_env)
                await asyncio.sleep(0.1)

                assert step.status == "verified"
                assert plan.status == "completed"

                await planner.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_handle_work_result_complex_spawns_reviewer(self) -> None:
        """Complex tasks should spawn a reviewer (strategy C)."""

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                planner = PlannerAgent(task_id="test-cr", runtime_root=Path(tmp))
                await planner.start(bus)

                addr = planner.agent_address
                envelope = MessageEnvelope.create(
                    source=TASK_ROUTER_ADDRESS,
                    target=addr,
                    message_type=MessageType.TASK_REQUEST,
                    payload={
                        "goal_id": "g1",
                        "requirement": "complex refactor",
                        "project_root": "/tmp",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await asyncio.sleep(0.1)

                plan = planner._plan
                assert plan is not None
                step = plan.steps[0]
                step.complexity = "complex"

                result_env = MessageEnvelope.create(
                    source=worker_address("w1"),
                    target=addr,
                    message_type=MessageType.WORK_RESULT,
                    payload={
                        "task_id": step.step_id,
                        "plan_id": plan.plan_id,
                        "status": "completed",
                    },
                    correlation_id=plan.correlation_id,
                )
                await bus.publish(result_env)
                await asyncio.sleep(0.1)

                assert step.status == "completed"
                assert plan.status == "executing"

                # Check that VERIFY_REQUEST was published
                collected: list[MessageEnvelope] = []
                for qname, q in bus._queues.items():
                    if qname.startswith("reviewer-"):
                        while not q.empty():
                            collected.append(await q.get())

                assert any(m.message_type == MessageType.VERIFY_REQUEST for m in collected)

                await planner.stop()
                await bus.shutdown()

        asyncio.run(_run())

    def test_plan_persistence_and_recovery(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                planner = PlannerAgent(task_id="test-pr", runtime_root=Path(tmp))
                await planner.start(bus)

                addr = planner.agent_address
                envelope = MessageEnvelope.create(
                    source=TASK_ROUTER_ADDRESS,
                    target=addr,
                    message_type=MessageType.TASK_REQUEST,
                    payload={
                        "goal_id": "g1",
                        "requirement": "persist test",
                        "project_root": "/tmp",
                    },
                    metadata={"origin_channel_id": "channel-web"},
                )
                await bus.publish(envelope)
                await asyncio.sleep(0.1)

                assert planner._plan is not None
                plan_id = planner._plan.plan_id

                await planner.stop()

                planner2 = PlannerAgent(task_id="test-pr", runtime_root=Path(tmp))
                bus2 = MessageBus(runtime_root=Path(tmp))
                await planner2.start(bus2)

                assert planner2._plan is not None
                assert planner2._plan.plan_id == plan_id
                await planner2.stop()
                await bus.shutdown()
                await bus2.shutdown()

        asyncio.run(_run())


# --- Worker tests ---


class TestWorkerProcess:
    def test_worker_process_defaults(self) -> None:
        wp = WorkerProcess(worker_id="w1", task_id="t1", plan_id="p1")
        assert wp.status == "pending"
        assert wp.result is None


class TestWorkerPool:
    def test_parse_output_json(self) -> None:
        pool = WorkerPool(
            config=WorkerConfig(),
            runtime_root=Path("/tmp"),
        )
        result = pool._parse_output(
            '{"status": "completed", "changed_files": {"a.py": "modified"}, "summary": "done"}'
        )
        assert result["status"] == "completed"
        assert "a.py" in result["changed_files"]

    def test_parse_output_wrapped_json(self) -> None:
        pool = WorkerPool(
            config=WorkerConfig(),
            runtime_root=Path("/tmp"),
        )
        output = 'Here is the result:\n```json\n{"status": "completed", "summary": "ok"}\n```\n'
        result = pool._parse_output(output)
        assert result["status"] == "completed"

    def test_parse_output_plain_text(self) -> None:
        pool = WorkerPool(
            config=WorkerConfig(),
            runtime_root=Path("/tmp"),
        )
        result = pool._parse_output("I made the changes successfully.")
        assert result["status"] == "completed"
        assert result["summary"].startswith("I made the changes")

    def test_parse_verify_output(self) -> None:
        pool = WorkerPool(
            config=WorkerConfig(),
            runtime_root=Path("/tmp"),
        )
        result = pool._parse_output(
            '{"passed": true, "findings": [], "summary": "all good"}',
            is_verify=True,
        )
        assert result["passed"] is True
        assert result["findings"] == []

    def test_parse_verify_output_failed(self) -> None:
        pool = WorkerPool(
            config=WorkerConfig(),
            runtime_root=Path("/tmp"),
        )
        result = pool._parse_output(
            '{"passed": false, "findings": ["missing null check"], "summary": "issues found"}',
            is_verify=True,
        )
        assert result["passed"] is False
        assert len(result["findings"]) == 1

    def test_build_work_prompt(self) -> None:
        pool = WorkerPool(
            config=WorkerConfig(),
            runtime_root=Path("/tmp"),
        )
        prompt = pool._build_work_prompt({
            "instruction": "add tests for login",
            "target_files": ["auth.py"],
            "validation_commands": ["pytest tests/test_auth.py"],
            "acceptance_criteria": ["all tests pass"],
        })
        assert "add tests for login" in prompt
        assert "auth.py" in prompt
        assert "pytest tests/test_auth.py" in prompt
        assert "all tests pass" in prompt

    def test_build_verify_prompt(self) -> None:
        pool = WorkerPool(
            config=WorkerConfig(),
            runtime_root=Path("/tmp"),
        )
        prompt = pool._build_verify_prompt({
            "work_result": {"changed_files": {"auth.py": "modified"}},
            "acceptance_criteria": ["code is clean"],
        })
        assert "auth.py" in prompt
        assert "code is clean" in prompt

    def test_pool_start_stop(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                pool = WorkerPool(
                    config=WorkerConfig(),
                    runtime_root=Path(tmp),
                )
                await pool.start(bus)
                assert pool.is_healthy()
                assert pool.active_count() == 0
                status = pool.status()
                assert status["pool_size"] == 4
                assert status["active"] == 0

                await pool.stop()
                await bus.shutdown()

        asyncio.run(_run())
