"""Authenticated workspace operations read back disposable PostgreSQL and real git worktrees."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import asyncpg
import pytest

from looper.agents import api_v1, operator_notes, room, step_workspaces as ws, task_workspaces as tw
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.tiers.task import TaskRouter
from looper.db import agent_sessions, projects, step_workspaces as db_ws, task_worktrees, tasks
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import CLIENT, CLIENT_TOKEN, _bearer, _send, _serving, clients as clients
from tests.test_operator_auth import TOKEN
from tests.test_step_workspaces_db import _repo, _task, _workspace, git

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(operator_notes, "_root", str(tmp_path / "notes"))


@asynccontextmanager
async def _server(root):
    root.mkdir(exist_ok=True)
    async with _serving(root) as server:
        server._task_router = TaskRouter(runtime_root=root)
        server._client_ops = ClientOps(Platform(bus=SimpleNamespace(), task_router=server._task_router, runtime_root=root))
        yield server


async def _post(server, action, data, *, client=False, session="one", token=None):
    status, _, body = await _send(server, f"POST /api/v1/{action}", {
        **_bearer(token or (CLIENT_TOKEN if client else TOKEN)), "X-Looper-Session": session}, json.dumps(data))
    if status == 200 and action != "confirm":
        operation = api_v1.find("POST", f"/api/v1/{action}")
        api_v1.validate_arguments(operation.result, body["result"])
    return status, body


async def _prepare(pool, root, repo, task_id="task-a"):
    await _task(pool, task_id, str(repo), status="stopped")
    await pool.execute("UPDATE tasks SET workspace_dir=$2, workspace_mode='worktree' WHERE id=$1", task_id, str(repo))

    async def note(*_):
        pass

    work = Path(await tw.prepare(await tasks.get_task(task_id), runtime_root=root, links=(), note=note))
    session = room.run_session_dir(root, "codex", task_id, "run-example")
    session.mkdir(parents=True)
    (session / "transcript.jsonl").write_text("kept session\n")
    await agent_sessions.save_session(f"planner-{task_id}", "session-example", {})
    return work, session


@pytest.mark.parametrize("action", ["tasks/delete", "workspaces/discard"])
def test_high_requests_preserve_files_and_rows_until_same_principal_and_session_confirm(
    tmp_path, monkeypatch, clients, action,
):
    repo = tmp_path / "repo"
    _repo(repo)

    async def run(pool):
        root = tmp_path / "runtime"
        work, session_dir = await _prepare(pool, root, repo)
        workspace = await _workspace(str(work), root, "task-a", "s1")
        data = {"task_id": "task-a", **({"workspace_id": workspace.id} if action.endswith("discard") else {})}
        before = (await tasks.get_task("task-a"), await db_ws.get(workspace.id), await task_worktrees.get("task-a"))
        session_before = await agent_sessions.get_session("planner-task-a")
        step_file = Path(workspace.path) / "pending.txt"
        step_file.write_text("step pending work\n")
        async with _server(root) as server:
            status, abandoned = await _post(server, action, data, client=True)
            assert status == 202 and abandoned["status"] == "needs_confirmation" and abandoned["impact"]
            status, pending = await _post(server, action, data, client=True)
            assert status == 202 and pending["confirmation_id"] != abandoned["confirmation_id"]
            assert (await _post(server, "confirm", {"confirmation_id": abandoned["confirmation_id"]}, client=True))[0] == 404
            confirmation = {"confirmation_id": pending["confirmation_id"]}
            assert (await _post(server, "confirm", confirmation, client=True, session="other"))[0] == 404
            assert (await _post(server, "confirm", confirmation))[0] == 404
            from looper.db import api_clients
            other_token = "lpc_" + "d" * 43
            monkeypatch.setattr(api_clients, "find", lambda token: _find_other(token, other_token))
            assert (await _post(server, "confirm", confirmation, token=other_token))[0] == 404
            assert (await tasks.get_task("task-a"), await db_ws.get(workspace.id), await task_worktrees.get("task-a")) == before
            assert await agent_sessions.get_session("planner-task-a") == session_before
            assert (session_dir / "transcript.jsonl").read_text() == "kept session\n"
            assert (work / "a.txt").read_text() == "a\n" and step_file.read_text() == "step pending work\n"
            assert work.is_dir() and session_dir.is_dir() and Path(workspace.path).is_dir()
            status, result = await _post(server, "confirm", confirmation, client=True)
            assert status == 200 and result["status"] == "ok", result
            assert not Path(workspace.path).exists()
            assert (await _post(server, "confirm", confirmation, client=True))[0] == 404
            assert (await tasks.get_task("task-a") is None) == action.endswith("delete")
            audit = await pool.fetch("SELECT status, confirmation_id, client_id, chat FROM manager_actions "
                                     "WHERE tool=$1 ORDER BY id", action.replace("/", "."))
            assert [(row["status"], row["confirmation_id"]) for row in audit] == [
                ("needs_confirmation", abandoned["confirmation_id"]),
                ("needs_confirmation", pending["confirmation_id"]), ("ok", pending["confirmation_id"])]
            assert all(row["client_id"] == CLIENT["id"] and row["chat"] == "one" for row in audit)
    run_db(run, monkeypatch, timeout=30)


async def _find_other(token, other_token):
    return ({**CLIENT, "id": "client-other"} if token == other_token else CLIENT) if token in (other_token, CLIENT_TOKEN) else None


def test_expired_delete_confirmation_preserves_business_state(tmp_path, monkeypatch, clients):
    repo = tmp_path / "repo"
    _repo(repo)

    async def run(pool):
        root = tmp_path / "runtime"
        work, session = await _prepare(pool, root, repo)
        before = await tasks.get_task("task-a")
        async with _server(root) as server:
            now = [0]
            server._client_ops._clock = lambda: now[0]
            status, pending = await _post(server, "tasks/delete", {"task_id": "task-a"}, client=True)
            assert status == 202
            now[0] = 10_000
            status, answer = await _post(server, "confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 410 and answer["error"]["code"] == "expired"
            assert await tasks.get_task("task-a") == before and work.is_dir() and session.is_dir()
    run_db(run, monkeypatch, timeout=30)


def test_active_delete_rejects_without_cleanup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _repo(repo)

    async def run(pool):
        root = tmp_path / "runtime"
        work, session = await _prepare(pool, root, repo)
        await pool.execute("UPDATE tasks SET status='running' WHERE id='task-a'")
        async with _server(root) as server:
            status, body = await _post(server, "tasks/delete", {"task_id": "task-a"})
            assert status == 400 and body["error"]["code"] == "invalid"
            assert (await tasks.get_task("task-a"))["status"] == "running"
            assert work.is_dir() and session.is_dir() and await agent_sessions.get_session("planner-task-a")
    run_db(run, monkeypatch, timeout=30)


def test_finish_failure_is_conflict_and_retains_task_session_checkout_and_branch(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _repo(repo)

    async def run(pool):
        root = tmp_path / "runtime"
        work, session = await _prepare(pool, root, repo)
        git(work, "checkout", "--detach", "-q")
        before = await tasks.get_task("task-a")
        async with _server(root) as server:
            status, body = await _post(server, "tasks/delete", {"task_id": "task-a"})
            assert status == 409 and body["error"]["code"] == "conflict"
            assert await tasks.get_task("task-a") == before
            assert work.is_dir() and session.is_dir() and await agent_sessions.get_session("planner-task-a")
            assert (await task_worktrees.get("task-a"))["state"] == "active"
            assert await ws.branch_exists(str(repo), "looper/task-a/task")
    run_db(run, monkeypatch, timeout=30)


def test_delete_keeps_cleanup_order_and_unrelated_rows_files_and_committed_work(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _repo(repo)

    async def run(pool):
        root = tmp_path / "runtime"
        work, session = await _prepare(pool, root, repo)
        workspace = await _workspace(str(work), root, "task-a", "s1")
        other_work, other_session = await _prepare(pool, root, repo, "task-b")
        (work / "pending.txt").write_text("saved work\n")
        stages = []
        remove, finish, remove_sessions, delete = ws.remove_task, tw.finish, room.remove_run_sessions, tasks.delete_task

        async def step_cleanup(task_id):
            stages.append("steps")
            return await remove(task_id)

        async def task_cleanup(task_id, **kwargs):
            assert not Path(workspace.path).exists() and session.is_dir() and await tasks.get_task(task_id)
            stages.append("checkout")
            return await finish(task_id, **kwargs)

        def session_cleanup(runtime_root, task_id):
            assert not work.exists() and session.is_dir()
            stages.append("sessions")
            remove_sessions(runtime_root, task_id)

        async def db_cleanup(task_id, **kwargs):
            assert not session.exists() and await tasks.get_task(task_id)
            stages.append("database")
            return await delete(task_id, **kwargs)

        for module, name, wrapper in ((ws, "remove_task", step_cleanup), (tw, "finish", task_cleanup),
                                       (room, "remove_run_sessions", session_cleanup), (tasks, "delete_task", db_cleanup)):
            monkeypatch.setattr(module, name, wrapper)
        async with _server(root) as server:
            status, body = await _post(server, "tasks/delete", {"task_id": "task-a"})
            assert status == 200 and body["result"] == {"deleted": "task-a"}
            assert stages == ["steps", "checkout", "sessions", "database"]
            assert await tasks.get_task("task-a") is None and await agent_sessions.get_session("planner-task-a") is None
            assert await db_ws.list_for_task("task-a") == [] and await task_worktrees.get("task-a") is None
            assert git(repo, "show", "looper/task-a/task:pending.txt") == "saved work"
            assert not await ws.branch_exists(str(repo), workspace.branch)
            assert other_work.is_dir() and other_session.is_dir() and await tasks.get_task("task-b")
            assert await agent_sessions.get_session("planner-task-b") and (repo / "a.txt").read_text() == "a\n"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("case,status", [("active", 409), ("planner", 409), ("foreign", 404), ("missing", 404),
                                         ("merging", 409), ("merged", 409), ("removed", 409)])
def test_discard_refuses_active_foreign_and_illegal_workspaces_without_changes(tmp_path, monkeypatch, case, status):
    repo = tmp_path / "repo"
    _repo(repo)

    async def run(pool):
        root = tmp_path / "runtime"
        await _task(pool, "task-a", str(repo), status="running" if case == "active" else "stopped")
        await _task(pool, "task-b", str(repo), status="stopped")
        workspace = await _workspace(str(repo), root, "task-a", "s1")
        if case in ("merging", "merged"):
            workspace = replace(workspace, state=ws.StepWorkspaceState(case))
        if case == "removed":
            workspace = replace(workspace, removed_at=ws.now())
        await db_ws.save(workspace.row())
        before = await db_ws.get(workspace.id)
        async with _server(root) as server:
            monkeypatch.setattr(server._task_router, "has_planner", lambda _: case == "planner")
            code, body = await _post(server, "workspaces/discard", {
                "task_id": "task-missing" if case == "missing" else "task-b" if case == "foreign" else "task-a",
                "workspace_id": workspace.id})
            assert code == status and body["error"]["code"] == ("not_found" if status == 404 else "conflict")
            assert await db_ws.get(workspace.id) == before and Path(workspace.path).is_dir()
            assert await ws.branch_exists(str(repo), workspace.branch)
    run_db(run, monkeypatch, timeout=30)


def test_discard_holds_task_lock_and_returns_real_projection(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _repo(repo)

    async def run(pool):
        root = tmp_path / "runtime"
        await _task(pool, "task-a", str(repo), status="stopped")
        workspace = await _workspace(str(repo), root, "task-a", "s1")
        real = ws.discard

        async def while_locked(workspace):
            other = await asyncpg.connect(DSN, server_settings={"search_path": await pool.fetchval("SELECT current_schema()"),
                                                               "lock_timeout": "100ms"})
            try:
                with pytest.raises(asyncpg.LockNotAvailableError):
                    await other.execute("UPDATE tasks SET status='pending' WHERE id='task-a'")
            finally:
                await other.close()
            return await real(workspace)

        monkeypatch.setattr(ws, "discard", while_locked)
        async with _server(root) as server:
            status, body = await _post(server, "workspaces/discard", {"task_id": "task-a", "workspace_id": workspace.id})
            assert status == 200, body
            saved = ws.StepWorkspace.from_row(await db_ws.get(workspace.id))
            assert body["result"] == {"workspace": {**saved.view(), "removed_at": saved.removed_at.isoformat()}}
            assert saved.state is ws.StepWorkspaceState.DISCARDED and saved.removed_at and not saved.discardable
            assert not Path(workspace.path).exists() and not await ws.branch_exists(str(repo), workspace.branch)
            assert (await tasks.get_task("task-a"))["status"] == "stopped"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("case,expected", [("default", "worktree"), ("directory", "worktree"),
                                         ("in_place", "in_place"), ("base", "worktree"),
                                         ("non_git", "in_place"), ("bad_base", None), ("base_in_place", None)])
def test_check_preserves_defaults_modes_bases_and_warning_shapes(tmp_path, monkeypatch, clients, case, expected):
    repo = tmp_path / "repo"
    _repo(repo)
    plain = tmp_path / "plain"
    plain.mkdir()
    (repo / "a.txt").write_text("pending changes\n")

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(repo), execution={"task_workspaces": "worktree"})
        await _task(pool, "task-shared", str(repo))
        data = {"project_id": project["id"]}
        data.update({"dir": str(repo)} if case == "directory" else
                    {"mode": "in_place"} if case == "in_place" else
                    {"base": "main"} if case == "base" else
                    {"dir": str(plain), "mode": "worktree"} if case == "non_git" else
                    {"base": "missing-branch"} if case == "bad_base" else
                    {"base": "main", "mode": "in_place"} if case == "base_in_place" else {})
        async with _server(tmp_path / "runtime") as server:
            status, answer = await _post(server, "workspaces/check", data, client=True)
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
            if expected is None:
                assert status == 400 and answer["error"]["code"] == "invalid"
                assert "base" in answer["error"]["message"] or "branch" in answer["error"]["message"]
            else:
                assert status == 200, answer
                result = answer["result"]
                assert result["mode"] == expected and result["default_mode"] == "worktree" and result["notes_on"]
                assert result["notes"] == {"found": False, "files": [], "omitted": []}
                warnings = {warning["kind"]: warning for warning in result["warnings"]}
                if case == "non_git":
                    assert result["git"] is None and warnings["not_git"]["text"]
                else:
                    assert result["workspace"] == str(repo) and warnings["shared"]["tasks"][0]["task_id"] == "task-shared"
                    if expected == "worktree":
                        assert warnings["uncommitted"]["files"] == ["a.txt"]
                        assert result["git"]["base_commit"] == git(repo, "rev-parse", "main")
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action,legacy", [
    ("tasks/delete", "POST /api/tasks/delete"),
    ("workspaces/check", "GET /api/workspaces/check"),
    ("workspaces/discard", "POST /api/tasks/task-a/workspaces/{workspace_id}/discard"),
])
def test_legacy_routes_are_404_with_authenticated_v1_positive_controls(tmp_path, monkeypatch, action, legacy):
    repo = tmp_path / "repo"
    _repo(repo)

    async def run(pool):
        root = tmp_path / "runtime"
        await _task(pool, "task-a", str(repo), status="stopped")
        workspace = await _workspace(str(repo), root, "task-a", "s1")
        data = ({"dir": str(repo)} if action.endswith("check") else
                {"task_id": "task-a", **({"workspace_id": workspace.id} if action.endswith("discard") else {})})
        async with _server(root) as server:
            status, _, _ = await _send(server, legacy.format(workspace_id=workspace.id), _bearer(TOKEN), json.dumps(data))
            assert status == 404
            status, body = await _post(server, action, data)
            assert status == 200 and body["result"], body
    run_db(run, monkeypatch, timeout=30)
