"""The consult contract, prompt bounds and session cleanup, without a database."""

from __future__ import annotations

import asyncio
import os
import subprocess
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

