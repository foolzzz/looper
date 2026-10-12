"""Tests for the Planner Agent and Worker Pool."""

from __future__ import annotations

import asyncio
import json
import os
import sys
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


@pytest.mark.usefixtures("agent_db")
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

    def test_plan_and_dispatch(self, agent_db: Any) -> None:
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

                phases = [c.args for c in agent_db.update_task_phase.await_args_list]
                assert phases == [("test-pd", "planning"), ("test-pd", "executing")]
                agent_db.upsert_steps.assert_awaited_once()

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
        assert '"deleted_files"' in prompt  # the report contract judge_result checks

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

    @pytest.mark.parametrize("message_type", [MessageType.WORK_ITEM, MessageType.VERIFY_REQUEST])
    def test_result_returns_to_the_dispatching_planner(self, message_type: MessageType) -> None:
        """Per-task Planners subscribe to planner-<task_id>, not the bare "planner" address."""

        async def _run() -> MessageBus:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                pool = WorkerPool(config=WorkerConfig(), runtime_root=Path(tmp))
                pool._bus = bus
                pool._execute = AsyncMock(return_value={"status": "completed", "passed": True})  # type: ignore[method-assign]
                await pool._run_worker(MessageEnvelope.create(
                    source=planner_address("t1"),
                    target=worker_address("w1"),
                    message_type=message_type,
                    payload={"task_id": "s1", "plan_id": "p1"},
                ))
                return bus

        bus = asyncio.run(_run())
        reply = bus.get_queue("planner-t1").get_nowait()
        assert reply.payload["task_id"] == "s1"
        assert bus.get_queue("planner").empty()


def _fake_claude(tmp: Path, body: str) -> str:
    """A stand-in `claude` binary that ignores its arguments."""
    script = tmp / "fake-claude"
    script.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n{body}\n")
    script.chmod(0o755)
    return str(script)


def _work_item(project_root: Path) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=planner_address("t1"),
        target=worker_address("w1"),
        message_type=MessageType.WORK_ITEM,
        payload={"task_id": "s1", "instruction": "x", "context": {"project_root": str(project_root)}},
    )


class TestWorkerSubprocess:
    def test_long_stream_json_line_is_read(self) -> None:
        # Claude's stream-json init line lists every tool and skill and can
        # exceed asyncio's default 64 KiB line limit.
        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    "print(json.dumps({'type': 'system', 'tools': ['t' * 200_000]}))\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
                    "'result': json.dumps({'status': 'completed', 'summary': 'ok'})}))"
                ))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                return await pool._execute(_work_item(Path(tmp)))

        assert asyncio.run(_run())["status"] == "completed"

    @pytest.mark.parametrize(("create", "expected"), [(True, "completed"), (False, "failed")])
    def test_report_is_checked_against_the_project(self, create: bool, expected: str) -> None:
        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    ("open('out.txt', 'w').write('x')\n" if create else "")
                    + "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
                    "'result': json.dumps({'status': 'completed', 'changed_files': {'out.txt': 'added'}})}))"
                ))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                return await pool._execute(_work_item(Path(tmp)))

        assert asyncio.run(_run())["status"] == expected

    def test_max_turns_is_a_failure(self) -> None:
        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'working on it'}]}}))\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True}))"
                ))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                return await pool._execute(_work_item(Path(tmp)))

        result = asyncio.run(_run())
        assert result["status"] == "failed"
        assert "error_max_turns" in result["error"]

    def test_subprocess_is_killed_when_reading_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loopx.agents.tiers import worker as worker_module

        monkeypatch.setattr(worker_module, "STREAM_LINE_LIMIT", 1024)

        async def _run() -> int:
            with tempfile.TemporaryDirectory() as tmp:
                pid_file = Path(tmp) / "pid"
                claude = _fake_claude(Path(tmp), (
                    f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                    "print('x' * 4096, flush=True)\n"
                    "time.sleep(30)"
                ))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                with pytest.raises(ValueError):
                    await pool._execute(_work_item(Path(tmp)))
                return int(pid_file.read_text())

        pid = asyncio.run(_run())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def _result(text: str | None = None, **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "result", "subtype": "success", "is_error": False}
    if text is not None:
        event["result"] = text
    event.update(overrides)
    return event


class TestJudgeResult:
    """A step only counts as done when the run succeeded and its report matches the files."""

    @pytest.mark.parametrize(
        ("event", "error"),
        [
            (None, "without a result event"),
            (_result(subtype="error_max_turns", is_error=True), "error_max_turns"),
            (_result("x", subtype="error_during_execution"), "error_during_execution"),
            (_result("x", is_error=True), "did not succeed"),
            (_result("I made the changes successfully."), "JSON status report"),
            (_result('{"summary": "no status"}'), "JSON status report"),
            (_result('{"status": "partial"}'), "unknown status"),
            (_result('{"status": "completed", "changed_files": ["a.py"]}'), "malformed"),
            (_result('{"status": "completed", "changed_files": {"missing.py": "added"}}'), "missing.py"),
            (_result('{"status": "completed", "deleted_files": ["a.py"]}'), "a.py"),
        ],
    )
    def test_work_fails_closed(self, tmp_path: Path, event: dict[str, Any] | None, error: str) -> None:
        from loopx.agents.tiers.worker import judge_result

        (tmp_path / "a.py").write_text("x")
        result = judge_result(event, project_root=tmp_path, is_verify=False)
        assert result["status"] == "failed"
        assert error in result["error"]

    def test_missing_report_keeps_the_final_message(self, tmp_path: Path) -> None:
        from loopx.agents.tiers.worker import judge_result

        result = judge_result(_result("The sleep timed out, so I stopped."), project_root=tmp_path, is_verify=False)
        assert result["status"] == "failed"
        assert result["final_message"] == "The sleep timed out, so I stopped."

    def test_reported_failure_with_nested_files_stays_failed(self, tmp_path: Path) -> None:
        from loopx.agents.tiers.worker import judge_result

        text = (
            "Could not finish.\n```json\n"
            '{"status": "failed", "changed_files": {"a.py": "partial"}, "summary": "tests still fail"}'
            "\n```"
        )
        result = judge_result(_result(text), project_root=tmp_path, is_verify=False)
        assert result["status"] == "failed"
        assert result["error"] == "tests still fail"
        assert result["changed_files"] == {"a.py": "partial"}

    def test_completed_report_matching_the_files(self, tmp_path: Path) -> None:
        from loopx.agents.tiers.worker import judge_result

        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("x")
        report = {
            "status": "completed",
            "changed_files": {"a.py": "added", str(tmp_path / "b.py"): "edited"},
            "deleted_files": ["gone.py"],
            "summary": "done",
        }
        # An example object earlier in the text must not override the final report.
        text = 'The format is {"status": "failed"}.\n' + json.dumps(report)
        result = judge_result(_result(text), project_root=tmp_path, is_verify=False)
        assert result["status"] == "completed"
        assert result["changed_files"] == report["changed_files"]
        assert result["deleted_files"] == ["gone.py"]
        assert result["summary"] == "done"

    @pytest.mark.parametrize(
        ("text", "passed"),
        [
            ('{"passed": true, "findings": [], "summary": "ok"}', True),
            ('```json\n{"passed": false, "findings": ["no test"], "summary": "x"}\n```', False),
            ("Looks good to me.", False),
            ('{"passed": "yes"}', False),
        ],
    )
    def test_verdict(self, tmp_path: Path, text: str, passed: bool) -> None:
        from loopx.agents.tiers.worker import judge_result

        assert judge_result(_result(text), project_root=tmp_path, is_verify=True)["passed"] is passed

    def test_failed_review_run_is_not_a_pass(self, tmp_path: Path) -> None:
        from loopx.agents.tiers.worker import judge_result

        event = _result(subtype="error_max_turns", is_error=True)
        result = judge_result(event, project_root=tmp_path, is_verify=True)
        assert result["passed"] is False
        assert "error_max_turns" in result["findings"][0]
