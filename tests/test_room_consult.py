"""The consult contract, prompt bounds and session cleanup, without a database."""

from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.consult import CONSULT_CONTRACT, ConsultService, consult_answers, consult_prompt
from looper.agents.system_prompt import REVIEW_CONTRACT, WORK_CONTRACT


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def test_the_server_listens_before_recovering_consults_and_starting_agents_or_tasks() -> None:
    import looper.agents.serve as serve

    tree = ast.parse(Path(serve.__file__).read_text())
    # The entry point owns cleanup; the owned startup performs recovery and starts the services.
    run = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
               and node.name == "_run_agent_system_owned")
    # Startup cleanup guards keep their normal execution order; exception handlers are not startup.
    body = [statement for node in run.body
            for statement in (node.body if isinstance(node, ast.Try) else [node])]
    calls = [node.value.value.func for node in body if isinstance(node, ast.Expr)
             and isinstance(node.value, ast.Await) and isinstance(node.value.value, ast.Call)]
    names = [ast.unparse(call) for call in calls]
    # Recovered consults and replayed agent turns can call platform tools immediately.
    startup = ["http_server.start", "consults.resume_all", "supervisor.start_all", "carry_on_tasks"]
    assert [name for name in names if name in startup] == startup


def test_the_consult_contract_replaces_the_runs_and_its_answers_are_read_by_question() -> None:
    assert consult_prompt("x\n\n" + WORK_CONTRACT) == "x\n\n" + CONSULT_CONTRACT
    assert consult_prompt("x\n\n" + REVIEW_CONTRACT) == "x\n\n" + CONSULT_CONTRACT
    assert consult_prompt("no contract") == "no contract\n\n" + CONSULT_CONTRACT
    text = 'ok {"answers": [{"reply_to": 3, "body": "qty"}, {"reply_to": 9, "body": "not asked"}, ' \
           '{"reply_to": 4, "body": " "}], "unsure": "the port"}'
    assert consult_answers(text, [3, 4]) == ({3: "qty"}, "the port")
    with pytest.raises(ValueError):
        consult_answers("I think qty.", [3])


def test_a_throwaway_checkout_shows_what_the_consult_changed_and_goes(tmp_path: Path) -> None:
    from looper.agents.consult import _changed, _cleanup, _snapshot

    repo = tmp_path / "repo"
    _repo(repo)
    snapshot = _snapshot(repo / "app", tmp_path / "consults" / "consult-1")
    assert snapshot is not None
    _, target, inside = snapshot
    assert inside == target / "app" and (inside / "a.py").read_text() == "x = 1\n"
    assert _changed(inside) == []
    (inside / "b.py").write_text("y = 2\n")
    assert _changed(inside) == ["app/b.py"]
    _cleanup(snapshot, tmp_path / "fork")
    assert not target.exists()
    assert "consult-1" not in subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True,
                                             text=True).stdout
    assert _snapshot(tmp_path / "plain", tmp_path / "consults" / "consult-2") is None


def test_the_questions_share_one_prompt_allowance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import room as db_room

    async def messages(*args: Any) -> list[Any]:
        return []

    monkeypatch.setattr(db_room, "messages", messages)
    service = ConsultService(room=None, worker_pool=None, runtime_root=tmp_path)
    questions = [{"id": i, "sender_seat": "worker@s2", "body": "x" * 20_000} for i in range(1, 11)]
    prompt = asyncio.run(service._prompt("task", "worker@s1", {"id": "s1", "status": "verified"}, questions))
    assert len(prompt) < 23_000
    assert all(f"- #{i} from worker@s2:" in prompt for i in range(1, 11))
    assert prompt.count("…") == 10


@pytest.mark.parametrize("exit_code", [0, 7])
def test_dsh_consult_verifies_the_process_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                             exit_code: int) -> None:
    from dataclasses import replace
    from looper.agents import claude_cli
    from looper.agents.system_prompt import SystemPrompt
    from tests.test_dsh_runtime import _run_launch

    saved = {}

    class Recorder:
        cap_stop = None

        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        async def record(self, event):
            pass

        async def finish(self, **kwargs):
            saved.update(kwargs)

    monkeypatch.setattr(claude_cli, "ClaudeRunRecorder", Recorder)
    payload = (Path(__file__).parent / "fixtures/dsh_json/success.jsonl").read_bytes()
    # The same valid final must succeed only after a verified zero exit.
    script = ("import sys; sys.stdin.read(); "
              f"sys.stdout.buffer.write({payload!r}); sys.stdout.flush(); sys.exit({exit_code})")
    service = ConsultService(room=None, worker_pool=None, runtime_root=tmp_path)
    launch = replace(_run_launch(tmp_path), consult=True)
    result, started, timed_out = asyncio.run(service._execute(
        "task", {"id": "step"}, "consult-test", [sys.executable, "-c", script], str(tmp_path),
        "Answer the question", SystemPrompt(CONSULT_CONTRACT, {}), launch, 5))
    assert started and not timed_out
    assert result is not None and result["is_error"] is (exit_code != 0)
    assert saved["result_event"] == result
    assert not any("exit was not verified" in error for error in result.get("errors", []))


def _pi_launch(**kwargs: Any) -> Any:
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.profile_registry import CatalogModel, LegacyProviderSpec as ProviderSpec, RuntimeSpec
    from tests.runtime_model_helpers import resolved_profile
    from looper.agents.runtimes import ApiProtocol, AuthKind, Capability

    profile = resolved_profile("pi-test", "pi-test", RuntimeSpec("pi", "pi", "pi", capabilities=frozenset({
        Capability.READ_TOOLS})), ProviderSpec(
            "test", "test", auth=AuthKind.API_KEY, api_key="test-key",
            protocols=frozenset({ApiProtocol.OPENAI_RESPONSES}), base_url="https://pi.example.invalid/v1",
            catalog=(CatalogModel("selected", input_usd_per_mtok=1, output_usd_per_mtok=2,
                                  cache_read_usd_per_mtok=0, cache_write_usd_per_mtok=0),)), "selected")
    return Launch(AgentSettings(profile=profile), **kwargs)


def _pi_session(directory: Path, session_id: str = "source-session", name: str = "timestamp_session.jsonl") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps({"type": "session", "id": session_id}) + '\n{"type":"message","text":"history"}\n')
    return path


def _digests(directory: Path) -> dict[str, str]:
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def test_a_pi_consult_forks_only_the_requested_session_copy(tmp_path: Path) -> None:
    from fake_claude import fake_claude

    launch = _pi_launch(consult=True)
    source = tmp_path / "source"
    session = _pi_session(source)
    _pi_session(source, "unrelated-session", "other.jsonl")
    (source / "settings.json").write_text("private state that must not be copied")
    before = _digests(source)
    original_stat = session.stat()
    prompt = tmp_path / ".local" / "prompts" / "consult.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("consult instructions")
    fork = tmp_path / "fork"
    executable = fake_claude(tmp_path / "pi", '''
import json, os, pathlib, sys, uuid
args = sys.argv[1:]
def flag(name):
    return args[args.index(name) + 1]
directory = pathlib.Path(flag('--session-dir'))
session = pathlib.Path(flag('--fork'))
assert session.is_absolute() and session.parent == directory
assert os.environ['PI_CODING_AGENT_SESSION_DIR'] == str(directory)
assert not any(x in args for x in ('--no-session', '--resume', '--session-id', '--session', '--continue'))
assert set(flag('--tools').split(',')) == {'read', 'grep', 'find', 'ls', 'mcp__looper__*'}
rows = [json.loads(line) for line in session.read_text().splitlines()]
assert rows[0]['id'] == 'source-session'
fork_id = str(uuid.uuid4())
rows[0].update(id=fork_id, parentSession=str(session), cwd=os.getcwd())
fork = directory / (fork_id + '.jsonl')
fork.write_text(''.join(json.dumps(row) + '\\n' for row in rows))
with fork.open('a') as saved:
    saved.write(json.dumps({'text': sys.stdin.read()}) + '\\n')
''')
    command = launch.runtime.consult_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch,
                                             session_id="source-session", session_dir=source, fork_dir=fork)
    assert list(fork.iterdir()) == [fork / session.name]
    assert (fork / session.name).read_bytes() == session.read_bytes()
    assert (fork / session.name).stat().st_mode & 0o777 == 0o400
    subprocess.run(command, cwd=tmp_path, env=launch.environment(), input="question", text=True, check=True,
                   capture_output=True, timeout=10)
    assert (fork / session.name).read_bytes() == session.read_bytes()
    forked = next(path for path in fork.iterdir() if path.name != session.name)
    assert json.loads(forked.read_text().splitlines()[0])["id"] != "source-session"
    assert json.loads(forked.read_text().splitlines()[-1]) == {"text": "question"}
    assert _digests(source) == before
    assert session.stat().st_mtime_ns == original_stat.st_mtime_ns


@pytest.mark.parametrize("missing", ["directory", "session", "prefix", "ambiguous", "linked-file", "linked-directory"])
def test_a_pi_consult_requires_one_exact_private_session(tmp_path: Path, missing: str) -> None:
    from looper.agents.runtimes import RuntimeUnsupported

    launch = _pi_launch(consult=True)
    source = tmp_path / "source"
    if missing != "directory":
        session_id = {"session": "another-session", "prefix": "source-session-longer"}.get(missing, "source-session")
        session = _pi_session(source, session_id)
        if missing == "ambiguous":
            _pi_session(source, name="duplicate.jsonl")
        elif missing == "linked-file":
            session.rename(tmp_path / "outside.jsonl")
            session.symlink_to(tmp_path / "outside.jsonl")
        elif missing == "linked-directory":
            source.rename(tmp_path / "outside")
            source.symlink_to(tmp_path / "outside", target_is_directory=True)
    fork = tmp_path / "fork"
    before = _digests(tmp_path)
    with pytest.raises(RuntimeUnsupported, match="session"):
        launch.runtime.consult_command("pi", cwd=tmp_path, prompt_file=tmp_path / "unused.md", launch=launch,
                                       session_id="source-session", session_dir=source, fork_dir=fork)
    assert not fork.exists() and _digests(tmp_path) == before


@pytest.mark.parametrize("failure", ["copy", "command"])
def test_a_pi_consult_reclaims_a_partial_fork_when_preparation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from looper.agents.runtimes import pi

    launch = _pi_launch(consult=True)
    source = tmp_path / "source"
    _pi_session(source)
    before = _digests(source)
    fork = tmp_path / "fork"

    def fail_copy(source: Path, target: Path) -> None:
        target.write_text("partial copy")
        raise OSError("copy failed")

    def fail_command(*args: Any, **kwargs: Any) -> None:
        assert (fork / "timestamp_session.jsonl").is_file()
        raise OSError("command failed")

    if failure == "copy":
        monkeypatch.setattr(pi.shutil, "copyfile", fail_copy)
    else:
        monkeypatch.setattr(launch.runtime, "run_command", fail_command)
    with pytest.raises(OSError, match=f"{failure} failed"):
        launch.runtime.consult_command("pi", cwd=tmp_path, prompt_file=tmp_path / "unused.md", launch=launch,
                                       session_id="source-session", session_dir=source, fork_dir=fork)
    assert not fork.exists() and _digests(source) == before


@pytest.mark.parametrize("role", ["planner", "worker", "reviewer"])
def test_pi_limits_tools_only_for_planners_and_consults(tmp_path: Path, role: str) -> None:
    from looper.agents.agent_settings import AgentType
    from looper.agents.runtimes import ReadOnly

    launch = _pi_launch(role=AgentType(role))
    assert launch.read_only_mode is ReadOnly.TOOLS
    prompt = tmp_path / ".local" / "prompts" / "test.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("instructions")
    for consult in (False, True):
        selected = replace(launch, consult=consult)
        command = selected.runtime.run_command("pi", cwd=tmp_path, prompt_file=prompt, launch=selected)
        if consult or role == "planner":
            assert set(command[command.index("--tools") + 1].split(",")) == {
                "read", "grep", "find", "ls", "mcp__looper__*"}
        else:
            assert "--tools" not in command


@pytest.mark.parametrize("git_checkout", [False, True])
@pytest.mark.parametrize("outcome", ["answer", "exception", "cancel", "missing-session", "failed-run"])
def test_pi_consult_service_cleans_up_and_keeps_source_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, git_checkout: bool,
) -> None:
    from looper.agents import consult
    from looper.agents.agent_settings import AgentType
    from looper.agents.room import FallbackReason, MemberKind

    repo = tmp_path / "repo"
    if git_checkout:
        _repo(repo)
    else:
        (repo / "app").mkdir(parents=True)
        (repo / "app" / "a.py").write_text("x = 1\n")
    launch = _pi_launch(role=AgentType.WORKER)
    room = SimpleNamespace(finish_consult=AsyncMock())
    service = ConsultService(room=room, worker_pool=None, runtime_root=tmp_path)
    source = tmp_path / ".local" / "pi" / "run-sessions" / "task" / "worker-1"
    session = _pi_session(source, "other" if outcome == "missing-session" else "source-session")
    before = _digests(source)
    row = {"source_record": 1, "source_run": "worker-1", "questions": [17]}
    record = {"id": 1, "status": "completed", "session_id": "source-session",
              "metadata": {"profile": launch.profile.record(), "cwd": str(repo / "app"),
                           "system_prompt": WORK_CONTRACT}}
    monkeypatch.setattr(consult.db_room, "get_consult", AsyncMock(return_value=row))
    monkeypatch.setattr(consult.db_room, "run_record", AsyncMock(return_value=record))
    monkeypatch.setattr(consult.db_room, "latest_run", AsyncMock(return_value=record))
    monkeypatch.setattr(consult.db_room, "get", AsyncMock(return_value={
        "id": 17, "sender_seat": "worker@s2", "body": "question"}))
    monkeypatch.setattr(consult.db_room, "messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(consult.db_tasks, "get_task", AsyncMock(return_value={"work_dir": str(repo / "app")}))
    monkeypatch.setattr(consult, "load_agent_settings", AsyncMock(return_value=launch.settings))
    monkeypatch.setattr(consult, "_run_cost", AsyncMock(return_value=0.01))
    monkeypatch.setattr(service, "_note", AsyncMock())
    fork = tmp_path / ".local" / "pi" / "consult-sessions" / "task" / "consult-1"
    snapshot = tmp_path / ".local" / "consults" / "consult-1"

    async def execute(*args: Any) -> Any:
        command, cwd = args[3], Path(args[4])
        assert cwd == repo / "app"
        assert not snapshot.exists()
        copied = Path(command[command.index("--fork") + 1])
        assert copied == fork / session.name and copied.read_bytes() == session.read_bytes()
        (fork / "new-session.jsonl").write_text(copied.read_text() + '{"text":"consult"}\n')
        if outcome == "exception":
            raise OSError("execution failed")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        final = {"result": '{"answers":[{"reply_to":17,"body":"answer"}]}'}
        return ({**final, "is_error": True} if outcome == "failed-run" else final), True, False

    executed = AsyncMock(side_effect=execute)
    monkeypatch.setattr(service, "_execute", executed)

    async def run() -> None:
        if outcome == "exception":
            expected = pytest.raises(OSError, match="execution failed")
        elif outcome == "cancel":
            expected = pytest.raises(asyncio.CancelledError)
        elif outcome in ("missing-session", "failed-run"):
            expected = pytest.raises(consult._Failed)
        else:
            await service._consult(1, "task", "worker@s1", MemberKind.WORK, AgentType.WORKER,
                                   {"id": "s1", "step_key": "s1", "status": "verified"}, "consult-1")
            assert room.finish_consult.await_args.kwargs["state"] == "answered"
            assert room.finish_consult.await_args.kwargs["columns"]["read_only"] == "runtime"
            return
        with expected as error:
            await service._consult(1, "task", "worker@s1", MemberKind.WORK, AgentType.WORKER,
                                   {"id": "s1", "step_key": "s1", "status": "verified"}, "consult-1")
        if outcome == "missing-session":
            assert error.value.reason is FallbackReason.RUNTIME_CANNOT
            executed.assert_not_awaited()
        if outcome == "failed-run":  # a valid final of a failed run is not delivered: it falls back
            assert error.value.reason is FallbackReason.CONTRACT
        room.finish_consult.assert_not_awaited()

    asyncio.run(run())
    assert _digests(source) == before
    assert (repo / "app" / "a.py").read_text() == "x = 1\n"
    assert not fork.exists() and not snapshot.exists()
    if git_checkout:
        assert "consult-1" not in subprocess.run(["git", "worktree", "list"], cwd=repo,
                                                 capture_output=True, text=True, check=True, timeout=10).stdout


def _repo(repo: Path) -> None:
    """A temporary repository with app/a.py in its first tree."""
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.py").write_text("x = 1\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args: str, text: str = "") -> str:
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo, env=env, input=text,
                              capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q")
    blob = git("hash-object", "-w", "--stdin", text="x = 1\n")
    app = git("mktree", text=f"100644 blob {blob}\ta.py\n")
    tree = git("mktree", text=f"040000 tree {app}\tapp\n")
    commit = git("commit-tree", tree, text="init\n")
    git("update-ref", "HEAD", commit)



def test_dsh_room_sessions_are_managed_and_cleanup_keeps_active_tasks(tmp_path: Path) -> None:
    from looper.agents.room import remove_run_sessions, run_session_dir, sweep_run_sessions

    directories = {}
    for task in ("active", "recent", "expired"):
        directory = run_session_dir(tmp_path, "dsh", task, "worker-1")
        assert directory is not None
        directory.mkdir(parents=True)
        (directory / "history").write_bytes(b"opaque session")
        directories[task] = directory
    owners = tmp_path / ".local/dsh/owners/resident"
    owners.mkdir(parents=True)
    removed = sweep_run_sessions(tmp_path, keep=["active", "recent"])
    assert removed == [directories["expired"].parent]
    assert directories["active"].is_dir() and directories["recent"].is_dir() and owners.is_dir()
    remove_run_sessions(tmp_path, "recent")
    assert not directories["recent"].exists()
    assert directories["active"].is_dir() and owners.is_dir()


def test_dsh_room_cleanup_does_not_follow_a_linked_managed_parent(tmp_path: Path) -> None:
    from looper.agents.room import remove_run_sessions, sweep_run_sessions

    outside = tmp_path / "outside"
    (outside / "expired").mkdir(parents=True)
    (outside / "expired/history").write_bytes(b"untouched")
    parent = tmp_path / ".local/dsh"
    parent.mkdir(parents=True)
    (parent / "run-sessions").symlink_to(outside, target_is_directory=True)
    assert sweep_run_sessions(tmp_path, keep=[]) == []
    remove_run_sessions(tmp_path, "expired")
    assert (outside / "expired/history").read_bytes() == b"untouched"


def test_a_failed_run_with_a_valid_final_is_not_delivered_as_an_answer(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path) -> None:
    from dataclasses import replace
    from looper.agents import claude_cli
    from looper.agents.consult import _unsuccessful
    from looper.agents.system_prompt import SystemPrompt
    from tests.test_dsh_runtime import _run_launch

    class Recorder:
        cap_stop = None

        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        async def record(self, event):
            pass

        async def finish(self, **kwargs):
            pass

    monkeypatch.setattr(claude_cli, "ClaudeRunRecorder", Recorder)
    payload = (Path(__file__).parent / "fixtures/dsh_json/success.jsonl").read_bytes()
    script = f"import sys; sys.stdin.read(); sys.stdout.buffer.write({payload!r}); sys.stdout.flush(); sys.exit(7)"
    service = ConsultService(room=None, worker_pool=None, runtime_root=tmp_path)
    launch = replace(_run_launch(tmp_path), consult=True)
    result, _, _ = asyncio.run(service._execute(
        "task", {"id": "step"}, "consult-test", [sys.executable, "-c", script], str(tmp_path),
        "Answer the question", SystemPrompt(CONSULT_CONTRACT, {}), launch, 5))
    assert result is not None and result["result"]  # the final survives the failed exit
    assert _unsuccessful(result) is not None
    assert _unsuccessful({"is_error": False, "subtype": "error_max_turns"}) is not None
    assert _unsuccessful({"is_error": False, "subtype": "success"}) is None
