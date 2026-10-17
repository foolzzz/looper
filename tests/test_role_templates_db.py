"""A project's role templates and the user's edits of a task's roles, on a real PostgreSQL: a template is checked
as a task role is; its tasks' Planners copy one as a task role, the fields they give overriding it; the user's edit
on the task page is a new version that tells the Planner, which cannot change that role afterwards; and a version
is written only on top of the one its writer read.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_ops_tasks_db import _error, _project, _task
from tests.test_planner_tools_db import _allow, _haiku, _journal, _ops, _Planner
from tests.test_task_agents_db import _call, _profiles
from tests.test_task_roles_db import TESTER, _roles_on, _versions
from tests.test_usage_api_db import DSN, _run, _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

SECURITY = {"kind": "review", "description": "steps that change authentication",
            "role_prompt": "You check the step's code for security problems.", "harness": None, "profile_id": None}


def _templates(answer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["key"]: t for t in answer["templates"]}


async def _put(project: dict[str, Any], key: str, body: dict[str, Any]) -> tuple[int, Any]:
    return await _call("PUT", f"/api/projects/{project['id']}/role-templates/{key}", body)


def test_a_template_is_checked_as_a_task_role_is_and_listed_by_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _profiles(pool)
        project = await _project(pool)
        status, answer = await _call("GET", f"/api/projects/{project['id']}/role-templates")
        assert (status, answer["templates"], answer["role_text_max_chars"]) == (200, [], 8000)
        assert (await _call("GET", "/api/projects/proj-nope/role-templates"))[0] == 404

        await _roles_on(pool, project, role_text_max_chars=500)
        refused = [
            ("security", {**SECURITY, "color": "red"}, "unknown field 'color'"),
            ("Worker", SECURITY, "names a built-in role"),
            ("security", {**SECURITY, "kind": "plan"}, "kind must be one of"),
            ("security", {**SECURITY, "role_prompt": "  "}, "role_prompt is required"),
            ("security", {**SECURITY, "role_prompt": "x" * 501}, "this project allows at most 500"),
            ("security", {**SECURITY, "description": "x" * 301}, "at most 300 characters"),
            ("security", {**SECURITY, "profile_id": "claude-code/sonnet"}, "its list is empty"),
        ]
        for key, body, message in refused:
            status, answer = await _put(project, key, body)
            assert status == 400 and message in answer["error"], (key, body, answer)
        assert await pool.fetchval("SELECT count(*) FROM project_role_templates") == 0
        assert (await _call("PUT", "/api/projects/proj-nope/role-templates/security", SECURITY))[0] == 404

        await _allow(project, "agent-sonnet", "agent-one-off")
        status, answer = await _put(project, "security", {**SECURITY, "profile_id": "claude-code/opus"})
        assert status == 400 and "not on the project's list; you may choose" in answer["error"], answer
        status, answer = await _put(project, "security", {**SECURITY, "profile_id": "claude-code/sonnet",
                                                          "description": "  steps that\nchange auth  "})
        assert status == 200, answer
        security = _templates(answer)["security"]
        assert security["description"] == "steps that change auth"
        assert security["profile"] == {"profile_id": "agent-sonnet", "name": "claude-code/sonnet", "allowed": True}
        # A PUT replaces the whole template.
        status, answer = await _put(project, "tester", {"kind": "work", "role_prompt": TESTER["role_prompt"]})
        assert status == 200 and list(_templates(answer)) == ["security", "tester"]
        assert _templates(answer)["tester"]["description"] == "" and _templates(answer)["tester"]["harness"] is None
        status, answer = await _put(project, "security", {**SECURITY, "harness": "## How you work\nRead first."})
        assert _templates(answer)["security"]["profile"] is None
        assert _templates(answer)["security"]["harness"] == "## How you work\nRead first."

        # A profile the list drops afterwards stays on the template, shown as not allowed now.
        await _put(project, "security", {**SECURITY, "profile_id": "agent-sonnet"})
        await _allow(project, "agent-one-off")
        status, answer = await _call("GET", f"/api/projects/{project['id']}/role-templates")
        assert _templates(answer)["security"]["profile"]["allowed"] is False

        assert (await _call("DELETE", f"/api/projects/{project['id']}/role-templates/nope"))[0] == 404
        status, answer = await _call("DELETE", f"/api/projects/{project['id']}/role-templates/tester")
        assert status == 200 and list(_templates(answer)) == ["security"]

    _run(test, monkeypatch)


def test_a_planner_copies_a_template_as_a_task_role_and_the_fields_it_gives_override_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        await _profiles(pool)
        await _haiku(pool)
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _roles_on(pool, project)
        await _allow(project, "agent-sonnet", "agent-haiku")
        await _put(project, "security", {**SECURITY, "profile_id": "claude-code/sonnet",
                                         "harness": "## How you work\nRead first."})
        await _put(project, "tester", {"kind": "work", "role_prompt": TESTER["role_prompt"]})
        planner = _Planner(_ops(), "task-a")

        read = (await planner("looper_task_roles", {}))["result"]
        assert [(t["id"], t["kind"], t["description"], t["profile"]) for t in read["templates"]] == [
            ("security", "review", "steps that change authentication", "claude-code/sonnet"),
            ("tester", "work", "", None)]

        refused = [
            ({"template": "nope"}, "this project has no role template 'nope'; its templates: security, tester"),
            ({"template": "security", "kind": "work"}, "template security is a review role"),
            ({"id": "doc", "kind": "work"}, "role_prompt is required"),
        ]
        for arguments, message in refused:
            answer = await planner("looper_task_role_set", arguments)
            assert _error(answer) == "invalid" and message in answer["error"]["message"], (arguments, answer)
        assert await _versions(pool, "task-a") == []

        # Its id, texts and profile come from the template; the version records it.
        answer = await planner("looper_task_role_set", {"template": "security", "reason": "auth changes"})
        assert answer["status"] == "ok" and answer["result"]["template"] == "security", answer
        row = await pool.fetchrow("SELECT * FROM task_roles WHERE task_id = 'task-a' AND role_key = 'security'")
        assert (row["kind"], row["role_prompt"], row["harness"], row["profile_id"], row["via"], row["template_key"]) \
            == ("review", SECURITY["role_prompt"], "## How you work\nRead first.", "agent-sonnet", "planner",
                "security")
        (entry,) = await _journal(pool, "task-a", "role_defined")
        assert entry[1].startswith("Role security (review) v1 from template security: profile claude-code/sonnet")
        # The same template again changes nothing.
        assert (await planner("looper_task_role_set", {"template": "security"}))["result"]["changed"] is False

        # Under another id, with its own prompt and profile: the given fields override the template's.
        answer = await planner("looper_task_role_set", {"template": "tester", "id": "api-tester",
                                                        "role_prompt": "You test the API.",
                                                        "profile_id": "claude-code/haiku"})
        assert answer["status"] == "ok", answer
        row = await pool.fetchrow("SELECT * FROM task_roles WHERE role_key = 'api-tester'")
        assert (row["role_prompt"], row["profile_id"], row["template_key"]) == ("You test the API.", "agent-haiku",
                                                                                "tester")

        # A template's profile the project no longer allows is left out of the copy, and the answer says so.
        await _allow(project, "agent-haiku")
        answer = await planner("looper_task_role_set", {"template": "security", "id": "sec2"})
        assert answer["status"] == "ok" and "not on the project's list any more" in answer["result"]["note"]
        assert await pool.fetchval("SELECT profile_id FROM task_roles WHERE role_key = 'sec2'") is None

        roles = {r["id"]: r for r in (await planner("looper_task_roles", {}))["result"]["roles"]}
        assert roles["security"]["template"] == "security" and "edited_by_user" not in roles["security"]

    _run(test, monkeypatch)


class _Bus:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.sent.append(envelope)


async def _edit(task_id: str, key: str, body: Any, *, planners: tuple[str, ...] = ()) -> tuple[int, Any, _Bus]:
    """``PUT /api/tasks/<task_id>/roles/<key>`` on a server whose TaskRouter runs the Planners of ``planners``."""
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.message import channel_address

    bus = _Bus()
    server = AgentHTTPServer(web_adapter=SimpleNamespace(_address=channel_address("web")),
                             supervisor=SimpleNamespace(bus=bus),
                             task_router=SimpleNamespace(has_planner=lambda t: t in planners))
    writer = _Writer()
    await server._route_api("PUT", f"/api/tasks/{task_id}/roles/{key}", {}, json.dumps(body).encode(), writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload), bus


def test_the_users_edit_is_a_new_version_that_tells_the_planner_and_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import task_changes

    async def test(pool: Any) -> None:
        await _profiles(pool)
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _task(pool, "task-done", "completed", project)
        await _roles_on(pool, project, role_text_max_chars=500)
        await _allow(project, "agent-sonnet")
        planner = _Planner(_ops(), "task-a")
        await planner("looper_task_role_set", TESTER)
        edit = {"role_prompt": "You write the tests first.", "harness": None, "profile_id": None}

        refused = [
            ("task-nope", "tester", edit, 404, "task not found"),
            ("task-a", "nope", edit, 404, "has no role 'nope'"),
            ("task-a", "tester", {**edit, "kind": "review"}, 400, "unknown field 'kind'"),
            ("task-a", "tester", {**edit, "role_prompt": ""}, 400, "role_prompt is required"),
            ("task-a", "tester", {**edit, "harness": "x" * 501}, 400, "harness has 501 characters"),
            ("task-a", "tester", {**edit, "profile_id": "claude-code/opus"}, 400, "not on the project's list"),
            ("task-a", "tester", {**edit, "role_prompt": TESTER["role_prompt"]}, 400, "nothing to change"),
        ]
        for task_id, key, body, code, message in refused:
            status, answer, bus = await _edit(task_id, key, body, planners=("task-a",))
            assert (status, bus.sent) == (code, []) and message in answer["error"], (body, answer)
        await pool.execute("INSERT INTO task_roles (task_id, role_key, version, kind, role_prompt) "
                           "VALUES ('task-done', 'tester', 1, 'work', 'x')")
        status, answer, _ = await _edit("task-done", "tester", edit)
        assert status == 409 and "completed" in answer["error"]
        assert await _versions(pool, "task-a") == [("tester", 1, None, None)]

        # A Planner that runs is told at once; the version is the user's, with its journal entry and a supplement.
        status, answer, bus = await _edit("task-a", "tester", {**edit, "profile_id": "claude-code/sonnet"},
                                          planners=("task-a",))
        assert status == 200 and answer == {"task_id": "task-a", "id": "tester", "kind": "work", "version": 2,
                                            "via": "user", "changed": ["role prompt", "profile"]}, answer
        assert [(e.message_type.value, e.payload) for e in bus.sent] == [("task_change", {"task_id": "task-a"})]
        assert await _versions(pool, "task-a") == [("tester", 1, None, None), ("tester", 2, "agent-sonnet", None)]
        assert await pool.fetchval("SELECT via FROM task_roles WHERE role_key = 'tester' AND version = 2") == "user"
        (_, defined) = await _journal(pool, "task-a", "role_defined")
        assert defined[1] == "Role tester (work) v2 by the user: changed role prompt, profile"
        pending = await task_changes.pending("task-a", pool=pool)
        (supplement,) = pending.supplements
        assert supplement.via == "task_page"
        assert supplement.text.startswith("The user edited role tester on the task page: it is now version 2")

        # Its Planner cannot change it, but defines another role under a new id.
        answer = await planner("looper_task_role_set", {**TESTER, "role_prompt": "Back to mine."})
        assert _error(answer) == "forbidden" and "the user edited role tester (v2)" in answer["error"]["message"]
        assert (await planner("looper_task_role_set", {**TESTER, "id": "tester2"}))["status"] == "ok"
        read = {r["id"]: r for r in (await planner("looper_task_roles", {}))["result"]["roles"]}
        assert read["tester"]["edited_by_user"] is True and "edited_by_user" not in read["tester2"]

        # A task that ended without completing takes the edit too; with no Planner running, nothing is sent.
        await pool.execute("UPDATE tasks SET status = 'stopped' WHERE id = 'task-a'")
        status, answer, bus = await _edit("task-a", "tester", {**edit, "harness": "## How you work\nTDD."})
        assert (status, answer["version"], bus.sent) == (200, 3, [])

        # The task page names who wrote each version.
        status, detail = await _call("GET", "/api/tasks/task-a")
        tester = next(r for r in detail["roles"] if r["id"] == "tester")
        assert (tester["via"], [v["via"] for v in tester["versions"]]) == ("user", ["planner", "user", "user"])

    _run(test, monkeypatch)


def test_a_version_is_written_only_on_top_of_the_one_its_writer_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import task_roles

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        values = {"kind": "work", "role_prompt": "first", "via": "planner"}
        entry = {"step_id": "", "kind": "role_defined", "summary": "defined", "learnings": []}
        assert await task_roles.add_version("task-a", "tester", values, entry, after=0, pool=pool) == 1
        # A writer that read no role, or version 0, after version 1 was written: nothing is written.
        assert await task_roles.add_version("task-a", "tester", {**values, "role_prompt": "late"}, entry, after=0,
                                            pool=pool) is None
        assert await task_roles.add_version("task-a", "tester", {**values, "via": "user"}, [entry, entry], after=1,
                                            pool=pool) == 2
        assert await _versions(pool, "task-a") == [("tester", 1, None, None), ("tester", 2, None, None)]
        assert len(await _journal(pool, "task-a", "role_defined")) == 3

    _run(test, monkeypatch)
