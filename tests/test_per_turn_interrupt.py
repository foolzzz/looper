"""``PerTurnSession.interrupt`` (``looper/agents/runtimes/per_turn.py``): the user's stop ends the turn's process tree
and keeps the session, for every runtime that runs one process per turn."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents import proc_tree
from looper.agents.claude_cli import USER_STOP
from looper.agents.runtimes import StopMethod

_PRELUDE = '''
import json, os, subprocess, sys, time
HOME = os.environ[%r]
args = sys.argv[1:]
open(os.path.join(HOME, 'argv.jsonl'), 'a').write(json.dumps(args) + '\\n')
sys.stdin.read()
def emit(event):
    print(json.dumps(event), flush=True)
def hang():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    open(os.path.join(HOME, 'pids'), 'w').write('%%d %%d' %% (os.getpid(), child.pid))
    time.sleep(30)
'''

_OMP = _PRELUDE % "HOME" + '''
emit({'type': 'session', 'version': 3, 'id': 'sess-1', 'cwd': os.getcwd()})
emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'toolUse', 'model': 'gpt-5.5',
      'usage': {'input': 60, 'output': 5, 'cacheRead': 40, 'cacheWrite': 0},
      'content': [{'type': 'toolCall', 'id': 'c1', 'name': 'bash', 'arguments': {'command': 'true'}}]}})
hang()
'''

_CODEX = _PRELUDE % "CODEX_HOME" + '''
emit({'type': 'thread.started', 'thread_id': 'thread-1'})
emit({'type': 'turn.started'})
hang()
'''


def _pids(home: Path) -> list[int]:
    return [int(p) for p in (home / "pids").read_text().split()]


async def _until(predicate: Any) -> None:
    while not predicate():
        await asyncio.sleep(0.02)


def _alive(pid: int) -> bool:
    return pid in proc_tree._snapshot()


async def _stop_a_turn(session: Any, home: Path) -> Any:
    async def on_event(event: dict[str, Any]) -> None:
        pass

    running = asyncio.create_task(session.run_turn("hello", on_event))
    try:
        await asyncio.wait_for(_until(lambda: (home / "pids").exists()), 8)
        await asyncio.sleep(0.3)  # the stand-in's first event reaches the stream
        parent, child = _pids(home)
        assert await session.interrupt() is StopMethod.KILL
        turn = await asyncio.wait_for(running, 5)
        assert not _alive(parent) and not _alive(child)
        return turn
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def _home(command: list[str], name: str) -> Path:
    return Path(next(a for a in command if a.startswith(f"{name}=")).partition("=")[2])


def _omp(tmp_path: Path) -> tuple[Any, list[str], dict[str, str], Path]:
    from test_omp_runtime import OMP, _launch, _resident_command

    root = tmp_path / "runtime"
    command = _resident_command(root, fake_claude(tmp_path / "omp", _OMP), "prompt")
    env = _launch().environment()
    return OMP.resident_session("manager"), command, env, _home(command, "HOME")


def _codex(tmp_path: Path) -> tuple[Any, list[str], dict[str, str], Path]:
    from test_codex_runtime import CODEX, _launch, _resident_command

    root = tmp_path / "runtime"
    command = _resident_command(root, fake_claude(tmp_path / "codex", _CODEX), "prompt")
    env = _launch().environment()
    source = _home(command, "CODEX_HOME")  # the turns run in the owner's own home beside it
    return CODEX.resident_session("manager"), command, env, source.parent.parent / "sessions" / "manager"


@pytest.mark.parametrize("runtime", ["omp", "codex"])
def test_interrupt_ends_the_process_tree_and_keeps_the_session(tmp_path: Path, runtime: str) -> None:
    session, command, env, home = (_omp if runtime == "omp" else _codex)(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    resume = "--resume" if runtime == "omp" else "resume"

    async def run() -> Any:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        assert await session.interrupt() is None  # no turn running
        turn = await _stop_a_turn(session, home)
        assert not session.stale("s")
        (home / "pids").unlink()
        next_turn = asyncio.create_task(session.run_turn("again", lambda event: asyncio.sleep(0)))
        try:
            await asyncio.wait_for(_until(lambda: (home / "pids").exists()), 8)
            await session.interrupt()
            await asyncio.wait_for(next_turn, 5)
        finally:
            next_turn.cancel()
            await asyncio.gather(next_turn, return_exceptions=True)
        return turn, session.totals

    turn, totals = asyncio.run(asyncio.wait_for(run(), 40))
    assert turn.stopped is StopMethod.KILL and turn.result_event[USER_STOP]
    assert turn.session_started
    runs = [json.loads(line) for line in (home / "argv.jsonl").read_text().splitlines()]
    assert resume not in runs[0]
    assert runs[1][runs[1].index(resume) + 1] == ("sess-1" if runtime == "omp" else "thread-1")
    if runtime == "omp":
        assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.cache_read_tokens) == (60, 5, 40)
        assert turn.usage_complete
        assert totals.input_tokens == 120  # both stopped turns moved the totals on
    else:
        assert turn.usage.input_tokens in (None, 0) and turn.usage.output_tokens in (None, 0)
        assert turn.usage_complete is False


def test_dsh_session_is_not_stale_after_a_stopped_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_dsh_runtime import _resident_dsh_stand_in, _run_launch, _run_prompt_file, runtimes

    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.1)
    executable = _resident_dsh_stand_in(tmp_path)
    runtime = runtimes.adapter("dsh")
    launch = _run_launch(tmp_path, role="manager")
    command = runtime.resident_command(executable, cwd=tmp_path, prompt_file=_run_prompt_file(tmp_path), launch=launch)

    async def run() -> Any:
        owner = runtime.resident_session("manager")
        await owner.start(command, cwd=str(tmp_path), signature="s", env=launch.environment())
        home = _home(owner.command, "DSH_HOME")

        async def on_event(event: dict[str, Any]) -> None:
            pass

        running = asyncio.create_task(owner.run_turn("hang", on_event))
        try:
            await asyncio.wait_for(_until(lambda: (home / "child-pid").exists()), 8)
            await asyncio.sleep(0.3)
            parent, child = owner.pid, int((home / "child-pid").read_text())
            assert await owner.interrupt() is StopMethod.KILL
            turn = await asyncio.wait_for(running, 5)
            assert not _alive(parent) and not _alive(child)
            return turn, owner.stale("s"), owner._session
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await owner.stop()

    turn, stale, session = asyncio.run(asyncio.wait_for(run(), 30))
    assert turn.stopped is StopMethod.KILL and turn.result_event[USER_STOP]
    assert not stale and session


@pytest.mark.parametrize("runtime", ["omp", "codex"])
def test_a_stopped_turn_returns_only_after_its_process_tree_cleanup(
    tmp_path: Path, runtime: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, command, env, home = (_omp if runtime == "omp" else _codex)(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    real_end = proc_tree.TurnTree.end
    ended: list[bool] = []

    async def slow_end(tree: Any) -> Any:
        tree.process.kill()  # the turn's pipe closes while the tree's cleanup is still going
        await asyncio.sleep(0.5)
        try:
            return await real_end(tree)
        finally:
            ended.append(True)

    async def run() -> Any:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)

        async def on_event(event: dict[str, Any]) -> None:
            pass

        running = asyncio.create_task(session.run_turn("hello", on_event))
        try:
            await asyncio.wait_for(_until(lambda: (home / "pids").exists()), 8)
            await asyncio.sleep(0.3)
            monkeypatch.setattr(proc_tree.TurnTree, "end", slow_end)
            stopper = asyncio.create_task(session.interrupt())
            turn = await asyncio.wait_for(running, 5)
            assert ended == [True]
            await stopper
            return turn
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            monkeypatch.setattr(proc_tree.TurnTree, "end", real_end)
            await session.stop()

    turn = asyncio.run(asyncio.wait_for(run(), 30))
    assert turn.stopped is StopMethod.KILL


@pytest.mark.parametrize("runtime", ["omp", "codex"])
@pytest.mark.parametrize("past_bound", [False, True], ids=["within", "past"])
def test_a_stopped_turn_waits_out_the_whole_end_and_keeps_one_that_outlasts_it(
    tmp_path: Path, runtime: str, past_bound: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``TurnTree.end`` may run past the grace and the reap while ``ps`` blocks: its last look and the ownership
    # listings' own budget come after them. The turn waits that long; an end that runs longer still is kept, not
    # dropped with the turn, and finishes.
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.3)
    monkeypatch.setattr(proc_tree, "REAP_SEC", 0.3)
    session, command, env, home = (_omp if runtime == "omp" else _codex)(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    real_end = proc_tree.TurnTree.end
    spec = proc_tree.TERM_GRACE_SEC + proc_tree.REAP_SEC + proc_tree._LAST_LOOK_SEC + 2 * proc_tree._OWNED_SEC
    ended: list[bool] = []

    async def slow_end(tree: Any) -> Any:
        tree.process.kill()
        try:
            return await real_end(tree)
        finally:
            await asyncio.sleep(spec + 2 if past_bound else spec - 0.5)
            ended.append(True)

    async def run() -> Any:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        running = asyncio.create_task(session.run_turn("hello", _noop))
        try:
            await asyncio.wait_for(_until(lambda: (home / "pids").exists()), 8)
            await asyncio.sleep(0.3)
            monkeypatch.setattr(proc_tree.TurnTree, "end", slow_end)
            session._cancel.request()
            turn = await asyncio.wait_for(running, 30)
            if past_bound:
                assert ended == []
                assert [task for task in proc_tree._finishing if not task.done()]
                await asyncio.wait_for(asyncio.gather(*proc_tree._finishing), 10)
            assert ended == [True]
            return turn
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            monkeypatch.setattr(proc_tree.TurnTree, "end", real_end)
            await session.stop()

    turn = asyncio.run(asyncio.wait_for(run(), 40))
    assert turn.stopped is StopMethod.KILL


@pytest.mark.parametrize("runtime", ["omp", "codex"])
@pytest.mark.parametrize("phase", ["argv", "spawn"])
def test_a_stop_before_the_process_exists_is_kept(
    tmp_path: Path, runtime: str, phase: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, command, env, home = (_omp if runtime == "omp" else _codex)(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    reached, release = asyncio.Event(), asyncio.Event()
    spawned: list[Any] = []

    async def run() -> Any:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        loop = asyncio.get_running_loop()
        real_argv, real_spawn = session._turn_argv, proc_tree.spawn

        def slow_argv() -> list[str]:
            if phase == "argv":
                loop.call_soon_threadsafe(reached.set)
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
            return real_argv()

        async def slow_spawn(*args: Any, **kwargs: Any) -> Any:
            if phase == "spawn":
                reached.set()
                await release.wait()
            process = await real_spawn(*args, **kwargs)
            spawned.append(process)
            return process

        monkeypatch.setattr(session, "_turn_argv", slow_argv)
        monkeypatch.setattr(proc_tree, "spawn", slow_spawn)

        async def on_event(event: dict[str, Any]) -> None:
            pass

        running = asyncio.create_task(session.run_turn("hello", on_event))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            assert await session.interrupt() is StopMethod.KILL
            release.set()
            turn = await asyncio.wait_for(running, 5)
            for process in spawned:
                await asyncio.wait_for(process.wait(), 5)  # a process started anyway is ended
            return turn
        finally:
            release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await session.stop()

    turn = asyncio.run(asyncio.wait_for(run(), 30))
    assert turn.stopped is StopMethod.KILL and turn.result_event[USER_STOP]
    assert (phase == "argv") == (not spawned)


# --- ``run_turn(..., cancel=)``: the one stop ---


def _cancel() -> Any:
    from looper.agents.runtimes.cancel import TurnCancel

    return TurnCancel()


async def _noop(event: dict[str, Any]) -> None:
    pass


def test_a_cancel_requested_before_run_turn_spawns_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, command, env, home = _omp(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    spawned: list[Any] = []
    real_spawn = proc_tree.spawn

    async def spy(*args: Any, **kwargs: Any) -> Any:
        spawned.append(args)
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(proc_tree, "spawn", spy)

    async def run() -> Any:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        cancel = _cancel()
        cancel.request()
        return await session.run_turn("hello", _noop, cancel=cancel)

    turn = asyncio.run(asyncio.wait_for(run(), 20))
    assert turn.stopped is StopMethod.KILL and turn.result_event[USER_STOP]
    assert not turn.prompt_sent and turn.usage_complete and not spawned


@pytest.mark.parametrize("phase", ["argv", "spawn"])
def test_a_cancel_during_preparation_or_spawn(tmp_path: Path, phase: str, monkeypatch: pytest.MonkeyPatch) -> None:
    session, command, env, home = _codex(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    reached, release = asyncio.Event(), asyncio.Event()
    spawned: list[Any] = []

    async def run() -> Any:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        loop = asyncio.get_running_loop()
        real_argv, real_spawn = session._turn_argv, proc_tree.spawn

        def slow_argv() -> list[str]:
            if phase == "argv":
                loop.call_soon_threadsafe(reached.set)
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
            return real_argv()

        async def slow_spawn(*args: Any, **kwargs: Any) -> Any:
            if phase == "spawn":
                reached.set()
                await release.wait()
            process = await real_spawn(*args, **kwargs)
            spawned.append(process)
            return process

        monkeypatch.setattr(session, "_turn_argv", slow_argv)
        monkeypatch.setattr(proc_tree, "spawn", slow_spawn)
        cancel = _cancel()
        running = asyncio.create_task(session.run_turn("hello", _noop, cancel=cancel))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            cancel.request()
            release.set()
            turn = await asyncio.wait_for(running, 8)
            for process in spawned:
                await asyncio.wait_for(process.wait(), 5)
            return turn
        finally:
            release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    turn = asyncio.run(asyncio.wait_for(run(), 30))
    assert turn.stopped is StopMethod.KILL and not turn.prompt_sent
    assert (phase == "argv") == (not spawned)


_CHATTY = _PRELUDE % "CODEX_HOME" + '''
emit({'type': 'thread.started', 'thread_id': 'thread-1'})
emit({'type': 'turn.started'})
subprocess.Popen([sys.executable, '-c', 'import os, time; os.setsid(); open(%r, "w").write(str(os.getpid())); time.sleep(60)' % os.path.join(HOME, 'grandchild')])
open(os.path.join(HOME, 'pids'), 'w').write('%d 0' % os.getpid())
for i in range(400):
    emit({'type': 'item.completed', 'item': {'id': 'i%d' % i, 'type': 'agent_message', 'text': 'tick %d' % i}})
    time.sleep(0.05)
'''


def test_after_the_stop_no_event_reaches_on_event_and_a_setsid_grandchild_is_gone(tmp_path: Path) -> None:
    from test_codex_runtime import CODEX, _launch, _resident_command

    command = _resident_command(tmp_path / "runtime", fake_claude(tmp_path / "codex", _CHATTY), "prompt")
    session, env = CODEX.resident_session("manager"), _launch().environment()
    home = _home(command, "CODEX_HOME")
    home = home.parent.parent / "sessions" / "manager"
    home.mkdir(parents=True, exist_ok=True)
    got: list[dict[str, Any]] = []
    after_stop: list[int] = []

    async def run() -> Any:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        cancel = _cancel()

        async def on_event(event: dict[str, Any]) -> None:
            got.append(event)
            if cancel.requested:
                after_stop.append(1)

        running = asyncio.create_task(session.run_turn("hello", on_event, cancel=cancel))
        try:
            await asyncio.wait_for(_until(lambda: (home / "grandchild").exists() and len(got) > 3), 10)
            grandchild = int((home / "grandchild").read_text())
            assert _alive(grandchild)
            cancel.request()
            turn = await asyncio.wait_for(running, 8)
            assert not _alive(grandchild)
            return turn
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    turn = asyncio.run(asyncio.wait_for(run(), 30))
    assert turn.stopped is StopMethod.KILL and turn.prompt_sent
    assert not after_stop


def test_a_stderr_that_never_closes_does_not_hang_the_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.runtimes import per_turn
    from test_codex_runtime import CODEX, _launch, _resident_command

    body = _PRELUDE % "CODEX_HOME" + '''
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True, stdout=subprocess.DEVNULL)
emit({'type': 'thread.started', 'thread_id': 'thread-1'})
emit({'type': 'turn.started'})
emit({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}})
# stdout ends before the exit is reported, as on a slow runner: the turn must not wait for the exit through the pipes
os.close(1); time.sleep(0.5)
'''
    monkeypatch.setattr(per_turn, "STDERR_DRAIN_SEC", 0.3)
    command = _resident_command(tmp_path / "runtime", fake_claude(tmp_path / "codex", body), "prompt")
    session, env = CODEX.resident_session("manager"), _launch().environment()
    home = _home(command, "CODEX_HOME").parent.parent / "sessions" / "manager"
    home.mkdir(parents=True, exist_ok=True)

    async def run() -> Any:
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        return await session.run_turn("hello", _noop)

    turn = asyncio.run(asyncio.wait_for(run(), 15))
    assert turn.stopped is None and turn.prompt_sent
