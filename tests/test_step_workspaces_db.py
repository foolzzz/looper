"""Step workspaces in a real PostgreSQL: their rows, the rules the table keeps, what is removed and when, and the
task page's view of them and its discard.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own; the worktrees are of a real git
repository in a temporary directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from looper.agents import step_workspaces as ws
from looper.agents.step_workspaces import MergeError, StepWorkspaceState
from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def _git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(cwd: Path | str, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.rstrip()


def _repo(path: Path) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "User")
    git(path, "config", "user.email", "user@example.com")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return git(path, "rev-parse", "HEAD")


async def _task(pool: Any, task_id: str, work_dir: str, *, status: str = "running", steps: tuple[str, ...] = ("s1",)
                ) -> None:
    from looper.db import plans, projects, tasks

    project = await projects.create_project(name=f"calc-{task_id}", work_dir=work_dir, pool=pool)
    await tasks.create_task(task_id=task_id, requirement="build", goal_id="g1", project_id=project["id"],
                            work_dir=work_dir, correlation_id="corr", status=status, pool=pool)
    await plans.save(task_id, plan={"plan_id": "plan-1", "status": "executing"},
                     steps=[{"id": f"{task_id}-{key}", "index": i, "key": key, "instruction": f"do {key}"}
                            for i, key in enumerate(steps)], pool=pool)


async def _workspace(repo: str, root: Path, task_id: str, key: str, *, worktree: bool = True) -> ws.StepWorkspace:
    from looper.db import step_workspaces as db

    found = await ws.inspect(repo)
    assert isinstance(found, ws.Repo)
    created = ws.new_workspace(found, runtime_root=root, workspace_id=await db.next_id(), task_id=task_id,
                               step_id=f"{task_id}-{key}", step_key=key, base=found.head, checkpointed=False)
    await db.save(created.row())
    if worktree:
        await ws.ensure_worktree(created)
    return created


def test_a_workspace_row_is_written_as_it_changes_and_the_table_keeps_its_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import step_workspaces as db, tasks

    repo = tmp_path / "repo"
    _repo(repo)

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", str(repo), steps=("s1", "s2"))
        first = await _workspace(str(repo), tmp_path / "rt", "task-a", "s1")
        merging = first.moved(StepWorkspaceState.MERGING)
        await db.save(merging.row())
        unmerged = merging.moved(StepWorkspaceState.UNMERGED, merge_error=MergeError.CONFLICT, detail="a.txt",
                                 merge_files=("a.txt",), links=(".venv",))
        await db.save(unmerged.row())
        rows = await db.list_for_task("task-a")
        assert [ws.StepWorkspace.from_row(row) for row in rows] == [unmerged]

        # An unmerged one says why; one that is in use or merged does not; a step has one in use at a time.
        second = await _workspace(str(repo), tmp_path / "rt", "task-a", "s2", worktree=False)
        with pytest.raises(asyncpg.CheckViolationError):
            await db.save({**second.row(), "state": "unmerged"})
        with pytest.raises(asyncpg.CheckViolationError):
            await db.save({**second.row(), "state": "merged", "merge_error": "conflict"})
        with pytest.raises(asyncpg.UniqueViolationError):
            await db.save({**second.row(), "id": await db.next_id()})
        retried = await _workspace(str(repo), tmp_path / "rt", "task-a", "s1", worktree=False)  # after unmerged
        assert [row["id"] for row in await db.list_for_task("task-a")] == [first.id, second.id, retried.id]

        kept = await db.list_kept()
        assert {(row["id"], row["task_status"]) for row in kept} == {
            (first.id, "running"), (second.id, "running"), (retried.id, "running")}
        await db.save({**second.row(), "removed_at": ws.now()})
        assert second.id not in {row["id"] for row in await db.list_kept()}

        # A deleted task's rows go with it.
        await ws.remove_task("task-a")
        await pool.execute("UPDATE tasks SET status = 'stopped' WHERE id = 'task-a'")
        assert await tasks.delete_task("task-a", pool=pool)
        assert await db.list_for_task("task-a") == []

    _run(test, monkeypatch)


def test_a_planner_reads_back_the_start_of_its_branch_and_each_step_s_newest_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    start = _repo(repo)

    async def test(pool: Any) -> None:
        await _task(pool, "task-b", str(repo))
        older = await _workspace(str(repo), tmp_path / "rt", "task-b", "s1", worktree=False)
        from looper.db import step_workspaces as db

        await db.save(older.moved(StepWorkspaceState.DISCARDED).row())
        newer = await _workspace(str(repo), tmp_path / "rt", "task-b", "s1", worktree=False)
        first = PlannerAgent(task_id="task-b", runtime_root=tmp_path / "rt")
        saved = TaskPlan(plan_id="plan-1", goal_id="g1", requirement="build", work_dir=str(repo), status="executing",
                         start_branch="refs/heads/main", start_commit=start)
        saved.steps = [PlanStep(step_id="task-b-s1", index=0, instruction="do s1", key="s1", status="dispatched",
                                worker_id="worker-1")]
        await first._save(saved, *saved.steps, plan_row=True)

        second = PlannerAgent(task_id="task-b", runtime_root=tmp_path / "rt")
        await second._load_plan()
        loaded = second._plan
        assert loaded is not None
        assert (loaded.start_branch, loaded.start_commit) == ("refs/heads/main", start)
        assert loaded.steps[0].workspace == newer and loaded.steps[0].isolated

    _run(test, monkeypatch)


def test_removing_and_sweeping_follow_what_each_task_keeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _repo(repo)
    root = tmp_path / "rt"

    async def test(pool: Any) -> None:
        await _task(pool, "task-done", str(repo), status="completed")
        await _task(pool, "task-stopped", str(repo), status="stopped")
        await _task(pool, "task-anew", str(repo), status="stopped")
        done = await _workspace(str(repo), root, "task-done", "s1")
        stopped = await _workspace(str(repo), root, "task-stopped", "s1")
        anew = await _workspace(str(repo), root, "task-anew", "s1")
        orphan = root / "worktrees" / "task-gone" / "s1-99"
        git(repo, "worktree", "add", "-q", "-b", "looper/task-gone/s1-99", str(orphan), "HEAD")

        # Planning a task anew (or deleting it) removes its workspaces, branches included.
        assert await ws.remove_task("task-anew") == 1
        assert not Path(anew.path).exists()
        # At startup a completed task keeps none, a stopped one keeps its unmerged work, and no row, no directory.
        assert await ws.sweep(root) == 2
        assert not Path(done.path).exists() and not orphan.exists() and Path(stopped.path).exists()
        assert git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/looper/") == stopped.branch
        states = {row["task_id"]: (row["state"], row["removed_at"] is not None)
                  for row in await pool.fetch("SELECT * FROM step_workspaces")}
        assert states == {"task-done": ("discarded", True), "task-stopped": ("active", False),
                          "task-anew": ("discarded", True)}

    _run(test, monkeypatch)


def test_the_task_page_shows_a_task_s_workspaces_and_discards_one_a_stopped_task_keeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import activity, step_workspaces as db
    from tests.test_continue_at_cap_db import _call, _setup

    repo = tmp_path / "repo"
    start = _repo(repo)
    root = tmp_path / "rt"

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", str(repo), steps=("s1", "s2", "s3"))
        await pool.execute("UPDATE task_plans SET start_branch = 'refs/heads/main', start_commit = $1", start)
        await _task(pool, "task-b", str(repo))
        one, two, three = [await _workspace(str(repo), root, "task-a", key) for key in ("s1", "s2", "s3")]
        (Path(one.path) / "a.txt").write_text("from s1\n")
        (Path(two.path) / "a.txt").write_text("from s2\n")
        merged = (await ws.merge(one, step_key="s1", title="do s1", save=lambda w: db.save(w.row()))).workspace
        unmerged = (await ws.merge(two, step_key="s2", title="do s2", save=lambda w: db.save(w.row()))).workspace
        assert unmerged.merge_error is MergeError.CONFLICT
        server, router, _ = _setup()

        status, detail = await _call(server, "GET", "/api/tasks/task-a")
        assert status == 200
        view = detail["step_workspaces"]
        assert view["start"] == {"branch": "main", "commit": start}
        assert [(w["step_key"], w["state"], w["merge_error"], w["merge_files"], w["discardable"])
                for w in view["workspaces"]] == [
            ("s1", "merged", None, [], False), ("s2", "unmerged", "conflict", ["a.txt"], True),
            ("s3", "active", None, [], True)]
        assert view["workspaces"][0]["merge_commit"] == merged.merge_commit
        assert [c["command"] for c in view["undo"]][:2] == [
            "git log --first-parent --grep='^Looper-Task: task-a$' main",
            f"git switch main && git revert -m 1 {merged.merge_commit}"]
        assert (await _call(server, "GET", "/api/tasks/task-b"))[1]["step_workspaces"] is None

        discard = f"/api/tasks/task-a/workspaces/{unmerged.id}/discard"
        assert await _call(server, "POST", discard) == (409, {"error": "stop the task before discarding its step "
                                                                       "worktrees"})
        await pool.execute("UPDATE tasks SET status = 'stopped' WHERE id = 'task-a'")
        monkeypatch.setattr(router, "has_planner", lambda task_id: True)  # its Planner has not ended yet
        assert (await _call(server, "POST", discard))[0] == 409
        monkeypatch.setattr(router, "has_planner", lambda task_id: False)
        assert Path(unmerged.path).exists() and ws.StepWorkspace.from_row(await db.get(unmerged.id)) == unmerged

        status, body = await _call(server, "POST", discard)
        assert status == 200 and (body["workspace"]["state"], body["workspace"]["discardable"]) == ("discarded", False)
        row = await db.get(unmerged.id)
        assert row is not None and (row["state"], row["removed_at"] is not None) == ("discarded", True)
        assert not Path(unmerged.path).exists() and not await ws.branch_exists(str(repo), unmerged.branch)
        notes = [r["summary"] for r in await activity.list_activity("task-a")]
        assert f"Discarded a step worktree from the task page: branch {unmerged.branch} and its directory removed" \
            in notes
        assert (await _call(server, "POST", discard))[1] == {
            "error": f"step workspace {unmerged.id} is gone; it cannot be discarded"}
        assert (await _call(server, "POST", f"/api/tasks/task-a/workspaces/{merged.id}/discard"))[1] == {
            "error": f"step workspace {merged.id} is merged; it cannot be discarded"}
        assert (await _call(server, "POST", f"/api/tasks/task-b/workspaces/{three.id}/discard"))[0] == 404
        assert (await _call(server, "POST", "/api/tasks/task-a/workspaces/999999/discard"))[0] == 404
        assert Path(three.path).exists()  # only the one asked for

        # While it is discarded, the task's row is locked: a restart's status update waits for it.
        blocked: list[str] = []
        real = ws.discard

        async def discard_while_restarted(workspace: ws.StepWorkspace) -> ws.StepWorkspace:
            other = await asyncpg.connect(DSN, server_settings={
                "search_path": await pool.fetchval("SELECT current_schema()"), "lock_timeout": "200ms"})
            try:
                await other.execute("UPDATE tasks SET status = 'pending' WHERE id = 'task-a'")
            except asyncpg.LockNotAvailableError:
                blocked.append("waited")
            finally:
                await other.close()
            return await real(workspace)

        monkeypatch.setattr(ws, "discard", discard_while_restarted)
        assert (await _call(server, "POST", f"/api/tasks/task-a/workspaces/{three.id}/discard"))[0] == 200
        assert blocked == ["waited"] and not Path(three.path).exists()
        assert await pool.fetchval("SELECT status FROM tasks WHERE id = 'task-a'") == "stopped"

    _run(test, monkeypatch)


def test_doctor_reads_the_projects_whose_steps_work_in_worktrees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                ) -> None:
    from looper.db import projects

    async def test(pool: Any) -> None:
        await projects.create_project(name="beta", work_dir="/b", pool=pool,
                                      execution={"step_workspaces": "worktree", "step_workspace_links": [".venv"]})
        await projects.create_project(name="alpha", work_dir="/a", pool=pool, execution={"step_workspaces": "worktree"})
        await projects.create_project(name="shared", work_dir="/s", pool=pool, execution={"step_workspaces": "shared"})
        await projects.create_project(name="plain", work_dir="/p", pool=pool)
        assert [(p["name"], p["work_dir"], p["step_workspace_links"]) for p in await projects.worktree_projects()] == [
            ("alpha", "/a", None), ("beta", "/b", [".venv"])]

    _run(test, monkeypatch)
