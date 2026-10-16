"""A completed Worker report is checked against where its files are: the working directory, the other worktrees of
its repository, and the commits made during the run (``looper.agents.reported_files``, read by ``judge_result``).

Every repository is a real git repository in a temporary directory, its commits from before the run dated a day
earlier.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import reported_files
from looper.agents import step_workspaces as ws
from looper.agents.bus import MessageBus
from looper.agents.tiers.worker import WorkerConfig, WorkerPool, judge_result
from tests.test_agent_planner_worker import _fake_claude, _work_item

_BEFORE_THE_RUN = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(cwd: Path | str, *args: str, date: str | None = None) -> str:
    env = {**os.environ, **({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {})}
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, check=True).stdout.rstrip()


def _repo(path: Path) -> Path:
    """A repository on main with ``a.txt`` and ``pkg/old.py``, committed before the run."""
    (path / "pkg").mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "User")
    git(path, "config", "user.email", "user@example.com")
    (path / "a.txt").write_text("a\n")
    (path / "pkg" / "old.py").write_text("old\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init", date=_BEFORE_THE_RUN)
    return path.resolve()


def _judge(work_dir: Path, changed: dict[str, str], deleted: list[str] | None = None, *,
           started_at: datetime | None) -> dict[str, Any]:
    report = {"status": "completed", "changed_files": changed, "deleted_files": deleted or [], "summary": "done"}
    event = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(report)}
    return judge_result(event, work_dir=work_dir, is_verify=False, started_at=started_at)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_an_absolute_path_inside_a_worktree_is_on_disk(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    started = _now()
    git(repo, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "wt"))
    (tmp_path / "wt" / "new.py").write_text("x\n")

    judged = _judge(repo, {str(tmp_path / "wt" / "new.py"): "added"}, started_at=started)
    assert judged["status"] == "completed"
    assert "found_elsewhere" not in judged  # where the report says


@pytest.mark.parametrize("subdir", ["", "pkg"])
def test_a_relative_path_from_a_worktree_is_found_there(tmp_path: Path, subdir: str) -> None:
    # The working directory stays on main; the Worker made the change in a worktree of its own, uncommitted.
    repo = _repo(tmp_path / "repo")
    started = _now()
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    (worktree / subdir / "new.py").write_text("x\n")

    judged = _judge(repo / subdir, {"new.py": "added"}, started_at=started)
    assert judged["status"] == "completed"
    assert judged["found_elsewhere"] == {"new.py": f"in worktree {worktree.resolve()}"}


def _commit_in_worktree_then_remove(repo: Path, tmp_path: Path) -> str:
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    (worktree / "new.py").write_text("x\n")
    (worktree / "pkg" / "new.py").write_text("x\n")
    git(worktree, "add", "new.py", "pkg/new.py")
    git(worktree, "commit", "-qm", "add new.py")
    sha = git(worktree, "rev-parse", "HEAD")
    git(repo, "worktree", "remove", str(worktree))
    return sha


def _commit_then_switch_away(repo: Path, tmp_path: Path) -> str:
    git(repo, "switch", "-q", "-c", "feature")
    (repo / "new.py").write_text("x\n")
    git(repo, "add", "new.py")
    git(repo, "commit", "-qm", "add new.py")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    return sha


def _push_then_delete_the_branch(repo: Path, tmp_path: Path) -> str:
    git(tmp_path, "init", "-q", "--bare", "origin.git")
    git(repo, "remote", "add", "origin", str(tmp_path / "origin.git"))
    sha = _commit_in_worktree_then_remove(repo, tmp_path)
    git(repo, "push", "-q", "origin", "feature")
    git(repo, "fetch", "-q", "origin")
    git(repo, "branch", "-q", "-D", "feature")  # only origin/feature has it now
    return sha


@pytest.mark.parametrize(("make", "absolute"), [
    (_commit_in_worktree_then_remove, False),
    (_commit_then_switch_away, False),
    (_push_then_delete_the_branch, False),
    (_commit_in_worktree_then_remove, True),  # reported by its path in the worktree, which is gone
])
def test_a_file_committed_during_the_run_counts_when_it_is_no_longer_on_disk(
    tmp_path: Path, make: Any, absolute: bool,
) -> None:
    repo = _repo(tmp_path / "repo")
    started = _now()
    sha = make(repo, tmp_path)
    reported = str(tmp_path / "wt" / "pkg" / "new.py") if absolute else "new.py"
    assert not (repo / "new.py").exists() and not (tmp_path / "wt").exists()

    judged = _judge(repo, {reported: "added"}, started_at=started)
    assert judged["status"] == "completed"
    assert judged["found_elsewhere"] == {reported: f"in commit {sha[:12]}"}


@pytest.mark.parametrize(("committed", "reported"), [
    ("README.md", "README.md"),
    ("README.md", "pkg/README.md"),
    ("docs/README.md", "README.md"),
])
def test_an_absolute_path_is_not_matched_by_its_file_name_alone(tmp_path: Path, committed: str, reported: str) -> None:
    # The run committed an unrelated file of the same name; the reported one, in a worktree that is gone, is not it.
    repo = _repo(tmp_path / "repo")
    started = _now()
    git(repo, "switch", "-q", "-c", "docs")
    (repo / committed).parent.mkdir(parents=True, exist_ok=True)
    (repo / committed).write_text("readme\n")
    git(repo, "add", committed)
    git(repo, "commit", "-qm", f"add {committed}")
    git(repo, "switch", "-q", "main")
    gone = tmp_path / "gone-worktree" / reported

    judged = _judge(repo, {str(gone): "added"}, started_at=started)
    assert judged["status"] == "failed"
    assert f"changed files not found: {gone}" in judged["error"]


def test_a_file_found_nowhere_fails_and_says_where_it_looked(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    started = _now()
    sha = _commit_then_switch_away(repo, tmp_path)
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "feature")

    judged = _judge(repo, {"new.py": "added", "ghost.py": "added"}, started_at=started)
    assert judged["status"] == "failed"
    since = (started - reported_files.CLOCK_SLACK).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert judged["error"] == (
        "Worker report does not match the files: changed files not found: ghost.py (looked in the working "
        f"directory; in 1 other worktree of its repository: {(tmp_path / 'wt').resolve()}; in 1 commit made on any "
        f"branch, tag or remote-tracking branch since {since})"
    )
    assert sha  # new.py was found in the worktree, so only ghost.py is missing


def _delete_in_worktree(repo: Path, tmp_path: Path) -> None:
    git(repo, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "wt"))
    (tmp_path / "wt" / "a.txt").unlink()  # not committed


def _delete_by_commit_then_remove(repo: Path, tmp_path: Path) -> None:
    git(repo, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "wt"))
    git(tmp_path / "wt", "rm", "-q", "a.txt")
    git(tmp_path / "wt", "commit", "-qm", "remove a.txt")
    git(repo, "worktree", "remove", str(tmp_path / "wt"))


def _absent_where_it_never_was(repo: Path, tmp_path: Path) -> None:
    # A worktree on a branch that never had a.txt: its absence there is not a deletion.
    git(repo, "switch", "-q", "--orphan", "other")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-qm", "other", date=_BEFORE_THE_RUN)
    git(repo, "switch", "-q", "main")
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "other")


def _worktree_directory_gone(repo: Path, tmp_path: Path) -> None:
    # Its directory deleted without git: the worktree is listed (prunable), and a.txt is absent from nothing.
    git(repo, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "wt"))
    shutil.rmtree(tmp_path / "wt")
    git(repo, "worktree", "add", "-q", "-b", "other", str(tmp_path / "wt2"))


@pytest.mark.parametrize(("make", "accepted"), [
    (_delete_in_worktree, True),
    (_delete_by_commit_then_remove, True),
    (_absent_where_it_never_was, False),
    (_worktree_directory_gone, False),
])
def test_a_deleted_file_still_in_the_working_directory(tmp_path: Path, make: Any, accepted: bool) -> None:
    repo = _repo(tmp_path / "repo")
    started = _now()
    make(repo, tmp_path)
    assert (repo / "a.txt").exists()

    judged = _judge(repo, {}, ["a.txt"], started_at=started)
    if accepted:
        assert judged["status"] == "completed"
        assert judged["found_elsewhere"]["a.txt"].startswith("deleted in ")
    else:
        assert judged["status"] == "failed"
        assert judged["error"].startswith("Worker report does not match the files: deleted files still present: "
                                          "a.txt (looked in the working directory; in 1 other worktree")


def test_a_work_dir_outside_git_is_checked_on_disk_only(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("b\n")
    assert subprocess.run(["git", "rev-parse"], cwd=tmp_path, capture_output=True).returncode != 0

    judged = _judge(tmp_path, {"a.txt": "added"}, ["b.txt"], started_at=_now())
    assert judged["error"] == (
        "Worker report does not match the files: changed files not found: a.txt; deleted files still present: b.txt"
    )


@pytest.mark.parametrize("dated", [True, False])
def test_only_a_commit_made_during_the_run_counts(tmp_path: Path, dated: bool) -> None:
    repo = _repo(tmp_path / "repo")
    git(repo, "switch", "-q", "-c", "feature")
    (repo / "new.py").write_text("x\n")
    git(repo, "add", "new.py")
    git(repo, "commit", "-qm", "add new.py", date=_BEFORE_THE_RUN if dated else None)
    git(repo, "switch", "-q", "main")

    # Committed a day before the run started; or during it, but the run's start is unknown.
    judged = _judge(repo, {"new.py": "added"}, started_at=_now() if dated else None)
    assert judged["status"] == "failed"
    assert "changed files not found: new.py" in judged["error"]


def test_a_step_workspace_is_the_working_directory(tmp_path: Path) -> None:
    # Looper's own worktree mode: the Worker runs in the step's worktree, at the task directory's place in it.
    repo = _repo(tmp_path / "repo")
    started = _now()
    found = asyncio.run(ws.inspect(str(repo / "pkg")))
    assert isinstance(found, ws.Repo) and found.subdir == "pkg"
    workspace = ws.new_workspace(found, runtime_root=tmp_path / "runtime", workspace_id=1, task_id="t1",
                                 step_id="s1", step_key="s1", base=found.head, checkpointed=False)
    asyncio.run(ws.ensure_worktree(workspace))
    work_dir = Path(workspace.work_dir)
    (work_dir / "new.py").write_text("x\n")
    git(work_dir, "add", "new.py")
    git(work_dir, "commit", "-qm", "add new.py")
    (work_dir / "draft.py").write_text("x\n")  # left uncommitted
    (work_dir / "old.py").unlink()

    judged = _judge(work_dir, {"new.py": "added", "draft.py": "added"}, ["old.py"], started_at=started)
    assert judged["status"] == "completed"
    assert "found_elsewhere" not in judged


@pytest.mark.parametrize("bound", ["worktrees", "commits", "timeout"])
def test_the_search_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str) -> None:
    repo = _repo(tmp_path / "repo")
    started = _now()
    if bound == "worktrees":
        monkeypatch.setattr(reported_files, "MAX_WORKTREES", 0)
        git(repo, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "wt"))
        (tmp_path / "wt" / "new.py").write_text("x\n")
        expected = "in the first 0 of 1 other worktree of its repository"
    elif bound == "commits":
        monkeypatch.setattr(reported_files, "MAX_COMMITS", 1)
        _commit_then_switch_away(repo, tmp_path)
        later = (_now() + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        git(repo, "commit", "-q", "--allow-empty", "-m", "later", date=later)  # the newest: the only one read
        expected = "in the latest 1 commit made"
    else:
        monkeypatch.setattr(reported_files, "GIT_TIMEOUT_SEC", 1e-6)
        _commit_then_switch_away(repo, tmp_path)
        expected = None  # git does not answer in time: the disk is the only evidence

    judged = _judge(repo, {"new.py": "added"}, started_at=started)
    assert judged["status"] == "failed"
    if expected:
        assert expected in judged["error"]
    else:
        assert judged["error"] == "Worker report does not match the files: changed files not found: new.py"


# A Worker that works in a git worktree of its own outside the working directory, commits there, removes the
# worktree and reports the file relative to the working directory.
_WORKTREE_WORKER = """\
import subprocess
sys.stdin.readline()
def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)
outside = os.path.join(os.path.dirname(os.getcwd()), "worker-wt")
git("worktree", "add", "-q", "-b", "notes", outside)
open(os.path.join(outside, "notes.txt"), "w").write("notes\\n")
git("-C", outside, "add", "notes.txt")
git("-C", outside, "commit", "-qm", "add notes")
git("worktree", "remove", outside)
report = {"status": "completed", "changed_files": {"notes.txt": "added"}, "summary": "wrote the notes"}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(report)}))
"""


def test_a_worker_that_committed_in_a_worktree_it_removed_completes(tmp_path: Path, agent_db: Any) -> None:
    repo = _repo(tmp_path / "repo")
    claude = _fake_claude(tmp_path, _WORKTREE_WORKER)

    async def _run() -> dict[str, Any]:
        bus = MessageBus(runtime_root=tmp_path)
        pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=tmp_path / "runtime")
        pool._bus = bus
        envelope = _work_item(repo)
        envelope.payload["parent_task_id"] = "task-1"
        await pool._run_worker(envelope)
        return bus.get_queue("planner-t1").get_nowait().payload

    reply = asyncio.run(_run())
    assert reply["status"] == "completed", reply.get("error")
    sha = git(repo, "rev-parse", "notes")
    assert reply["found_elsewhere"] == {"notes.txt": f"in commit {sha[:12]}"}
    summaries = [call.kwargs["summary"] for call in agent_db.insert_activity.await_args_list]
    assert f"Reported files found outside the working directory: notes.txt in commit {sha[:12]}" in summaries
