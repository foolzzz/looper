"""The consult contract, prompt bounds and session cleanup, without a database."""

from __future__ import annotations

import asyncio
import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from looper.agents.consult import CONSULT_CONTRACT, ConsultService, consult_answers, consult_prompt
from looper.agents.system_prompt import REVIEW_CONTRACT, WORK_CONTRACT


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def test_the_server_recovers_consults_before_starting_agents_tasks_or_http() -> None:
    import looper.agents.serve as serve

    tree = ast.parse(Path(serve.__file__).read_text())
    run = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
               and node.name == "_run_agent_system")
    calls = [node.value.value.func for node in run.body if isinstance(node, ast.Expr)
             and isinstance(node.value, ast.Await) and isinstance(node.value.value, ast.Call)]
    names = [ast.unparse(call) for call in calls]
    assert names.count("consults.resume_all") == 1
    assert all(names.index("consults.resume_all") < names.index(name)
               for name in ("supervisor.start_all", "carry_on_tasks", "http_server.start"))


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
