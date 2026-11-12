"""Retry Cursor's exhausted reconnects through real local stand-in processes."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents import proc_tree
from looper.agents.runtimes import Launcher, cursor_login, ttadk
from looper.agents.tiers import worker
from test_agent_planner_worker import _step_envelope, _worker_row
from test_cursor_agent import CURSOR, _launch, _prompt, cursor_tmpdirs  # noqa: F401
from test_ttadk_cursor_agent import _cursor_script

RETRY_LINE = "Cursor's CLI gave up after reconnecting; retried once"
SIGNATURE = "RetriableError: WritableIterable is closed\nError: command failed unexpectedly."
CONTINUATION_PROMPT = (
    "[Looper] Your previous reply was cut off by a connection error before it reached Looper. "
    "Continue from where you stopped. If your step's work is already done, send your final JSON report now; "
    "do not redo work that is already in the files or commits."
)


def _stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, *, wrapped: bool = False,
              verify: bool = False, progress: tuple[str, ...] = ("assistant",)):
    calls = tmp_path / "calls.jsonl"
    report = {"passed": True, "findings": [], "summary": "recovered"} if verify else {
        "status": "completed", "changed_files": {}, "summary": "recovered"}
    body = f'''
import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ['status', '--format', 'json']:
    print('{{"status":"authenticated","isAuthenticated":true}}'); sys.exit(0)
log = Path({str(calls)!r})
text = sys.stdin.read()
assert Path(os.environ['TMPDIR']).is_dir()
with log.open('a') as out:
    out.write(json.dumps({{'pid': os.getpid(), 'argv': sys.argv[1:], 'stdin': text,
                          'config': os.environ['CURSOR_CONFIG_DIR'], 'tmpdir': os.environ['TMPDIR']}}) + '\\n')
attempt = len(log.read_text().splitlines())
mode = {mode!r}
if mode == 'retry-lost-session' and attempt == 3:
    print('session not found; attempt-3', file=sys.stderr, flush=True)
    sys.exit(1)
if mode == 'retry-lost-session' and attempt == 2: mode = 'twice'
if mode == 'lost-init' and attempt <= 2: mode = 'once'
if mode not in ('no-chat', 'no-chat-fails', 'lost-init') and not (mode == 'resume-no-init' and attempt > 1):
    print(json.dumps({{'type': 'system', 'subtype': 'init', 'session_id': 'chat-retry'}}), flush=True)
if mode == 'result':
    print(json.dumps({{'type': 'result', 'subtype': 'error', 'is_error': True}}))
if mode == 'held-stderr' and attempt == 1:
    import subprocess
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
if (attempt == 1 and mode != 'retry-lost-session') or (mode == 'three' and attempt <= 3) or mode in ('twice', 'no-chat-fails', 'resume-no-init', 'long-failure', 'resume-other', 'other', 'other-retriable', 'result', 'timeout', 'signal', 'lost-init', 'exit2'):
    progress = {progress!r}
    event = progress[min(attempt - 1, len(progress) - 1)]
    if event == 'assistant':
        print(json.dumps({{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': 'abandoned draft'}}]}}}}), flush=True)
    elif event == 'tool':
        print(json.dumps({{'type': 'tool_call', 'subtype': 'started', 'call_id': 'read-1',
                          'tool_call': {{'readToolCall': {{'args': {{'path': 'task.txt'}}}}}}}}), flush=True)
    print(json.dumps({{'type': 'connection', 'subtype': 'reconnecting'}}), flush=True)
    error = {{'other': 'unrelated failure', 'other-retriable': 'RetriableError: authentication failed'}}.get(mode, {SIGNATURE!r})
    if mode == 'resume-other' and attempt > 1: error = 'unrelated failure'
    print(error, file=sys.stderr, flush=True)
    if mode in ('lost-init', 'long-failure'): print('x' * 1200, file=sys.stderr, flush=True)
    print('attempt-' + str(attempt), file=sys.stderr, flush=True)
    if mode == 'timeout': time.sleep(10)
    if mode == 'signal': os.kill(os.getpid(), 15)
    sys.exit(2 if mode == 'exit2' else 1)
print(json.dumps({{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': {json.dumps(report)!r}}}]}}}}))
print(json.dumps({{'type': 'result', 'subtype': 'success'}}))
'''
    executable = _cursor_script(tmp_path / "cursor-agent", body) if wrapped else fake_claude(tmp_path / "cursor-agent", body)
    launch = _launch(executable=executable)
    if wrapped:
        wrapper = fake_claude(tmp_path / "ttadk", '''
import subprocess, sys
assert sys.argv[1:6] == ['code', '-t', 'cursor-cli', '--skip-check', '--']
subprocess.run(['agent', *sys.argv[6:]], check=False)
sys.exit(0)
''')
        launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile,
            runtime=replace(launch.profile.runtime, executable=wrapper, launcher=Launcher.TTADK))))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "operator"))
    return launch, calls


@pytest.fixture
def no_tree_end(monkeypatch):
    end = AsyncMock(wraps=proc_tree.end)
    tree_end = AsyncMock()
    monkeypatch.setattr(proc_tree, "end", end)
    monkeypatch.setattr(proc_tree.TurnTree, "end", tree_end)
    yield
    end.assert_not_awaited()
    tree_end.assert_not_awaited()


@pytest.mark.parametrize(("mode", "wrapped", "verify", "attempts"), [
    ("once", False, False, 2), ("once", False, True, 2),
    ("twice", False, False, 4), ("other", False, False, 1),
    ("three", False, False, 4), ("resume-no-init", False, False, 4),
    ("long-failure", False, False, 4), ("resume-other", False, False, 2),
    ("no-chat", False, False, 2), ("no-chat-fails", False, False, 2),
    ("other-retriable", False, False, 1),
    ("result", False, False, 1), ("signal", False, False, 1), ("exit2", False, False, 1),
    ("once", True, False, 2), ("twice", True, False, 4), ("other", True, False, 1),
    ("long-failure", True, False, 4), ("no-chat-fails", True, False, 2),
])
def test_worker_cursor_retry(mode, wrapped, verify, attempts, tmp_path, monkeypatch, agent_db: Any, no_tree_end):
    launch, calls = _stand_in(tmp_path, monkeypatch, mode, wrapped=wrapped, verify=verify)
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 15, "max_turns": 0})

    async def settings(*args, **kwargs):
        return launch.settings

    monkeypatch.setattr(worker, "load_agent_settings", settings)
    work = tmp_path / "work"
    work.mkdir()

    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        return await pool._execute(_step_envelope(work, verify=verify), is_verify=verify)

    result = asyncio.run(asyncio.wait_for(run(), 30))
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == attempts
    assert all(not Path(call["tmpdir"]).exists() for call in invocations)
    restarting = mode in ("no-chat", "no-chat-fails")
    for invocation in invocations[1:]:
        assert invocations[0]["pid"] != invocation["pid"]
        assert invocations[0]["config"] == invocation["config"]
        assert invocations[0]["tmpdir"] == invocation["tmpdir"]
        if restarting:
            assert invocation["stdin"] == invocations[0]["stdin"]
            assert invocation["argv"] == invocations[0]["argv"]
        else:
            assert invocation["stdin"] == CONTINUATION_PROMPT
            assert invocation["argv"] == invocations[0]["argv"] + ["--resume", "chat-retry"]
    assert agent_db.start_record.await_count == 1
    assert agent_db.finish_record.await_count == 1
    events = [event for call in agent_db.append_events.await_args_list for _, event in call.args[1]]
    retries = [event for event in events if event.get("subtype") == "cursor_run_retry"]
    assert len(retries) == attempts - 1
    if retries:
        assert retries[0]["reason"] == "transient_backend" and "attempt-1" in retries[0]["stderr"]
        assert [event["attempt"] for event in retries] == list(range(1, attempts))
        assert all(event["mode"] == ("restart" if restarting else "resume") for event in retries)
    activities = [call.kwargs["summary"] for call in agent_db.insert_activity.await_args_list]
    assert "abandoned draft" not in activities
    assert all(event["summary"] in activities for event in retries)
    if mode in ("once", "three", "no-chat"):
        assert result.get("passed") is True if verify else result["status"] == "completed"
        assert agent_db.finish_record.await_args.kwargs["error"] is None
    else:
        assert result["status"] == "failed"
        if attempts > 1:
            assert len(result["stderr"]) <= 1000
            for attempt in range(1, attempts + 1):
                assert f"attempt-{attempt}" in result["error"]
                assert f"attempt-{attempt}" in result["stderr"]
                assert f"attempt-{attempt}" in agent_db.finish_record.await_args.kwargs["error"]


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(("mode", "progress", "retry_modes"), [
    ("once", ("init",), ["restart"]),
    ("twice", ("init",), ["restart"]),
    ("once", ("tool",), ["resume"]),
    ("three", ("init", "assistant"), ["restart", "resume", "resume"]),
    ("twice", ("init", "assistant"), ["restart", "resume", "resume"]),
    ("twice", ("assistant", "init", "assistant"), ["resume", "restart", "resume"]),
    ("twice", ("assistant", "init"), ["resume", "restart"]),
])
def test_worker_cursor_retry_requires_progress_in_failed_attempt(
    mode, progress, retry_modes, wrapped, tmp_path, monkeypatch, agent_db: Any,
):
    launch, calls = _stand_in(tmp_path, monkeypatch, mode, wrapped=wrapped, progress=progress)
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 15, "max_turns": 0})
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=launch.settings))
    work = tmp_path / "work"
    work.mkdir()

    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        return await pool._execute(_step_envelope(work, verify=False))

    result = asyncio.run(asyncio.wait_for(run(), 30))
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == len(retry_modes) + 1
    assert "ROLE" in invocations[0]["stdin"]
    for invocation, retry_mode in zip(invocations[1:], retry_modes):
        if retry_mode == "restart":
            assert invocation["argv"] == invocations[0]["argv"]
            assert "--resume" not in invocation["argv"]
            assert invocation["stdin"] == invocations[0]["stdin"]
        else:
            assert invocation["argv"] == invocations[0]["argv"] + ["--resume", "chat-retry"]
            assert invocation["stdin"] == CONTINUATION_PROMPT
    events = [event for call in agent_db.append_events.await_args_list for _, event in call.args[1]]
    retries = [event for event in events if event.get("subtype") == "cursor_run_retry"]
    assert [event["mode"] for event in retries] == retry_modes
    assert [event["attempt"] for event in retries] == list(range(1, len(retry_modes) + 1))
    assert result["status"] == ("completed" if mode in ("once", "three") else "failed")
    assert all(not Path(call["tmpdir"]).exists() for call in invocations)


@pytest.mark.parametrize(("mode", "wrapped", "attempts"), [
    ("once", False, 2), ("no-chat", False, 2), ("twice", False, 2),
    ("other", False, 1), ("result", False, 1), ("signal", False, 1),
    ("other-retriable", False, 1),
    ("held-stderr", False, 2),
    ("once", True, 2), ("twice", True, 2), ("other", True, 1),
])
def test_session_cursor_retry(mode, wrapped, attempts, tmp_path, monkeypatch, no_tree_end):
    launch, calls = _stand_in(tmp_path, monkeypatch, mode, wrapped=wrapped)
    command = CURSOR.resident_command(launch.profile.runtime.executable, cwd=tmp_path,
                                     prompt_file=_prompt(tmp_path / "runtime"), launch=launch)
    events = []
    processes = []
    spawn = proc_tree.spawn

    async def capture_process(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(proc_tree, "spawn", capture_process)

    async def run():
        session = CURSOR.resident_session("manager")
        async def on_event(event):
            events.append(event)
        await session.start(command, cwd=str(tmp_path), signature="same", env=launch.environment())
        try:
            result = await session.run_turn("same turn", on_event)
            assert session.stale("same") is (mode not in ("once", "no-chat", "held-stderr", "result"))
            return result
        finally:
            await session.stop()
            # Wait for the stand-in's three-second child to close stderr before closing the event loop.
            await asyncio.wait_for(asyncio.gather(*(process.stderr.read() for process in processes)), 5)

    result = asyncio.run(asyncio.wait_for(run(), 15))
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == attempts
    assert len([event for event in events if event.get("subtype") == "cursor_run_retry"]) == attempts - 1
    if attempts == 2:
        assert invocations[0]["pid"] != invocations[1]["pid"]
        assert invocations[0]["config"] == invocations[1]["config"]
        if mode == "no-chat":
            assert "--resume" not in invocations[1]["argv"]
            assert invocations[0]["stdin"] == invocations[1]["stdin"]
        else:
            assert invocations[1]["argv"][-2:] == ["--resume", "chat-retry"]
            assert invocations[1]["stdin"] == "same turn"
    if mode in ("once", "no-chat", "held-stderr"):
        assert result.result_event["subtype"] == "success"
    elif mode == "twice":
        assert result.result_event is None
        assert "attempt-1" not in result.stderr and "attempt-2" in result.stderr
        assert "attempt-1" in result.stderr_detail and "attempt-2" in result.stderr_detail


@pytest.fixture(autouse=True)
def _forget_login():
    cursor_login.forget_probe()
    ttadk.forget_probe()
    yield
    cursor_login.forget_probe()
    ttadk.forget_probe()


@pytest.mark.parametrize("ending", ["cancel", "timeout", "cancel-before-retry"])
def test_session_stop_and_timeout_do_not_retry(ending, tmp_path, monkeypatch):
    from looper.agents.runtimes.cancel import TurnCancel

    launch, calls = _stand_in(tmp_path, monkeypatch, "once" if ending == "cancel-before-retry" else "timeout")
    command = CURSOR.resident_command(launch.profile.runtime.executable, cwd=tmp_path,
                                     prompt_file=_prompt(tmp_path / "runtime"), launch=launch)

    async def run():
        session = CURSOR.resident_session("planner-test")
        cancel = TurnCancel()
        connected = asyncio.Event()
        async def on_event(event):
            if event.get("subtype") == "cursor_connection":
                connected.set()
                if ending == "cancel":
                    cancel.request()
            if event.get("subtype") == "cursor_run_retry" and ending == "cancel-before-retry":
                cancel.request()
        await session.start(command, cwd=str(tmp_path), signature="same", env=launch.environment())
        task = asyncio.create_task(session.run_turn("same turn", on_event, cancel=cancel))
        try:
            await asyncio.wait_for(connected.wait(), 5)
            if ending == "timeout":
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, 0.05)
            else:
                result = await task
                assert result.stopped is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await session.stop()

    asyncio.run(asyncio.wait_for(run(), 10))
    assert len(calls.read_text().splitlines()) == 1


def test_worker_timeout_does_not_retry(tmp_path, monkeypatch, agent_db: Any):
    launch, calls = _stand_in(tmp_path, monkeypatch, "timeout")
    # Leave room for macOS process startup on a shared machine; the stand-in sleeps for ten seconds.
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 5, "max_turns": 0})
    async def settings(*args, **kwargs):
        return launch.settings
    monkeypatch.setattr(worker, "load_agent_settings", settings)
    work = tmp_path / "work"
    work.mkdir()
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        with pytest.raises(asyncio.TimeoutError):
            await pool._execute(_step_envelope(work, verify=False))
    asyncio.run(asyncio.wait_for(run(), 15))
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == 1
    assert not Path(invocations[0]["tmpdir"]).exists()
    events = [event for call in agent_db.append_events.await_args_list for _, event in call.args[1]]
    assert not any(event.get("subtype") == "cursor_run_retry" for event in events)


@pytest.mark.parametrize("boundary", ["before-retry", "after-retry-event", "after-first-resume"])
def test_worker_cursor_retry_keeps_original_deadline(boundary, tmp_path, monkeypatch, agent_db: Any):
    launch, calls = _stand_in(tmp_path, monkeypatch, "twice")
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 15, "max_turns": 0})
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=launch.settings))
    monotonic = worker.time.monotonic
    elapsed, failures = 0, 0
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: monotonic() + elapsed))
    record = worker.ClaudeRunRecorder.record

    async def advance_deadline(self, event):
        nonlocal elapsed, failures
        await record(self, event)
        if event.get("subtype") == "cursor_connection":
            failures += 1
            if boundary == "before-retry" or (boundary == "after-first-resume" and failures == 2):
                elapsed = 30
        if boundary == "after-retry-event" and event.get("subtype") == "cursor_run_retry":
            elapsed = 30

    monkeypatch.setattr(worker.ClaudeRunRecorder, "record", advance_deadline)
    work = tmp_path / "work"
    work.mkdir()

    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        with pytest.raises(asyncio.TimeoutError):
            await pool._execute(_step_envelope(work, verify=False))

    asyncio.run(asyncio.wait_for(run(), 30))
    assert len(calls.read_text().splitlines()) == (2 if boundary == "after-first-resume" else 1)
    assert agent_db.start_record.await_count == agent_db.finish_record.await_count == 1


@pytest.mark.parametrize("blocked", ["wait", "stderr", "failure-stderr", "stderr-eof"])
def test_worker_exit_waits_respect_run_timeout(blocked, tmp_path, monkeypatch, agent_db: Any):
    launch, calls = _stand_in(tmp_path, monkeypatch, "result" if blocked == "failure-stderr" else "once")
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 0 if blocked == "stderr-eof" else 1,
                             "max_turns": 0})
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=launch.settings))
    work = tmp_path / "work"
    work.mkdir()

    async def run():
        entered = asyncio.Event()
        wait = worker.ProcessRun.wait

        async def held_open(self):
            if blocked == "wait":
                await wait(self)
            entered.set()
            await asyncio.Event().wait()

        if blocked == "stderr-eof":
            async def exited_with_open_stderr(self):
                code = await wait(self)
                if not entered.is_set():
                    # Retain the transient error even though the inherited pipe never reaches EOF.
                    self.process.stderr = asyncio.StreamReader()
                    self.process.stderr.feed_data(SIGNATURE.encode())
                    entered.set()
                return code
            monkeypatch.setattr(worker.ProcessRun, "wait", exited_with_open_stderr)
        else:
            monkeypatch.setattr(worker.ProcessRun, "wait" if blocked == "wait" else "error_output", held_open)
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        started = asyncio.get_running_loop().time()
        if blocked == "stderr-eof":
            result = await asyncio.wait_for(pool._execute(_step_envelope(work, verify=False)), 3)
            assert result["status"] == "completed"
        else:
            with pytest.raises(asyncio.TimeoutError, match="timeout_seconds"):
                await asyncio.wait_for(pool._execute(_step_envelope(work, verify=False)), 3)
        assert entered.is_set(), "the stalled exit operation must be reached"
        assert asyncio.get_running_loop().time() - started < 2

    asyncio.run(asyncio.wait_for(run(), 5))
    assert len(calls.read_text().splitlines()) == (2 if blocked == "stderr-eof" else 1)
    assert agent_db.start_record.await_count == agent_db.finish_record.await_count == 1
    finish = agent_db.finish_record.await_args.kwargs
    if blocked == "stderr-eof":
        events = [event for call in agent_db.append_events.await_args_list for _, event in call.args[1]]
        assert any(event.get("subtype") == "cursor_run_retry" and SIGNATURE in event["stderr"] for event in events)
        assert finish["error"] is None
    else:
        assert finish["status"] == "failed"
        assert "timeout_seconds" in finish["error"]


def test_worker_cancel_before_retry_leaves_exited_tree_alone(tmp_path, monkeypatch, agent_db: Any, no_tree_end):
    launch, calls = _stand_in(tmp_path, monkeypatch, "once")
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 5, "max_turns": 0})
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=launch.settings))
    record = worker.ClaudeRunRecorder.record

    async def cancel_retry(self, event):
        if event.get("subtype") == "cursor_run_retry":
            raise asyncio.CancelledError
        await record(self, event)

    monkeypatch.setattr(worker.ClaudeRunRecorder, "record", cancel_retry)
    work = tmp_path / "work"
    work.mkdir()

    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        with pytest.raises(asyncio.CancelledError):
            await pool._execute(_step_envelope(work, verify=False))

    asyncio.run(asyncio.wait_for(run(), 10))
    assert len(calls.read_text().splitlines()) == 1


@pytest.mark.parametrize("role", ["manager", "planner"])
@pytest.mark.parametrize("mode", ["once", "twice", "lost-init", "retry-lost-session"])
def test_tier_turn_records_cursor_retry(role, mode, tmp_path, monkeypatch, agent_db: Any):
    from looper.agents.bus import MessageBus
    from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType
    from looper.agents.system_prompt import SystemPrompt
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.agents.tiers.planner import PlannerAgent, PlannerTurnError, TaskPlan
    from tests.test_manager_reply import WEB

    launch, calls = _stand_in(tmp_path, monkeypatch, mode)

    async def run():
        bus = MessageBus(runtime_root=tmp_path / "runtime")
        if role == "manager":
            agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path / "runtime")
            agent._bus = bus
            agent._composed_prompt = lambda _: SystemPrompt("PROMPT")
        else:
            agent = PlannerAgent(task_id="task-1", runtime_root=tmp_path / "runtime")
            agent._plan = TaskPlan("plan-1", "goal-1", "Inspect", str(tmp_path))
            monkeypatch.setattr(agent, "_system_prompt", lambda *args, **kwargs: SystemPrompt("PROMPT"))
        agent._load_agent_settings = AsyncMock(return_value=launch.settings)
        try:
            if mode in ("lost-init", "retry-lost-session"):
                if role == "manager":
                    await agent._handle(MessageEnvelope.create(source=WEB, target=MANAGER_ADDRESS,
                        message_type=MessageType.USER_MESSAGE, payload={"text": "first"}))
                else:
                    assert "recovered" in await agent._turn("first")
                if mode == "retry-lost-session":
                    # Restart the runtime so the next turn resumes the stored session.
                    await agent._claude.stop()
            if role == "manager":
                await agent._handle(MessageEnvelope.create(source=WEB, target=MANAGER_ADDRESS,
                    message_type=MessageType.USER_MESSAGE, payload={"text": "hello"}))
            elif mode in ("twice", "lost-init"):
                with pytest.raises(PlannerTurnError, match="attempt-"):
                    await agent._turn("hello")
            else:
                assert "recovered" in await agent._turn("hello")
        finally:
            await agent.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 15))
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == (4 if mode in ("lost-init", "retry-lost-session") else 2)
    if mode == "retry-lost-session":
        assert "--resume" not in invocations[0]["argv"]
        assert invocations[1]["argv"][-2:] == ["--resume", "chat-retry"]
        assert invocations[2]["argv"][-2:] == ["--resume", "chat-retry"]
        assert "--resume" not in invocations[3]["argv"]
        assert invocations[1]["pid"] != invocations[2]["pid"]
    else:
        assert invocations[-1]["argv"][-2:] == ["--resume", "chat-retry"]
    events = [event for call in agent_db.append_events.await_args_list for _, event in call.args[1]]
    retry_count = 2 if mode == "lost-init" else 1
    assert len([event for event in events if event.get("subtype") == "cursor_run_retry"]) == retry_count
    finish = agent_db.finish_record.await_args.kwargs
    if mode == "retry-lost-session":
        failures = [call.kwargs["error"] for call in agent_db.finish_record.await_args_list
                    if call.kwargs["error"]]
        assert len(failures) == 1
        assert "attempt-2" in failures[0] and "session not found; attempt-3" in failures[0]
        assert "retried in a new session" in failures[0]
        assert finish["error"] is None
        assert "recovered" in finish["output_text"]
    elif mode != "once":
        first, second = (3, 4) if mode == "lost-init" else (1, 2)
        assert f"attempt-{first}" in finish["error"] and f"attempt-{second}" in finish["error"]
    else:
        assert finish["error"] is None
    if role == "planner":
        assert sum(call.kwargs["summary"] == RETRY_LINE for call in agent_db.insert_activity.await_args_list) == retry_count


def test_missing_cursor_login_does_not_launch(tmp_path, monkeypatch, agent_db: Any):
    launch, calls = _stand_in(tmp_path, monkeypatch, "once")
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 5, "max_turns": 0})
    async def settings(*args, **kwargs):
        return replace(launch.settings, profile_error="Cursor is not signed in on this machine")
    monkeypatch.setattr(worker, "load_agent_settings", settings)
    work = tmp_path / "work"
    work.mkdir()
    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        return await pool._execute(_step_envelope(work, verify=False))
    result = asyncio.run(asyncio.wait_for(run(), 5))
    assert result["status"] == "failed" and "not signed in" in result["error"]
    assert not calls.exists()
