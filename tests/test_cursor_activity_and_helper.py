"""Cursor activity ordering and private helper lifetime through local stand-ins."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from fake_claude import fake_claude
from looper.agents.read_only_copies import reclaim_sessions
from looper.agents.runtimes import Launcher
from looper.agents.runtimes.cursor_agent import CursorStream, run_tmpdir
from test_agent_planner_worker import _step_envelope, _worker_row
from test_cursor_agent import CURSOR, _RESIDENT_CURSOR, _blocks, _launch, _prompt, cursor_tmpdirs  # noqa: F401
from test_ttadk_cursor_agent import _cursor_script
from looper.agents.tiers import worker


@pytest.mark.parametrize("phase", ["started", "completed"])
def test_text_is_committed_before_each_tool_event_and_only_once(phase):
    stream = CursorStream()
    call = {"type": "tool_call", "subtype": phase, "call_id": "read",
            "tool_call": {"readToolCall": {"args": {"path": "file"}}}}
    if phase == "completed":
        stream.translate({**call, "subtype": "started"})
    text = {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}
    assert stream.translate(text) == []
    events = stream.translate(call)
    assert events[0] == text
    assert _blocks(events[1:], "tool_use" if phase == "started" else "tool_result")
    assert not _blocks(stream.translate({"type": "result"}), "text")
    # Equal text in a later, successful attempt is a distinct transcript message.
    assert stream.translate(text) == []
    assert _blocks(stream.translate({"type": "result"}), "text") == text["message"]["content"]


@pytest.mark.parametrize("wrapped", [False, True], ids=["native", "ttadk"])
def test_same_cwd_and_prompt_runs_get_private_short_tmpdirs(wrapped, tmp_path, monkeypatch, agent_db):
    captures = tmp_path / "captures"
    captures.mkdir()
    executable = _cursor_script(tmp_path / "cursor-agent", f"""
import json, os, pathlib, stat, subprocess, sys
sys.stdin.read()
directory = pathlib.Path(os.environ['TMPDIR'])
assert directory.is_dir()
assert stat.S_IMODE(directory.stat().st_mode) == 0o700
assert subprocess.check_output(['/bin/sh', '-c', 'printf %s "$TMPDIR"'], text=True) == str(directory)
pathlib.Path({str(captures)!r}, directory.name).write_text(json.dumps({{
    'tmpdir': str(directory), 'config': os.environ['CURSOR_CONFIG_DIR']}}))
report = json.dumps({{'status': 'completed', 'changed_files': {{}}, 'summary': 'run complete'}})
print(json.dumps({{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': report}}]}}}}))
print(json.dumps({{'type': 'result', 'subtype': 'success'}}))
""")
    launch = _launch(executable=executable)
    if wrapped:
        wrapper = fake_claude(tmp_path / "ttadk", """
import os, sys
assert sys.argv[1:6] == ['code', '-t', 'cursor-cli', '--skip-check', '--']
os.execvp('agent', ['agent', *sys.argv[6:]])
""")
        launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile,
            runtime=replace(launch.profile.runtime, executable=wrapper, launcher=Launcher.TTADK))))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 5, "max_turns": 0})

    async def settings(*args, **kwargs):
        return launch.settings

    monkeypatch.setattr(worker, "load_agent_settings", settings)
    work = tmp_path / "work"
    work.mkdir()

    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        return await asyncio.gather(*(pool._execute(_step_envelope(work, verify=False)) for _ in range(2)))

    results = asyncio.run(asyncio.wait_for(run(), 15))
    assert all(result["status"] == "completed" for result in results)
    records = [json.loads(path.read_text()) for path in captures.iterdir()]
    assert len(records) == 2
    assert len({record["tmpdir"] for record in records}) == 2
    for record in records:
        directory = Path(record["tmpdir"])
        assert str(directory).startswith("/tmp/looper-cur-") and len(str(directory)) < 40
        assert directory == run_tmpdir(Path(record["config"]).parent)
        assert not directory.exists()


def test_session_tmpdir_survives_turns_and_stop_until_session_removed(tmp_path):
    executable = fake_claude(tmp_path / "cursor-agent", _RESIDENT_CURSOR)
    root = tmp_path / "runtime"
    launch = _launch(executable=executable)
    command = CURSOR.resident_command(executable, cwd=tmp_path, prompt_file=_prompt(root), launch=launch)
    sessions = [CURSOR.resident_session(label) for label in ("manager", "planner-task-1")]

    async def run():
        async def on_event(event):
            pass

        for session in sessions:
            await session.start(command, cwd=str(tmp_path), signature="a", env=launch.environment())
            try:
                for _ in range(2):
                    turn = await session.run_turn("hello", on_event)
                    assert turn.result_event["result"] == "pong"
            finally:
                await session.stop()

    asyncio.run(asyncio.wait_for(run(), 15))
    homes = [root / ".local/cursor-agent/sessions" / session.label for session in sessions]
    directories = [run_tmpdir(home) for home in homes]
    assert len(set(directories)) == 2
    for home, directory in zip(homes, directories):
        turns = [json.loads(line) for line in (home / "turns.jsonl").read_text().splitlines()]
        assert [turn["tmpdir"] for turn in turns] == [str(directory)] * 2
        assert directory.is_dir() and stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert len(str(directory)) < 40
    assert reclaim_sessions(root, saved=[("manager", "cursor-agent")], command_lines=[]) == [homes[1]]
    assert directories[0].is_dir() and not directories[1].exists()
    assert reclaim_sessions(root, saved=[], command_lines=[]) == [homes[0]]
    assert not directories[0].exists()
