"""Tests for the Planner Agent and Worker Pool."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from library_fixtures import ALPHA_SKILL, DEMO_PLUGIN, plugin_dirs
from loopx.agents.bus import MessageBus
from loopx.agents.claude_cli import ISOLATION_ID, isolation_args
from loopx.agents.message import (
    MANAGER_ADDRESS,
    TASK_ROUTER_ADDRESS,
    AgentAddress,
    MessageEnvelope,
    MessageType,
    planner_address,
    worker_address,
)
from loopx.agents.agent_settings import DEFAULT_HARNESS_PROMPT
from loopx.agents.system_prompt import REVIEW_CONTRACT, WORK_CONTRACT
from loopx.agents.tiers.planner import PlanStep, PlannerAgent, TaskPlan
from loopx.agents.tiers.worker import (
    DEFAULT_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
    WorkerConfig,
    WorkerPool,
    WorkerProcess,
)


# --- TaskPlan unit tests ---


class TestTaskPlan:
    def test_ready_steps_no_deps(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", work_dir="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="do A"),
            PlanStep(step_id="s2", index=1, instruction="do B"),
        ]
        ready = plan.ready_steps()
        assert len(ready) == 2

    def test_ready_steps_with_deps(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", work_dir="/tmp"
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
            plan_id="p1", goal_id="g1", requirement="test", work_dir="/tmp"
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
            plan_id="p1", goal_id="g1", requirement="test", work_dir="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="B", status="verified"),
        ]
        assert plan.is_complete

    def test_completed_not_treated_as_done(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", work_dir="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="B", status="completed"),
        ]
        assert not plan.is_complete

    def test_is_not_complete(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", work_dir="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="B", status="dispatched"),
        ]
        assert not plan.is_complete

    def test_has_failed(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", work_dir="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="B", status="failed"),
        ]
        assert plan.has_failed

    def test_progress_summary(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", work_dir="/tmp"
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
            plan_id="p1", goal_id="g1", requirement="test", work_dir="/tmp"
        )
        plan.steps = [PlanStep(step_id="s1", index=0, instruction="do A")]
        d = plan.to_dict()
        assert d["plan_id"] == "p1"
        assert len(d["steps"]) == 1
        assert d["steps"][0]["step_id"] == "s1"


# --- PlannerAgent tests ---


@pytest.mark.usefixtures("agent_db")
class TestPlannerAgent:
    def test_agent_id_includes_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            planner = PlannerAgent(task_id="abc-123", runtime_root=Path(tmp))
            assert planner.agent_id == "planner-abc-123"
            assert planner.agent_address == planner_address("abc-123")

    @staticmethod
    def _request(planner: PlannerAgent, work_dir: str) -> MessageEnvelope:
        return MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS,
            target=planner.agent_address,
            message_type=MessageType.TASK_REQUEST,
            payload={"goal_id": "g1", "requirement": "add logout button", "work_dir": work_dir},
            metadata={"origin_channel_id": "channel-web"},
        )

    def test_plan_and_dispatch(self, agent_db: Any, planner_claude: Any) -> None:
        from tests.test_planner_agent import plan, step

        claude = planner_claude(plan(step("s1")))

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                planner = PlannerAgent(task_id="test-pd", runtime_root=Path(tmp), claude_bin=claude.bin)
                await planner.start(bus)
                await bus.publish(self._request(planner, tmp))

                # The planner publishes TASK_ACCEPTED + GOAL_STATUS to task-router once Claude has planned.
                router_q = bus.get_queue("task-router")
                for _ in range(500):
                    if router_q.qsize() >= 2:
                        break
                    await asyncio.sleep(0.01)
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

    def test_plan_persistence_and_recovery(self, planner_claude: Any) -> None:
        from tests.test_planner_agent import plan, step

        claude = planner_claude(plan(step("s1"), step("s2", "s1")))

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                planner = PlannerAgent(task_id="test-pr", runtime_root=Path(tmp), claude_bin=claude.bin)
                await planner.start(bus)
                await bus.publish(self._request(planner, tmp))
                for _ in range(500):
                    if planner._plan is not None and planner._plan.steps:
                        break
                    await asyncio.sleep(0.01)
                assert planner._plan is not None
                plan_id = planner._plan.plan_id
                await planner.stop()

                planner2 = PlannerAgent(task_id="test-pr", runtime_root=Path(tmp))
                bus2 = MessageBus(runtime_root=Path(tmp))
                await planner2.start(bus2)

                restored = planner2._plan
                assert restored is not None
                assert restored.plan_id == plan_id
                # The Claude session resumes after a restart, and the steps keep their labels and order.
                assert restored.session_id == "sess-p"
                assert [(s.label, s.status) for s in restored.steps] == [("s1", "dispatched"), ("s2", "pending")]
                assert restored.steps[1].depends_on == [restored.steps[0].step_id]
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
        step = {
            "instruction": "add tests for login",
            "target_files": ["auth.py"],
            "validation_commands": ["pytest tests/test_auth.py"],
            "acceptance_criteria": ["all tests pass"],
        }
        prompt = pool._build_work_prompt({
            **step, "dependencies": [{"step": "s1", "summary": "added login()", "changed_files": ["login.py"]}],
        })
        # The step is the system prompt's task section; the user turn has the dependencies' reports.
        for part in ("add tests for login", "auth.py", "pytest tests/test_auth.py", "all tests pass"):
            assert part not in prompt
        assert "s1: added login() (changed: login.py)" in prompt
        task = pool._task_section(step, "/w", is_verify=False)
        for part in ("add tests for login", "auth.py", "$ pytest tests/test_auth.py", "- all tests pass"):
            assert part in task
        # The report contract judge_result checks is in the system prompt, not the step's user turn.
        assert '"deleted_files"' not in prompt and '"deleted_files"' in WORK_CONTRACT

    def test_build_verify_prompt(self) -> None:
        pool = WorkerPool(
            config=WorkerConfig(),
            runtime_root=Path("/tmp"),
        )
        payload = {
            "instruction": "harden the login check",
            "work_result": {"changed_files": {"auth.py": "modified"}, "summary": "hardened the login check"},
            "acceptance_criteria": ["code is clean"],
        }
        prompt = pool._build_verify_prompt(payload)
        assert "auth.py" in prompt
        assert "hardened the login check" in prompt  # the Worker's report is data under review
        assert "code is clean" not in prompt and "harden the login check" not in prompt
        task = pool._task_section(payload, "/w", is_verify=True)
        assert "code is clean" in task and "harden the login check" in task
        assert "hardened the login check" not in task
        assert '"passed"' not in prompt and '"passed"' in REVIEW_CONTRACT

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


def _work_item(work_dir: Path) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=planner_address("t1"),
        target=worker_address("w1"),
        message_type=MessageType.WORK_ITEM,
        payload={"task_id": "s1", "instruction": "add the parser", "context": {"work_dir": str(work_dir)}},
    )


def _worker_row(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, Any] | None = None,
    *,
    error: Exception | None = None,
) -> AsyncMock:
    """Serve the ``worker`` row of agent_configs (or a failed read) to the pool."""
    from loopx.db import agent_configs

    get_config = AsyncMock(return_value=row, side_effect=error)
    monkeypatch.setattr(agent_configs, "get_config", get_config)
    return get_config


def _start_warm(monkeypatch: pytest.MonkeyPatch, claude: str) -> list[asyncio.subprocess.Process]:
    """Launch ``claude`` once untimed; the worker then starts it the same way.

    Where new programs are checked at launch, a new script with new arguments
    can take seconds to start. The worker's launch gets the same (empty)
    arguments as this warm-up, so a timed test times only the run. The fake
    must exit at once when WARM_UP is set (``_WARM_UP_EXIT``). Returns the
    processes the worker starts.
    """
    subprocess.run([claude], check=True, env={**os.environ, "WARM_UP": "1"})
    started: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def _start(program: str, *_args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await real_exec(program, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _start)
    return started


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


_SUCCESS = (
    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
    "'result': json.dumps({'status': 'completed', 'summary': 'ok'})}))"
)
_SAVE_ARGV = "json.dump(sys.argv[1:], open('argv.json', 'w'))\n"
_WARM_UP_EXIT = "if os.environ.get('WARM_UP'):\n    sys.exit()\n"
# What Claude writes with --include-partial-messages while the model thinks.
_THINKING_DELTA = {
    "type": "stream_event",
    "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}},
}


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

    def test_run_is_recorded(self, agent_db: Any) -> None:
        async def _run() -> tuple[str, dict[str, Any]]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    "open('out.txt', 'w').write('x')\n"
                    "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'done'}]}}))\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
                    "'result': json.dumps({'status': 'completed', 'changed_files': {'out.txt': 'added'}})}))"
                ))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                envelope = _work_item(Path(tmp))
                envelope.payload["parent_task_id"] = "task-1"
                await pool._execute(envelope)
                return claude, {"cwd": tmp}

        claude, where = asyncio.run(_run())
        start = agent_db.start_record.await_args.kwargs
        assert (start["agent_id"], start["task_id"]) == ("w1", "task-1")
        assert start["metadata"]["command"][0] == claude
        assert start["metadata"]["cwd"] == where["cwd"]
        assert start["metadata"]["step_id"] == "s1"
        assert "--dangerously-skip-permissions" in start["metadata"]["command"]
        recorded = [call.args[1][0][1]["type"] for call in agent_db.append_events.await_args_list]
        assert recorded == ["assistant", "result"]
        finish = agent_db.finish_record.await_args.kwargs
        assert finish["status"] == "completed" and finish["error"] is None
        assert '"status": "completed"' in finish["output_text"]

    def test_mismatched_report_is_recorded_as_failed(self, agent_db: Any) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
                    "'result': json.dumps({'status': 'completed', 'changed_files': {'missing.txt': 'added'}})}))"
                ))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                await pool._execute(_work_item(Path(tmp)))

        asyncio.run(_run())
        finish = agent_db.finish_record.await_args.kwargs
        assert finish["status"] == "failed"
        assert "missing.txt" in finish["error"]

    def test_missing_work_directory_fails_without_running_claude(self) -> None:
        async def _run() -> tuple[dict[str, Any], bool]:
            with tempfile.TemporaryDirectory() as tmp:
                marker = Path(tmp) / "ran"
                claude = _fake_claude(Path(tmp), f"open({str(marker)!r}, 'w').write('1')")
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                envelope = _work_item(Path(tmp))
                envelope.payload["context"] = {}
                result = await pool._execute(envelope)
                return result, marker.exists()

        result, ran = asyncio.run(_run())
        assert result["status"] == "failed"
        assert "work directory" in result["error"]
        assert not ran

    def test_missing_work_directory_is_created(self) -> None:
        async def _run() -> tuple[dict[str, Any], bool]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    "open('out.txt', 'w').write('x')\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
                    "'result': json.dumps({'status': 'completed', 'changed_files': {'out.txt': 'added'}})}))"
                ))
                target = Path(tmp) / "new" / "project"
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                result = await pool._execute(_work_item(target))
                return result, (target / "out.txt").exists()

        result, written = asyncio.run(_run())
        assert result["status"] == "completed"
        assert written  # Claude ran inside the directory it created

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

    def test_run_over_the_time_limit_is_killed(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        # Claude keeps writing thinking deltas: output does not extend the limit.
        _worker_row(monkeypatch, {"timeout_seconds": 1, "max_turns": 0})

        async def _run() -> list[asyncio.subprocess.Process]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    _WARM_UP_EXIT
                    + "for _ in range(25):\n"
                    f"    print(json.dumps({_THINKING_DELTA!r}), flush=True)\n"
                    "    time.sleep(0.2)\n"
                    + _SUCCESS
                ))
                started = _start_warm(monkeypatch, claude)
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                with pytest.raises(asyncio.TimeoutError, match=r"longer than the 1 s limit \(timeout_seconds\)"):
                    await pool._execute(_work_item(Path(tmp)))
                return started

        (process,) = asyncio.run(_run())
        assert process.returncode == -signal.SIGKILL  # killed and reaped
        agent_db.touch_record.assert_awaited()  # output arrived and fed the heartbeat
        agent_db.append_events.assert_not_awaited()  # partial deltas are not stored
        finish = agent_db.finish_record.await_args.kwargs
        assert finish["status"] == "failed"
        assert finish["error"] == "Claude ran longer than the 1 s limit (timeout_seconds)"

    def test_time_limit_is_the_step_failure_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _worker_row(monkeypatch, {"timeout_seconds": 1, "max_turns": 0})

        async def _run() -> MessageBus:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), "time.sleep(30)")
                bus = MessageBus(runtime_root=Path(tmp))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                pool._bus = bus
                await pool._run_worker(_work_item(Path(tmp)))
                return bus

        reply = asyncio.run(_run()).get_queue("planner-t1").get_nowait()
        assert reply.payload["status"] == "failed"
        assert reply.payload["error"] == "Claude ran longer than the 1 s limit (timeout_seconds)"

    def test_zero_timeout_has_no_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        get_config = _worker_row(monkeypatch, {"timeout_seconds": 0, "max_turns": 0})

        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), "time.sleep(1.5)\n" + _SUCCESS)
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                return await pool._execute(_work_item(Path(tmp)))

        assert asyncio.run(_run())["status"] == "completed"
        get_config.assert_awaited_with("worker")

    @pytest.mark.parametrize(("max_turns", "expected"), [(7, ["--max-turns", "7"]), (0, [])])
    def test_command_follows_the_worker_config(
        self, monkeypatch: pytest.MonkeyPatch, max_turns: int, expected: list[str],
    ) -> None:
        _worker_row(monkeypatch, {"timeout_seconds": 86400, "max_turns": max_turns})

        async def _run() -> list[str]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), _SAVE_ARGV + _SUCCESS)
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                assert (await pool._execute(_work_item(Path(tmp))))["status"] == "completed"
                return json.loads((Path(tmp) / "argv.json").read_text())

        argv = asyncio.run(_run())
        assert "--include-partial-messages" in argv
        assert "--dangerously-skip-permissions" in argv
        # The Worker loads none of the operator's settings, memory, hooks, plugins or MCP servers.
        assert _flag(argv, "--setting-sources") == "project"
        assert "--strict-mcp-config" in argv and "--settings" in argv
        # The system prompt is a file, not an argument.
        assert "--append-system-prompt" not in argv and "--append-system-prompt-file" in argv
        turns = argv[argv.index("--max-turns"):][:2] if "--max-turns" in argv else []
        assert turns == expected

    def test_successful_result_survives_a_nonzero_exit(self) -> None:
        """Claude can exit non-zero after a successful result (e.g. an MCP/hook
        teardown error); the judged result decides, not the exit code."""

        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    "open('out.txt', 'w').write('x')\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
                    "'result': json.dumps({'status': 'completed', 'changed_files': {'out.txt': 'added'}})}))\n"
                    "sys.exit(1)"
                ))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                return await pool._execute(_work_item(Path(tmp)))

        assert asyncio.run(_run())["status"] == "completed"

    def test_nonzero_exit_without_a_result_still_fails(self) -> None:
        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                # Prints no result event, then exits non-zero.
                claude = _fake_claude(Path(tmp), "sys.exit(1)")
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                return await pool._execute(_work_item(Path(tmp)))

        result = asyncio.run(_run())
        assert result["status"] == "failed" and "exited with code 1" in result["error"]

    @pytest.mark.parametrize("failure", [None, RuntimeError("database pool not initialised")])
    def test_unreadable_config_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception | None,
    ) -> None:
        from loopx.agents.tiers.worker import _load_settings

        _worker_row(monkeypatch, None, error=failure)
        settings = asyncio.run(_load_settings())
        assert (settings.timeout_seconds, settings.max_turns) == (86400, 0)  # 86400 s, no turn limit
        assert settings.system_prompt == DEFAULT_SYSTEM_PROMPT.strip()

    @pytest.mark.parametrize("prompt", ["", "   \n"])
    def test_an_empty_role_prompt_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, prompt: str,
    ) -> None:
        from loopx.agents.tiers.worker import _load_settings

        _worker_row(monkeypatch, {"system_prompt": prompt, "timeout_seconds": 5, "max_turns": 3})
        settings = asyncio.run(_load_settings())
        assert (settings.timeout_seconds, settings.max_turns) == (5, 3)
        assert settings.system_prompt == DEFAULT_SYSTEM_PROMPT.strip()


# What the stand-in Claude saw: its argv, the system prompt its file held and its first user turn.
_RECORD_RUN = (
    "first = json.loads(sys.stdin.readline())['message']['content']\n"
    "prompt_file = sys.argv[sys.argv.index('--append-system-prompt-file') + 1]\n"
    "prompt = open(prompt_file, encoding='utf-8').read()\n"
    "json.dump({'argv': sys.argv[1:], 'stdin': first, 'prompt': prompt}, open('run.json', 'w'))\n"
)
_VERDICT = (
    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
    "'result': json.dumps({'passed': True, 'findings': [], 'summary': 'ok'})}), flush=True)"
)
ROLE = "You are a careful Worker."


REQUIREMENT = "Build a small calculator package."


def _step_envelope(work_dir: Path, *, verify: bool, **extra: Any) -> MessageEnvelope:
    payload: dict[str, Any] = {
        "task_id": "s1", "parent_task_id": "task-1", "plan_id": "p1", "instruction": "add calc.py",
        "acceptance_criteria": ["calc.add(1, 2) == 3"], "context": {"work_dir": str(work_dir)},
        "requirement": REQUIREMENT, "step_key": "calc-module", **extra,
    }
    if verify:
        payload["work_result"] = {"status": "completed", "changed_files": {"calc.py": "added"},
                                  "summary": "added calc.py with add()"}
    return MessageEnvelope.create(
        source=planner_address("task-1"),
        target=worker_address("reviewer-1" if verify else "worker-1"),
        message_type=MessageType.VERIFY_REQUEST if verify else MessageType.WORK_ITEM,
        payload=payload,
    )


def _run_step(*, verify: bool = False, **extra: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one Worker (or Reviewer) step; return its outcome and what its Claude saw."""

    async def _run() -> tuple[dict[str, Any], dict[str, Any]]:
        with tempfile.TemporaryDirectory() as tmp:
            claude = _fake_claude(Path(tmp), _RECORD_RUN + (_VERDICT if verify else _SUCCESS))
            pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
            result = await pool._execute(_step_envelope(Path(tmp), verify=verify, **extra), is_verify=verify)
            return result, {**json.loads((Path(tmp) / "run.json").read_text()), "work_dir": tmp}

    return asyncio.run(_run())


def _system_prompt(seen: dict[str, Any]) -> str:
    return seen["prompt"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _in_order(text: str, *parts: str) -> None:
    at = [text.index(part) for part in parts]
    assert at == sorted(at), f"out of order: {parts}"


def _task_section(prompt: str, contract: str) -> str:
    return prompt[prompt.index("## Task"):prompt.index(contract)].rstrip("\n")


class TestWorkerSystemPrompt:
    """A Worker's or Reviewer's system prompt: the harness, its role prompt,
    the project's background (read when the run starts), the task section (the
    step, with the task it belongs to) and the code-owned report contract,
    last. The first user turn on stdin carries only other agents' output."""

    @pytest.fixture(autouse=True)
    def _role(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        _worker_row(monkeypatch, {"system_prompt": ROLE + "\n", "timeout_seconds": 0, "max_turns": 0})
        agent_db.get_task_project.return_value = {"id": "proj-1", "name": "calc", "background": "Use uv.\nPython 3.12.",
                                                  "updated_at": "2026-10-13T12:00:00+00:00"}

    def test_worker_gets_the_harness_role_project_task_and_work_contract(self, agent_db: Any) -> None:
        result, seen = _run_step()

        assert result["status"] == "completed"
        prompt = _system_prompt(seen)
        _in_order(prompt, f"{DEFAULT_HARNESS_PROMPT}\n\n{ROLE}\n\n", "## Project: calc\nUse uv.\nPython 3.12.\n\n",
                  "## Task\n", REQUIREMENT, "calc-module", "add calc.py", "calc.add(1, 2) == 3", WORK_CONTRACT)
        assert prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\n{ROLE}\n\n") and prompt.endswith(WORK_CONTRACT)
        agent_db.get_task_project.assert_awaited_once_with("task-1")
        # The step is the system prompt's task section; stdin only starts the work.
        for part in ("add calc.py", "calc.add(1, 2) == 3", REQUIREMENT, '"deleted_files"', "End your final message"):
            assert part not in seen["stdin"]
        assert seen["stdin"].strip()
        # The shared isolation flags, for the step's directory, and the prompt file named by its hash.
        isolation = isolation_args(seen["work_dir"])
        at = seen["argv"].index(isolation[0])
        assert seen["argv"][at:at + len(isolation)] == isolation
        assert "--append-system-prompt" not in seen["argv"]
        assert Path(_flag(seen["argv"], "--append-system-prompt-file") or "").name == f"{_sha(prompt)}.md"
        assert _flag(seen["argv"], "--system-prompt-snapshot") == "off"
        # The run's record keeps the exact argv, the system prompt, its hash and its layers.
        metadata = agent_db.start_record.await_args.kwargs["metadata"]
        assert metadata["command"][1:] == seen["argv"]
        assert metadata["system_prompt"] == prompt
        assert metadata["system_prompt_sha256"] == _sha(prompt)
        layers = metadata["prompt_layers"]
        assert layers["harness"] == {"source": "builtin_default", "sha256": _sha(DEFAULT_HARNESS_PROMPT)}
        assert layers["role"] == {"config_id": "worker", "sha256": _sha(ROLE), "recommended": False}
        assert layers["project"] == {"id": "proj-1", "background_sha256": _sha("Use uv.\nPython 3.12."),
                                     "updated_at": "2026-10-13T12:00:00+00:00"}
        assert layers["task"] == {"kind": "work", "task_id": "task-1", "step_id": "s1",
                                  "sha256": _sha(_task_section(prompt, WORK_CONTRACT))}
        assert layers["contract"] == {"name": "WORK_CONTRACT", "sha256": _sha(WORK_CONTRACT)}
        assert layers["isolation"] == ISOLATION_ID
        summaries = activity(agent_db)
        assert "Project background applied (20 chars)" in summaries
        # The timeline's launch line names the prompt file and keeps the isolation settings short.
        (launch,) = [line for line in summaries if line.startswith("$ ")]
        assert "--append-system-prompt-file " in launch and "Use uv." not in launch
        assert "claudeMdExcludes" not in launch

    def test_reviewer_gets_the_step_in_its_task_and_the_work_as_data(self, agent_db: Any) -> None:
        result, seen = _run_step(verify=True)

        assert result["passed"] is True
        prompt = _system_prompt(seen)
        _in_order(prompt, f"{DEFAULT_HARNESS_PROMPT}\n\n{ROLE}\n\n", "## Project: calc\n", "## Task\n",
                  "add calc.py", "calc.add(1, 2) == 3", REVIEW_CONTRACT)
        assert REQUIREMENT in prompt and prompt.endswith(REVIEW_CONTRACT)
        metadata = agent_db.start_record.await_args.kwargs["metadata"]
        assert metadata["system_prompt"] == prompt
        assert metadata["prompt_layers"]["role"]["config_id"] == "reviewer"
        assert metadata["prompt_layers"]["task"]["kind"] == "review"
        assert metadata["prompt_layers"]["contract"]["name"] == "REVIEW_CONTRACT"
        # The work under review, the Worker's own words included, stays in the user turn.
        for data in ("calc.py", "added calc.py with add()"):
            assert data in seen["stdin"]
        assert "added calc.py with add()" not in prompt
        for part in ("add calc.py\n", "calc.add(1, 2) == 3", REQUIREMENT, '"passed"'):
            assert part not in seen["stdin"]

    def test_a_retried_step_has_its_feedback_in_the_task(self, agent_db: Any) -> None:
        _, seen = _run_step(feedback=["import calc from the package", "add sub()"], attempt=3)

        task = _task_section(_system_prompt(seen), WORK_CONTRACT)
        assert "attempt 3" in task and "- import calc from the package\n- add sub()" in task
        assert "import calc from the package" not in seen["stdin"]

    def test_the_dependencies_reports_are_the_user_turn(self, agent_db: Any) -> None:
        deps = [{"step": "core", "summary": "added the core module", "changed_files": ["core.py"]}]
        _, seen = _run_step(dependencies=deps)

        assert "core" in seen["stdin"] and "added the core module" in seen["stdin"] and "core.py" in seen["stdin"]
        assert "added the core module" not in _system_prompt(seen)

    def test_without_a_background_it_is_the_role_the_task_and_the_contract(self, agent_db: Any) -> None:
        agent_db.get_task_project.return_value = {"id": "proj-1", "name": "calc", "background": ""}
        _, seen = _run_step()

        prompt = _system_prompt(seen)
        assert prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\n{ROLE}\n\n## Task\n") and prompt.endswith(WORK_CONTRACT)
        assert "## Project" not in prompt
        assert not [line for line in activity(agent_db) if "background" in line]

    @pytest.mark.parametrize("lookup", [ConnectionError("database is down"), None])
    def test_an_unreadable_project_runs_the_step_without_it_and_warns(
        self, agent_db: Any, lookup: Exception | None,
    ) -> None:
        if lookup is None:
            agent_db.get_task_project.return_value = None  # the task's project is gone
        else:
            agent_db.get_task_project.side_effect = lookup
        result, seen = _run_step()

        assert result["status"] == "completed"
        assert _system_prompt(seen).startswith(f"{DEFAULT_HARNESS_PROMPT}\n\n{ROLE}\n\n## Task\n")
        assert agent_db.start_record.await_args.kwargs["metadata"]["prompt_layers"]["project"] is None
        warnings = [call.kwargs for call in agent_db.insert_activity.await_args_list
                    if "background" in str(call.kwargs.get("summary"))]
        assert len(warnings) == 1
        assert warnings[0]["kind"] == "error" and warnings[0]["worker"] == "worker-1"
        assert warnings[0]["summary"].startswith("Warning: the project background was unavailable")

    def test_each_run_reads_the_background_when_it_starts(self, agent_db: Any) -> None:
        _, first = _run_step()
        agent_db.get_task_project.return_value = {"id": "proj-1", "name": "calc", "background": "Now use pnpm."}
        _, second = _run_step(verify=True)

        assert "## Project: calc\nUse uv.\nPython 3.12.\n\n" in _system_prompt(first)
        assert "## Project: calc\nNow use pnpm.\n\n" in _system_prompt(second)

    def test_the_default_role_prompt_applies_without_a_config_row(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
    ) -> None:
        _worker_row(monkeypatch, None, error=RuntimeError("database pool not initialised"))
        _, seen = _run_step()

        prompt = _system_prompt(seen)
        assert prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\n{DEFAULT_SYSTEM_PROMPT.strip()}\n\n## Project: calc\n")
        assert prompt.endswith(WORK_CONTRACT)
        role = agent_db.start_record.await_args.kwargs["metadata"]["prompt_layers"]["role"]
        assert role == {"config_id": "worker", "sha256": _sha(DEFAULT_SYSTEM_PROMPT.strip()), "recommended": True}

    def test_a_reviewer_without_a_config_row_gets_the_reviewer_role_prompt(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
    ) -> None:
        get_config = _worker_row(monkeypatch, None)
        _, seen = _run_step(verify=True)

        get_config.assert_awaited_with("reviewer")
        prompt = _system_prompt(seen)
        assert prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\n{REVIEWER_SYSTEM_PROMPT.strip()}\n\n## Project")
        assert DEFAULT_SYSTEM_PROMPT.strip() not in prompt  # not the Worker's role
        role = agent_db.start_record.await_args.kwargs["metadata"]["prompt_layers"]["role"]
        assert role == {"config_id": "reviewer", "sha256": _sha(REVIEWER_SYSTEM_PROMPT.strip()), "recommended": True}

    def test_an_unedited_row_is_the_recommended_role_prompt(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
    ) -> None:
        _worker_row(monkeypatch, {"system_prompt": ROLE, "default_prompt": ROLE + "\n", "timeout_seconds": 0,
                                  "max_turns": 0})
        _run_step()
        assert agent_db.start_record.await_args.kwargs["metadata"]["prompt_layers"]["role"]["recommended"] is True


def _project_row(project_id: str = "proj-1", *, worker: str = "", planner: str = "") -> dict[str, Any]:
    return {"id": project_id, "name": project_id, "background": "", "worker_harness_prompt": worker,
            "planner_harness_prompt": planner}


class TestWorkerHarnessAndLaunch:
    """Each Worker and Reviewer run reads the harness, the model, the effort
    and the environment as it starts: its project's Worker harness else the
    Settings default, its own row's model else the Settings default."""

    @pytest.fixture
    def rows(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
        """The ``worker`` and ``reviewer`` config rows, by agent id."""
        from loopx.db import agent_configs

        rows = {"worker": {"system_prompt": "WORKER ROLE", "timeout_seconds": 0, "max_turns": 0, "model": None},
                "reviewer": {"system_prompt": "REVIEWER ROLE", "timeout_seconds": 0, "max_turns": 0, "model": None}}

        async def _config(agent_id: str) -> dict[str, Any] | None:
            return dict(rows[agent_id]) if agent_id in rows else None

        monkeypatch.setattr(agent_configs, "get_config", _config)
        return rows

    @staticmethod
    def _settings(agent_db: Any, **stored: str) -> None:
        keys = {"harness": "agent.harness_prompt", "model": "agent.model", "effort": "agent.effort", "env": "agent.env"}
        agent_db.get_settings.return_value = {keys[name]: value for name, value in stored.items()}

    def test_the_settings_default_harness_comes_first_for_both(
        self, agent_db: Any, rows: dict[str, Any],
    ) -> None:
        self._settings(agent_db, harness="DEFAULT HARNESS")
        agent_db.get_task_project.return_value = _project_row()
        for verify, role, contract in ((False, "WORKER ROLE", WORK_CONTRACT), (True, "REVIEWER ROLE", REVIEW_CONTRACT)):
            _, seen = _run_step(verify=verify)
            prompt = _system_prompt(seen)
            assert prompt.startswith(f"DEFAULT HARNESS\n\n{role}\n\n## Task\n") and prompt.endswith(contract)
            layers = agent_db.start_record.await_args.kwargs["metadata"]["prompt_layers"]
            assert layers["harness"] == {"source": "system_default", "sha256": _sha("DEFAULT HARNESS")}

    def test_the_projects_worker_harness_reaches_its_worker_and_reviewer_only(
        self, agent_db: Any, rows: dict[str, Any],
    ) -> None:
        self._settings(agent_db, harness="DEFAULT HARNESS")
        agent_db.get_task_project.return_value = _project_row("proj-a", worker="A WORKERS", planner="A PLANNER")
        prompts = [_system_prompt(_run_step(verify=verify)[1]) for verify in (False, True)]
        layers = [call.kwargs["metadata"]["prompt_layers"]["harness"]
                  for call in agent_db.start_record.await_args_list]
        assert [p.split("\n\n")[0] for p in prompts] == ["A WORKERS", "A WORKERS"]
        assert all("DEFAULT HARNESS" not in p and "A PLANNER" not in p for p in prompts)
        assert layers == [{"source": "project_override", "sha256": _sha("A WORKERS"), "project_id": "proj-a"}] * 2
        # A task in another project gets the default.
        agent_db.get_task_project.return_value = _project_row("proj-b")
        for verify in (False, True):
            assert _system_prompt(_run_step(verify=verify)[1]).startswith("DEFAULT HARNESS\n\n")

    def test_a_changed_default_applies_to_the_next_run(self, agent_db: Any, rows: dict[str, Any]) -> None:
        self._settings(agent_db, harness="V1")
        _, first = _run_step()
        self._settings(agent_db, harness="V2")
        rows["worker"]["system_prompt"] = "WORKER ROLE v2"
        _, second = _run_step()
        assert _system_prompt(first).startswith("V1\n\nWORKER ROLE\n\n")
        assert _system_prompt(second).startswith("V2\n\nWORKER ROLE v2\n\n")

    def test_the_default_model_reaches_both_commands_and_is_recorded(
        self, agent_db: Any, rows: dict[str, Any],
    ) -> None:
        for verify in (False, True):
            _, seen = _run_step(verify=verify)
            assert _flag(seen["argv"], "--model") == "opus" and "--effort" not in seen["argv"]
            metadata = agent_db.start_record.await_args.kwargs["metadata"]
            assert metadata["model_choice"] == {"model": "opus", "source": "builtin_default", "effort": None}
            assert "env" not in metadata

    def test_an_agent_model_beats_the_settings_default(self, agent_db: Any, rows: dict[str, Any]) -> None:
        self._settings(agent_db, model="sonnet", effort="high")
        rows["reviewer"]["model"] = "claude-haiku-4-5"
        _, work = _run_step()
        work_metadata = agent_db.start_record.await_args.kwargs["metadata"]
        _, review = _run_step(verify=True)
        review_metadata = agent_db.start_record.await_args.kwargs["metadata"]
        assert (_flag(work["argv"], "--model"), _flag(work["argv"], "--effort")) == ("sonnet", "high")
        assert (_flag(review["argv"], "--model"), _flag(review["argv"], "--effort")) == ("claude-haiku-4-5", "high")
        assert work_metadata["model_choice"] == {"model": "sonnet", "source": "system_default", "effort": "high"}
        assert review_metadata["model_choice"] == {"model": "claude-haiku-4-5", "source": "agent_override",
                                                   "effort": "high"}

    def test_the_settings_environment_reaches_the_subprocess_and_its_names_are_recorded(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, rows: dict[str, Any],
    ) -> None:
        self._settings(agent_db, env=json.dumps([{"key": "LOOPX_TEST_FLAVOUR", "value": "mint"}]))
        monkeypatch.setenv("LOOPX_SERVER_SIDE", "kept")
        launched: list[dict[str, Any]] = []
        real_exec = asyncio.create_subprocess_exec

        async def _exec(*args: Any, **kwargs: Any) -> Any:
            launched.append(kwargs)
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
        for verify in (False, True):
            _run_step(verify=verify)
        assert len(launched) == 2
        for kwargs in launched:
            assert kwargs["env"]["LOOPX_TEST_FLAVOUR"] == "mint"
            assert kwargs["env"]["LOOPX_SERVER_SIDE"] == "kept"  # on top of the server's environment
        recorded = [call.kwargs["metadata"]["env"] for call in agent_db.start_record.await_args_list]
        assert [r["keys"] for r in recorded] == [{"LOOPX_TEST_FLAVOUR": _sha("mint")}] * 2
        assert "mint" not in json.dumps(recorded)

    def test_without_settings_variables_the_subprocess_inherits_the_servers_environment(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, rows: dict[str, Any],
    ) -> None:
        launched: list[dict[str, Any]] = []
        real_exec = asyncio.create_subprocess_exec

        async def _exec(*args: Any, **kwargs: Any) -> Any:
            launched.append(kwargs)
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
        _run_step()
        assert launched[0]["env"] is None


class TestWorkerLibrary:
    """Each Worker and Reviewer run loads the library's enabled items as it starts."""

    def test_both_commands_carry_the_library_and_record_it(self, agent_db: Any, enabled_library: Any) -> None:
        demo, alpha = enabled_library(DEMO_PLUGIN, ALPHA_SKILL)
        for verify in (False, True):
            _, seen = _run_step(verify=verify)
            plugin, skills = plugin_dirs(seen["argv"])
            assert plugin == f"{seen['work_dir']}/.local/library/{demo['sha256']}/demo"
            assert (Path(skills) / "skills" / "alpha" / "SKILL.md").name == "SKILL.md"
            assert skills.endswith("/personal")
            library = agent_db.start_record.await_args.kwargs["metadata"]["library"]
            assert [(i["name"], i["sha256"]) for i in library["items"]] == [
                ("demo@market", demo["sha256"]), ("alpha", alpha["sha256"])]

    def test_a_disabled_item_is_not_loaded_by_the_next_run(self, agent_db: Any, enabled_library: Any) -> None:
        enabled_library(DEMO_PLUGIN, ALPHA_SKILL)
        _, first = _run_step()
        enabled_library(DEMO_PLUGIN)
        _, second = _run_step()
        assert len(plugin_dirs(first["argv"])) == 2
        (only,) = plugin_dirs(second["argv"])
        assert only.endswith("/demo")
        recorded = [call.kwargs["metadata"]["library"] for call in agent_db.start_record.await_args_list]
        assert [[i["name"] for i in r["items"]] for r in recorded] == [["demo@market", "alpha"], ["demo@market"]]


def activity(agent_db: Any) -> list[str]:
    return [str(call.kwargs.get("summary")) for call in agent_db.insert_activity.await_args_list]


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
        result = judge_result(event, work_dir=tmp_path, is_verify=False)
        assert result["status"] == "failed"
        assert error in result["error"]

    def test_missing_report_keeps_the_final_message(self, tmp_path: Path) -> None:
        from loopx.agents.tiers.worker import judge_result

        result = judge_result(_result("The sleep timed out, so I stopped."), work_dir=tmp_path, is_verify=False)
        assert result["status"] == "failed"
        assert result["final_message"] == "The sleep timed out, so I stopped."

    def test_reported_failure_with_nested_files_stays_failed(self, tmp_path: Path) -> None:
        from loopx.agents.tiers.worker import judge_result

        text = (
            "Could not finish.\n```json\n"
            '{"status": "failed", "changed_files": {"a.py": "partial"}, "summary": "tests still fail"}'
            "\n```"
        )
        result = judge_result(_result(text), work_dir=tmp_path, is_verify=False)
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
        result = judge_result(_result(text), work_dir=tmp_path, is_verify=False)
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

        assert judge_result(_result(text), work_dir=tmp_path, is_verify=True)["passed"] is passed

    def test_failed_review_run_is_not_a_pass(self, tmp_path: Path) -> None:
        from loopx.agents.tiers.worker import judge_result

        event = _result(subtype="error_max_turns", is_error=True)
        result = judge_result(event, work_dir=tmp_path, is_verify=True)
        assert result["passed"] is False
        assert "error_max_turns" in result["findings"][0]



_LISTENING_CLAUDE = (
    "first = json.loads(sys.stdin.readline())\n"
    "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}), flush=True)\n"
    "line = sys.stdin.readline()\n"
    "if line:\n"
    "    text = json.loads(line)['message']['content']\n"
    "    print(json.dumps({'type': 'user', 'message': {'content': text}}), flush=True)\n"
    "    answer = 'got: ' + text\n"
    "else:\n"
    "    answer = 'no input'\n"
    "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': answer}]}}), flush=True)\n"
    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
    "'result': json.dumps({'status': 'completed', 'changed_files': {}, 'summary': answer})}), flush=True)\n"
    "sys.stdin.read()\n"
)


class TestWorkerInput:
    """A running worker reads further user turns on stdin (stream-json input)."""

    def test_the_run_starts_with_a_user_turn_on_stdin(self, agent_db: Any) -> None:
        async def _run() -> list[str]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    "first = json.loads(sys.stdin.readline())\n"
                    "assert first['type'] == 'user' and first['message']['content'].strip()\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
                    "'result': json.dumps({'status': 'completed', 'changed_files': {}, 'summary': 'ok'})}), flush=True)\n"
                    "sys.stdin.read()"
                ))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                result = await pool._execute(_work_item(Path(tmp)))
                assert result["status"] == "completed"
                return agent_db.start_record.await_args.kwargs["metadata"]["command"]

        command = asyncio.run(_run())
        assert "--input-format" in command and command[command.index("--input-format") + 1] == "stream-json"
        assert "--replay-user-messages" in command
        assert not any("add the parser" in arg for arg in command)  # the step is not an argument

    def test_message_reaches_a_running_worker(self, agent_db: Any) -> None:
        async def _run() -> tuple[bool, dict[str, Any], bool]:
            with tempfile.TemporaryDirectory() as tmp:
                pool = WorkerPool(config=WorkerConfig(claude_bin=_fake_claude(Path(tmp), _LISTENING_CLAUDE)),
                                  runtime_root=Path(tmp))
                run = asyncio.create_task(pool._execute(_work_item(Path(tmp))))
                for _ in range(200):
                    if pool.accepts_input("w1"):
                        break
                    await asyncio.sleep(0.02)
                delivered = await pool.send_input("w1", "also add tests")
                result = await asyncio.wait_for(run, 20)
                late = await pool.send_input("w1", "too late")
                return delivered, result, late

        delivered, result, late = asyncio.run(_run())
        assert delivered
        assert result["status"] == "completed"
        assert "got: also add tests" in result["summary"]
        assert not late  # the run has ended; nothing reads its stdin any more

    def test_worker_input_over_the_bus(self, agent_db: Any) -> None:
        async def _run() -> tuple[bool, bool]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                pool = WorkerPool(config=WorkerConfig(claude_bin=_fake_claude(Path(tmp), _LISTENING_CLAUDE)),
                                  runtime_root=Path(tmp))
                await pool.start(bus)
                await asyncio.sleep(0)
                await bus.publish(_work_item(Path(tmp)))
                for _ in range(200):
                    if pool.accepts_input("w1"):
                        break
                    await asyncio.sleep(0.02)

                def ask(text: str) -> MessageEnvelope:
                    return MessageEnvelope.create(
                        source=planner_address("t1"), target=worker_address("w1"),
                        message_type=MessageType.WORKER_INPUT, payload={"text": text},
                    )

                first = await bus.request(ask("hello"), timeout=5)
                unknown = await bus.request(MessageEnvelope.create(
                    source=planner_address("t1"), target=worker_address("nobody"),
                    message_type=MessageType.WORKER_INPUT, payload={"text": "x"},
                ), timeout=5)
                await pool.stop()
                await bus.shutdown()
                return first.payload["delivered"], unknown.payload["delivered"]

        assert asyncio.run(_run()) == (True, False)


class TestWorkerKill:
    """The Planner can stop a running worker mid-run; a killed worker publishes
    no result and frees its pool slot."""

    def test_kill_stops_a_running_worker(self, agent_db: Any) -> None:
        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                pool = WorkerPool(config=WorkerConfig(claude_bin=_fake_claude(Path(tmp), _LISTENING_CLAUDE)),
                                  runtime_root=Path(tmp))
                await pool.start(bus)
                await asyncio.sleep(0)
                await bus.publish(_work_item(Path(tmp)))
                for _ in range(200):
                    if pool.accepts_input("w1"):
                        break
                    await asyncio.sleep(0.02)
                wp = pool._active["w1"]
                assert wp.process is not None and wp.process.returncode is None  # the dead-code fix
                proc = wp.process

                killed = await pool.kill("w1")
                missing = await pool.kill("w1")  # already gone
                await proc.wait()
                result = {
                    "killed": killed,
                    "missing": missing,
                    "returncode": proc.returncode,
                    "active": pool.active_count(),
                    "accepts_input": pool.accepts_input("w1"),
                    "published": not bus.get_queue("planner-t1").empty(),
                }
                await pool.stop()
                await bus.shutdown()
                return result

        result = asyncio.run(_run())
        assert result["killed"] is True
        assert result["missing"] is False  # a finished/unknown worker is not "killed"
        assert result["returncode"] is not None  # the subprocess was actually stopped
        assert result["active"] == 0  # the slot is freed
        assert result["accepts_input"] is False
        assert result["published"] is False  # a killed worker publishes no WORK_RESULT
