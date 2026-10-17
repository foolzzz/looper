"""Task workspaces on a real PostgreSQL: creating a task (the API and the Manager), the workspace check, the task
page, and deleting a task whose worktree holds work.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. The
workspaces are real git repositories; the operator's notes root is a temporary
directory.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from looper.agents import task_workspaces as tw
from looper.agents.operator_notes import directory_name
from tests.test_continue_at_cap_db import _call, _setup
from tests.test_ops_tasks_db import _Chat
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "notes"
    monkeypatch.setenv("LOOPER_OPERATOR_NOTES_ROOT", str(root))
    return root


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=U", "-c", "user.email=u@x", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


def test_a_task_names_its_workspace_and_is_told_what_it_will_run_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _env: Path,
) -> None:
    from looper.db import projects, tasks

    project_dir, workspace = tmp_path / "project", _repo(tmp_path / "workspace")
    project_dir.mkdir()
    (workspace / "a.txt").write_text("changed\n")
    memory = _env / directory_name(os.path.realpath(workspace)) / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("Staging listens on 59322.\n")

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(project_dir), pool=pool)
        server, _, _ = _setup()
        await tasks.create_task(task_id="task-run", requirement="r", project_id=project["id"], status="running",
                                work_dir=str(workspace), pool=pool)

        status, check = await _check(server, {"dir": str(workspace), "project_id": project["id"]})
        assert status == 200 and (check["mode"], check["default_mode"], check["notes_on"]) == (
            "worktree", "worktree", False)
        assert [w["kind"] for w in check["warnings"]] == ["uncommitted", "shared"]
        assert check["notes"] == {"found": True, "files": ["MEMORY.md"], "omitted": []}
        status, here = await _check(server, {"project_id": project["id"], "mode": "worktree"})
        assert status == 200 and here["mode"] == "in_place" and here["default_mode"] == "in_place"  # not in git

        status, body = await _call(server, "POST", "/api/tasks/create", {
            "requirement": "build it", "project_id": project["id"], "work_dir": str(workspace), "base": "main"})
        assert status == 201 and body["workspace"]["mode"] == "worktree"
        row = await tasks.get_task(body["task_id"], pool=pool)
        assert (row["workspace_dir"], row["workspace_mode"], row["workspace_base"], row["work_dir"]) == (
            str(workspace), "worktree", "main", str(workspace))
        status, detail = await _call(server, "GET", f"/api/tasks/{body['task_id']}")
        assert detail["task_workspace"] == {"workspace_dir": str(workspace), "mode": "worktree", "base": "main",
                                            "worktree": None}

        for bad, says in (({"base": "nope"}, "'nope' is not a branch"), ({"workspace_mode": "copy"}, "one of"),
                          ({"base": "main", "workspace_mode": "in_place"}, "give it with workspace_mode worktree")):
            status, refused = await _call(server, "POST", "/api/tasks/create", {
                "requirement": "x", "project_id": project["id"], "work_dir": str(workspace), **bad})
            assert status == 400 and says in refused["error"], refused
        # A new project's tasks run in worktrees; one made before keeps running in place.
        status, made = await _call(server, "POST", "/api/projects", {"name": "fresh", "work_dir": str(workspace)})
        assert made["task_workspaces"] == "worktree" and project.get("task_workspaces") is None

    _run(test, monkeypatch)


async def _check(server: Any, query: dict[str, str]) -> tuple[int, Any]:
    import json

    from tests.test_usage_api_db import _Writer

    writer = _Writer()
    await server._route_api("GET", "/api/workspaces/check", query, b"", writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_a_worktree_tasks_row_its_page_and_its_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import projects, task_worktrees, tasks

    workspace = _repo(tmp_path / "workspace")

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(workspace), pool=pool,
                                                execution={"task_workspaces": "worktree"})
        await tasks.create_task(task_id="task-w", requirement="r", project_id=project["id"], status="stopped",
                                work_dir=str(workspace), workspace_dir=str(workspace), workspace_mode="worktree",
                                pool=pool)
        notes: list[str] = []

        async def note(kind: str, text: str) -> None:
            notes.append(text)

        work_dir = await tw.prepare(await tasks.get_task("task-w", pool=pool), runtime_root=tmp_path / "rt",
                                    links=(), note=note)
        assert (await tasks.get_task("task-w", pool=pool))["work_dir"] == work_dir
        row = await task_worktrees.get("task-w", pool=pool)
        assert row is not None and (row["branch"], row["state"]) == ("looper/task-w/task", "active")
        with pytest.raises(asyncpg.CheckViolationError):  # removed exactly when it has a removal time
            await task_worktrees.save({**row, "state": "removed", "removed_at": None}, pool=pool)

        server, _, _ = _setup()
        status, detail = await _call(server, "GET", "/api/tasks/task-w")
        view = detail["task_workspace"]["worktree"]
        assert (view["branch"], view["state"], view["path"]) == ("looper/task-w/task", "active", work_dir)
        assert [c["what"] for c in view["commands"]] == ["See the task's commits", "See its changes",
                                                         "Merge it into the branch you have checked out"]

        (Path(work_dir) / "wip.txt").write_text("work in progress\n")
        status, body = await _call(server, "POST", "/api/tasks/delete", {"task_id": "task-w"})
        assert status == 200 and not Path(work_dir).exists()
        assert git(workspace, "show", "looper/task-w/task:wip.txt") == "work in progress"  # the branch stays
        assert await tasks.get_task("task-w", pool=pool) is None and await task_worktrees.get("task-w") is None

    _run(test, monkeypatch)


def test_the_manager_creates_a_task_in_a_worktree_and_says_what_to_know(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import projects, tasks

    workspace = _repo(tmp_path / "workspace")
    (workspace / "a.txt").write_text("changed\n")

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(tmp_path), pool=pool)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("确认", ("looper_task_create", {
            "requirement": "build it", "title": "构建", "project": "calc", "work_dir": str(workspace),
            "base": "main"}))
        assert answer["status"] == "ok" and answer["result"]["workspace_mode"] == "worktree"
        task_id = answer["result"]["task_id"]
        row = await tasks.get_task(task_id, pool=pool)
        assert (row["workspace_mode"], row["workspace_base"], row["project_id"]) == ("worktree", "main", project["id"])
        (block,) = chat.turns[0].blocks
        assert f"- 运行方式：在这个目录所在仓库的 git worktree 里，开始时建分支 looper/{task_id}/task" in block
        assert "- 提醒：目录里有 1 个未提交的改动不会进入任务的 worktree（a.txt），任务需要的话先提交" in block

        (answer,) = await chat.say("确认", ("looper_task_create", {
            "requirement": "x", "title": "x", "project": "calc", "work_dir": str(workspace), "base": "nope"}))
        assert answer["status"] == "error" and "'nope' is not a branch" in answer["error"]["message"]

    _run(test, monkeypatch)
