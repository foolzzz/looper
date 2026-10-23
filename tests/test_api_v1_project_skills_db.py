"""Project-owned skills through authenticated v1 dispatch and disposable PostgreSQL."""

import json
from contextlib import asynccontextmanager

import pytest

from looper.agents import api_v1, catalog, project_skills
from looper.agents.bus import MessageBus
from looper.agents.library import PROJECT_SKILLS_PLUGIN
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.v1_domains.catalog import CATALOG_ITEM, INSTALL, PROJECT_INSTALLS
from looper.db import api_clients, library, projects
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
SKILL = {"name": "example", "description": "Example instructions", "content": "Check the project.\n"}


def _validate(schema, value):
    api_v1.validate_arguments({"type": "object", "properties": {"result": schema}}, {"result": value})


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("project skill validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                               task_router=server._task_router, runtime_root=tmp_path))
        async def request(action, body=None, *, client=False, method="POST", session="project-skills"):
            path = action if action.startswith("/") else "/api/v1/" + (
                action if "." in action or action == "confirm" else "project_skills." + action).replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}",
                {**_bearer(token if client else TOKEN), "X-Looper-Session": session}, json.dumps(body or {}))
            if status == 200 and not action.startswith("/"):
                _validate(api_v1.find(method, path).result, answer["result"])
            return status, answer
        yield request


@pytest.mark.parametrize("enabled", [None, False], ids=["default-enabled", "disabled"])
def test_creation_persists_content_and_full_project_install_schemas(tmp_path, monkeypatch, enabled):
    async def run(pool):
        project = await projects.create_project(name="Example")
        skill = {**SKILL, **({"enabled": enabled} if enabled is not None else {})}
        async with _api(tmp_path, pool) as call:
            status, added = await call("add", {"project_id": project["id"], "skill": skill}, client=True)
            assert status == 200, added
            result = added["result"]
            _validate(PROJECT_INSTALLS, result)
            entry = result["installs"][0]
            _validate(INSTALL, entry)
            _validate(CATALOG_ITEM, entry["item"])
            assert entry["enabled"] is (enabled if enabled is not None else True)
            assert result["owned_count"] == 1 and entry["item"]["owner_project_id"] == project["id"]
            assert result["skill"]["qualified_name"] == PROJECT_SKILLS_PLUGIN + ":example"
            assert result["skill"]["size_bytes"] > len(SKILL["content"].encode())
            status, read = await call("get", {"project_id": project["id"], "skill_id": result["skill"]["skill_id"]}, client=True)
            assert status == 200 and read["result"] == {"skill": result["skill"], "content": SKILL["content"]}
            assert read["result"]["skill"]["name"] == SKILL["name"]
            assert read["result"]["skill"]["description"] == SKILL["description"]
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("field", "value", "accepted"), [
    ("name", "a" * project_skills.NAME_MAX_CHARS, True),
    ("name", "a" * (project_skills.NAME_MAX_CHARS + 1), False),
    *[("name", value, False) for value in ("", "Upper", "has space", "-start", "end-", "two--hyphens")],
    ("description", "a" * project_skills.DESCRIPTION_MAX_CHARS, True),
    ("description", "a" * (project_skills.DESCRIPTION_MAX_CHARS + 1), False),
    *[("description", value, False) for value in ("", "two\nlines", "two\rlines", "control\x01")],
    ("content", "界" * (project_skills.CONTENT_MAX_BYTES // 3) + "a", True),
    ("content", "界" * (project_skills.CONTENT_MAX_BYTES // 3 + 1), False),
    *[("content", value, False) for value in ("", "contains\x00nul", "---\nname: example\n---\nBody")],
], ids=["name-max", "name-over-max", "name-empty", "name-uppercase", "name-space", "name-leading-hyphen",
        "name-trailing-hyphen", "name-double-hyphen", "description-max", "description-over-max",
        "description-empty", "description-newline", "description-return", "description-control",
        "content-max-bytes", "content-over-max-bytes", "content-empty", "content-nul", "content-frontmatter"])
def test_skill_field_rules_are_atomic(tmp_path, monkeypatch, field, value, accepted):
    async def run(pool):
        project = await projects.create_project(name="Example")
        async with _api(tmp_path, pool) as call:
            status, answer = await call("add", {"project_id": project["id"], "skill": {**SKILL, field: value}})
            assert status == (200 if accepted else 400), answer
            assert len(await library.list_owned_items(project["id"])) == int(accepted)
            if accepted:
                _, read = await call("get", {"project_id": project["id"], "skill_id": answer["result"]["skill"]["skill_id"]})
                if field == "content":
                    assert read["result"]["content"] == value + "\n"
                else:
                    assert read["result"]["skill"][field] == value
    run_db(run, monkeypatch, timeout=30)


def test_project_skill_count_limit_preserves_existing_items(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example")
        for index in range(project_skills.PROJECT_MAX_SKILLS):
            await project_skills.add_skill(project["id"], {**SKILL, "name": f"skill-{index}"})
        before = await library.list_owned_items(project["id"])
        async with _api(tmp_path, pool) as call:
            status, answer = await call("add", {"project_id": project["id"], "skill": SKILL})
            assert status == 400 and str(project_skills.PROJECT_MAX_SKILLS) in answer["error"]["message"]
            assert await library.list_owned_items(project["id"]) == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["add", "update"])
def test_name_collision_returns_409_without_modifying_prior_skill(tmp_path, monkeypatch, action):
    async def run(pool):
        project = await projects.create_project(name="Example")
        first = await project_skills.add_skill(project["id"], SKILL)
        second = await project_skills.add_skill(project["id"], {**SKILL, "name": "second"})
        before = [await project_skills.get_skill(project["id"], item["skill"]["skill_id"])
                  for item in (first, second)]
        body = {"project_id": project["id"], **({"skill": SKILL} if action == "add" else {
            "skill_id": second["skill"]["skill_id"], "fields": {"name": first["skill"]["name"], "content": "Changed."}})}
        async with _api(tmp_path, pool) as call:
            assert (await call(action, body))[0] == 409
            assert [await project_skills.get_skill(project["id"], item["skill"]["skill_id"])
                    for item in (first, second)] == before
            assert len(await library.list_owned_items(project["id"])) == 2
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["get", "update", "delete"])
def test_skill_identifiers_cannot_cross_project_boundaries(tmp_path, monkeypatch, action):
    async def run(pool):
        owner, other = await projects.create_project(name="Owner"), await projects.create_project(name="Other")
        added = await project_skills.add_skill(owner["id"], SKILL)
        skill_id = added["skill"]["skill_id"]
        before = await project_skills.get_skill(owner["id"], skill_id)
        async with _api(tmp_path, pool) as call:
            status, answer = await call(action, {"project_id": other["id"], "skill_id": skill_id,
                **({"fields": {"content": "Changed."}} if action == "update" else {})}, client=True)
            assert status == 404 and answer["error"]["code"] == "not_found"
            assert await project_skills.get_skill(owner["id"], skill_id) == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("fields", [{"name": "changed"}, {"description": "Changed description"},
                                    {"content": "Changed content.\n"}, {}, {"enabled": False}])
def test_partial_update_changes_only_provided_fields_and_refuses_enabled(tmp_path, monkeypatch, fields):
    async def run(pool):
        project = await projects.create_project(name="Example")
        added = await project_skills.add_skill(project["id"], {**SKILL, "enabled": False})
        ids = {"project_id": project["id"], "skill_id": added["skill"]["skill_id"]}
        async with _api(tmp_path, pool) as call:
            status, answer = await call("update", {**ids, "fields": fields}, client=True)
            accepted = bool(fields) and "enabled" not in fields
            assert status == (200 if accepted else 400), answer
            _, read = await call("get", ids)
            expected = {**SKILL, **(fields if accepted else {})}
            assert {key: read["result"]["skill"][key] for key in ("name", "description")} == {
                key: expected[key] for key in ("name", "description")}
            assert read["result"]["skill"]["qualified_name"] == PROJECT_SKILLS_PLUGIN + ":" + expected["name"]
            assert read["result"]["content"] == expected["content"]
            assert (await library.list_installs(item_ids=[ids["skill_id"]]))[0]["enabled"] is False
    run_db(run, monkeypatch, timeout=30)


def test_delete_pending_preserves_state_and_confirm_removes_current_cross_project_installs(tmp_path, monkeypatch):
    async def run(pool):
        owner, other, later = [await projects.create_project(name=name) for name in ("Owner", "Other", "Later")]
        added = await project_skills.add_skill(owner["id"], SKILL)
        unrelated = await project_skills.add_skill(other["id"], {**SKILL, "name": "unrelated"})
        skill_id = added["skill"]["skill_id"]
        await catalog.install({"item_id": skill_id, "scope": "project", "project_id": other["id"]})
        before = await library.list_installs()
        async with _api(tmp_path, pool) as call:
            status, asked = await call("delete", {"project_id": owner["id"], "skill_id": skill_id}, client=True)
            assert status == 202 and asked["status"] == "needs_confirmation"
            assert all(p["id"] in asked["impact"] or p["name"] in asked["impact"] for p in (owner, other))
            assert await library.list_installs() == before and await library.get_item(skill_id)
            await catalog.install({"item_id": skill_id, "scope": "project", "project_id": later["id"]})
            confirm = {"confirmation_id": asked["confirmation_id"]}
            assert (await call("confirm", confirm, client=True, session="different"))[0] == 404
            status, done = await call("confirm", confirm, client=True)
            assert status == 200 and done["result"]["deleted"] == skill_id and done["result"]["owned_count"] == 0
            _validate(api_v1.find("POST", "/api/v1/project_skills/delete").result, done["result"])
            _validate(PROJECT_INSTALLS, done["result"])
            assert await library.get_item(skill_id) is None and await library.list_installs(item_ids=[skill_id]) == []
            assert await project_skills.get_skill(other["id"], unrelated["skill"]["skill_id"])
            assert len(await library.list_installs()) == 1
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("method", "action"), [("POST", "add"), ("GET", "get"), ("PUT", "update"), ("DELETE", "delete")])
def test_retired_routes_are_404_with_authenticated_v1_positive_control(tmp_path, monkeypatch, method, action):
    async def run(pool):
        project = await projects.create_project(name="Example")
        added = await project_skills.add_skill(project["id"], SKILL)
        skill_id = added["skill"]["skill_id"]
        path = f"/api/projects/{project['id']}/skills" + ("" if action == "add" else f"/{skill_id}")
        body = {"project_id": project["id"], **({"skill": {**SKILL, "name": "new"}} if action == "add" else {
            "skill_id": skill_id, **({"fields": {"description": "Changed"}} if action == "update" else {})})}
        async with _api(tmp_path, pool) as call:
            assert (await call(path, method=method))[0] == 404
            status, answer = await call(action, body)
            assert status == 200 and answer["status"] == "ok", answer
    run_db(run, monkeypatch, timeout=30)
