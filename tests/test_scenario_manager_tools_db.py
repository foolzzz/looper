"""A scenario Manager's tools and the tasks it owns, on a real PostgreSQL: the tool set and the task scope as two
independent settings, the one authorisation check (also at confirmation), the revision check that keeps a failed
apply from leaving a narrowed scope open, attribution under the Manager's row lock, and the reads derived from a
Manager's own tasks.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.managers_api import ManagerDefinition, ManagerTools
from looper.agents.ops.service import MANAGER_TOOLS, ManagerOps, Platform, scope_for
from tests.test_gate_channels_db import _gate, _gate_step
from tests.test_managers_schema_db import _manager
from tests.test_ops_tasks_db import _Chat, _error, _project
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

REVIEW = "manager-review"


async def _definition(pool: Any, manager_id: str = REVIEW) -> ManagerDefinition:
    from looper.db import managers

    row = await managers.get_manager(manager_id, pool=pool)
    assert row is not None
    return ManagerDefinition.from_row(row)


def _scoped_chat(tmp: Path, definition: ManagerDefinition | None = None, planners: set[str] | None = None) -> _Chat:
    """A Lark chat with the Manager ``definition`` names (the default Manager without one), over a platform that
    can also answer the overview."""
    chat = _Chat(tmp, planners)
    router = SimpleNamespace(has_planner=chat.ops.platform.task_router.has_planner, running_planners=lambda: [])
    supervisor = SimpleNamespace(agent_status=lambda: {})
    manager_id = definition.id if definition is not None else "manager"
    platform = replace(chat.ops.platform, task_router=router, supervisor=supervisor,
                       manager=SimpleNamespace(agent_id=manager_id, busy=False))
    chat.ops = ManagerOps(platform, url="http://127.0.0.1:9", token="tok",
                          scope=scope_for(definition) if definition is not None else None)
    return chat


async def _review(pool: Any, tmp: Path, **values: Any) -> tuple[_Chat, _Chat]:
    """Manager ``manager-review`` made with ``values``: its chat and the default Manager's."""
    await _manager(pool, "review", **values)
    return _scoped_chat(tmp, await _definition(pool)), _scoped_chat(tmp)


async def _task(pool: Any, task_id: str, manager_id: str, project: dict[str, Any], status: str = "running") -> None:
    from looper.db import tasks

    await tasks.create_task(task_id=task_id, requirement=f"requirement of {task_id}", title=f"title of {task_id}",
                            project_id=project["id"], work_dir="/w", status=status, manager_id=manager_id, pool=pool)


async def _tasks(pool: Any) -> dict[str, dict[str, Any]]:
    project = await _project(pool)
    await _task(pool, "task-own", REVIEW, project)
    await _task(pool, "task-default", "manager", project)
    return project


def _ok(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer["status"] == "ok", answer
    return answer["result"]


def _listed(answer: dict[str, Any]) -> list[str]:
    return sorted(task["task_id"] for task in _ok(answer)["tasks"])


def _bump(pool: Any, **sets: str) -> Any:
    assignments = ", ".join(f"{name} = '{value}'" for name, value in sets.items())
    return pool.execute(f"UPDATE managers SET {assignments}, revision = revision + 1 WHERE id = '{REVIEW}'")


# --- The tool set ---


def test_a_managers_listing_is_its_scope_and_follows_the_database(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        chat, _ = await _review(pool, tmp_path, tools="tasks")
        names = [tool["name"] for tool in chat.ops.listing()]
        assert names == list(chat.ops.scope.tools)
        assert "looper_projects" in names and "looper_confirm" in names and "looper_settings" not in names
        assert names == [tool.name for tool in MANAGER_TOOLS if tool.name in names]  # in MANAGER_TOOLS order
        assert [tool["name"] for tool in _Chat(tmp_path).ops.listing()] == [tool.name for tool in MANAGER_TOOLS]

        await _bump(pool, tools="all")  # no set_scope: the fleet's apply has not run
        assert [tool["name"] for tool in chat.ops.listing()] == names
        assert [tool["name"] for tool in await chat.ops.current_listing()] == [tool.name for tool in MANAGER_TOOLS]

    _run(test, monkeypatch)


@pytest.mark.parametrize("tools,task_scope", [("all", "own"), ("tasks", "all"), ("tasks", "own"), ("all", "all")])
def test_tools_and_task_scope_are_independent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: str,
                                              task_scope: str) -> None:
    async def test(pool: Any) -> None:
        chat, default = await _review(pool, tmp_path, tools=tools, task_scope=task_scope)
        await _tasks(pool)
        settings = ("looper_settings", {"action": "view"})
        (answer,) = await chat.say("x", settings)
        if tools == "all":
            _ok(answer)
        else:
            assert _error(answer) == "forbidden" and "no longer has looper_settings" in answer["error"]["message"]
        (answer,) = await chat.say("x", ("looper_tasks", {"action": "list"}))
        assert _listed(answer) == (["task-own"] if task_scope == "own" else ["task-default", "task-own"])
        (answer,) = await chat.say("x", ("looper_task_instruct", {"task_id": "task-default", "text": "hi"}))
        if task_scope == "own":
            assert _error(answer) == "forbidden" and "belongs to another Manager" in answer["error"]["message"]
        else:
            _ok(answer)
        (answer,) = await chat.say("x", ("looper_overview", {}))
        assert _ok(answer)["tasks"]["total"] == (1 if task_scope == "own" else 2)
        (answer,) = await chat.say("x", ("looper_task_control", {"task_id": "task-own", "action": "stop"}))
        _ok(answer)
        # The default Manager acts on a scoped Manager's task.
        (answer,) = await default.say("x", ("looper_task_instruct", {"task_id": "task-own", "text": "hi"}))
        _ok(answer)

    _run(test, monkeypatch)


def test_a_task_scope_change_is_in_force_before_the_fleet_applies_it(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        from looper.db import tasks

        chat, default = await _review(pool, tmp_path, tools="all", task_scope="all")
        await _tasks(pool)
        get = ("looper_tasks", {"action": "get", "task_id": "task-default"})
        _ok((await chat.say("x", get))[0])
        await _bump(pool, task_scope="own")
        assert _error((await chat.say("x", get))[0]) == "forbidden"
        await _bump(pool, task_scope="all")
        _ok((await chat.say("x", get))[0])

        # A confirmation asked under all, then the switch to own: the recheck refuses it.
        await pool.execute("UPDATE tasks SET status = 'failed' WHERE id = 'task-default'")
        (asked,) = await chat.say("cancel it", ("looper_task_control", {"task_id": "task-default",
                                                                         "action": "start_over"}))
        assert asked["status"] == "needs_confirmation"
        await _bump(pool, task_scope="own")
        (answer,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert _error(answer) == "forbidden" and "no longer do it" in answer["error"]["message"]
        assert (await tasks.get_task("task-default", pool=pool))["status"] == "failed"
        assert default.sent() == [] and chat.sent() == []

    _run(test, monkeypatch)


def test_the_revision_check_never_falls_back_to_the_old_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        from looper.db import managers

        chat, default = await _review(pool, tmp_path, tools="all")
        settings = ("looper_settings", {"action": "set", "values": {"run_budget_usd": 3}})
        view = ("looper_settings", {"action": "view"})
        before = _ok((await default.say("x", view))[0])

        await _bump(pool, tools="tasks")
        (answer,) = await chat.say("x", settings)
        assert _error(answer) == "forbidden"
        assert _ok((await default.say("x", view))[0]) == before

        # The database cannot be read: nothing runs, whatever the old scope said.
        await _bump(pool, tools="all")
        real = managers.revision_of

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise OSError("down")

        monkeypatch.setattr(managers, "revision_of", broken)
        (answer,) = await chat.say("x", settings)
        assert _error(answer) == "unavailable" and "try again in a moment" in answer["error"]["message"]
        monkeypatch.setattr(managers, "revision_of", real)

        await pool.execute(f"UPDATE managers SET enabled = false WHERE id = '{REVIEW}'")
        (answer,) = await chat.say("x", settings)
        assert _error(answer) == "forbidden" and "turned off" in answer["error"]["message"]
        assert _ok((await default.say("x", view))[0]) == before
        with pytest.raises(Exception, match="turned off"):
            await chat.ops.current_listing()

    _run(test, monkeypatch)


def test_projects_are_read_only_for_a_tasks_manager_and_the_refusal_is_audited(monkeypatch: pytest.MonkeyPatch,
                                                                              tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        chat, _ = await _review(pool, tmp_path, tools="tasks")
        await _project(pool)
        _ok((await chat.say("x", ("looper_projects", {"action": "list"})))[0])
        (answer,) = await chat.say("x", ("looper_projects", {"action": "create", "name": "new"}))
        assert _error(answer) == "forbidden" and "can only read projects" in answer["error"]["message"]
        row = await pool.fetchrow("SELECT status, manager_id, error_code FROM manager_actions "
                                  "WHERE tool = 'looper_projects' AND status = 'error'")
        assert tuple(row) == ("error", REVIEW, "forbidden")

    _run(test, monkeypatch)


def test_a_set_scope_swaps_everything_at_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        chat, _ = await _review(pool, tmp_path, tools="tasks")
        before = len(chat.ops.listing())
        (answer,) = await chat.say("x", ("looper_projects", {"action": "create", "name": "new"}))
        assert _error(answer) == "forbidden"
        await _bump(pool, tools="all")
        chat.ops.set_scope(scope_for(await _definition(pool)))
        assert len(chat.ops.listing()) == len(MANAGER_TOOLS) > before
        (answer,) = await chat.say("x", ("looper_projects", {"action": "create", "name": "new"}))
        _ok(answer)

    _run(test, monkeypatch)


# --- Confirmations ---


@pytest.mark.parametrize("set_scope_drops", [True, False])
def test_a_confirmation_is_rechecked_against_the_scope_in_force(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                set_scope_drops: bool) -> None:
    async def test(pool: Any) -> None:
        chat, default = await _review(pool, tmp_path, tools="all")
        view = ("looper_settings", {"action": "view"})
        before = _ok((await default.say("x", view))[0])
        (asked,) = await chat.say("set it", ("looper_settings", {"action": "set", "values": {"run_budget_usd": 9}}))
        assert asked["status"] == "needs_confirmation"
        narrowed = scope_for(replace(await _definition(pool), tools=ManagerTools.TASKS))
        if not set_scope_drops:  # the recheck alone must refuse
            monkeypatch.setattr(ManagerOps, "set_scope", lambda self, scope: setattr(self, "_scope", scope))
        chat.ops.set_scope(narrowed)
        assert bool(chat.ops._pending) is not set_scope_drops
        (answer,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert _error(answer) == ("not_found" if set_scope_drops else "forbidden")
        assert _ok((await default.say("x", view))[0]) == before

    _run(test, monkeypatch)


def test_a_confirmation_for_a_task_that_changed_owner_does_not_run(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        from looper.db import manager_actions, tasks

        chat, _ = await _review(pool, tmp_path, task_scope="own")
        await _tasks(pool)
        (asked,) = await chat.say("again", ("looper_task_control", {"task_id": "task-own", "action": "cancel"}))
        assert asked["status"] == "needs_confirmation"
        await pool.execute("UPDATE tasks SET manager_id = 'manager' WHERE id = 'task-own'")
        (answer,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert _error(answer) == "forbidden"
        assert (await tasks.get_task("task-own", pool=pool))["status"] == "running"
        assert chat.sent() == []
        row = await pool.fetchrow("SELECT status, error_code, confirmation_id, manager_id FROM manager_actions "
                                  "WHERE confirmation_id = $1 AND status = 'error'", asked["confirmation_id"])
        assert (row["error_code"], row["manager_id"]) == ("forbidden", REVIEW)
        assert await manager_actions.for_task("task-own", pool=pool)

    _run(test, monkeypatch)


# --- Creating tasks ---


def test_a_task_is_attributed_to_the_manager_that_creates_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        from looper.db import tasks

        chat, default = await _review(pool, tmp_path)
        await _project(pool)
        arguments = {"requirement": "r", "title": "t", "project": "calc"}
        scoped = _ok((await chat.say("go", ("looper_task_create", arguments)))[0])
        plain = _ok((await default.say("go", ("looper_task_create", arguments)))[0])
        assert (await tasks.get_task(scoped["task_id"], pool=pool))["manager_id"] == REVIEW
        assert (await tasks.get_task(plain["task_id"], pool=pool))["manager_id"] == "manager"
        row = await pool.fetchrow("SELECT manager_id FROM manager_actions WHERE tool = 'looper_task_create' "
                                  "AND task_id = $1", scoped["task_id"])
        assert row["manager_id"] == REVIEW

    _run(test, monkeypatch)


@pytest.mark.parametrize("profiles", [False, True])
def test_a_turned_off_manager_creates_no_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                              profiles: bool) -> None:
    async def test(pool: Any) -> None:
        chat, _ = await _review(pool, tmp_path)
        await _project(pool)
        if profiles:
            await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) VALUES ('agent-sonnet', "
                               "'claude-code/sonnet', 'rt-claude-code', (SELECT id FROM llm_models WHERE provider_id = "
                               "'prov-claude-subscription' AND name = 'sonnet'))")
        arguments: dict[str, Any] = {"requirement": "r", "title": "t", "project": "calc"}
        if profiles:
            arguments["profiles"] = {"worker": "claude-code/sonnet"}
        await pool.execute(f"UPDATE managers SET enabled = false WHERE id = '{REVIEW}'")
        # The ops object outlives the row's state (an apply not yet run): make the revision check see no change.
        chat.ops._scope = replace(chat.ops.scope, revision=-1)
        (answer,) = await chat.say("go", ("looper_task_create", arguments))
        assert _error(answer) == "conflict" and "turned off or deleted" in answer["error"]["message"]
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0

    _run(test, monkeypatch)


def test_a_task_waits_for_a_delete_that_holds_the_managers_row(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        chat, _ = await _review(pool, tmp_path)
        await _project(pool)
        chat.ops._scope = replace(chat.ops.scope, revision=-1)
        arguments = {"requirement": "r", "title": "t", "project": "calc"}
        async with pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()
            await conn.execute(f"SELECT 1 FROM managers WHERE id = '{REVIEW}' FOR UPDATE")
            call = asyncio.ensure_future(chat.say("go", ("looper_task_create", arguments)))
            await asyncio.sleep(0.5)
            assert not call.done()
            await conn.execute(f"UPDATE managers SET deleted_at = now(), enabled = false WHERE id = '{REVIEW}'")
            await transaction.commit()
        (answer,) = await asyncio.wait_for(call, 10)
        assert _error(answer) == "conflict" and "turned off or deleted" in answer["error"]["message"]
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0

    _run(test, monkeypatch)


# --- Acting on tasks ---


def test_a_scoped_manager_acts_only_on_its_own_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        from looper.db import tasks

        chat, default = await _review(pool, tmp_path, task_scope="own")
        project = await _tasks(pool)
        await _task(pool, "task-ready-default", "manager", project, status="init")
        await _task(pool, "task-ready-own", REVIEW, project, status="init")
        (answer,) = await chat.say("x", ("looper_task_control", {"task_id": "task-ready-default", "action": "start"}))
        assert _error(answer) == "forbidden"
        assert (await tasks.get_task("task-ready-default", pool=pool))["status"] == "init"
        (answer,) = await chat.say("x", ("looper_task_instruct", {"task_id": "task-default", "text": "hi"}))
        assert _error(answer) == "forbidden"
        _ok((await chat.say("x", ("looper_task_control", {"task_id": "task-ready-own", "action": "start"})))[0])
        (sent,) = chat.sent()
        assert sent.source.agent_id == REVIEW
        _ok((await default.say("x", ("looper_task_control", {"task_id": "task-own", "action": "stop"})))[0])
        (sent,) = default.sent()
        assert sent.source.agent_id == "manager"
        (answer,) = await chat.say("x", ("looper_task_gate_reply", {"task_id": "task-default", "gate": "g1"}))
        assert _error(answer) == "forbidden"
        (answer,) = await chat.say("x", ("looper_tasks", {"action": "list"}))
        assert _listed(answer) == ["task-own", "task-ready-own"]
        (answer,) = await chat.say("x", ("looper_tasks", {"action": "list", "query": "default"}))
        assert _listed(answer) == []

    _run(test, monkeypatch)


# --- Reads derived from a Manager's tasks ---


def test_the_overview_of_a_scoped_manager_counts_its_own_tasks(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        from looper.db import llm_records, plans

        chat, default = await _review(pool, tmp_path, task_scope="own")
        project = await _tasks(pool)
        for task_id in ("task-own", "task-default"):
            step_id = f"step-{task_id}"
            await plans.save(task_id, plan={"plan_id": f"plan-{task_id}", "status": "executing"},
                             steps=[{**_gate_step(step_id), "status": "waiting_user"}],
                             gates=[_gate(step_id, state="open", opened_at=datetime.now(timezone.utc))])
        now = datetime.now(timezone.utc)

        def run(agent_id: str, task_id: str, role: str) -> dict[str, Any]:
            return {"agent_id": agent_id, "role": role, "task_id": task_id, "project_id": project["id"],
                    "step": "s", "instruction": "i", "state": "running", "since": now - timedelta(seconds=5),
                    "task_role": None}

        pool_runs = [run("worker-1", "task-own", "worker"), run("worker-2", "task-default", "worker")]
        platform = replace(chat.ops.platform, worker_pool=SimpleNamespace(runs=lambda: pool_runs),
                           supervisor=SimpleNamespace(agent_status=lambda: {
                               REVIEW: {"healthy": True, "state": "running", "up": True, "restart_count": 0,
                                        "last_crash_at": None, "next_restart_at": None},
                               "worker-pool": {"healthy": False, "state": "restarting", "up": False,
                                               "restart_count": 1, "last_crash_at": None, "next_restart_at": None}}),
                           task_router=SimpleNamespace(
                               has_planner=lambda task_id: True,
                               running_planners=lambda: [
                                   {"planner_id": f"planner-{t}", "task_id": t, "project_id": project["id"],
                                    "requirement": "r", "name": t} for t in ("task-own", "task-default")]))
        chat.ops = ManagerOps(platform, url="http://127.0.0.1:9", token="tok", scope=chat.ops.scope)
        default.ops = ManagerOps(replace(platform, manager=SimpleNamespace(agent_id="manager", busy=False)),
                                 url="http://127.0.0.1:9", token="tok")
        for agent_id, task_id in (("manager-review", "task-own"), ("worker-x", "task-default"),
                                  ("planner-task-default", "task-default"), ("manager", None)):
            record = await llm_records.start_record(agent_id=agent_id, task_id=task_id, pool=pool)
            await llm_records.finish_record(record, status="completed", cost_usd=1.0, input_tokens=1, output_tokens=1,
                                            cache_read_tokens=0, cache_creation_tokens=0, context_tokens=None,
                                            compactions=[], pool=pool)

        scoped = _ok((await chat.say("x", ("looper_overview", {})))[0])
        full = _ok((await default.say("x", ("looper_overview", {})))[0])
        assert set(scoped) == set(full)
        assert scoped["tasks"]["total"] == 1 and full["tasks"]["total"] == 2
        assert [r["task_id"] for r in scoped["step_runs"]] == ["task-own"]
        assert sorted(r["task_id"] for r in full["step_runs"]) == ["task-default", "task-own"]
        assert [r["task_id"] for r in scoped["running_tasks"]] == ["task-own"]
        assert scoped["waiting_gates"] == 1 and full["waiting_gates"] == 2
        assert scoped["projects"] == [{**scoped["projects"][0], "tasks": {"running": 1, "ready": 0},
                                       "workers": {"running": 1, "waiting": 0},
                                       "reviewers": {"running": 0, "waiting": 0}}]
        assert full["projects"][0]["tasks"]["running"] == 2
        # Its own run and its task's runs, not the other task's or the default Manager's.
        assert scoped["spend_today"]["runs"] == 1 and full["spend_today"]["runs"] == 4
        assert scoped["unhealthy"] == []
        assert [entry["name"] for entry in full["unhealthy"]] == ["worker-pool"]

    _run(test, monkeypatch)


def test_projects_count_only_a_scoped_managers_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        chat, default = await _review(pool, tmp_path, task_scope="own")
        await _tasks(pool)
        (listed,) = _ok((await chat.say("x", ("looper_projects", {"action": "list"})))[0])["projects"]
        assert (listed["tasks"], listed["active_tasks"], listed["schedules"]) == (1, 1, 0)
        got = _ok((await chat.say("x", ("looper_projects", {"action": "get", "project": "calc"})))[0])["project"]
        assert (got["task_count"], got["active_count"], got["schedule_count"]) == (1, 1, 0)
        (listed,) = _ok((await default.say("x", ("looper_projects", {"action": "list"})))[0])["projects"]
        assert listed["tasks"] == 2

    _run(test, monkeypatch)


def test_the_costs_of_a_scoped_manager_name_only_its_own_tasks(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        from looper.db import llm_records

        chat, default = await _review(pool, tmp_path, tools="all", task_scope="own")
        await _tasks(pool)
        for agent_id, task_id in (("planner-task-own", "task-own"), ("planner-task-default", "task-default")):
            record = await llm_records.start_record(agent_id=agent_id, task_id=task_id, pool=pool)
            await llm_records.finish_record(record, status="completed", cost_usd=1.0, input_tokens=1, output_tokens=1,
                                            cache_read_tokens=0, cache_creation_tokens=0, context_tokens=None,
                                            compactions=[], pool=pool)
        for arguments in ({"project": "calc"}, {}):
            scoped = _ok((await chat.say("x", ("looper_costs", arguments)))[0])
            full = _ok((await default.say("x", ("looper_costs", arguments)))[0])
            assert scoped["total"]["runs"] == 1 and full["total"]["runs"] == 2, (scoped, full)
            if "project" in arguments:
                assert [t["task_id"] for t in scoped["tasks"]] == ["task-own"]
                assert sorted(t["task_id"] for t in full["tasks"]) == ["task-default", "task-own"]
            else:
                assert [p["tasks"] for p in scoped["projects"]] == [1] and [p["tasks"] for p in full["projects"]] == [2]

    _run(test, monkeypatch)


# --- Workspaces ---


def test_a_workspace_warning_does_not_name_another_managers_task(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    from tests.test_task_workspaces_db import _repo

    async def test(pool: Any) -> None:
        from looper.db import projects, tasks

        repo = _repo(tmp_path / "repo")
        chat, default = await _review(pool, tmp_path)
        project = await projects.create_project(name="calc", work_dir=str(repo), pool=pool)
        for task_id, manager_id in (("task-a", "manager"), ("task-b", REVIEW)):
            await tasks.create_task(task_id=task_id, requirement="r", title=f"secret title {task_id}",
                                    project_id=project["id"], work_dir=str(repo), workspace_dir=str(repo),
                                    workspace_mode="in_place", status="running", manager_id=manager_id, pool=pool)
        arguments = {"requirement": "r", "title": "t", "project": "calc", "workspace_mode": "in_place"}
        answer = await chat.say("go", ("looper_task_create", arguments))
        result = _ok(answer[0])
        block = chat.turns[-1].blocks[0]
        assert "secret title task-a" not in json.dumps(answer) + block
        assert "另有 1 个其他管家的任务" in block and "secret title task-b" in block
        from looper.agents import task_workspaces

        check = await task_workspaces.check(str(repo), mode=task_workspaces.TaskWorkspaces.IN_PLACE,
                                            visible_to=REVIEW)
        (warning,) = [w for w in check["warnings"] if w["kind"] == "shared"]
        foreign = [t for t in warning["tasks"] if t.get("foreign")]
        assert foreign == [{"task_id": None, "name": "a task of another Manager", "status": None,
                            "mode": "in_place", "foreign": True}]
        assert "secret title task-a" not in json.dumps(check)
        assert result["task_id"]

        plain = await default.say("go", ("looper_task_create", arguments))
        _ok(plain[0])
        assert "secret title task-b" in default.turns[-1].blocks[0]

    _run(test, monkeypatch)
