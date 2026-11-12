"""Cursor through TTADK, using only local stand-ins and disposable run state."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentType, Launch, profile_launch_problem
from looper.agents.knowledge import AgentKnowledge
from looper.agents.library import AgentLibrary
from looper.agents.runtimes import AuthKind, Launcher, Probe, RuntimeUnsupported, cursor_agent, ttadk
from looper.agents.runtimes.cursor_agent import write_ttadk_shim
from looper.agents.tiers import worker
from test_cursor_agent import CURSOR, PERSONAL_CONFIG, _launch as _native_launch, _prompt, cursor_tmpdirs  # noqa: F401
from test_ttadk_runtime import _worker_run


@pytest.fixture(autouse=True)
def _isolated_ttadk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "operator"))
    ttadk.forget_probe()
    yield
    ttadk.forget_probe()


def _launch() -> Launch:
    launch = _native_launch()
    runtime = replace(launch.profile.runtime, id="rt-ttadk-cursor-agent", name="ttadk-cursor-agent",
                      executable="ttadk", launcher=Launcher.TTADK, version="ttadk 1.0.37 / cursor-agent test")
    return replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, runtime=runtime)))


def _cursor_script(path: Path, body: str) -> str:
    # The shim resolves symlinks, so this stand-in has its own executable body.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize("builder", ["run", "resident-turn"])
@pytest.mark.parametrize("launch_path", [None, "/opt/tools/bin"])
def test_ttadk_cursor_command_preserves_native_arguments(builder: str, launch_path: str | None,
                                                        tmp_path: Path, monkeypatch) -> None:
    if builder == "resident-turn" and not hasattr(cursor_agent, "ResidentCursor"):
        pytest.skip("The native Cursor resident-session implementation is not present in this checkout")
    prompt = _prompt(tmp_path / "runtime")
    inherited_path = os.environ["PATH"]

    def command(launch: Launch) -> list[str]:
        if launch_path is not None:
            launch = replace(launch, settings=replace(launch.settings, env=(("PATH", launch_path),)))
        launch = replace(launch, library=AgentLibrary(plugin_dirs=(str(tmp_path / "plugin"),)),
                         knowledge=AgentKnowledge(add_dirs=(str(tmp_path / "knowledge"),)))
        executable = launch.profile.runtime.executable
        kwargs = {"cwd": tmp_path, "prompt_file": prompt, "launch": launch}
        if builder == "run":
            return CURSOR.run_command(executable, **kwargs)
        argv = CURSOR.resident_command(executable, **kwargs)
        session = CURSOR.resident_session("cursor-command-test")
        session._base, session._session, program = session._split(argv)
        assert program == executable
        # A resumed chat retains the owner's instructions from its initial turn.
        session._turn_argv()
        session._base, session._session, program = session._split(argv + CURSOR.resume_args("saved-chat"))
        assert program == executable
        session._session_initialized()
        return session._turn_argv()

    native, wrapped = command(_native_launch()), command(_launch())
    if builder == "run":
        # Each command now owns execution state, even with identical cached inputs.
        for argv in (native, wrapped):
            run = str(Path(argv[1].partition("=")[2]).parent)
            argv[:] = [arg.replace(run, "<run>") for arg in argv]
    assert native[:3] == wrapped[:3]
    assert native[1].startswith("CURSOR_CONFIG_DIR=") and native[2].startswith("CURSOR_DATA_DIR=")
    shim = tmp_path / "runtime/.local/cursor-agent/ttadk-bin"
    assert wrapped[3].startswith("TMPDIR=") and native[3].startswith("TMPDIR=")
    assert wrapped[4:11] == [f"PATH={shim}{os.pathsep}{launch_path or inherited_path}", "ttadk", "code", "-t",
                             "cursor-cli", "--skip-check", "--"]
    assert wrapped[11:] == native[5:]
    if builder == "resident-turn":
        assert native[-2:] == wrapped[-2:] == CURSOR.resume_args("saved-chat")
    assert not any(arg.startswith("HOME=") for arg in wrapped)
    assert "LOOPER PROMPT" not in wrapped
    monkeypatch.setattr(CURSOR, "launch_args", lambda launch: ["--version"])
    with pytest.raises(RuntimeUnsupported, match="cannot forward"):
        command(_launch())


@pytest.mark.parametrize("missing", [None, "CURSOR_CONFIG_DIR", "CURSOR_DATA_DIR"])
def test_cursor_shim_guards_directories_and_strips_ttadk_token(missing, tmp_path: Path, monkeypatch) -> None:
    capture = tmp_path / "capture.json"
    binary = tmp_path / "Cursor's CLI"
    _cursor_script(binary / "cursor-agent", f"""
import json, os, pathlib, sys
pathlib.Path({str(capture)!r}).write_text(json.dumps({{
    'argv': sys.argv[1:], 'HOME': os.environ.get('HOME'),
    'token': os.environ.get('TTADK_JWT_TOKEN'),
    'config': os.environ.get('CURSOR_CONFIG_DIR'), 'data': os.environ.get('CURSOR_DATA_DIR')}}))
""")
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    directory = write_ttadk_shim(tmp_path / "local")
    shim = directory / "agent"
    inode = shim.stat().st_ino
    assert write_ttadk_shim(tmp_path / "local") == directory and shim.stat().st_ino == inode
    assert stat.S_IMODE(shim.stat().st_mode) == stat.S_IMODE(directory.stat().st_mode) == 0o700
    env = {**os.environ, "CURSOR_CONFIG_DIR": str(tmp_path / "config"),
           "CURSOR_DATA_DIR": str(tmp_path / "data"), "TTADK_JWT_TOKEN": "x"}
    if missing:
        env.pop(missing)
    args = ["--model", "a model", "", "quote'\"$literal"]
    done = subprocess.run([str(shim), *args], env=env, capture_output=True, text=True, timeout=5)
    if missing:
        assert done.returncode == 2 and "only in a Looper run" in done.stderr
        assert not capture.exists()
    else:
        assert done.returncode == 0, done.stderr
        assert json.loads(capture.read_text()) == {
            "argv": args, "HOME": os.environ.get("HOME"), "token": None,
            "config": env["CURSOR_CONFIG_DIR"], "data": env["CURSOR_DATA_DIR"]}
        _cursor_script(binary / "new-cursor", "")
        (binary / "cursor-agent").unlink()
        (binary / "cursor-agent").symlink_to(binary / "new-cursor")
        write_ttadk_shim(tmp_path / "local")
        assert shim.stat().st_ino != inode
        assert shlex.split(shim.read_text().splitlines()[-1])[1] == os.path.realpath(binary / "new-cursor")


@pytest.mark.parametrize("problem", ["missing", "recursive"])
def test_cursor_shim_refuses_missing_or_recursive_target(problem, tmp_path: Path, monkeypatch) -> None:
    local = tmp_path / "local"
    shim = local / "cursor-agent/ttadk-bin"
    shim.mkdir(parents=True)
    if problem == "recursive":
        (shim / "agent").write_text("#!/bin/sh\nexit 1\n")
        (shim / "agent").chmod(0o700)
        (shim / "cursor-agent").symlink_to(shim / "agent")
    monkeypatch.setenv("PATH", str(shim))
    with pytest.raises(RuntimeUnsupported, match="not on the server's PATH" if problem == "missing" else "run itself"):
        write_ttadk_shim(local)


def test_ttadk_cursor_environment_and_record_keep_native_login() -> None:
    removed = dict.fromkeys(ttadk.ENV_UNSET, "yes")
    native = _native_launch(env=tuple(removed.items()))
    wrapped = replace(_launch(), settings=replace(_launch().settings, env=tuple(removed.items())))
    assert CURSOR.environment(wrapped) == ttadk.environment(CURSOR.environment(native))
    assert CURSOR.environment(wrapped)["HOME"] == os.environ["HOME"]
    assert all(name not in CURSOR.environment(wrapped) for name in removed)
    assert CURSOR.launchers == frozenset({Launcher.NATIVE, Launcher.TTADK})
    assert CURSOR.ttadk_runtime_name == "ttadk-cursor-agent" and CURSOR.ttadk_tool == "cursor-cli"
    assert CURSOR.auth_kinds_for(Launcher.TTADK) == frozenset({AuthKind.OAUTH})
    assert CURSOR.launch_record(wrapped) == {"personal_config": list(PERSONAL_CONFIG),
        "launcher": {"kind": "ttadk", "version": wrapped.profile.runtime.version, "tool": "cursor-cli", "mode": None}}
    assert "launcher" not in CURSOR.launch_record(native)


@pytest.mark.parametrize("mode", ["success", "ttadk-error", "native-error", "unknown", "wrong-tool"])
def test_ttadk_cursor_probe_reports_versions_and_errors(mode, monkeypatch) -> None:
    monkeypatch.setattr(ttadk, "probe_version", lambda executable: ("1.0.37", "wrapper failed" if mode == "ttadk-error" else None))
    def native_probe(executable):
        assert executable == "cursor-agent" and mode != "ttadk-error"
        return Probe(None if mode == "unknown" else "test", CURSOR.capabilities,
                     "native failed" if mode == "native-error" else None)
    monkeypatch.setattr(CURSOR, "probe", native_probe)
    monkeypatch.setattr(ttadk, "tool_command", lambda tool: "other" if mode in ("native-error", "wrong-tool") else "agent")
    result = CURSOR.probe_for("ttadk", Launcher.TTADK)
    assert result.version == (None if mode == "ttadk-error" else
                             f"ttadk 1.0.37 / cursor-agent {'unknown' if mode == 'unknown' else 'test'}")
    assert result.error == {"ttadk-error": "wrapper failed", "native-error": "native failed",
        "wrong-tool": "TTADK's cursor-cli command is `other`, not `agent`: Looper will not launch it"}.get(mode)


@pytest.mark.parametrize("check_mode", ["success", "timeout", "signed-out"])
def test_worker_reads_cursor_report_and_instructions_through_ttadk(
    check_mode, tmp_path: Path, monkeypatch, agent_db: Any,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(cursor_agent.cursor_login, "launch_problem", AsyncMock(return_value=None))
    monkeypatch.setattr(ttadk, "_CURSOR_SIGN_IN_TIMEOUT_SEC", 1.0)
    capture = tmp_path / "stdin.txt"
    _cursor_script(tmp_path / "cursor-agent", f"""
import json, os, pathlib, sys
assert 'TTADK_JWT_TOKEN' not in os.environ
assert os.environ['HOME'] == {os.environ['HOME']!r}
assert 'CI' not in os.environ
assert sys.argv[1] == '-p'
prompt = sys.stdin.read()
assert prompt.startswith('<looper_instructions>\\n') and '## How you work' in prompt
assert '</looper_instructions>\\n\\n' in prompt
assert not any('<looper_instructions>' in arg for arg in sys.argv)
pathlib.Path({str(capture)!r}).write_text(prompt)
report = json.dumps({{'status': 'completed', 'summary': 'through Cursor shim', 'changed_files': {{}}}})
for event in [{{'type': 'system', 'subtype': 'init', 'session_id': 'cursor-ttadk'}},
              {{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': report}}]}}}},
              {{'type': 'result', 'subtype': 'success', 'result': report}}]:
    print(json.dumps(event))
""")
    fake_claude(tmp_path / "ttadk", f"""
import os, sys, time
if sys.argv[1] == 'models':
    assert sys.argv[1:] == ['models', '-t', 'cursor-cli', '-f', 'json']
    if {check_mode!r} == 'timeout': time.sleep(3)
    if {check_mode!r} == 'signed-out': sys.exit(1)
    print('{{"models": []}}'); sys.exit(0)
assert sys.argv[1:6] == ['code', '-t', 'cursor-cli', '--skip-check', '--']
for line in ['TTADK banner', 'Preparing cursor-cli']: print(line, flush=True)
os.environ['TTADK_JWT_TOKEN'] = 'x'
os.execvp('agent', ['agent', *sys.argv[6:]])
""")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    (tmp_path / "work").mkdir()
    result = asyncio.run(asyncio.wait_for(_worker_run(tmp_path, monkeypatch, _launch(), gate=True), 15))
    if check_mode == "signed-out":
        assert result["status"] == "failed"
        assert ttadk.TtadkProblem.NOT_SIGNED_IN.message("cursor-agent") in result["error"]
        assert not capture.exists()
        agent_db.start_record.assert_not_awaited()
        return
    assert result["status"] == "completed" and result["summary"] == "through Cursor shim"
    assert capture.is_file()
    metadata = agent_db.start_record.await_args.kwargs["metadata"]
    assert metadata["launcher"]["tool"] == "cursor-cli" and metadata["personal_config"] == list(PERSONAL_CONFIG)
    if check_mode == "timeout":
        assert metadata["ttadk_check"] == "timed out or failed; the run started anyway"
    else:
        assert "ttadk_check" not in metadata


def test_wrong_ttadk_cursor_tool_refuses_worker_before_any_process(tmp_path: Path, monkeypatch, agent_db: Any) -> None:
    home = tmp_path / "operator"
    (home / ".ttadk").mkdir(parents=True)
    (home / ".ttadk/setting.json").write_text(json.dumps({"ai_tool_commands": {"cursor-cli": "other"}}))
    monkeypatch.setenv("HOME", str(home))
    def refuse(*args, **kwargs):
        pytest.fail("A mismatched TTADK tool must be rejected without spawning a process")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    monkeypatch.setattr(worker.proc_tree, "spawn", refuse)
    launch = _launch()
    assert CURSOR.ttadk_problem() is ttadk.TtadkProblem.WRONG_TOOL
    problem = asyncio.run(profile_launch_problem(launch.profile, AgentType.WORKER))
    assert "TTADK's settings make cursor-cli run `other`, so Looper cannot confirm it is Cursor's CLI" in problem
    assert ttadk.TtadkProblem.WRONG_TOOL.message("cursor-agent") in problem
    result = asyncio.run(asyncio.wait_for(_worker_run(tmp_path, monkeypatch, launch, gate=True), 5))
    assert result["status"] == "failed" and result["error"] == problem


def test_ttadk_cursor_zero_exit_without_result_keeps_error_but_drops_preamble(
    tmp_path: Path, monkeypatch, agent_db: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    _cursor_script(tmp_path / "cursor-agent", "")
    fake_claude(tmp_path / "ttadk", """
import json, sys
assert sys.argv[1:6] == ['code', '-t', 'cursor-cli', '--skip-check', '--']
sys.stdin.read()
print('Signed in as someone@example.com')
print('Launching Cursor CLI...')
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'cursor-ttadk'}))
print(json.dumps({'type': 'retry', 'subtype': 'starting', 'attempt': 1}))
print('Reconnecting someone@example.com')
print(json.dumps({'type': 'retry', 'subtype': 'resuming', 'attempt': 1}))
print('RetriableError: WritableIterable is closed', file=sys.stderr)
print('Error: command failed unexpectedly.', file=sys.stderr)
sys.exit(0)
""")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    (tmp_path / "work").mkdir()
    result = asyncio.run(asyncio.wait_for(_worker_run(tmp_path, monkeypatch, _launch()), 15))
    assert result["status"] == "failed"
    assert "Cursor Agent through TTADK ended without a result" in result["error"]
    assert result["error"].endswith("Error: command failed unexpectedly.")
    assert "RetriableError: WritableIterable is closed" in result["stderr"]
    finish = agent_db.finish_record.await_args.kwargs
    assert finish["error"] == result["error"] and finish["outcome"] == "run_failed"
    events = str(agent_db.append_events.await_args_list)
    assert "cursor-ttadk" in events and "cursor_retry" in events
    # Check every captured database write, including events, records and activities,
    # and every generated runtime file as well as Python logs and the returned failure.
    writes = [str(value.mock_calls) for value in vars(agent_db).values() if hasattr(value, "mock_calls")]
    files = [path.read_text() for path in (tmp_path / "runtime").rglob("*") if path.is_file()]
    for text in [str(result), caplog.text, *writes, *files]:
        assert "someone@example.com" not in text
        assert "Launching Cursor CLI..." not in text
