"""Authenticated workspace operations, real git worktrees and disposable PostgreSQL readback."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper.agents import api_v1, operator_notes, room, step_workspaces as ws, task_workspaces as tw
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import api_clients, projects, step_workspaces, task_worktrees, tasks
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN
from tests.test_step_workspaces_db import _repo, _task, _workspace, git

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(operator_notes, "_root", str(tmp_path / "notes"))


@asynccontextmanager
async def _workspaces(tmp_path, *, planner=False):
    async with _serving(tmp_path) as server:
        bus = MessageBus(runtime_root=tmp_path)
        server._task_router = SimpleNamespace(has_planner=lambda _: planner)
        server._supervisor = SimpleNamespace(bus=bus)
        server._client_ops = ClientOps(Platform(bus=bus, task_router=server._task_router, runtime_root=tmp_path))

        async def post(operation, data, *, token=TOKEN, session="one"):
            status, _, answer = await _send(server, "POST /api/v1/" + operation,
                {**_bearer(token), "X-Looper-Session": session}, json.dumps(data))
            if status == 200 and operation != "confirm":
                op = next(op for op in api_v1.operations() if op.operation_id == operation.replace("/", "."))
                api_v1.validate_arguments(op.result, answer["result"])
            return status, answer

        try:
            yield server, post
        finally:
            await bus.shutdown()


async def _prepared(pool, tmp_path, task_id="task-a", *, status="stopped"):
    repo = tmp_path / task_id
    _repo(repo)
    await _task(pool, task_id, str(repo), status=status)
    await pool.execute("UPDATE tasks SET workspace_dir = $2, workspace_mode = 'worktree' WHERE id = $1",
                       task_id, str(repo))

    async def note(*_):
        pass

    path = await tw.prepare(await tasks.get_task(task_id), runtime_root=tmp_path, links=(), note=note)
    session = room.run_session_dir(tmp_path, "codex", task_id, "worker-1")
    session.mkdir(parents=True)
    (session / "session.json").write_text("{}")
    return repo, Path(path), session


@pytest.mark.parametrize("operation", ["tasks/delete", "workspaces/discard"])
def test_client_pending_replaced_and_foreign_confirmation_preserve_real_state(tmp_path, monkeypatch, operation):
    async def run(pool):
        repo, checkout, session = await _prepared(pool, tmp_path)
        workspace = await _workspace(str(checkout), tmp_path, "task-a", "s1")
        client, token = await api_clients.create("first", pool=pool)
        _, other = await api_clients.create("other", pool=pool)
        data = {"task_id": "task-a", **({"workspace_id": workspace.id} if operation.endswith("discard") else {})}
        before_task, before_workspace = await tasks.get_task("task-a"), await step_workspaces.get(workspace.id)

        async def unchanged():
            assert await tasks.get_task("task-a") == before_task
            assert await step_workspaces.get(workspace.id) == before_workspace
            assert checkout.exists() and session.exists() and Path(workspace.path).exists()
            assert git(checkout, "rev-parse", "--verify", workspace.branch)

        async with _workspaces(tmp_path) as (_, post):
            status, abandoned = await post(operation, data, token=token)
            assert status == 202 and abandoned["status"] == "needs_confirmation"
            assert "task-a" in abandoned["impact"] and abandoned["expires_in_seconds"] > 0
            await unchanged()
            status, pending = await post(operation, data, token=token)
            assert status == 202 and pending["confirmation_id"] != abandoned["confirmation_id"]
            assert (await post("confirm", {"confirmation_id": abandoned["confirmation_id"]}, token=token))[0] == 404
            confirmation = {"confirmation_id": pending["confirmation_id"]}
            for wrong in ({"token": token, "session": "two"}, {"token": other}, {}):
                status, answer = await post("confirm", confirmation, **wrong)
                assert status == 404 and answer["error"]["code"] == "not_found"
                await unchanged()
            status, answer = await post("confirm", confirmation, token=token)
            assert status == 200 and answer["status"] == "ok", answer
            assert not Path(workspace.path).exists()
            assert (await post("confirm", confirmation, token=token))[0] == 404
            if operation.endswith("delete"):
                assert await tasks.get_task("task-a") is None and not checkout.exists() and not session.exists()
            else:
                assert (await step_workspaces.get(workspace.id))["state"] == "discarded"
                assert await tasks.get_task("task-a") == before_task and checkout.exists() and session.exists()
            actions = await pool.fetch("SELECT status FROM manager_actions WHERE client_id = $1", client["id"])
            assert [row["status"] for row in actions].count("ok") == 1
    run_db(run, monkeypatch, timeout=30)


def test_active_delete_rejects_before_any_cleanup(tmp_path, monkeypatch):
    async def run(pool):
        _, checkout, session = await _prepared(pool, tmp_path, status="running")
        workspace = await _workspace(str(checkout), tmp_path, "task-a", "s1")
        before = await tasks.get_task("task-a")
        async with _workspaces(tmp_path) as (_, post):
            status, answer = await post("tasks/delete", {"task_id": "task-a"})
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert "stop the task" in answer["error"]["message"]
        assert await tasks.get_task("task-a") == before
        assert checkout.exists() and session.exists() and Path(workspace.path).exists()
        assert (await step_workspaces.get(workspace.id))["state"] == "active"
    run_db(run, monkeypatch, timeout=30)


def test_finish_failure_keeps_task_session_checkout_and_task_branch(tmp_path, monkeypatch):
    async def run(pool):
        repo, checkout, session = await _prepared(pool, tmp_path)
        (checkout / "wip.txt").write_text("work to keep\n")
        git(checkout, "switch", "-c", "operator-work")
        before, worktree = await tasks.get_task("task-a"), await task_worktrees.get("task-a")
        branch_tip = git(repo, "rev-parse", tw.task_branch("task-a"))
        async with _workspaces(tmp_path) as (_, post):
            status, answer = await post("tasks/delete", {"task_id": "task-a"})
            assert status == 409 and answer["error"]["code"] == "conflict"
            assert "worktree was kept" in answer["error"]["message"]
        assert await tasks.get_task("task-a") == before
        assert await task_worktrees.get("task-a") == worktree
        assert checkout.exists() and session.exists() and (checkout / "wip.txt").read_text() == "work to keep\n"
        assert git(repo, "rev-parse", tw.task_branch("task-a")) == branch_tip
        assert git(checkout, "branch", "--show-current") == "operator-work"
    run_db(run, monkeypatch, timeout=30)


def test_operator_delete_removes_owned_state_in_original_order_and_retires_legacy(tmp_path, monkeypatch):
    async def run(pool):
        repo, checkout, session = await _prepared(pool, tmp_path)
        workspace = await _workspace(str(checkout), tmp_path, "task-a", "s1")
        _, other_checkout, other_session = await _prepared(pool, tmp_path, "task-b")
        other = await _workspace(str(other_checkout), tmp_path, "task-b", "s1")
        unrelated = tmp_path / "unrelated.txt"
        unrelated.write_text("keep\n")
        (checkout / "wip.txt").write_text("saved on branch\n")
        events = []
        remove_steps, finish, remove_sessions, delete = ws.remove_task, tw.finish, room.remove_run_sessions, tasks.delete_task

        async def observed_steps(task_id):
            assert checkout.exists() and session.exists() and await tasks.get_task(task_id)
            result = await remove_steps(task_id)
            events.append("steps")
            return result

        async def observed_finish(task_id, **kwargs):
            assert events == ["steps"] and not Path(workspace.path).exists() and session.exists()
            result = await finish(task_id, **kwargs)
            events.append("checkout")
            return result

        def observed_sessions(root, task_id):
            assert events == ["steps", "checkout"] and not checkout.exists()
            remove_sessions(root, task_id)
            events.append("session")

        async def observed_delete(task_id, **kwargs):
            assert events == ["steps", "checkout", "session"] and not session.exists()
            result = await delete(task_id, **kwargs)
            events.append("database")
            return result

        monkeypatch.setattr(ws, "remove_task", observed_steps)
        monkeypatch.setattr(tw, "finish", observed_finish)
        monkeypatch.setattr(room, "remove_run_sessions", observed_sessions)
        monkeypatch.setattr(tasks, "delete_task", observed_delete)
        async with _workspaces(tmp_path) as (server, post):
            assert (await _send(server, "POST /api/tasks/delete", _bearer(TOKEN), '{"task_id":"task-a"}'))[0] == 404
            status, answer = await post("tasks/delete", {"task_id": " task-a "})
            assert status == 200 and answer["result"] == {"deleted": "task-a"}, answer
        assert events == ["steps", "checkout", "session", "database"]
        assert await tasks.get_task("task-a") is None and await task_worktrees.get("task-a") is None
        assert await step_workspaces.get(workspace.id) is None
        assert git(repo, "show", f"{tw.task_branch('task-a')}:wip.txt") == "saved on branch"
        assert await tasks.get_task("task-b") and await step_workspaces.get(other.id)
        assert other_checkout.exists() and other_session.exists() and Path(other.path).exists()
        assert unrelated.read_text() == "keep\n"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("case", "expected"), [("active", 409), ("planner", 409), ("foreign", 404),
                                                  ("merging", 409), ("merged", 409), ("discarded", 409)])
def test_discard_refusals_preserve_directory_branch_and_row(tmp_path, monkeypatch, case, expected):
    async def run(pool):
        _, checkout, _ = await _prepared(pool, tmp_path, status="running" if case == "active" else "stopped")
        workspace = await _workspace(str(checkout), tmp_path, "task-a", "s1")
        task_id = "task-a"
        if case == "foreign":
            await _task(pool, "task-b", str(checkout), status="stopped")
            task_id = "task-b"
        if case in {"merging", "merged", "discarded"}:
            await step_workspaces.save({**workspace.row(), "state": case,
                                       "removed_at": ws.now() if case == "discarded" else None})
        before = await step_workspaces.get(workspace.id)
        async with _workspaces(tmp_path, planner=case == "planner") as (_, post):
            status, answer = await post("workspaces/discard", {"task_id": task_id, "workspace_id": workspace.id})
            assert status == expected and answer["error"]["code"] == ("conflict" if expected == 409 else "not_found")
        assert await step_workspaces.get(workspace.id) == before and Path(workspace.path).exists()
        assert git(checkout, "rev-parse", "--verify", workspace.branch)
    run_db(run, monkeypatch, timeout=30)


def test_operator_discard_removes_only_selected_workspace_and_retires_legacy(tmp_path, monkeypatch):
    async def run(pool):
        _, checkout, session = await _prepared(pool, tmp_path)
        workspace = await _workspace(str(checkout), tmp_path, "task-a", "s1")
        async with _workspaces(tmp_path) as (server, post):
            legacy = f"POST /api/tasks/task-a/workspaces/{workspace.id}/discard"
            assert (await _send(server, legacy, _bearer(TOKEN), "{}"))[0] == 404
            status, answer = await post("workspaces/discard", {"task_id": "task-a", "workspace_id": workspace.id})
            assert status == 200 and answer["result"]["workspace"]["state"] == "discarded", answer
            assert answer["result"]["workspace"]["discardable"] is False
        assert (await step_workspaces.get(workspace.id))["removed_at"] is not None
        assert not Path(workspace.path).exists() and checkout.exists() and session.exists()
        assert not await ws.branch_exists(str(checkout), workspace.branch)
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("query", "mode", "default_mode", "warning", "status"), [
    ({}, "in_place", "in_place", None, 200),
    ({"mode": "worktree", "base": "main"}, "worktree", "in_place", None, 200),
    ({"base": "main"}, None, None, None, 400),
    ({"mode": "worktree", "base": "missing"}, None, None, None, 400),
    ({"mode": "in_place"}, "in_place", "in_place", None, 200),
    ({"non_git": True}, "in_place", "worktree", "not_git", 200),
    ({"non_git": True, "mode": "worktree"}, "in_place", "worktree", "not_git", 200),
    ({"non_git": True, "mode": "in_place"}, "in_place", "worktree", None, 200),
    ({"projectless": True}, "worktree", "worktree", None, 200),
    ({"projectless": True, "mode": "in_place"}, "in_place", "worktree", None, 200),
])
def test_workspace_check_project_defaults_base_validation_and_warnings(
    tmp_path, monkeypatch, query, mode, default_mode, warning, status,
):
    async def run(pool):
        repo = tmp_path / "repo"
        _repo(repo)
        project = await projects.create_project(name="check", work_dir=str(repo), pool=pool)
        data = {key: value for key, value in query.items() if key not in {"non_git", "projectless"}}
        data.update({"dir": str(repo)} if query.get("projectless") else {"project_id": project["id"]})
        if query.get("non_git"):
            plain = tmp_path / "plain"
            plain.mkdir()
            data["dir"] = str(plain)
        async with _workspaces(tmp_path) as (server, post):
            if status == 200:
                assert (await _send(server, "GET /api/workspaces/check", _bearer(TOKEN)))[0] == 404
            code, answer = await post("workspaces/check", data)
            assert code == status, answer
            if status == 200:
                result = answer["result"]
                assert result["mode"] == mode and result["notes_on"] is True
                assert result["default_mode"] == default_mode
                assert result["requested_mode"] == data.get("mode", default_mode)
                assert [w["kind"] for w in result["warnings"]] == ([warning] if warning else [])
                if query.get("base"):
                    assert result["git"]["base_commit"] == git(repo, "rev-parse", "main")
            else:
                assert answer["error"]["code"] == "invalid"
    run_db(run, monkeypatch, timeout=30)
