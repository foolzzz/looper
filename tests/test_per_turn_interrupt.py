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
