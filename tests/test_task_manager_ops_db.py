"""Task-owned tools and review hooks over isolated HTTP and PostgreSQL."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest

from looper.agents import outbound, outbound_hook, task_conversations_settings as settings
from looper.agents.bus import MessageBus
from looper.agents.http_server import AgentHTTPServer
from looper.agents.library import EMPTY_LIBRARY
from looper.agents.ops import OpError
from looper.agents.ops.planner import PlannerOps
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.ops.task_manager import TaskManagerOps
from looper.db import activity, projects, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def work_mode(monkeypatch):
    monkeypatch.setattr(settings, "_enabled", True)


def _platform(tmp_path):
    return Platform(bus=MessageBus(runtime_root=tmp_path),
                    task_router=SimpleNamespace(has_planner=lambda _: False), runtime_root=tmp_path)


async def _task(pool, tmp_path, task_id="task-one", *, status="draft", mode="in_place"):
    project = await projects.create_project(name=task_id, work_dir=str(tmp_path), pool=pool)
    await tasks.create_task(task_id=task_id, requirement="Original requirement", project_id=project["id"],
                            title=task_id, status=status, work_dir=str(tmp_path), workspace_dir=str(tmp_path),
                            workspace_mode=mode, original_user_text="Implement this change", pool=pool)
    return task_id


@asynccontextmanager
async def _http(tmp_path, platform, ops):
    planners = PlannerOps(platform, url="http://127.0.0.1:9")
    manager = ManagerOps(platform, url="http://127.0.0.1:9", token="manager-test-token")
    server = AgentHTTPServer(host="127.0.0.1", port=0, static_dir=tmp_path, runtime_root=tmp_path,
        web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(), task_router=platform.task_router,
        manager_ops=manager, planner_ops=planners, task_manager_ops=ops)
    await server.start()
    url = f"http://127.0.0.1:{server._port}"
    assert urlparse(url).port != 8767
    ops._url = url
    try:
        async with httpx.AsyncClient(base_url=url, timeout=10, trust_env=False) as client:
            async def request(token, path="tools", arguments=None):
                headers = {"Authorization": f"Bearer {token}"}
                if arguments is None:
                    return await client.get("/api/agent/v1/" + path, headers=headers)
                return await client.post("/api/agent/v1/" + path, headers=headers, json=arguments)
            yield request, planners
    finally:
        await server.stop()


def test_token_scope(monkeypatch, tmp_path):
    async def check(pool):
        await _task(pool, tmp_path)
        await _task(pool, tmp_path, "task-other")
        platform = _platform(tmp_path)
        ops = TaskManagerOps(platform, url="http://127.0.0.1:9")
        token = ops.issue("task-one")
        assert token.startswith("taskmgr.") and ops.claims(f"Bearer {token}")
        assert token not in repr(ops._grants)
        async with _http(tmp_path, platform, ops) as (request, planners):
            response = await request(token)
            assert response.status_code == 200
            assert {t["name"] for t in response.json()["tools"]} == {"task_status", "task_requirement_set"}
            response = await request(token, "ops/task_status", {})
            assert response.json()["result"]["task_id"] == "task-one"
            other = ops.issue("task-other")
            response = await request(other, "ops/task_status", {})
            assert response.json()["result"]["task_id"] == "task-other"
            assert (await request(token)).status_code == 200
            assert (await request(planners.issue("task-one"))).status_code == 200
            planner_tools = (await request(planners.issue("task-other"))).json()["tools"]
            assert "looper_task_profiles" in {t["name"] for t in planner_tools}
            assert "task_status" not in {t["name"] for t in planner_tools}
            manager_tools = (await request("manager-test-token")).json()["tools"]
            assert "looper_tasks" in {t["name"] for t in manager_tools}
            assert "task_status" not in {t["name"] for t in manager_tools}
            newer = ops.issue("task-one")
            assert (await request(token)).status_code == 401
            ops.revoke(token)
            assert (await request(newer)).status_code == 200
            await tasks.update_task_status("task-one", "completed", pool=pool)
            assert (await request(newer, "ops/task_status", {})).json()["status"] == "ok"
            ops.revoke(newer)
            response = await request(newer)
            assert response.status_code == 401
            assert response.json()["error"] == "this task Manager's token no longer works"
    run_db(check, monkeypatch)


def test_write_only_in_turn(monkeypatch, tmp_path):
    async def check(pool):
        await _task(pool, tmp_path)
        ops = TaskManagerOps(_platform(tmp_path), url="http://127.0.0.1:9")
        token = ops.issue("task-one")
        header = f"Bearer {token}"
        answer = await ops.call(header, "task_requirement_set", {"requirement": "New requirement"})
        assert answer["error"]["code"] == "forbidden"
        assert "not in a turn Looper sent" in answer["error"]["message"]
        assert await pool.fetchval("SELECT count(*) FROM task_requirement_versions") == 0
        ops.begin_turn(token)
        try:
            answer = await ops.call(header, "task_requirement_set", {"requirement": "New requirement"})
            assert answer["status"] == "ok", answer
        finally:
            ops.end_turn(token)
        rows = await pool.fetch("SELECT version,text,via FROM task_requirement_versions ORDER BY version")
        assert [tuple(r) for r in rows] == [(1, "Original requirement", "created"),
                                          (2, "New requirement", "task_manager")]
        assert (await tasks.get_task("task-one"))["requirement"] == "New requirement"
        answer = await ops.call(header, "task_requirement_set", {"requirement": "Another requirement"})
        assert answer["error"]["code"] == "forbidden"
    run_db(check, monkeypatch)


@pytest.mark.parametrize("status", ["draft", "init", "executing", "completed"])
def test_requirement_set_modes(monkeypatch, tmp_path, status):
    async def check(pool):
        await _task(pool, tmp_path, status=status)
        ops = TaskManagerOps(_platform(tmp_path), url="http://127.0.0.1:9")
        token = ops.issue("task-one")
        header = f"Bearer {token}"
        ops.begin_turn(token)
        listed = {t["name"] for t in await ops.listing(header)}
        assert listed == ({"task_status"} if status == "executing" else {"task_status", "task_requirement_set"})
        answer = await ops.call(header, "task_requirement_set", {"requirement": "Confirmed requirement"})
        if status in ("draft", "init"):
            assert answer["status"] == "ok", answer
            assert (await tasks.get_task("task-one"))["requirement"] == "Confirmed requirement"
        else:
            assert answer["error"]["code"] == "conflict"
            message = answer["error"]["message"]
            if status == "executing":
                assert message == "the background is working on this task"
            else:
                assert message == ("the task is completed: only a task that has not ended can change; "
                                   "start it again to run the requirement it has")
            assert await pool.fetchval("SELECT count(*) FROM task_requirement_versions") == 0
    run_db(check, monkeypatch)


def test_task_status_shape(monkeypatch, tmp_path):
    work = tmp_path / "checkout"
    work.mkdir()
    for command in (("init", "-b", "task-branch"), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-m", "Seed")):
        subprocess.run(["git", "-C", str(work), *command], check=True, capture_output=True)
    (work / "uncommitted.txt").write_text("changed")

    async def check(pool):
        await _task(pool, work, mode="worktree")
        await pool.execute("INSERT INTO task_journal(task_id,step_id,kind,summary) VALUES "
                           "('task-one','','progress','Latest progress')")
        await activity.insert_activity(task_id="task-one", kind="phase", summary="Recent activity", pool=pool)
        ops = TaskManagerOps(_platform(tmp_path), url="http://127.0.0.1:9")
        header = f"Bearer {ops.issue('task-one')}"
        answer = await ops.call(header, "task_status", {})
        assert answer["status"] == "ok", answer
        result = answer["result"]
        assert result["interaction_mode"] == "direct"
        assert result["requirement"] == '<untrusted source="task_requirement">\nOriginal requirement\n</untrusted>'
        assert result["latest_progress"] == '<untrusted source="journal">\nLatest progress\n</untrusted>'
        assert result["activity"][0]["summary"] == '<untrusted source="journal">\nRecent activity\n</untrusted>'
        assert result["workspace"] == {"dir": str(work), "mode": "worktree", "branch": "task-branch",
            "head": subprocess.check_output(["git", "-C", str(work), "rev-parse", "--short", "HEAD"], text=True).strip(),
            "changed": ["uncommitted.txt"], "uncommitted": 1}
        for index in range(55):
            (work / f"changed-{index}.txt").write_text("changed")
        workspace = (await ops.call(header, "task_status", {}))["result"]["workspace"]
        assert len(workspace["changed"]) == 50 and workspace["uncommitted"] == 56
        plain = tmp_path / "plain"
        plain.mkdir()
        await _task(pool, plain, "task-plain")
        result = (await ops.call(f"Bearer {ops.issue('task-plain')}", "task_status", {}))["result"]
        assert result["workspace"] == {"dir": str(plain), "mode": "in_place", "branch": None, "head": None,
                                       "changed": [], "uncommitted": 0}
    run_db(check, monkeypatch)


def test_outbound_check_through_token(monkeypatch, tmp_path):
    reviews = []

    async def review(action, asker, origin, **kwargs):
        reviews.append((action, asker, origin, kwargs))
        return outbound.Review(outbound.Decision.ALLOW, "Allowed test action")

    monkeypatch.setattr(outbound, "review", review)

    async def check(pool):
        await _task(pool, tmp_path)
        platform = _platform(tmp_path)
        ops = TaskManagerOps(platform, url="http://127.0.0.1:9")
        token = ops.issue("task-one")
        payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "tool_use_id": "call-one"}
        async with _http(tmp_path, platform, ops) as (request, _):
            hook = ops.tool_hook(token, "task-one", tmp_path, "not-a-real-model", EMPTY_LIBRARY)
            config = tmp_path / ".local" / "outbound-hook" / "task-manager-task-one.json"
            saved = config.read_bytes()
            assert urlparse(json.loads(saved)["url"]).port != 8767
            response = await request(token, "outbound/check", payload)
            assert response.status_code == 200 and response.json() == {"decision": "allow"}
            assert len(reviews) == 1
            action, asker, origin, context = reviews[0]
            assert action.target == "git push"
            assert asker.who is outbound.Who.OPERATOR
            assert asker.message == "Implement this change"
            assert origin.task_id == "task-one" and origin.agent_id == "task-manager-task-one"
            assert any(text == "Original requirement" for _, text in context["context"])
            ops.revoke(token)
            assert not config.exists()
            assert (await request(token, "outbound/check", payload)).status_code == 401
            # A stale client config must fail closed even while this HTTP server still answers.
            stale = tmp_path / "stale-hook.json"
            stale.write_bytes(saved)
            process = await asyncio.create_subprocess_exec(sys.executable, "-I", str(outbound_hook.CLIENT), str(stale),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), 10)
            assert process.returncode == 0, stderr
            output = json.loads(stdout)["hookSpecificOutput"]
            assert output["permissionDecision"] == json.loads(outbound_hook.DENY_OUTPUT)["hookSpecificOutput"]["permissionDecision"]
            assert "did not answer" in output["permissionDecisionReason"]
            assert "outbound_hook_client.py" in hook.command
            fallback = await asyncio.create_subprocess_exec("/bin/sh", "-c",
                outbound_hook.hook_command(stale, python="not-an-interpreter"),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(fallback.communicate(), 10)
            assert fallback.returncode == 0, stderr
            assert stdout.decode() == outbound_hook.DENY_OUTPUT
            assert len(reviews) == 1
    run_db(check, monkeypatch)


@pytest.mark.parametrize(("name", "arguments", "error"), [
    ("unknown_tool", {}, "not_found"),
    ("task_status", {"task_id": "task-other"}, "invalid"),
    ("task_requirement_set", {"requirement": "Updated", "task_id": "task-other"}, "invalid"),
    ("task_requirement_set", {"requirement": ""}, "invalid"),
    ("task_requirement_set", {"requirement": "x" * 20001}, "invalid"),
], ids=["unknown", "status-task-id", "write-task-id", "empty", "too-long"])
def test_closed_schemas(monkeypatch, tmp_path, name, arguments, error):
    async def check(pool):
        await _task(pool, tmp_path)
        ops = TaskManagerOps(_platform(tmp_path), url="http://127.0.0.1:9")
        token = ops.issue("task-one")
        ops.begin_turn(token)
        answer = await ops.call(f"Bearer {token}", name, arguments)
        assert answer["error"]["code"] == error
        assert (await tasks.get_task("task-one"))["requirement"] == "Original requirement"
    run_db(check, monkeypatch)


def test_deleted_task_and_disabled_switch_serve_no_tools(monkeypatch, tmp_path):
    async def check(pool):
        await _task(pool, tmp_path)
        platform = _platform(tmp_path)
        ops = TaskManagerOps(platform, url="http://127.0.0.1:9")
        token = ops.issue("task-one")
        async with _http(tmp_path, platform, ops) as (request, _):
            await pool.execute("DELETE FROM tasks WHERE id='task-one'")
            assert (await request(token)).status_code == 401
            await _task(pool, tmp_path, "task-enabled")
            token = ops.issue("task-enabled")
            assert (await request(token)).status_code == 200
            settings.set_enabled(False)
            assert await ops.listing(f"Bearer {token}") == []
            assert (await request(token)).status_code == 401
            with pytest.raises(OpError):
                ops.issue("task-enabled")
            assert not ops.authorized(f"Bearer {token}")
    run_db(check, monkeypatch)
