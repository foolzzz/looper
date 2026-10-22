"""Scenario Managers and their task templates over ``/api/managers`` (``looper/agents/managers_api.py``), on a real
PostgreSQL: validation, the limit on enabled Managers under concurrency, soft delete, profile and project usage.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable

import pytest

from tests.test_task_agents_db import _call, _profiles
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

EXAMPLE = Path(__file__).resolve().parent.parent / "docs" / "examples" / "manager-github-pr-review.json"
PROMPT = "You review code."


def _body(key: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "name": key.replace("-", " ").title(), "system_prompt": PROMPT, **extra}


async def _create(key: str, **extra: Any) -> tuple[int, Any]:
    return await _call("POST", "/api/managers", _body(key, **extra))


async def _list() -> dict[str, dict[str, Any]]:
    status, answer = await _call("GET", "/api/managers")
    assert status == 200, answer
    return {m["id"]: m for m in answer["managers"]}


async def _project(name: str = "calc") -> str:
    from looper.db import projects

    return (await projects.create_project(name=name, work_dir="/w"))["id"]


async def _profile(pool: Any, profile_id: str = "agent-one") -> str:
    await pool.execute(
        "INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
        "SELECT $1, $1, runtime_id, model_id FROM agent_profiles WHERE builtin ORDER BY id LIMIT 1", profile_id)
    return profile_id


def _template(**extra: Any) -> dict[str, Any]:
    return {"name": "PR review", "requirement": "Review {{pr_url}}.",
            "inputs": [{"key": "pr_url", "label": "Link", "kind": "url"}], **extra}


class _Listener:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, manager_id: str) -> None:
        self.calls.append(manager_id)


async def _blocked(pool: Any, count: int = 1) -> None:
    for _ in range(500):
        if await pool.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                               "AND wait_event_type = 'Lock'") >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no request is waiting for a lock")


def test_the_default_manager_is_listed_and_creating_checks_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _profiles(pool)
        status, answer = await _call("GET", "/api/managers")
        (default,) = answer["managers"]
        assert (status, answer["max_enabled"]) == (200, 8)
        assert (default["id"], default["builtin"], default["tools"], default["task_scope"], default["templates"]) == (
            "manager", True, "all", "all", [])

        status, answer = await _create("code-review")
        assert status == 201, answer
        made = answer["manager"]
        assert (made["id"], made["tools"], made["task_scope"], made["task_policy"], made["enabled"],
                made["builtin"]) == ("manager-code-review", "tasks", "own", "free", True, False)
        assert (await _list())["manager-code-review"]["name"] == "Code Review"
        status, answer = await _create("wide", tools="all", task_scope="own")
        assert status == 201 and (answer["manager"]["tools"], answer["manager"]["task_scope"]) == ("all", "own")
        status, answer = await _create("seer", tools="tasks", task_scope="all", description=" a\nb ")
        assert status == 201 and (answer["manager"]["task_scope"], answer["manager"]["description"]) == ("all", "a b")

        refused = [
            (_body("x1", color="red"), 400, "unknown field 'color'; the fields are key, name"),
            (_body("Code"), 400, "key is required"),
            (_body("a"), 400, "key is required"),
            (_body("k" * 32), 400, "key is required"),
            (_body("code-review"), 409, "is used by another Manager"),
            ({**_body("other"), "name": "CODE review"}, 409, "name 'CODE review' is used by another Manager"),
            ({**_body("other"), "system_prompt": "  "}, 400, "system_prompt is required"),
            ({**_body("other"), "system_prompt": "x" * 20_001}, 400, "system_prompt has 20001"),
            ({**_body("other"), "profile_id": "agent-nope"}, 400, "no Agent Profile"),
            ({**_body("other"), "profile_id": "agent-one-off"}, 400, "cannot run the manager"),
            (_body("other", tools="admin"), 400, "tools must be 'all'"),
            (_body("other", task_scope="some"), 400,
             "task_scope must be 'all' (every task) or 'own' (only the tasks this Manager created)"),
            (_body("other", task_policy="x"), 400, "task_policy must be"),
            (_body("other", description="d" * 301), 400, "description must be at most 300"),
            (_body("other", enabled="yes"), 400, "enabled must be true or false"),
        ]
        for body, code, message in refused:
            status, answer = await _call("POST", "/api/managers", body)
            assert (status, message in answer["error"]) == (code, True), (body, answer)
        assert set(await _list()) == {"manager", "manager-code-review", "manager-wide", "manager-seer"}

        for key in ("e5", "e6", "e7", "e8"):
            assert (await _create(key))[0] == 201
        status, answer = await _create("e9")
        assert status == 409 and "at most 8 Managers can be enabled at once" in answer["error"]
        assert (await _create("e9", enabled=False))[0] == 201

    _run(test, monkeypatch)


def test_the_default_manager_takes_only_a_name_and_a_description(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        status, answer = await _call("PUT", "/api/managers/manager", {"name": "Chief"})
        assert status == 200 and answer["manager"]["name"] == "Chief"
        for body in ({"system_prompt": "x"}, {"tools": "tasks"}, {"task_scope": "own"}, {"enabled": False},
                     {"profile_id": None}):
            status, answer = await _call("PUT", "/api/managers/manager", body)
            assert status == 400 and "always on" in answer["error"], (body, answer)
        status, answer = await _call("PUT", "/api/managers/manager", {"key": "x"})
        assert status == 400 and "key cannot change" in answer["error"]
        assert (await _call("DELETE", "/api/managers/manager"))[0] == 409
        assert (await _call("PUT", "/api/managers/manager-nope", {"name": "x"}))[0] == 404

    _run(test, monkeypatch)


def test_updates_bump_the_revision_and_tell_the_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import managers_api

    async def test(pool: Any) -> None:
        listener = _Listener()
        managers_api.set_change_listener(listener)
        try:
            await _create("code-review")
            assert listener.calls == ["manager-code-review"]
            revision = (await _list())["manager-code-review"]["revision"]
            for scope in ("all", "own"):
                status, answer = await _call("PUT", "/api/managers/manager-code-review", {"task_scope": scope})
                revision += 1
                assert status == 200 and answer["manager"]["task_scope"] == scope
                assert answer["manager"]["revision"] == revision
            assert listener.calls == ["manager-code-review"] * 3
            await _call("PUT", "/api/managers/manager-code-review/templates/pr", _template())
            await _call("PUT", "/api/managers/manager-code-review/templates/pr", _template(name="PR 2"))
            revision += 2
            assert (await _list())["manager-code-review"]["revision"] == revision
            await _call("DELETE", "/api/managers/manager-code-review/templates/pr")
            assert (await _list())["manager-code-review"]["revision"] == revision + 1
            assert len(listener.calls) == 6
            # a failing listener does not undo the write
            async def failing(_: str) -> None:
                raise RuntimeError("down")
            managers_api.set_change_listener(failing)
            status, answer = await _call("PUT", "/api/managers/manager-code-review", {"description": "kept"})
            assert status == 200 and (await _list())["manager-code-review"]["description"] == "kept"
            managers_api.set_change_listener(listener)
            assert (await _call("DELETE", "/api/managers/manager-code-review"))[0] == 200
            assert listener.calls[-1] == "manager-code-review"
        finally:
            managers_api.set_change_listener(None)

    _run(test, monkeypatch)


def test_delete_is_soft_and_refused_while_the_manager_has_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        project = await _project()
        await _create("busy")
        await _create("idle")
        await _call("PUT", "/api/managers/manager-idle/templates/pr", _template())
        await tasks.create_task(task_id="task-1", requirement="x", project_id=project, manager_id="manager-busy")
        status, answer = await _call("DELETE", "/api/managers/manager-busy")
        assert status == 409 and "disable it instead" in answer["error"]
        assert (await _call("PUT", "/api/managers/manager-busy", {"enabled": False}))[0] == 200

        status, answer = await _call("DELETE", "/api/managers/manager-idle")
        assert status == 200 and "manager-idle" not in {m["id"] for m in answer["managers"]}
        assert await pool.fetchval("SELECT count(*) FROM manager_task_templates") == 0
        assert "manager-idle" not in await _list()
        assert (await _call("PUT", "/api/managers/manager-idle", {"name": "x"}))[0] == 404
        assert (await _call("DELETE", "/api/managers/manager-idle"))[0] == 404
        assert (await _call("PUT", "/api/managers/manager-idle/templates/pr", _template()))[0] == 404
        assert (await _call("DELETE", "/api/managers/manager-idle/templates/pr"))[0] == 404
        status, answer = await _create("idle")
        assert status == 409 and "key idle was used by a deleted Manager: pick another" in answer["error"]
        assert (await _call("POST", "/api/managers", {**_body("fresh"), "name": "Idle"}))[0] == 201

    _run(test, monkeypatch)


def test_a_task_created_while_the_delete_runs_keeps_the_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers, tasks

    async def test(pool: Any) -> None:
        project = await _project()
        await _create("xx")
        async with pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            assert await managers.lock_for_task("manager-xx", conn=conn)
            await tasks.create_task(task_id="task-1", requirement="r", project_id=project, manager_id="manager-xx",
                                    pool=conn)
            request = asyncio.ensure_future(_call("DELETE", "/api/managers/manager-xx"))
            await _blocked(pool)
            assert not request.done()
            await tx.commit()
        status, answer = await request
        assert status == 409 and "disable it instead" in answer["error"]
        assert (await _list())["manager-xx"]["enabled"]

    _run(test, monkeypatch)


def test_a_template_written_while_the_manager_is_deleted_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import profile_registry

    async def test(pool: Any) -> None:
        await _profiles(pool)
        profile = await _profile(pool)
        await _create("xx")
        async with pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            await conn.fetchrow("SELECT * FROM managers WHERE id = 'manager-xx' FOR UPDATE")
            await conn.execute("UPDATE managers SET deleted_at = now(), enabled = false WHERE id = 'manager-xx'")
            request = asyncio.ensure_future(_call(
                "PUT", "/api/managers/manager-xx/templates/pr", _template(profiles={"reviewer": profile})))
            await _blocked(pool)
            await tx.commit()
        status, answer = await request
        assert status == 404 and "no Manager" in answer["error"], answer
        assert await pool.fetchval("SELECT count(*) FROM manager_task_templates WHERE manager_id = 'manager-xx'") == 0
        assert await profile_registry.delete_profile(profile) == {"deleted": profile}

    _run(test, monkeypatch)


def test_templates_are_checked_and_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _profiles(pool)
        project = await _project()
        await _create("xx")
        url = "/api/managers/manager-xx/templates/pr_review"
        example = json.loads(EXAMPLE.read_text())["templates"]["pr_review"]
        status, answer = await _call("PUT", url, {**example, "project_id": project, "work_dir": "~/code",
                                                  "profiles": {"reviewer": "claude-code/sonnet"}})
        assert status == 200, answer
        (stored,) = next(m for m in answer["managers"] if m["id"] == "manager-xx")["templates"]
        assert stored["inputs"] == [{**i, "required": True} for i in example["inputs"]] and stored["project_id"] == project
        assert stored["profiles"] == {"reviewer": "agent-sonnet"} and stored["work_dir"].startswith("/")
        assert stored["requirement"] == example["requirement"]

        refused = [
            ({**_template(), "color": 1}, "unknown field 'color'"),
            ({"requirement": "x"}, "name must be one line"),
            (_template(requirement="Review {{nope}}."), "names no input"),
            (_template(inputs=[{"key": "pr_url", "label": "L", "pattern": "("}]), "does not compile"),
            (_template(inputs=[{"key": "pr_url", "label": "L", "pattern": "(a+)+$"}]), "too slow"),
            (_template(role_templates=["security"]), "need the template's project"),
            (_template(project_id=project, role_templates=["security"]), "has no role template 'security'"),
            (_template(project_id=project, role_templates=["security", "security"]), "security twice"),
            (_template(work_dir="rel/dir"), "absolute"),
            (_template(project_id="proj-nope"), "no project 'proj-nope'"),
            (_template(profiles={"planner": "one-off/opus"}), "profiles.planner"),
            (_template(profiles={"reviewer": "nope"}), "no Agent Profile"),
            (_template(profiles={"manager": "claude-code/sonnet"}), "not 'manager'"),
        ]
        for body, message in refused:
            status, answer = await _call("PUT", url, body)
            assert status == 400 and message in answer["error"], (body, answer)
        assert (await _call("PUT", "/api/managers/manager-xx/templates/Bad", _template()))[0] == 404  # not a route
        assert (await _call("PUT", "/api/managers/manager-no/templates/pr", _template()))[0] == 404
        assert (await _call("DELETE", "/api/managers/manager-xx/templates/nope"))[0] == 404
        assert await pool.fetchval("SELECT count(*) FROM manager_task_templates") == 1

        await pool.execute("INSERT INTO project_role_templates (project_id, template_key, kind, role_prompt) "
                           "VALUES ($1, 'security', 'review', 'Check.')", project)
        assert (await _call("PUT", url, _template(project_id=project, role_templates=["security"])))[0] == 200
        status, answer = await _call("DELETE", url)
        assert status == 200 and next(m for m in answer["managers"] if m["id"] == "manager-xx")["templates"] == []

    _run(test, monkeypatch)


def test_a_template_may_only_name_the_profiles_its_project_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _profiles(pool)
        project = await _project()
        other = await _profile(pool)
        await _create("xx")
        await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) VALUES ($1, $2)",
                           project, other)
        url = "/api/managers/manager-xx/templates/pr"
        status, answer = await _call("PUT", url, _template(project_id=project, profiles={"worker": "agent-sonnet"}))
        assert status == 400 and "not on project" in answer["error"], answer
        assert (await _call("PUT", url, _template(project_id=project, profiles={"worker": other})))[0] == 200

    _run(test, monkeypatch)


def test_the_example_file_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project()
        example = json.loads(EXAMPLE.read_text())
        status, answer = await _call("POST", "/api/managers", example["manager"])
        assert status == 201, answer
        for key, template in example["templates"].items():
            status, answer = await _call("PUT", f"/api/managers/{answer_id(example)}/templates/{key}",
                                         {**template, "project_id": project})
            assert status == 200, (key, answer)
        (manager,) = [m for m in answer["managers"] if m["id"] == answer_id(example)]
        assert [t["key"] for t in manager["templates"]] == ["bugfix", "pr_review"]

    def answer_id(example: dict[str, Any]) -> str:
        return f"manager-{example['manager']['key']}"

    _run(test, monkeypatch)


def test_profiles_and_projects_a_manager_or_template_uses_cannot_be_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import profile_registry, projects
    from looper.db import profile_registry as db

    async def test(pool: Any) -> None:
        await _profiles(pool)
        project = await _project()
        used, free = await _profile(pool, "agent-a"), await _profile(pool, "agent-b")
        await _create("review", name="Review", profile_id=used)
        with pytest.raises(profile_registry.RegistryConflict, match="Manager Review"):
            await profile_registry.delete_profile(used)
        assert (await db.profile_usage(used))[used]["managers"] == ["Review"]

        await _call("PUT", "/api/managers/manager-review/templates/pr", _template(
            name="PR review", project_id=project, profiles={"reviewer": free}))
        with pytest.raises(profile_registry.RegistryConflict, match="template PR review of Manager Review"):
            await profile_registry.delete_profile(free)
        with pytest.raises(projects.RequestError, match="used by template 'PR review' of Manager 'Review'") as err:
            await projects.delete_project(project)
        assert err.value.status.startswith("409")

        # deleting the Manager frees both
        assert (await _call("DELETE", "/api/managers/manager-review"))[0] == 200
        assert await db.profile_usage(used) == {} and await db.profile_usage(free) == {}
        assert await profile_registry.delete_profile(used) == {"deleted": used}
        assert await profile_registry.delete_profile(free) == {"deleted": free}
        assert await projects.delete_project(project) == {"deleted": project}

    _run(test, monkeypatch)


def test_editing_a_profile_keeps_it_able_to_run_what_managers_and_templates_use_it_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import profile_registry

    async def test(pool: Any) -> None:
        await _profiles(pool)
        await pool.execute("INSERT INTO agent_runtimes (id, name, type, capabilities) VALUES "
                           "('rt-resident', 'resident', 'claude-code', ARRAY['resident_session', 'resume'])")
        model_id = await pool.fetchval("SELECT model_id FROM agent_profiles WHERE id = 'agent-sonnet'")
        as_reviewer, as_manager = await _profile(pool, "agent-r"), await _profile(pool, "agent-m")
        await _create("xx", profile_id=as_manager)
        await _call("PUT", "/api/managers/manager-xx/templates/pr", _template(profiles={"reviewer": as_reviewer}))

        with pytest.raises(profile_registry.RegistryError, match="cannot run the reviewer"):
            await profile_registry.update_profile(as_reviewer, {"runtime_id": "rt-resident", "model_id": model_id})
        with pytest.raises(profile_registry.RegistryError, match="cannot run the manager"):
            await profile_registry.update_profile(as_manager, {"runtime_id": "rt-one-off", "model_id": model_id})
        await profile_registry.update_profile(as_reviewer, {"runtime_id": "rt-one-off", "model_id": model_id})

    _run(test, monkeypatch)


def test_concurrent_enables_never_exceed_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers

    async def test(pool: Any) -> None:
        for i in range(6):
            assert (await _create(f"m{i}"))[0] == 201
        results = await asyncio.gather(_create("n1"), _create("n2"))
        assert sorted(s for s, _ in results) == [201, 409]
        assert await managers.count_enabled() == 8

        assert (await _create("d1", enabled=False))[0] == 201
        assert (await _call("PUT", "/api/managers/manager-m0", {"enabled": False}))[0] == 200
        assert await managers.count_enabled() == 7
        results = await asyncio.gather(*(_call("PUT", f"/api/managers/manager-{key}", {"enabled": True})
                                         for key in ("m0", "d1")))
        assert sorted(s for s, _ in results) == [200, 409]
        assert await managers.count_enabled() == 8

    _run(test, monkeypatch)


def test_enabling_re_reads_the_manager_under_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers

    async def test(pool: Any) -> None:
        for key in ("xx", "e1", "e2", "e3", "e4", "e5", "e6"):
            assert (await _create(key))[0] == 201
        assert (await _create("yy", enabled=False))[0] == 201
        assert await managers.count_enabled() == 8
        async with pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            await managers.lock_managers(conn)
            # X reads as enabled before the lock; the request waits for it
            request = asyncio.ensure_future(_call("PUT", "/api/managers/manager-xx", {"enabled": True}))
            await _blocked(pool)
            await managers.update_manager("manager-xx", {"enabled": False}, conn=conn)
            await managers.update_manager("manager-yy", {"enabled": True}, conn=conn)
            await tx.commit()
        status, answer = await request
        assert status == 409 and "at most 8 Managers" in answer["error"], answer
        assert await managers.count_enabled() == 8

    _run(test, monkeypatch)


def test_each_managers_view_carries_the_fleets_state_under_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents import managers_api

        await _create("seer")
        view = await managers_api.managers_view(extra=lambda manager_id: {"sync": "ok", "id": "not-the-managers"})
        assert all(m["state"] == {"sync": "ok", "id": "not-the-managers"} and "sync" not in m
                   for m in view["managers"])
        assert {m["id"] for m in view["managers"]} >= {"manager", "manager-seer"}

    _run(test, monkeypatch)
