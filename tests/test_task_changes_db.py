"""A task's changes in a real PostgreSQL: supplements, requirement versions, and what its Planner has had.

A supplement and a requirement edit are pending until ``mark_delivered``; the
first edit keeps the text the task had as version 1; the user instructions
from before become supplements their Planner already had. Runs only when
LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from looper.agents import api_v1, step_workspaces
from looper.agents.action_levels import ActionLevel
from looper.db import task_worktrees
from tests.db_support import DSN, run_db, v1_api
from tests.test_task_changes import git, repository as _repository
from tests.test_task_capture_db import _task

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
repository = _repository


def test_supplements_and_requirement_edits_are_pending_until_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import task_changes, tasks
    from looper.db.task_changes import RequirementEdit

    async def test(pool: Any) -> None:
        await _task(pool)
        assert await task_changes.versions("task-a", pool=pool) == (0, 0)
        first = await task_changes.add_supplement("task-a", "use CSV", via="chat", pool=pool)
        assert await task_changes.versions("task-a", pool=pool) == (0, first)
        assert await task_changes.edit_requirement("task-a", "build calc, with a CLI", via="task_page",
                                                   delivered=False, pool=pool) == 2
        assert await task_changes.versions("task-a", pool=pool) == (2, first)
        pending = await task_changes.pending("task-a", pool=pool)
        assert [(s.id, s.text, s.via) for s in pending.supplements] == [(first, "use CSV", "chat")]
        assert pending.requirement == RequirementEdit(2, "build calc, with a CLI", 1, "build calc")
        assert (await tasks.get_task("task-a", pool=pool))["requirement"] == "build calc, with a CLI"

        # A change made while the Planner's turn ran is not delivered with the changes that turn had.
        second = await task_changes.add_supplement("task-a", "and a JSON export", via="manager:lark", pool=pool)
        assert await task_changes.edit_requirement("task-a", "build calc, with a CLI and docs", via="task_page",
                                                   delivered=False, pool=pool) == 3
        await task_changes.mark_delivered("task-a", pending, pool=pool)
        later = await task_changes.pending("task-a", pool=pool)
        assert [s.id for s in later.supplements] == [second]
        assert later.requirement == RequirementEdit(3, "build calc, with a CLI and docs", 2, "build calc, with a CLI")
        await task_changes.mark_delivered("task-a", later, pool=pool)
        assert (await task_changes.pending("task-a", pool=pool)).empty

        versions = await task_changes.requirement_versions("task-a", pool=pool)
        assert [(v["version"], v["text"], v["via"], v["delivered_at"] is not None) for v in versions] == [
            (1, "build calc", "created", True), (2, "build calc, with a CLI", "task_page", True),
            (3, "build calc, with a CLI and docs", "task_page", True)]
        # An edit of a task without a Planner yet is delivered at once; an unknown task has no version.
        assert await task_changes.edit_requirement("task-a", "v4", via="task_page", delivered=True, pool=pool) == 4
        assert (await task_changes.pending("task-a", pool=pool)).empty
        assert await task_changes.edit_requirement("task-nope", "x", via="task_page", delivered=True,
                                                   pool=pool) is None

    run_db(test, monkeypatch)


def test_a_title_is_set_in_any_status_and_a_delivered_note_is_not_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import task_changes, tasks

    async def test(pool: Any) -> None:
        await _task(pool)
        assert await tasks.get_task("task-a", pool=pool) is not None
        assert (await tasks.get_task("task-a", pool=pool))["name"] == "g1"
        assert await tasks.set_task_title("task-a", "  Calc \n tool ", pool=pool)
        row = await tasks.get_task("task-a", pool=pool)
        assert (row["title"], row["name"]) == ("Calc tool", "Calc tool")
        for status in ("completed", "cancelled", "init"):
            await pool.execute("UPDATE tasks SET status = $1 WHERE id = 'task-a'", status)
            assert await tasks.set_task_title("task-a", f"as {status}", pool=pool)
            assert (await tasks.get_task("task-a", pool=pool))["title"] == f"as {status}"
        assert await tasks.set_task_title("task-a", "x" * 100, pool=pool)
        assert (await tasks.get_task("task-a", pool=pool))["title"] == "x" * tasks.TITLE_MAX_CHARS
        assert not await tasks.set_task_title("task-nope", "x", pool=pool)
        for empty in ("", " \n\t ", None, 5):
            with pytest.raises(ValueError):
                await tasks.set_task_title("task-a", empty, pool=pool)
        assert (await tasks.get_task("task-a", pool=pool))["title"] == "x" * tasks.TITLE_MAX_CHARS

        await task_changes.add_supplement("task-a", "Renamed.", via="task_page", delivered=True, pool=pool)
        assert (await task_changes.pending("task-a", pool=pool)).empty

    run_db(test, monkeypatch)


@pytest.mark.parametrize("status", ["planning", "verifying", "stopping", "pending"])
def test_the_requirement_of_a_task_in_any_running_status_can_be_edited(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Any, status: str) -> None:
    """Whatever its Planner is doing (the statuses it stores as the task's), a task under way takes an edit."""
    from types import SimpleNamespace

    from looper.agents import projects
    from looper.agents.bus import MessageBus
    from looper.agents.message import channel_address

    async def test(pool: Any) -> None:
        await _task(pool)
        await pool.execute("UPDATE tasks SET status = $1 WHERE id = 'task-a'", status)
        edited = await projects.edit_requirement(
            "task-a", {"requirement": "build calc v2"}, bus=MessageBus(runtime_root=tmp_path),
            source=channel_address("web"), via="task_page", task_router=SimpleNamespace(has_planner=lambda _: False))
        assert edited["version"] == 2

    run_db(test, monkeypatch)


def test_schema_and_level():
    op = api_v1.find("POST", "/api/v1/tasks/changes")
    assert op is not None and op.operator_only and op.risk == "read"
    assert op.level is ActionLevel.MACHINE and op.tool is None and not op.hand
    assert len(op.summary) <= 120 and op.description.startswith(op.summary)
    assert op.request["additionalProperties"] is False and op.result["additionalProperties"] is False
    assert op.result["properties"]["files"]["maxItems"] == 200
    assert op.result["properties"]["files"]["items"]["additionalProperties"] is False


def test_http_permission_validation_and_real_git(tmp_path, monkeypatch, repository):
    repo = repository
    row = {"task_id": "t", "repo_dir": str(repo), "subdir": "", "path": str(repo), "branch": "task",
           "base_commit": git(repo, "rev-parse", "HEAD"), "state": "active", "links": []}

    async def run(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p','Project')")
        await pool.execute("INSERT INTO tasks(id,requirement,project_id,workspace_dir,work_dir,workspace_mode) "
                           "VALUES ('t','Changes','p',$1,$1,'worktree')", str(repo))
        await task_worktrees.save(row, pool=pool)
        calls = []
        original = step_workspaces._git

        async def record(*args, **kwargs):
            calls.append(args)
            return await original(*args, **kwargs)

        monkeypatch.setattr(step_workspaces, "_git", record)
        async with v1_api(tmp_path, pool) as call:
            code, body = await call("tasks.changes", {"task_id": "t"}, client=True)
            assert code == 403 and body["error"]["code"] == "forbidden" and calls == []
            code, body = await call("tasks.changes", {"task_id": "missing"})
            assert code == 404 and calls == []
            for args in ({}, {"task_id": ""}, {"task_id": "t", "limit": 0},
                         {"task_id": "t", "limit": 201}, {"task_id": "t", "limit": "1"},
                         {"task_id": "t", "limit": True}, {"task_id": "t", "cwd": "elsewhere"},
                         {"task_id": "t", "ref": "other"}):
                code, body = await call("tasks.changes", args)
                assert code == 400 and body["error"]["code"] == "invalid", body
            assert calls == []
            for i in range(201):
                (repo / f"new-{i:03}").write_text("new")
            for args, count in (({}, 100), ({"limit": 1}, 1), ({"limit": 200}, 200)):
                code, body = await call("tasks.changes", {"task_id": "t", **args})
                result = body["result"]
                assert code == 200 and result["kind"] == "ok", body
                assert len(result["files"]) == count and result["truncated"] and "total" not in result
                api_v1.validate_arguments(api_v1.find("POST", "/api/v1/tasks/changes").result, result)
            git(repo, "checkout", "-b", "other")
            code, body = await call("tasks.changes", {"task_id": "t"})
            assert code == 200 and body["result"]["reason"] == "branch_changed"
            assert body["result"]["files"] == []
    run_db(run, monkeypatch)


def test_http_persisted_scopes_and_unavailable(tmp_path, monkeypatch, repository):
    repo = repository
    base = git(repo, "rev-parse", "HEAD")
    (repo / "committed").write_text("task commit")
    git(repo, "commit", "-am", "Task")
    git(repo, "checkout", "-b", "other", base)
    (repo / "modified").write_text("current checkout")
    (repo / "untracked").write_text("current checkout")

    async def run(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p','Project')")
        await pool.execute("INSERT INTO tasks(id,requirement,project_id,workspace_dir,work_dir,workspace_mode) "
                           "VALUES ('t','Changes','p',$1,$1,'worktree')", str(repo))
        await task_worktrees.save({"task_id": "t", "repo_dir": str(repo), "subdir": "",
            "path": str(repo / "gone"), "branch": "task", "base_commit": base,
            "state": "removed", "removed_at": datetime.now(timezone.utc), "links": []}, pool=pool)
        async with v1_api(tmp_path, pool) as call:
            code, body = await call("tasks.changes", {"task_id": "t"})
            assert code == 200 and body["result"]["scope"] == "task_branch"
            assert body["result"]["files"] == [{"path": "committed", "status": "modified", "old_path": None}]
            git(repo, "branch", "-D", "task")
            code, body = await call("tasks.changes", {"task_id": "t"})
            assert code == 200 and body["result"]["reason"] == "branch_missing"
            await pool.execute("UPDATE task_worktrees SET state='active',removed_at=NULL WHERE task_id='t'")
            code, body = await call("tasks.changes", {"task_id": "t"})
            assert code == 200 and body["result"]["reason"] == "workspace_missing"
            await pool.execute("DELETE FROM task_worktrees WHERE task_id='t'")
            await pool.execute("UPDATE tasks SET workspace_mode='in_place' WHERE id='t'")
            code, body = await call("tasks.changes", {"task_id": "t"})
            assert code == 200 and body["result"]["scope"] == "working_tree"
            assert {f["path"] for f in body["result"]["files"]} == {"modified", "untracked"}
            await pool.execute("UPDATE tasks SET workspace_dir=$1 WHERE id='t'", str(tmp_path))
            code, body = await call("tasks.changes", {"task_id": "t"})
            assert code == 200 and body["result"]["reason"] == "not_git"
            original = step_workspaces._git

            async def fail(*args, **kwargs):
                raise step_workspaces.GitError("private diagnostic")

            monkeypatch.setattr(step_workspaces, "_git", fail)
            code, body = await call("tasks.changes", {"task_id": "t"})
            assert code == 200 and body["result"] == {"task_id": "t", "kind": "unavailable", "scope": None,
                "files": [], "truncated": False, "reason": "git_error"}
            monkeypatch.setattr(step_workspaces, "_git", original)
    run_db(run, monkeypatch)
