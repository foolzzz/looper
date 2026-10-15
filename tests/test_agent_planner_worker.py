"""Tests for the Planner Agent and Worker Pool."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_fixtures import profile_json

from fake_claude import fake_claude
from library_fixtures import ALPHA_SKILL, DEMO_PLUGIN, plugin_dirs
from looper.agents.bus import MessageBus
from looper.agents.claude_cli import ISOLATION_ID, isolation_args
from looper.agents.message import (
    MessageEnvelope,
    MessageType,
    planner_address,
    worker_address,
)
from looper.agents.agent_settings import DEFAULT_HARNESS_PROMPT
from looper.agents.system_prompt import REVIEW_CONTRACT, WORK_CONTRACT
from looper.agents.tiers.planner import PlanStep, TaskPlan
from looper.agents.tiers.worker import (
    DEFAULT_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
    WorkerConfig,
    WorkerPool,
)
from tests.test_planner_agent import _assert_unrestricted, _flag, _sha, activity


# --- TaskPlan unit tests ---


class TestTaskPlan:
    def test_completed_not_treated_as_done(self) -> None:
        plan = TaskPlan(
            plan_id="p1", goal_id="g1", requirement="test", work_dir="/tmp"
        )
        plan.steps = [
            PlanStep(step_id="s1", index=0, instruction="A", status="verified"),
            PlanStep(step_id="s2", index=1, instruction="B", status="completed"),
        ]
        assert not plan.is_complete

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


# --- Worker tests ---


class TestWorkerPool:
    def test_pool_start_stop(self, stub_claude_on_path: Path) -> None:
        # is_healthy() needs a `claude` on PATH; the stub keeps this off the machine's own install.
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
                assert pool.status()["active"] == 0

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
    return fake_claude(tmp / "fake-claude", f"import json, os, sys, time\n{body}\n")


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
    """Serve the ``worker`` row of role_configs (or a failed read) to the pool."""
    from looper.db import role_configs

    get_config = AsyncMock(return_value=row, side_effect=error)
    monkeypatch.setattr(role_configs, "get_config", get_config)
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
    def test_run_records_its_tokens_context_and_compactions(self, agent_db: Any) -> None:
        # Two model requests, an automatic compaction between them, and Claude's totals in the result.
        def request(input_tokens: int, cache_read: int, cache_creation: int) -> str:
            usage = {"input_tokens": input_tokens, "cache_read_input_tokens": cache_read,
                     "cache_creation_input_tokens": cache_creation, "output_tokens": 1}
            return ("print(json.dumps({'type': 'assistant', 'parent_tool_use_id': None, 'message': "
                    f"{{'content': [{{'type': 'text', 'text': 'step'}}], 'usage': {usage!r}}}}}))\n")

        async def _run() -> tuple[str, str]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    request(10, 15000, 3000)
                    + "print(json.dumps({'type': 'system', 'subtype': 'compact_boundary', 'compact_metadata': "
                      "{'trigger': 'auto', 'pre_tokens': 90000, 'post_tokens': 2000, 'duration_ms': 900, "
                      "'cumulative_dropped_tokens': 88000}}))\n"
                    + request(8, 15100, 2400)
                    + "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
                      "'result': json.dumps({'status': 'completed', 'summary': 'ok'}), 'total_cost_usd': 0.4, "
                      "'usage': {'input_tokens': 18, 'output_tokens': 2}, 'modelUsage': {"
                      "'claude-a': {'inputTokens': 5018, 'outputTokens': 902, 'cacheReadInputTokens': 30100, "
                      "'cacheCreationInputTokens': 5400, 'costUSD': 0.3}, "
                      "'claude-b': {'inputTokens': 2, 'outputTokens': 3, 'cacheReadInputTokens': 4, "
                      "'cacheCreationInputTokens': 5, 'costUSD': 0.1}}}))"
                ))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                envelope = _work_item(Path(tmp))
                envelope.payload["parent_task_id"] = "task-1"
                await pool._execute(envelope)
                return claude, tmp

        claude, cwd = asyncio.run(_run())
        start = agent_db.start_record.await_args.kwargs
        assert (start["agent_id"], start["task_id"]) == ("w1", "task-1")
        assert start["metadata"]["command"][0] == claude
        assert (start["metadata"]["cwd"], start["metadata"]["step_id"]) == (cwd, "s1")
        finish = agent_db.finish_record.await_args.kwargs
        assert finish["status"] == "completed" and finish["error"] is None
        assert '"status": "completed"' in finish["output_text"]
        # The run is the whole process: its totals over every model, compaction request included.
        assert finish["cost_usd"] == 0.4
        assert (finish["input_tokens"], finish["output_tokens"], finish["cache_read_tokens"],
                finish["cache_creation_tokens"]) == (5020, 905, 30104, 5405)
        assert finish["context_tokens"] == 8 + 15100 + 2400  # the last request's prompt
        assert finish["compactions"] == [
            {"trigger": "auto", "pre_tokens": 90000, "post_tokens": 2000, "duration_ms": 900},
        ]

    def test_the_outcome_is_the_last_result_once_background_teammates_end(self, agent_db: Any) -> None:
        # A Worker of a profile with agent teams, recorded with Claude Code 2.1.285 (DeepSeek, trimmed): its turn
        # ends with WAITING; once stdin is closed the teammate's message and its end each bring a turn and a result
        # of their own, and only then does Claude exit.
        fixture = Path(__file__).parent / "fixtures" / "claude_stream" / "agent_team_worker.jsonl"
        results = [e for e in map(json.loads, fixture.read_text().splitlines()) if e["type"] == "result"]
        assert [r["result"] for r in results][0] == "WAITING" and len(results) == 3

        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    "sys.stdin.readline()\n"
                    f"lines = open({str(fixture)!r}).read().splitlines()\n"
                    "first = next(i for i, line in enumerate(lines) if json.loads(line)['type'] == 'result')\n"
                    "print('\\n'.join(lines[:first + 1]), flush=True)\n"
                    "sys.stdin.read()\n"
                    "print('\\n'.join(lines[first + 1:]), flush=True)\n"
                ))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                envelope = _work_item(Path(tmp))
                envelope.payload["parent_task_id"] = "task-1"
                return await pool._execute(envelope)

        outcome = asyncio.run(_run())
        assert outcome["status"] == "completed" and "KUMQUAT-7" in outcome["summary"]
        finish = agent_db.finish_record.await_args.kwargs
        assert finish["output_text"] == results[-1]["result"]
        assert finish["cost_usd"] == results[-1]["total_cost_usd"]  # the lead's requests and the teammate's
        finished = [call.kwargs["summary"] for call in agent_db.insert_activity.await_args_list
                    if call.kwargs["summary"].startswith("Worker finished")]
        assert finished == [f"Worker finished (cost: ${results[-1]['total_cost_usd']:.4f})"]

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
    def test_report_is_checked_against_the_project(self, agent_db: Any, create: bool, expected: str) -> None:
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
        finish = agent_db.finish_record.await_args.kwargs  # the run's record agrees
        assert finish["status"] == expected
        if not create:
            assert "out.txt" in finish["error"]

    def test_run_over_the_time_limit_is_killed(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        # Claude keeps writing thinking deltas: output does not extend the limit.
        _worker_row(monkeypatch, {"timeout_seconds": 1, "max_turns": 0})

        async def _run() -> tuple[list[asyncio.subprocess.Process], MessageBus]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), (
                    _WARM_UP_EXIT
                    + "for _ in range(25):\n"
                    f"    print(json.dumps({_THINKING_DELTA!r}), flush=True)\n"
                    "    time.sleep(0.2)\n"
                    + _SUCCESS
                ))
                started = _start_warm(monkeypatch, claude)
                bus = MessageBus(runtime_root=Path(tmp))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                pool._bus = bus
                await pool._run_worker(_work_item(Path(tmp)))
                return started, bus

        (process,), bus = asyncio.run(_run())
        assert process.returncode == -signal.SIGKILL  # killed and reaped
        # The limit is the step's failure reason the Planner reads.
        reply = bus.get_queue("planner-t1").get_nowait()
        assert reply.payload["status"] == "failed"
        assert reply.payload["error"] == "Claude ran longer than the 1 s limit (timeout_seconds)"
        agent_db.touch_record.assert_awaited()  # output arrived and fed the heartbeat
        agent_db.append_events.assert_not_awaited()  # partial deltas are not stored
        finish = agent_db.finish_record.await_args.kwargs
        assert finish["status"] == "failed"
        assert finish["error"] == "Claude ran longer than the 1 s limit (timeout_seconds)"

    def test_zero_timeout_has_no_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        get_config = _worker_row(monkeypatch, {"timeout_seconds": 0, "max_turns": 0})

        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), "time.sleep(0.1)\n" + _SUCCESS)
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
        turns = argv[argv.index("--max-turns"):][:2] if "--max-turns" in argv else []
        assert turns == expected

    @pytest.mark.parametrize(("body", "status", "error"), [
        ("open('out.txt', 'w').write('x')\n"
         "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
         "'result': json.dumps({'status': 'completed', 'changed_files': {'out.txt': 'added'}})}))\n"
         "sys.exit(1)", "completed", None),
        ("sys.exit(1)", "failed", "exited with code 1"),  # no result event, then a non-zero exit
    ], ids=["result-decides", "no-result"])
    def test_successful_result_survives_a_nonzero_exit(self, body: str, status: str, error: str | None) -> None:
        """Claude can exit non-zero after a successful result (e.g. an MCP/hook
        teardown error); the judged result decides, not the exit code."""

        async def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as tmp:
                claude = _fake_claude(Path(tmp), body)
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                return await pool._execute(_work_item(Path(tmp)))

        result = asyncio.run(_run())
        assert result["status"] == status
        if error:
            assert error in result["error"]

    @pytest.mark.parametrize(("row", "failure", "limits"), [
        (None, None, (86400, 0)),  # no row: 86400 s, no turn limit
        (None, RuntimeError("database pool not initialised"), (86400, 0)),
        ({"system_prompt": "   \n", "timeout_seconds": 5, "max_turns": 3}, None, (5, 3)),  # a blank role prompt
    ])
    def test_unreadable_config_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch, row: dict[str, Any] | None, failure: Exception | None,
        limits: tuple[int, int],
    ) -> None:
        from looper.agents.tiers.worker import _load_settings

        _worker_row(monkeypatch, row, error=failure)
        settings = asyncio.run(_load_settings())
        assert (settings.timeout_seconds, settings.max_turns) == limits
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
        deps = [{"step": "core", "summary": "added the core module", "changed_files": ["core.py"]}]
        result, seen = _run_step(feedback=["import calc from the package", "add sub()"], attempt=3, dependencies=deps)

        assert result["status"] == "completed"
        prompt = _system_prompt(seen)
        _in_order(prompt, f"{DEFAULT_HARNESS_PROMPT}\n\n{ROLE}\n\n", "## Project: calc\nUse uv.\nPython 3.12.\n\n",
                  "## Task\n", REQUIREMENT, "calc-module", "add calc.py", "calc.add(1, 2) == 3", WORK_CONTRACT)
        assert prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\n{ROLE}\n\n") and prompt.endswith(WORK_CONTRACT)
        agent_db.get_task_project.assert_awaited_once_with("task-1")
        # The step is the system prompt's task section; stdin only starts the work.
        for part in ("add calc.py", "calc.add(1, 2) == 3", REQUIREMENT, '"deleted_files"', "End your final message"):
            assert part not in seen["stdin"]
        # A retried step's feedback is part of its task; the dependencies' reports are the user turn.
        task = _task_section(prompt, WORK_CONTRACT)
        assert "attempt 3" in task and "- import calc from the package\n- add sub()" in task
        assert "import calc from the package" not in seen["stdin"]
        assert "added the core module" in seen["stdin"] and "core.py" in seen["stdin"]
        assert "added the core module" not in prompt
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
        assert layers["harness"] == {"source": "builtin", "sha256": _sha(DEFAULT_HARNESS_PROMPT)}
        assert layers["role"] == {"config_id": "worker", "sha256": _sha(ROLE), "recommended": False}
        assert layers["project"] == {"id": "proj-1", "background_sha256": _sha("Use uv.\nPython 3.12."),
                                     "updated_at": "2026-10-13T12:00:00+00:00", "knowledge_sha256": None,
                                     "knowledge_documents": 0}
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

    @pytest.mark.parametrize(("verify", "config_id", "default", "other"), [
        (False, "worker", DEFAULT_SYSTEM_PROMPT, REVIEWER_SYSTEM_PROMPT),
        (True, "reviewer", REVIEWER_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT),
    ], ids=["worker", "reviewer"])
    def test_a_reviewer_without_a_config_row_gets_the_reviewer_role_prompt(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, verify: bool, config_id: str, default: str, other: str,
    ) -> None:
        get_config = _worker_row(monkeypatch, None)
        _, seen = _run_step(verify=verify)

        get_config.assert_awaited_with(config_id)  # each reads its own row
        prompt = _system_prompt(seen)
        assert prompt.startswith(f"{DEFAULT_HARNESS_PROMPT}\n\n{default.strip()}\n\n## Project")
        assert prompt.endswith(REVIEW_CONTRACT if verify else WORK_CONTRACT)
        assert other.strip() not in prompt  # not the other one's role
        role = agent_db.start_record.await_args.kwargs["metadata"]["prompt_layers"]["role"]
        assert role == {"config_id": config_id, "sha256": _sha(default.strip()), "recommended": True}

    def test_an_unedited_row_is_the_recommended_role_prompt(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
    ) -> None:
        _worker_row(monkeypatch, {"system_prompt": ROLE, "default_prompt": ROLE + "\n", "timeout_seconds": 0,
                                  "max_turns": 0})
        _run_step()
        assert agent_db.start_record.await_args.kwargs["metadata"]["prompt_layers"]["role"]["recommended"] is True


def _project_row(project_id: str = "proj-1") -> dict[str, Any]:
    return {"id": project_id, "name": project_id, "background": ""}


class TestWorkerHarnessAndLaunch:
    """Each Worker and Reviewer run reads its agent settings in its task's
    project as it starts (``agent_settings.load_agent_settings``)."""

    @pytest.fixture
    def rows(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
        """The ``worker`` and ``reviewer`` config rows, by agent id."""
        from looper.db import role_configs

        rows = {"worker": {"system_prompt": "WORKER ROLE", "timeout_seconds": 0, "max_turns": 0},
                "reviewer": {"system_prompt": "REVIEWER ROLE", "timeout_seconds": 0, "max_turns": 0}}

        async def _config(agent_id: str) -> dict[str, Any] | None:
            return dict(rows[agent_id]) if agent_id in rows else None

        monkeypatch.setattr(role_configs, "get_config", _config)
        return rows

    @staticmethod
    def _settings(agent_db: Any, **stored: Any) -> None:
        """The system layer, with these fields set."""
        agent_db.list_layers.return_value = [{"scope": "system", **stored}]

    def test_the_projects_worker_harness_reaches_its_worker_and_reviewer_only(
        self, agent_db: Any, rows: dict[str, Any],
    ) -> None:
        agent_db.list_layers.return_value = [
            {"scope": "system", "harness_prompt": "DEFAULT HARNESS"},
            {"scope": "role_project", "role": "worker", "project_id": "proj-a", "harness_prompt": "A WORKERS"},
            {"scope": "role_project", "role": "planner", "project_id": "proj-a", "harness_prompt": "A PLANNER"},
        ]
        agent_db.get_task_project.return_value = _project_row("proj-a")
        prompts = [_system_prompt(_run_step(verify=verify)[1]) for verify in (False, True)]
        layers = [call.kwargs["metadata"]["prompt_layers"]["harness"]
                  for call in agent_db.start_record.await_args_list]
        assert [p.split("\n\n")[0] for p in prompts] == ["A WORKERS", "A WORKERS"]
        assert all("DEFAULT HARNESS" not in p and "A PLANNER" not in p for p in prompts)
        worker = {"source": "role_project", "sha256": _sha("A WORKERS"), "project_id": "proj-a"}
        assert layers == [worker, {**worker, "from_role": "worker"}]
        assert [call.args for call in agent_db.list_layers.await_args_list] == [("proj-a",)] * 2
        # A task in another project gets the default.
        agent_db.get_task_project.return_value = _project_row("proj-b")
        for verify in (False, True):
            assert _system_prompt(_run_step(verify=verify)[1]).startswith("DEFAULT HARNESS\n\n")

    def test_a_changed_default_applies_to_the_next_run(self, agent_db: Any, rows: dict[str, Any]) -> None:
        self._settings(agent_db, harness_prompt="V1")
        agent_db.get_task_project.return_value = {**_project_row(), "background": "Use uv."}
        _, first = _run_step()
        self._settings(agent_db, harness_prompt="V2")
        rows["worker"]["system_prompt"] = "WORKER ROLE v2"
        agent_db.get_task_project.return_value = {**_project_row(), "background": "Now use pnpm."}
        _, second = _run_step()
        assert _system_prompt(first).startswith("V1\n\nWORKER ROLE\n\n## Project: proj-1\nUse uv.\n\n")
        assert _system_prompt(second).startswith("V2\n\nWORKER ROLE v2\n\n## Project: proj-1\nNow use pnpm.\n\n")

    def test_a_role_model_beats_the_system_default(self, agent_db: Any, rows: dict[str, Any]) -> None:
        agent_db.list_layers.return_value = [
            {"scope": "system", "profile": profile_json("sonnet"), "effort": "high"},
            {"scope": "role", "role": "reviewer", "profile": profile_json("claude-haiku-4-5")},
        ]
        _, work = _run_step()
        work_metadata = agent_db.start_record.await_args.kwargs["metadata"]
        _, review = _run_step(verify=True)
        review_metadata = agent_db.start_record.await_args.kwargs["metadata"]
        assert (_flag(work["argv"], "--model"), _flag(work["argv"], "--effort")) == ("sonnet", "high")
        assert (_flag(review["argv"], "--model"), _flag(review["argv"], "--effort")) == ("claude-haiku-4-5", "high")
        assert work_metadata["model_choice"] == {"model": "sonnet", "source": "system", "effort": "high"}
        assert review_metadata["model_choice"] == {"model": "claude-haiku-4-5", "source": "role", "effort": "high"}
        assert review_metadata["settings_sources"]["profile"] == "role"

    def test_the_settings_environment_reaches_the_subprocess_and_its_names_are_recorded(
        self, monkeypatch: pytest.MonkeyPatch, agent_db: Any, rows: dict[str, Any],
    ) -> None:
        self._settings(agent_db, env=[{"key": "LOOPER_TEST_FLAVOUR", "value": "mint"}])
        monkeypatch.setenv("LOOPER_SERVER_SIDE", "kept")
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
            assert kwargs["env"]["LOOPER_TEST_FLAVOUR"] == "mint"
            assert kwargs["env"]["LOOPER_SERVER_SIDE"] == "kept"  # on top of the server's environment
        recorded = [call.kwargs["metadata"]["env"] for call in agent_db.start_record.await_args_list]
        assert [r["keys"] for r in recorded] == [{"LOOPER_TEST_FLAVOUR": _sha("mint")}] * 2
        assert "mint" not in json.dumps(recorded)


class TestWorkerLibrary:
    """Each Worker and Reviewer run loads the items installed for its role as it starts."""

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
            assert library["role"] == ("reviewer" if verify else "worker")


# What the fake Claude reads like an agent would: every file the index names, joined to the directory above it.
_READ_KNOWLEDGE = (
    "import os, re\n"
    "reads, root = {}, ''\n"
    "for line in prompt.splitlines():\n"
    "    if line.startswith('In ') and line.endswith(':'):\n"
    "        root = line[3:-1]\n"
    "    elif match := re.fullmatch(r'- .*\\(([^)]+)\\)', line):\n"
    "        path = os.path.join(root, match.group(1))\n"
    "        reads[path] = open(path, encoding='utf-8').read()\n"
    "json.dump({'argv': sys.argv[1:], 'prompt': prompt, 'reads': reads}, open('knowledge.json', 'w'))\n"
)


def _add_dirs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"]


class TestWorkerKnowledge:
    """Each Worker and Reviewer run reads its project's knowledge as it starts: an index in the project
    layer of its system prompt, the stored documents materialized and mounted with --add-dir, and the
    project's directory mounted too when a path document is there and the step runs elsewhere."""

    @pytest.fixture(autouse=True)
    def _role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _worker_row(monkeypatch, {"system_prompt": ROLE, "timeout_seconds": 0, "max_turns": 0})

    @pytest.fixture
    def project_dir(self, tmp_path: Path, agent_db: Any) -> Path:
        directory = tmp_path / "calc"
        (directory / "docs").mkdir(parents=True)
        (directory / "docs" / "runbook.md").write_text("Deploy with MARKER-BETA.\n", encoding="utf-8")
        agent_db.get_task_project.return_value = {"id": "proj-1", "name": "calc", "background": "Use uv.",
                                                  "work_dir": str(directory)}
        return directory

    @staticmethod
    def _run(*, verify: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run one step in a new directory; return its outcome and what its Claude read."""

        async def _go() -> tuple[dict[str, Any], dict[str, Any]]:
            with tempfile.TemporaryDirectory() as tmp:
                work_dir = Path(tmp)
                claude = _fake_claude(Path(tmp), _RECORD_RUN + _READ_KNOWLEDGE + (_VERDICT if verify else _SUCCESS))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=Path(tmp))
                result = await pool._execute(_step_envelope(work_dir, verify=verify), is_verify=verify)
                seen = json.loads((work_dir / "knowledge.json").read_text())
                return result, {**seen, "root": tmp}

        return asyncio.run(_go())

    def test_both_read_the_documents_the_index_names_from_the_mounted_directories(
        self, agent_db: Any, project_documents: Any, project_dir: Path,
    ) -> None:
        project_documents(("text", "Glossary", "Terms the code uses", "MARKER-ALPHA"),
                          ("path", "Runbook", "How to deploy", "docs/runbook.md"))
        for verify, contract in ((False, WORK_CONTRACT), (True, REVIEW_CONTRACT)):
            result, seen = self._run(verify=verify)
            assert result.get("status") == "completed" or result.get("passed") is True
            set_dir, mounted_project = _add_dirs(seen["argv"])
            glossary = f"{set_dir}/Glossary.md"
            runbook = str(project_dir / "docs" / "runbook.md")
            # The stored document's set directory, and the project's since the step runs elsewhere.
            assert Path(set_dir).parent == Path(seen["root"]) / ".local" / "projects" / "proj-1" / "knowledge"
            assert mounted_project == str(project_dir)
            prompt = seen["prompt"]
            _in_order(prompt, "## Project: calc\nUse uv.\n\n", "## Project knowledge\n",
                      f"In {set_dir}:\n- Glossary: Terms the code uses (Glossary.md)\n",
                      f"In {project_dir}:\n- Runbook: How to deploy (docs/runbook.md)\n\n", "## Task\n", contract)
            assert "MARKER-ALPHA" not in prompt and "MARKER-BETA" not in prompt  # read on demand, not in the prompt
            assert seen["reads"] == {glossary: "MARKER-ALPHA", runbook: "Deploy with MARKER-BETA.\n"}
            metadata = agent_db.start_record.await_args.kwargs["metadata"]
            index = prompt[prompt.index("## Project knowledge"):prompt.index("\n\n## Task")]
            assert metadata["prompt_layers"]["project"]["knowledge_sha256"] == _sha(index)
            assert metadata["prompt_layers"]["project"]["knowledge_documents"] == 2
            assert metadata["command"][1:] == seen["argv"]
        assert activity(agent_db).count("Project knowledge applied (2 documents)") == 2

    def test_unreadable_knowledge_runs_the_step_without_it_and_warns(self, agent_db: Any, project_dir: Path) -> None:
        agent_db.list_documents.side_effect = ConnectionError("database is down")
        result, seen = self._run()
        assert result["status"] == "completed"
        assert "--add-dir" not in seen["argv"] and "## Project knowledge" not in seen["prompt"]
        project = agent_db.start_record.await_args.kwargs["metadata"]["prompt_layers"]["project"]
        assert project["knowledge_error"] == "the project's knowledge could not be read"
        assert "Warning: the project's knowledge could not be read; this run goes without it" in activity(agent_db)


# What the fake Claude does with its plugins, like Claude Code: reads each one's skills. It keeps what it
# saw beside its own script, so the step's directory holds only what the mechanism itself wrote there.
_READ_PLUGINS = (
    "first = sys.stdin.readline()\n"
    "state = os.path.dirname(os.path.abspath(__file__))\n"
    "dirs = [sys.argv[i + 1] for i, arg in enumerate(sys.argv) if arg == '--plugin-dir']\n"
    "skills = {d: {name: open(os.path.join(d, 'skills', name, 'SKILL.md'), encoding='utf-8').read()\n"
    "              for name in sorted(os.listdir(os.path.join(d, 'skills')))}\n"
    "          for d in dirs if os.path.isdir(os.path.join(d, 'skills'))}\n"
    "json.dump({'argv': sys.argv[1:], 'skills': skills}, open(os.path.join(state, 'seen.json'), 'w'))\n"
)


def _project_plugin_dirs(argv: list[str], root: str) -> list[str]:
    """The ``--plugin-dir`` values that are the generated plugin of a project's own skills."""
    return [d for d in plugin_dirs(argv) if d.startswith(f"{root}/.local/library/") and d.endswith("/project")]


class TestWorkerProjectSkills:
    """Each Worker and Reviewer run mounts the skills installed in its own project as one plugin under the
    runtime root: one --plugin-dir after the global ones, recorded with the run, and nothing in the step's
    directory."""

    @pytest.fixture(autouse=True)
    def _role(self, monkeypatch: pytest.MonkeyPatch, agent_db: Any) -> None:
        _worker_row(monkeypatch, {"system_prompt": ROLE, "timeout_seconds": 0, "max_turns": 0})
        agent_db.get_task_project.return_value = {"id": "proj-a", "name": "calc", "work_dir": ""}

    @staticmethod
    def _run(*, verify: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run one step in a fresh directory, apart from the runtime root; return its outcome and what its
        Claude saw, with the files the step's directory holds afterwards."""

        async def _go() -> tuple[dict[str, Any], dict[str, Any]]:
            with tempfile.TemporaryDirectory() as tmp:
                root, work, state = Path(tmp) / "runtime", Path(tmp) / "work", Path(tmp) / "state"
                work.mkdir()
                state.mkdir()
                claude = _fake_claude(state, _READ_PLUGINS + (_VERDICT if verify else _SUCCESS))
                pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=root)
                result = await pool._execute(_step_envelope(work, verify=verify), is_verify=verify)
                seen = json.loads((state / "seen.json").read_text())
                return result, {**seen, "root": str(root),
                                "work_files": sorted(p.relative_to(work).as_posix() for p in work.rglob("*"))}

        return asyncio.run(_go())

    def test_both_mount_their_projects_enabled_skills_as_one_plugin_and_record_them(
        self, agent_db: Any, project_skills: Any,
    ) -> None:
        project_skills(("proj-a", "release-notes", "Drafts release notes.", "Reply NOTES-OK."),
                       ("proj-a", "deploy-check", "Use before a deploy.", "Reply DEPLOY-OK."),
                       ("proj-b", "elsewhere", "Another project's.", "Reply ELSEWHERE."))
        for verify in (False, True):
            result, seen = self._run(verify=verify)
            assert result.get("status") == "completed" or result.get("passed") is True
            (plugin,) = _project_plugin_dirs(seen["argv"], seen["root"])
            files = seen["skills"][plugin]
            assert sorted(files) == ["deploy-check", "release-notes"]
            assert files["deploy-check"] == (
                "---\nname: deploy-check\ndescription: \"Use before a deploy.\"\n---\n\nReply DEPLOY-OK.\n")
            metadata = agent_db.start_record.await_args.kwargs["metadata"]
            library = metadata["library"]
            assert (library["role"], library["project_id"]) == ("reviewer" if verify else "worker", "proj-a")
            assert [(i["name"], i["plugin"], i["scope"]) for i in library["items"]] == [
                ("deploy-check", "project", "project"), ("release-notes", "project", "project")]
            assert metadata["command"][1:] == seen["argv"]
            assert seen["work_files"] == []  # nothing in the step's directory
        assert activity(agent_db).count("Project installs applied (2 skills)") == 2

    def test_unreadable_installs_run_the_step_without_them_and_warn(self, agent_db: Any) -> None:
        agent_db.list_installed.side_effect = ConnectionError("database is down")
        result, seen = self._run()
        assert result["status"] == "completed" and "--plugin-dir" not in seen["argv"]
        assert agent_db.start_record.await_args.kwargs["metadata"]["library"] == {
            "sha256": None, "role": "worker", "project_id": "proj-a", "items": [],
            "error": "the installed items could not be read"}
        assert "Warning: the installed items could not be read; this run goes without them" in activity(agent_db)


class TestWorkerPermissions:
    """A Worker and a Reviewer run like every agent: the shared permission flags and every tool."""

    @pytest.mark.parametrize("verify", [False, True], ids=["worker", "reviewer"])
    def test_the_command_carries_the_shared_permission_flags(self, agent_db: Any, verify: bool) -> None:
        _, seen = _run_step(verify=verify)
        _assert_unrestricted(seen["argv"])


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
            (_result("x", is_error=True), "did not succeed"),
            (_result("I made the changes successfully."), "JSON status report"),
            (_result('{"status": "partial"}'), "unknown status"),
            (_result('{"status": "completed", "changed_files": ["a.py"]}'), "malformed"),
            (_result('{"status": "completed", "changed_files": {"missing.py": "added"}}'), "missing.py"),
            (_result('{"status": "completed", "deleted_files": ["a.py"]}'), "a.py"),
        ],
    )
    def test_work_fails_closed(self, tmp_path: Path, event: dict[str, Any] | None, error: str) -> None:
        from looper.agents.tiers.worker import judge_result

        (tmp_path / "a.py").write_text("x")
        result = judge_result(event, work_dir=tmp_path, is_verify=False)
        assert result["status"] == "failed"
        assert error in result["error"]

    def test_missing_report_keeps_the_final_message(self, tmp_path: Path) -> None:
        from looper.agents.tiers.worker import judge_result

        result = judge_result(_result("The sleep timed out, so I stopped."), work_dir=tmp_path, is_verify=False)
        assert result["status"] == "failed"
        assert result["final_message"] == "The sleep timed out, so I stopped."

    def test_reported_failure_with_nested_files_stays_failed(self, tmp_path: Path) -> None:
        from looper.agents.tiers.worker import judge_result

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
        from looper.agents.tiers.worker import judge_result

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
        ("event", "passed", "finding"),
        [
            (_result('{"passed": true, "findings": [], "summary": "ok"}'), True, None),
            (_result('```json\n{"passed": false, "findings": ["no test"], "summary": "x"}\n```'), False, None),
            (_result("Looks good to me."), False, None),
            (_result('{"passed": "yes"}'), False, None),
            (_result(subtype="error_max_turns", is_error=True), False, "error_max_turns"),  # a failed review run
        ],
    )
    def test_verdict(self, tmp_path: Path, event: dict[str, Any], passed: bool, finding: str | None) -> None:
        from looper.agents.tiers.worker import judge_result

        result = judge_result(event, work_dir=tmp_path, is_verify=True)
        assert result["passed"] is passed
        if finding:
            assert finding in result["findings"][0]


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

    def test_worker_input_over_the_bus(self, agent_db: Any) -> None:
        async def _run() -> tuple[bool, str, bool, bool]:
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
                # The Worker answers with what it read; once its run ends, nothing reads its stdin.
                queue = bus.get_queue("planner-t1")
                result = await asyncio.wait_for(queue.get(), 20)
                while result.message_type != MessageType.WORK_RESULT:
                    result = await asyncio.wait_for(queue.get(), 20)
                late = await bus.request(ask("too late"), timeout=5)
                unknown = await bus.request(MessageEnvelope.create(
                    source=planner_address("t1"), target=worker_address("nobody"),
                    message_type=MessageType.WORKER_INPUT, payload={"text": "x"},
                ), timeout=5)
                await pool.stop()
                await bus.shutdown()
                return (first.payload["delivered"], result.payload["summary"], late.payload["delivered"],
                        unknown.payload["delivered"])

        assert asyncio.run(_run()) == (True, "got: hello", False, False)


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


def _run_item(worker_id: str, task_id: str, work_dir: Path, *, review: bool = False) -> MessageEnvelope:
    """A WORK_ITEM (or, with ``review``, a VERIFY_REQUEST) of task ``task_id``'s Planner."""
    return MessageEnvelope.create(
        source=planner_address(task_id), target=worker_address(worker_id),
        message_type=MessageType.VERIFY_REQUEST if review else MessageType.WORK_ITEM,
        payload={"task_id": f"step-{worker_id}", "parent_task_id": task_id, "instruction": "add the parser",
                 "context": {"work_dir": str(work_dir)}},
    )


class TestWorkerConcurrency:
    """A Worker or Reviewer run starts once fewer of its role run in its task's project than that role's limit
    there, read each time it checks."""

    @staticmethod
    def _projects(agent_db: Any, projects: dict[str, str]) -> None:
        """Put each task (task id -> project id) in its project."""
        agent_db.get_task_project.side_effect = lambda task_id, **_: {"id": projects[task_id]}

    @staticmethod
    def _limits(agent_db: Any, **roles: int) -> None:
        """Store each role's limit on its role layer."""
        agent_db.list_layers.return_value = [
            {"scope": "role", "role": role, "project_id": None, "max_concurrency": limit} for role, limit in roles.items()]

    @staticmethod
    async def _until_running(pool: WorkerPool, *worker_ids: str) -> None:
        for _ in range(500):
            if all(pool.accepts_input(w) for w in worker_ids):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"not all of {worker_ids} started: {pool.status()}")

    def test_runs_wait_for_room_in_their_project_and_role(self, agent_db: Any) -> None:
        self._projects(agent_db, {"t-a": "proj-a", "t-a2": "proj-a", "t-b": "proj-b"})
        self._limits(agent_db, worker=2, reviewer=1)

        async def _run() -> tuple[dict[str, str], list[str], dict[str, str], dict[str, tuple[Any, ...]]]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                pool = WorkerPool(config=WorkerConfig(claude_bin=_fake_claude(Path(tmp), _LISTENING_CLAUDE)),
                                  runtime_root=Path(tmp))
                await pool.start(bus)
                await asyncio.sleep(0)
                # Three Workers in proj-a (two tasks), one in proj-b, and a Reviewer in proj-a.
                for worker_id, task_id in (("w1", "t-a"), ("w2", "t-a2"), ("w3", "t-a"), ("w4", "t-b")):
                    await bus.publish(_run_item(worker_id, task_id, Path(tmp)))
                await bus.publish(_run_item("r1", "t-a", Path(tmp), review=True))
                await self._until_running(pool, "w1", "w2", "w4", "r1")
                before = {w: wp.status for w, wp in pool._active.items()}
                # What the dashboard shows of them.
                runs = {r["agent_id"]: (r["role"], r["state"], r["task_id"], r["project_id"], r["instruction"])
                        for r in pool.runs()}
                waits = [c.kwargs["summary"] for c in agent_db.insert_activity.await_args_list
                         if "waits for room" in c.kwargs["summary"]]

                assert await pool.send_input("w1", "finish")  # w1 ends, which leaves room for w3
                await self._until_running(pool, "w3")
                after = {w: wp.status for w, wp in pool._active.items()}
                await pool.stop()
                await bus.shutdown()
                return before, waits, after, runs

        before, waits, after, runs = asyncio.run(_run())
        assert before == {"w1": "running", "w2": "running", "w3": "pending", "w4": "running", "r1": "running"}
        parser = "add the parser"
        assert runs == {"w1": ("worker", "running", "t-a", "proj-a", parser),
                        "w2": ("worker", "running", "t-a2", "proj-a", parser),
                        "w3": ("worker", "waiting", "t-a", "proj-a", parser),
                        "w4": ("worker", "running", "t-b", "proj-b", parser),
                        "r1": ("reviewer", "running", "t-a", "proj-a", parser)}
        # The task's activity says why the step has not started, once.
        assert len(waits) == 1 and "2 running, limit 2" in waits[0]
        assert after == {"w2": "running", "w3": "running", "w4": "running", "r1": "running"}

    def test_a_raised_limit_starts_a_waiting_run_and_a_lowered_one_stops_none(self, agent_db: Any) -> None:
        self._projects(agent_db, {"t-a": "proj-a"})
        self._limits(agent_db, worker=1)

        async def _run() -> list[dict[str, str]]:
            with tempfile.TemporaryDirectory() as tmp:
                bus = MessageBus(runtime_root=Path(tmp))
                pool = WorkerPool(config=WorkerConfig(claude_bin=_fake_claude(Path(tmp), _LISTENING_CLAUDE)),
                                  runtime_root=Path(tmp))
                await pool.start(bus)
                await asyncio.sleep(0)
                for worker_id in ("w1", "w2"):
                    await bus.publish(_run_item(worker_id, "t-a", Path(tmp)))
                await self._until_running(pool, "w1")
                seen = [{w: wp.status for w, wp in pool._active.items()}]

                self._limits(agent_db, worker=2)
                await pool.recheck()
                await self._until_running(pool, "w2")

                self._limits(agent_db, worker=1)
                await pool.recheck()
                await bus.publish(_run_item("w3", "t-a", Path(tmp)))
                for _ in range(50):  # w3 has checked its limit and waits
                    if pool._slot_waiters:
                        break
                    await asyncio.sleep(0.02)
                seen.append({w: wp.status for w, wp in pool._active.items()})
                # A waiting run can be stopped like a running one, and publishes no result.
                seen.append({"killed": str(await pool.kill("w3")), "published": str(
                    not bus.get_queue("planner-t-a").empty())})
                await pool.stop()
                await bus.shutdown()
                return seen

        first, lowered, killed = asyncio.run(_run())
        assert first == {"w1": "running", "w2": "pending"}
        assert lowered == {"w1": "running", "w2": "running", "w3": "pending"}
        assert killed == {"killed": "True", "published": "False"}
