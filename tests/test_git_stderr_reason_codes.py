"""What git prints in a task repository never leaves Looper as text.

Git's messages can quote file content that Looper does not control. Each test makes git say a marker that is
only in a broken file git reads (or, where one command is made to fail,
prints it from that command), drives a path that runs git, and looks for the
marker in everything that path hands on: the error or returned detail, bus
messages, activity, the Planner's prompts and turns, the stored rows, the log
and the process's own output. Each also expects the typed reason in its place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import project_move, reported_files, step_workspaces as ws, task_workspaces as tw
from looper.agents.git_safe import GitFailure
from looper.agents.message import MessageType
from looper.agents.project_execution import TaskWorkspaces
from looper.agents.tiers.worker import judge_result
from test_planner_agent import REVIEW_PASS, WORK_OK, Harness, activity, decide, drive, plan, step

MARKER = "OUTSIDE-FILE-LINE-7f3a91c4"


@pytest.fixture(autouse=True)
def _git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def nothing_printed(caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> Any:
    """Neither the log nor the process's output carries the marker, whatever the test did."""
    caplog.set_level(logging.DEBUG)
    yield
    printed = capfd.readouterr()
    for phase in ("setup", "call"):
        for record in caplog.get_records(phase):
            text = record.getMessage() + (logging.Formatter().formatException(record.exc_info)
                                          if record.exc_info else "")
            assert MARKER not in text
    assert MARKER not in printed.out + printed.err


def git(cwd: Path | str, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args], cwd=cwd,
                          capture_output=True, text=True, check=True, timeout=10).stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "User")
    git(path, "config", "user.email", "user@example.com")
    (path / "shared.txt").write_text("a\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


def plant(repo: Path) -> None:
    """Make the repository's ``packed-refs`` a broken file whose line holds the marker: git quotes it."""
    (repo / ".git" / "packed-refs").write_text(f"password {MARKER}\n")


async def _save(_: Any) -> None:
    pass


def step_workspace(repo: Path, root: Path) -> ws.StepWorkspace:
    inspected = asyncio.run(ws.inspect(str(repo)))
    assert isinstance(inspected, ws.Repo)
    created = ws.new_workspace(inspected, runtime_root=root, workspace_id=1, task_id="task-1", step_id="step-1",
                               step_key="s1", base=inspected.head, checkpointed=False)
    asyncio.run(ws.ensure_worktree(created))
    (Path(created.work_dir) / "one.txt").write_text("one\n")
    return created


def clean(*handed_on: Any) -> str:
    text = "\n".join(str(item) for item in handed_on)
    assert MARKER not in text
    return text


def test_the_planted_file_makes_git_quote_it(tmp_path: Path) -> None:
    """The positive control: without it every absence below would prove nothing."""
    repo = init_repo(tmp_path / "repo")
    plant(repo)
    said = subprocess.run(["git", "status"], cwd=repo, capture_output=True, text=True, timeout=10)
    assert said.returncode != 0 and MARKER in said.stderr


def test_a_failure_is_its_subcommand_and_exit_code() -> None:
    assert str(GitFailure.of(("--no-optional-locks", "status", "--porcelain"), 128)) == "git_failed:status (exit 128)"
    assert str(GitFailure.of(("--version",), 1)) == "git_failed:command (exit 1)"


# --- Real git in a repository with the planted file ---


@pytest.mark.parametrize("ref_gone", [False, True], ids=["branch", "new-branch"])
def test_making_a_step_worktree(tmp_path: Path, ref_gone: bool) -> None:
    repo = init_repo(tmp_path / "repo")
    created = step_workspace(repo, tmp_path / "rt")
    git(repo, "worktree", "remove", "--force", created.path)
    if ref_gone:
        git(repo, "branch", "-D", created.branch)
    plant(repo)
    with pytest.raises(ws.GitError) as raised:
        asyncio.run(ws.ensure_worktree(created))
    assert "git_failed:worktree (exit " in clean(raised.value)


def test_merging_a_step(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    created = step_workspace(repo, tmp_path / "rt")
    plant(repo)
    rows: list[dict[str, Any]] = []

    async def save(current: ws.StepWorkspace) -> None:
        rows.append(current.row())

    outcome = asyncio.run(ws.merge(created, step_key="s1", title="one", save=save))
    assert outcome.workspace.merge_error is ws.MergeError.GIT_FAILED
    clean(outcome, outcome.workspace.view(), rows)
    assert outcome.workspace.detail.startswith("git_failed:")


def test_freezing_a_step_for_review(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    created = step_workspace(repo, tmp_path / "rt")
    plant(repo)
    with pytest.raises(ws.GitError) as raised:
        asyncio.run(ws.freeze_review(created, step_key="s1"))
    assert "git_failed:" in clean(raised.value)


def test_inspecting_and_snapshotting_the_task_directory(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    inspected = asyncio.run(ws.inspect(str(repo)))
    assert isinstance(inspected, ws.Repo)
    (repo / "pending.txt").write_text("pending\n")
    plant(repo)
    clean(asyncio.run(ws.inspect(str(repo))))
    for operation in (ws.checkpoint(inspected, task_id="task-1"),
                      ws.snapshot_review(inspected, task_id="task-1", step_key="s1")):
        with pytest.raises(ws.GitError) as raised:
            asyncio.run(operation)
        assert "git_failed:" in clean(raised.value)


class Store:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.writes: list[Any] = []

    async def save(self, row: Any, **_: Any) -> None:
        self.rows[row["task_id"]] = dict(row)
        self.writes.append(dict(row))

    async def get(self, task_id: str, **_: Any) -> dict[str, Any] | None:
        return dict(self.rows[task_id]) if task_id in self.rows else None

    async def set_work_dir(self, *args: Any, **_: Any) -> None:
        self.writes.append(args)

    async def active_workspaces(self, **_: Any) -> list[dict[str, Any]]:
        return []

    async def note(self, kind: str, text: str) -> None:
        self.writes.append((kind, text))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    from looper.db import task_worktrees, tasks

    fake = Store()
    monkeypatch.setattr(task_worktrees, "save", fake.save)
    monkeypatch.setattr(task_worktrees, "get", fake.get)
    monkeypatch.setattr(tasks, "set_work_dir", fake.set_work_dir)
    monkeypatch.setattr(tasks, "active_workspaces", fake.active_workspaces)
    return fake


def task(repo: Path) -> dict[str, Any]:
    return {"id": "task-1", "work_dir": str(repo), "workspace_dir": str(repo),
            "workspace_mode": TaskWorkspaces.WORKTREE.value, "workspace_base": ""}


def test_committing_what_a_task_left(tmp_path: Path, store: Store) -> None:
    repo = init_repo(tmp_path / "repo")
    work_dir = Path(asyncio.run(tw.prepare(task(repo), runtime_root=tmp_path / "rt", links=(), note=store.note)))
    (work_dir / "left.txt").write_text("left\n")
    plant(repo)
    with pytest.raises(tw.TaskWorkspaceError) as raised:
        asyncio.run(tw.finish("task-1", completed=True))
    assert "cannot commit what the task left in its worktree: git_failed:" in clean(raised.value, store.writes)


def test_checking_and_preparing_a_task_workspace(tmp_path: Path, store: Store) -> None:
    repo = init_repo(tmp_path / "repo")
    plant(repo)
    for operation in (tw.check(str(repo), mode=TaskWorkspaces.WORKTREE),
                      tw.prepare(task(repo), runtime_root=tmp_path / "rt", links=(), note=store.note)):
        with pytest.raises(tw.TaskWorkspaceError) as raised:
            asyncio.run(operation)
        assert "git_failed:rev-parse (exit " in clean(raised.value, store.writes)


def _judge(repo: Path) -> dict[str, Any]:
    report = {"status": "completed", "changed_files": {"gone.txt": "written"}, "deleted_files": [], "summary": "done"}
    event = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(report)}
    return judge_result(event, work_dir=repo, is_verify=False,
                        started_at=datetime.now(timezone.utc) - timedelta(minutes=1))


def test_checking_the_reported_files(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    plant(repo)
    outcome = _judge(repo)
    assert outcome["status"] == "failed"
    assert "could not look in commits: git_failed:log (exit " in clean(outcome)


# --- One command made to fail, printing the marker: each place that reads a failure ---


def failing(monkeypatch: pytest.MonkeyPatch, *command: str, code: int = 128) -> None:
    """The first git command that starts with ``command`` fails, printing the marker; the others run."""
    original = ws._git
    said = f"fatal: unexpected line: {MARKER}\n".encode()

    async def run(cwd: Any, *args: str, **kwargs: Any) -> ws._Run:
        if args[:len(command)] == command:
            return ws._Run(code, said, said)
        return await original(cwd, *args, **kwargs)

    monkeypatch.setattr(ws, "_git", run)


def _ancestor(repo: Path, created: ws.StepWorkspace) -> Any:
    return ws._is_ancestor(repo, created.base_commit, "HEAD")


def _frozen_check(repo: Path, created: ws.StepWorkspace) -> Any:
    tip = git(repo, "rev-parse", created.branch)
    return ws._check_frozen_result(ws.replace(created, result_commit=tip))


def _pending_commit(repo: Path, created: ws.StepWorkspace) -> Any:
    return ws.commit_pending(Path(created.path), (), "message")


def _paths_commit(repo: Path, created: ws.StepWorkspace) -> Any:
    return ws.commit_paths(Path(created.path), ["one.txt"], (), "message")


def _checked_out(repo: Path, created: ws.StepWorkspace) -> Any:
    return ws.branch_checked_out(str(repo), created.branch)


@pytest.mark.parametrize(("command", "operation", "subcommand"), [
    (("merge-base",), _ancestor, "merge-base"),
    (("diff", "--cached"), _frozen_check, "diff"),
    (("diff", "--cached"), _pending_commit, "diff"),
    (("diff", "--cached"), _paths_commit, "diff"),
    (("worktree", "list"), _checked_out, "worktree"),
], ids=["merge-base", "frozen-result", "commit-pending", "commit-paths", "worktree-list"])
def test_a_command_whose_failure_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: tuple[str, ...],
                                         operation: Any, subcommand: str) -> None:
    repo = init_repo(tmp_path / "repo")
    created = step_workspace(repo, tmp_path / "rt")
    failing(monkeypatch, *command)
    with pytest.raises(ws.GitError) as raised:
        asyncio.run(operation(repo, created))
    assert clean(raised.value) == f"git_failed:{subcommand} (exit 128)"


@pytest.mark.parametrize("command", ["merge-tree", "merge"])
def test_a_merge_command_that_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    repo = init_repo(tmp_path / "repo")
    created = step_workspace(repo, tmp_path / "rt")
    failing(monkeypatch, command)
    outcome = asyncio.run(ws.merge(created, step_key="s1", title="one", save=_save))
    clean(outcome)
    assert outcome.workspace.detail == f"git_failed:{command} (exit 128)"


@pytest.mark.parametrize(("command", "subcommand", "said"), [
    (("rev-parse", "--show-toplevel"), "rev-parse", "git repository inspection failed"),
    (("rev-parse", "--verify"), "rev-parse", "git HEAD inspection failed: the repository has no readable commit"),
    (("symbolic-ref",), "symbolic-ref", "git branch inspection failed"),
], ids=["location", "head", "branch"])
def test_a_workspace_inspection_that_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Store,
                                           command: tuple[str, ...], subcommand: str, said: str) -> None:
    repo = init_repo(tmp_path / "repo")
    failing(monkeypatch, *command)
    with pytest.raises(tw.TaskWorkspaceError) as raised:
        asyncio.run(tw.check(str(repo), mode=TaskWorkspaces.WORKTREE))
    text = clean(raised.value)
    assert said in text and f"git_failed:{subcommand} (exit 128)" in text


@pytest.mark.parametrize(("command", "expected"), [
    ("worktree", "could not look in other worktrees: git_failed:worktree (exit 128)"),
    ("log", "could not look in commits: git_failed:log (exit 128)"),
])
@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_a_reported_files_command_that_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
                                             expected: str, stream: str) -> None:
    repo = init_repo(tmp_path / "repo")
    original = reported_files._git

    def run(cwd: Path, *args: str, **kwargs: Any) -> Any:
        if args[0] == command:
            said = {"stdout": b"", "stderr": b"", stream: f"fatal: {MARKER}\n".encode()}
            return subprocess.CompletedProcess(args, 128, **said)
        return original(cwd, *args, **kwargs)

    monkeypatch.setattr(reported_files, "_git", run)
    assert expected in clean(_judge(repo))


@pytest.mark.parametrize("code", [0, 128])
def test_repairing_a_moved_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    repo = init_repo(tmp_path / "new")
    said = f"repair: {MARKER}\n".encode()

    async def spawn(*_: Any, **__: Any) -> Any:
        class Process:
            returncode = code

            async def communicate(self, _: Any = None) -> tuple[bytes, bytes]:
                return said, said

        return Process()

    monkeypatch.setattr(ws.git_safe, "spawn", spawn)
    move = project_move.ProjectMove(str(tmp_path / "old"), str(repo), (), "v", 0, 0, False, True, ())
    repairs = asyncio.run(project_move._repair(move, [], str(tmp_path / "old"), [tmp_path / "old"]))
    clean(repairs)
    assert repairs == [{"repository": str(repo), "ok": code == 0,
                        "output": "" if code == 0 else "git_failed:worktree (exit 128)"}]


# --- Through a Planner: activity, bus messages, its prompts and turns, and the stored rows ---


def handed_on(h: Harness, claude: Any, agent_db: Any) -> str:
    """Everything the Planner passed on."""
    return clean(activity(agent_db), [m.payload for m in (*h.dispatched, *h.statuses, *h.replies)],
                 claude.turns(), claude.prompts(), claude.argvs(), vars(agent_db.store),
                 [call for value in vars(agent_db).values() for call in getattr(value, "await_args_list", ())])


def parallel_project(agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {"id": "proj-1", "name": "calc", "description": "",
                                              "work_dir": "/srv/calc", "background": "", "step_workspaces": "worktree"}


def writing(h: Harness, *, planted: bool) -> Any:
    """s1's Worker writes a file; s2's keeps running. With ``planted`` the file appears as s2 is dispatched, when
    both steps have their worktrees and s1's result is not handled yet."""
    def outcome(envelope: Any) -> dict[str, Any] | None:
        if envelope.message_type == MessageType.VERIFY_REQUEST:
            return REVIEW_PASS
        if envelope.payload["step_key"] != "s1":
            if planted:
                plant(h.work_dir)
            return None
        (Path(envelope.payload["context"]["work_dir"]) / "s1.txt").write_text("s1\n")
        return {**WORK_OK, "changed_files": {"s1.txt": "written"}}

    return outcome


def until_said(agent_db: Any, claude: Any, line: str) -> Any:
    return lambda: any(line in said for said in activity(agent_db))


@pytest.mark.usefixtures("agent_db")
def test_a_merge_failure_reaches_the_planner_as_a_reason(planner_claude: Any, agent_db: Any) -> None:
    parallel_project(agent_db)
    claude = planner_claude(plan(step("s1"), step("s2")), decide("accept"), decide("fail", reason="stop"))
    seen: dict[str, str] = {}

    async def scenario(h: Harness) -> None:
        init_repo(h.work_dir)
        h.outcome = writing(h, planted=True)
        await h.start()
        await h.until(lambda: len(claude.turns()) >= 3)
        seen["text"] = handed_on(h, claude, agent_db)

    drive(claude, lambda _: None, scenario)
    assert "could not be merged into main (git_failed): git_failed:" in seen["text"]
    assert "- detail: git_failed:" in seen["text"]


@pytest.mark.usefixtures("agent_db")
def test_a_worktree_that_cannot_be_made_reaches_the_planner_as_a_reason(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parallel_project(agent_db)
    claude = planner_claude(plan(step("s1"), step("s2")), *[decide("fail", reason="stop")] * 2)
    original = ws.ensure_worktree
    seen: dict[str, str] = {}

    async def scenario(h: Harness) -> None:
        init_repo(h.work_dir)

        async def planted(workspace: ws.StepWorkspace) -> None:
            plant(h.work_dir)
            await original(workspace)

        monkeypatch.setattr(ws, "ensure_worktree", planted)
        await h.start()
        await h.until(lambda: len(claude.turns()) >= 2)
        seen["text"] = handed_on(h, claude, agent_db)

    drive(claude, lambda _: None, scenario)
    assert "Cannot create a worktree for step s1: git_failed:worktree (exit " in seen["text"]


@pytest.mark.usefixtures("agent_db")
def test_a_review_freeze_failure_reaches_the_gate_as_a_reason(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_review_panel_planner import MEMBERS, configure

    configure(agent_db, monkeypatch, review_panel={"members": MEMBERS, "accept_when": {"all_of": MEMBERS}})
    agent_db.get_task_project.return_value["step_workspaces"] = "worktree"
    claude = planner_claude(plan(step("s1", review=True), step("s2")), decide("review"))
    seen: dict[str, str] = {}

    async def scenario(h: Harness) -> None:
        init_repo(h.work_dir)
        h.outcome = writing(h, planted=True)
        await h.start()
        await h.until(until_said(agent_db, claude, "Review of s1 waits for the operator"))
        await asyncio.sleep(0.2)  # the gate's rows and notices
        seen["text"] = handed_on(h, claude, agent_db)

    drive(claude, lambda _: None, scenario)
    assert "Review of s1 waits for the operator: git_failed:" in seen["text"]
    assert "Review is waiting: git_failed:" in seen["text"]


@pytest.mark.usefixtures("agent_db")
def test_a_reported_files_failure_reaches_the_planner_as_a_reason(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(plan(step("s1")), decide("fail", reason="stop"))
    seen: dict[str, str] = {}

    async def scenario(h: Harness) -> None:
        init_repo(h.work_dir)

        def outcome(_: Any) -> dict[str, Any]:
            plant(h.work_dir)
            return _judge(h.work_dir)

        h.outcome = outcome
        await h.start()
        await h.finished()
        seen["text"] = handed_on(h, claude, agent_db)

    drive(claude, lambda _: None, scenario)
    assert "could not look in commits: git_failed:log (exit " in seen["text"]
