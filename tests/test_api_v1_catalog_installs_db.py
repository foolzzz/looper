"""Catalog migration contracts through authenticated dispatch and disposable PostgreSQL."""

import json
from contextlib import asynccontextmanager

import pytest

from looper.agents import api_v1, catalog, catalog_sources, mcp, project_skills
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import api_clients, library, projects
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _validate_result(schema, result):
    api_v1.validate_arguments({"type": "object", "properties": {"result": schema}}, {"result": result})


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("catalog validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                               task_router=server._task_router, runtime_root=tmp_path))
        async def request(operation, body=None, *, client=False, method="POST", raw=None):
            path = operation if operation.startswith("/") else "/api/v1/" + operation.replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}",
                {**_bearer(token if client else TOKEN), "X-Looper-Session": "catalog"},
                raw if raw is not None else json.dumps(body or {}))
            if status == 200 and not operation.startswith("/"):
                op = api_v1.find(method, path)
                _validate_result(op.result, answer["result"])
            return status, answer
        yield request


async def _skill(name="sample"):
    return (await catalog_sources.create_skill({"name": name, "description": "Sample instructions",
                                                "content": "Reply with the requested result.\n"}))["item"]


def test_full_catalog_detail_and_owned_inherited_readback(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example")
        own = await project_skills.add_skill(project["id"], {"name": "owned", "description": "Owned instructions",
                                                             "content": "Check the project.\n"})
        item = await _skill()
        async with _api(tmp_path, pool) as call:
            _, installed = await call("installs.create", {"item_id": item["item_id"], "scope": "global"}, client=True)
            assert installed["result"]["install"]["project_id"] is None
            _, listed = await call("catalog_items.list", client=True)
            assert listed["result"]["installed_count"] == 2 and listed["result"]["total_bytes"] > 0
            _, detail = await call("catalog_items.get", {"item_id": item["item_id"]}, client=True)
            assert detail["result"]["files"][0]["path"] == "SKILL.md"
            assert detail["result"]["files"][0]["size_bytes"] > 0 and detail["result"]["skill_md"]
            assert detail["result"]["content"] == "Reply with the requested result.\n"
            _, view = await call("installs.project", {"project_id": project["id"]}, client=True)
            entries = view["result"]["installs"]
            assert view["result"]["owned_count"] == 1 and view["result"]["skill_caps"]["project_max_skills"] > 0
            assert {(e["item_id"], e["inherited"]) for e in entries} == {
                (own["skill"]["skill_id"], False), (item["item_id"], True)}
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("scope", "roles", "expected"), [
    ("global", ["manager"], 200), ("global", ["task_role"], 200),
    ("project", ["worker"], 200), ("project", ["task_role"], 200),
    ("project", ["manager"], 400), ("global", [], 400), ("global", ["unknown"], 400),
])
def test_install_role_boundaries_are_atomic(tmp_path, monkeypatch, scope, roles, expected):
    async def run(pool):
        project = await projects.create_project(name="Example")
        item = await _skill()
        body = {"item_id": item["item_id"], "scope": scope, "roles": roles}
        if scope == "project":
            body["project_id"] = project["id"]
        async with _api(tmp_path, pool) as call:
            status, answer = await call("installs.create", body, client=True)
            assert status == expected, answer
            rows = await library.list_installs()
            assert len(rows) == (1 if expected == 200 else 0)
            if rows:
                assert list(rows[0]["roles"]) == roles
                status, answer = await call("installs.update", {"install_id": rows[0]["id"], "roles": roles,
                                                                "enabled": False}, client=True)
                assert status == 200 and answer["result"]["install"]["enabled"] is False
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("change", "status"), [
    ({}, 409), ({"item_id": "missing"}, 404), ({"scope": "project", "project_id": "missing"}, 404),
    ({"scope": "project"}, 400), ({"roles": ["unknown"]}, 400),
])
def test_duplicate_and_invalid_installs_preserve_existing_state(tmp_path, monkeypatch, change, status):
    async def run(pool):
        item = await _skill()
        installed = await catalog.install({"item_id": item["item_id"], "scope": "global"})
        before = await library.list_installs()
        async with _api(tmp_path, pool) as call:
            actual, answer = await call("installs.create", {"item_id": item["item_id"], "scope": "global", **change})
            assert actual == status, answer
            assert await library.list_installs() == before
            _, read = await call("catalog_items.get", {"item_id": item["item_id"]})
            assert read["result"]["item"]["installs"][0]["install_id"] == installed["install"]["install_id"]
    run_db(run, monkeypatch, timeout=30)


def test_name_conflict_and_project_owned_delete_do_not_change_items(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example")
        owned = await project_skills.add_skill(project["id"], {"name": "owned", "description": "Owned instructions",
                                                               "content": "Check the project."})
        first, second = await _skill("first"), await _skill("second")
        before = await library.list_items()
        async with _api(tmp_path, pool) as call:
            status, _ = await call("catalog_items.update", {"item_id": second["item_id"], "fields": {"name": "first"}})
            assert status == 409
            status, _ = await call("catalog_items.delete", {"item_id": owned["skill"]["skill_id"]})
            assert status == 400
            assert await library.list_items() == before
            assert (await catalog.item_detail(first["item_id"]))["item"]["name"] == "first"
    run_db(run, monkeypatch, timeout=30)


def test_mcp_updates_require_operator_and_preserve_masked_secrets(tmp_path, monkeypatch, caplog):
    async def run(pool):
        secret = "synthetic-secret-value"
        item = (await mcp.create({"name": "example", "transport": "stdio", "command": "example",
                                  "env": {"API_KEY": secret}}))["item"]
        async with _api(tmp_path, pool) as call:
            status, answer = await call("catalog_items.update", client=True, raw="not-json")
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
            status, answer = await call("catalog_items.update", {"item_id": item["item_id"],
                "fields": {"description": "Changed", "env": {"API_KEY": mcp.MASK}}})
            assert status == 200 and answer["result"]["item"]["mcp"]["env"] == {"API_KEY": mcp.MASK}
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
            stored = await pool.fetchval("SELECT env FROM library_mcp_servers WHERE item_id=$1", item["item_id"])
            assert json.loads(stored)["API_KEY"] == secret
            for operation, body in (("catalog_items.list", {}), ("catalog_items.get", {"item_id": item["item_id"]})):
                _, read = await call(operation, body, client=True)
                assert secret not in json.dumps(read) + caplog.text
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("kind", ["skill", "mcp"])
def test_empty_item_patch_is_refused_without_writing(tmp_path, monkeypatch, kind):
    async def run(pool):
        item = await _skill() if kind == "skill" else (await mcp.create({
            "name": "example", "transport": "stdio", "command": "example"}))["item"]
        before = await library.get_item(item["item_id"])
        async with _api(tmp_path, pool) as call:
            assert (await call("catalog_items.update", {"item_id": item["item_id"], "fields": {}}))[0] == 400
            assert await library.get_item(item["item_id"]) == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("origin", "status", "modified"), [("upload", 200, True), ("machine", 400, False)])
def test_editable_skill_modified_and_machine_item_immutable(tmp_path, monkeypatch, origin, status, modified):
    async def run(pool):
        item = await _skill()
        await pool.execute("UPDATE library_items SET origin=$1 WHERE id=$2", origin, item["item_id"])
        async with _api(tmp_path, pool) as call:
            actual, _ = await call("catalog_items.update", {"item_id": item["item_id"], "fields": {"content": "Changed."}})
            assert actual == status
            _, read = await call("catalog_items.get", {"item_id": item["item_id"]})
            assert read["result"]["item"]["modified"] is modified
            assert (read["result"]["content"] == "Changed.\n") is modified
    run_db(run, monkeypatch, timeout=30)


def test_delete_confirmation_and_ordinary_uninstall_preserve_unrelated_state(tmp_path, monkeypatch):
    async def run(pool):
        item, other = await _skill(), await _skill("other")
        install = (await catalog.install({"item_id": item["item_id"], "scope": "global"}))["install"]
        unrelated = await catalog.install({"item_id": other["item_id"], "scope": "global"})
        async with _api(tmp_path, pool) as call:
            status, answer = await call("installs.delete", {"install_id": install["install_id"]}, client=True)
            assert status == 200 and answer["result"]["item"]["installs"] == []
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status='ok'") == 1
            status, asked = await call("catalog_items.delete", {"item_id": item["item_id"]}, client=True)
            assert status == 202 and asked["impact"] and await library.get_item(item["item_id"])
            status, done = await call("confirm", {"confirmation_id": asked["confirmation_id"]}, client=True)
            assert status == 200 and done["result"] == {"deleted": item["item_id"]}
            assert await library.get_item(item["item_id"]) is None
            assert (await catalog.item_detail(other["item_id"]))["item"] == unrelated["item"]
            assert (await call("confirm", {"confirmation_id": asked["confirmation_id"]}, client=True))[0] == 404
    run_db(run, monkeypatch, timeout=30)


def test_actual_compact_catalog_outputs_match_declared_schemas(tmp_path, monkeypatch):
    async def run(pool):
        item = await _skill()
        await projects.create_project(name="Example")
        async with _api(tmp_path, pool) as call:
            prefix = "/api/v1/ops/catalog/"
            for body in ({"item": item["item_id"]}, {"item": item["item_id"], "project": "Example"}):
                status, answer = await call(prefix + "install", body, client=True)
                assert status == 200
                _validate_result(api_v1.find("POST", prefix + "install").result, answer["result"])
                install_id = answer["result"]["install"]["install_id"]
                for action, arguments in (("list", {"project": body["project"]} if "project" in body else {}),
                                          ("update", {"install_id": install_id, "enabled": False}),
                                          ("uninstall", {"install_id": install_id})):
                    status, answer = await call(prefix + action, arguments, client=True)
                    assert status == 200 and answer["result"]
                    _validate_result(api_v1.find("POST", prefix + action).result, answer["result"])
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("method", "path", "operation", "body"), [
    ("GET", "/api/catalog", "catalog_items.list", {}),
    ("GET", "/api/catalog/{item_id}", "catalog_items.get", {"item_id": "{item_id}"}),
    ("PUT", "/api/catalog/{item_id}", "catalog_items.update", {"item_id": "{item_id}", "fields": {"description": "Changed"}}),
    ("DELETE", "/api/catalog/{item_id}", "catalog_items.delete", {"item_id": "{item_id}"}),
    ("POST", "/api/installs", "installs.create", {"item_id": "{item_id}", "scope": "global"}),
    ("PUT", "/api/installs/{install_id}", "installs.update", {"install_id": "{install_id}", "enabled": False}),
    ("DELETE", "/api/installs/{install_id}", "installs.delete", {"install_id": "{install_id}"}),
    ("GET", "/api/projects/{project_id}/installs", "installs.project", {"project_id": "{project_id}"}),
])
def test_retired_routes_are_404_with_authenticated_v1_positive_control(tmp_path, monkeypatch, method, path, operation, body):
    async def run(pool):
        item = await _skill()
        project = await projects.create_project(name="Example")
        install = (await catalog.install({"item_id": item["item_id"], "scope": "project", "project_id": project["id"]}))["install"]
        values = {"item_id": item["item_id"], "install_id": install["install_id"], "project_id": project["id"]}
        body = {key: value.format(**values) if isinstance(value, str) else value for key, value in body_template.items()}
        async with _api(tmp_path, pool) as call:
            assert (await call(path.format(**values), method=method))[0] == 404
            status, answer = await call(operation, body)
            assert status == 200 and answer["status"] == "ok", answer
    body_template = body
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("transition", "expected"), [("removed", 404), ("owned", 400)])
def test_confirm_revalidates_item_existence_and_ownership(tmp_path, monkeypatch, transition, expected):
    async def run(pool):
        item, other = await _skill(), await _skill("other")
        project = await projects.create_project(name="Example")
        unrelated = await catalog.install({"item_id": other["item_id"], "scope": "global"})
        async with _api(tmp_path, pool) as call:
            status, asked = await call("catalog_items.delete", {"item_id": item["item_id"]}, client=True)
            assert status == 202
            if transition == "removed":
                await catalog.delete_item(item["item_id"])
            else:
                await pool.execute("UPDATE library_items SET origin='project', owner_project_id=$1 WHERE id=$2",
                                   project["id"], item["item_id"])
            status, answer = await call("confirm", {"confirmation_id": asked["confirmation_id"]}, client=True)
            assert status == expected and answer["error"]["code"] == ("not_found" if expected == 404 else "invalid")
            assert (await catalog.item_detail(other["item_id"]))["item"] == unrelated["item"]
            if transition == "owned":
                assert (await library.get_item(item["item_id"]))["owner_project_id"] == project["id"]
    run_db(run, monkeypatch, timeout=30)
