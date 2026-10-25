"""The capability catalog against a real PostgreSQL: the upgrade, installs, and what each run loads.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TASK_ROLES = ["planner", "worker", "reviewer"]


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


def _global_settings_request(method: str, path: str, body: Any) -> tuple[str, str, Any]:
    """Keep existing service-contract fixtures on the global settings v1 transport."""
    parts = path.split("/")
    operation, data = None, body
    if method == "GET" and path == "/api/roles":
        operation, data = "roles/list", {}
    elif len(parts) == 4 and parts[1:3] == ["api", "roles"] and method in ("GET", "PUT"):
        operation = "roles/get" if method == "GET" else "roles/update"
        data = {"role": parts[3], **({"fields": body} if method == "PUT" else {})}
    elif method == "POST" and len(parts) == 5 and parts[1:3] == ["api", "roles"] and parts[4] == "reset-prompt":
        operation, data = "roles/reset_prompt", {"role": parts[3]}
    elif method == "GET" and path == "/api/agent-settings":
        operation, data = "agent_settings/get", {}
    elif method == "PUT" and path == "/api/agent-settings/system":
        operation, data = "agent_settings/update_system", {"values": body}
    elif method == "PUT" and len(parts) == 5 and parts[1:4] == ["api", "agent-settings", "roles"]:
        operation, data = "agent_settings/update_role", {"role": parts[4], "values": body}
    if operation:
        return "POST", f"/api/v1/{operation}", body if isinstance(body, bytes) else data
    return method, path, body


async def _call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    method, path, body = _global_settings_request(method, path, body)
    if path in {"/api/v1/tasks/detail", "/api/v1/tasks/page"}:
        from tests.db_support import call_v1
        return await call_v1(server, path, body or {})
    writer = _Writer()
    raw = b"" if body is None else json.dumps(body).encode()
    if (method == "POST" and path in ("/api/v1/registry/view", "/api/v1/runtimes/probe")) or path.startswith(
            ("/api/v1/catalog_sources/", "/api/v1/catalog_items/", "/api/v1/installs/",
             "/api/v1/project_skills/", "/api/v1/ops/models/", "/api/v1/models/", "/api/v1/roles/", "/api/v1/agent_settings/", "/api/v1/schedule_admin/")):
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops.clients import ClientOps
        server._client_ops = ClientOps(SimpleNamespace())
        await server._handle_v1(method, path, {}, {}, raw or b"{}", writer, OPERATOR)
    else:
        await server._route_api(method, path, {}, raw, writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    answer = json.loads(payload)
    if (method == "POST" and path in ("/api/v1/registry/view", "/api/v1/runtimes/probe")) or path.startswith(
            ("/api/v1/catalog_sources/", "/api/v1/catalog_items/", "/api/v1/installs/",
             "/api/v1/project_skills/", "/api/v1/ops/models/", "/api/v1/models/", "/api/v1/roles/", "/api/v1/agent_settings/")):
        answer = answer["result"] if answer.get("status") == "ok" else {"error": answer["error"]["message"]}
    if path.startswith("/api/v1/schedule_admin/") and answer.get("status") == "ok":
        answer = answer["result"]
    return int(head.split()[1]), answer


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- The upgrade: imported items stay uninstalled, project skills move into the catalog ---

# The tables as they were before the catalog, with what an operator had in them.
_BEFORE_CATALOG = r"""
CREATE TABLE projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', work_dir TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE library_items (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('plugin', 'skill')), name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true, size_bytes BIGINT NOT NULL, file_count INTEGER NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, name));
CREATE TABLE library_files (
    item_id TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE, path TEXT NOT NULL,
    content TEXT NOT NULL, sha256 TEXT NOT NULL, executable BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (item_id, path));
CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE project_skills (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(name) <= 64 AND name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    description TEXT NOT NULL, content TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name));
INSERT INTO projects (id, name) VALUES ('proj-a', 'calc'), ('proj-b', 'docs');
INSERT INTO library_items (id, kind, name, sha256, enabled, size_bytes, file_count) VALUES
    ('lib-000000000001', 'plugin', 'demo@market', 'a', true, 1, 1),
    ('lib-000000000002', 'skill', 'alpha', 'b', true, 1, 1),
    ('lib-000000000003', 'skill', 'beta', 'c', false, 1, 1);
"""
# (id, project, name, description, content, enabled): a description YAML would misread, a body without its
# last newline, a disabled skill, and one name in two projects.
_PROJECT_SKILLS = [
    ("skill-0000000000a1", "proj-a", "deploy-check", 'Use before a deploy: "make check" # 中文 \\ ok',
     "Run the checks, then reply DEPLOY-OK.", True),
    ("skill-0000000000a2", "proj-a", "release-notes", "Drafts release notes.", "Reply NOTES-OK.\n\n", False),
    ("skill-0000000000b1", "proj-b", "deploy-check", "Another project's deploy check.", "Reply B-OK.\n", True),
]


def _old_skills_sql() -> str:
    def literal(value: Any) -> str:
        return "true" if value is True else "false" if value is False else "'" + str(value).replace("'", "''") + "'"

    rows = ", ".join("(" + ", ".join(literal(v) for v in row) + ")" for row in _PROJECT_SKILLS)
    return f"INSERT INTO project_skills (id, project_id, name, description, content, enabled) VALUES {rows};"


def test_the_upgrade_keeps_imported_items_uninstalled_and_moves_project_skills_with_parity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.library import item_sha256, load_installed
    from looper.agents.project_skills import skill_file
    from looper.db import connection

    async def _test(pool: Any) -> None:
        assert await pool.fetchval("SELECT to_regclass('project_skills')") is None
        assert await pool.fetchval("SELECT 1 FROM schema_migrations WHERE name = 'library_catalog_installs'")
        columns = {r["column_name"] for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'library_items' "
            "AND table_schema = current_schema()")}
        assert "enabled" not in columns and {"origin", "owner_project_id", "description"} <= columns
        # The items imported before stay in the catalog, installed nowhere, whatever their enabled flag was.
        machine = await pool.fetch("SELECT id, origin, owner_project_id FROM library_items WHERE id LIKE 'lib-%'")
        assert sorted((r["id"], r["origin"], r["owner_project_id"]) for r in machine) == [
            (f"lib-00000000000{n}", "machine", None) for n in (1, 2, 3)]
        assert await pool.fetchval(
            "SELECT count(*) FROM library_installs WHERE item_id LIKE 'lib-%'") == 0
        # Each project skill: the same id, project, name and description; its SKILL.md as runs wrote it; installed
        # in its project for the three task roles, enabled as it was.
        for skill_id, project_id, name, description, content, enabled in _PROJECT_SKILLS:
            item = await pool.fetchrow("SELECT * FROM library_items WHERE id = $1", skill_id)
            assert (item["kind"], item["origin"], item["owner_project_id"], item["name"], item["description"]) == (
                "skill", "project", project_id, name, description)
            files = [dict(r) for r in await pool.fetch(
                "SELECT path, content, sha256, executable FROM library_files WHERE item_id = $1", skill_id)]
            text = skill_file(name, description, content)
            assert files == [{"path": "SKILL.md", "content": text, "sha256": _sha(text), "executable": False}]
            assert (item["sha256"], item["size_bytes"], item["file_count"]) == (
                item_sha256(files), len(text.encode("utf-8")), 1)
            (install,) = await pool.fetch("SELECT * FROM library_installs WHERE item_id = $1", skill_id)
            assert (install["scope"], install["project_id"], list(install["roles"]), install["enabled"]) == (
                "project", project_id, TASK_ROLES, enabled)
        # Run again, the schema changes nothing.
        before = [dict(r) for r in await pool.fetch("SELECT * FROM library_installs ORDER BY id")]
        await connection.ensure_schema(pool)
        assert [dict(r) for r in await pool.fetch("SELECT * FROM library_installs ORDER BY id")] == before
        assert await pool.fetchval("SELECT count(*) FROM library_items") == 6

        # Runs load them as before: project:<name> for the task agents of their own project, enabled ones only.
        runtime = tmp_path / "runtime"
        worker = await load_installed(runtime, "worker", "proj-a")
        assert [(i.plugin, i.name) for i in worker.items] == [("project", "deploy-check")]
        (plugin,) = worker.plugin_dirs
        assert Path(plugin).name == "project"
        assert (Path(plugin) / "skills" / "deploy-check" / "SKILL.md").read_text() == skill_file(
            *_PROJECT_SKILLS[0][2:5])
        theirs = await load_installed(runtime, "reviewer", "proj-b")
        assert [(i.name, i.sha256) for i in theirs.items] == [("deploy-check", await pool.fetchval(
            "SELECT sha256 FROM library_items WHERE id = 'skill-0000000000b1'"))]
        assert (await load_installed(runtime, "manager")).items == ()
        # The API reads the body back as it was written (a missing last newline added).
        status, body = await _call("POST", "/api/v1/project_skills/get", {"project_id": 'proj-a', "skill_id": 'skill-0000000000a1'})
        assert status == 200 and body["content"] == "Run the checks, then reply DEPLOY-OK.\n"
        assert body["skill"]["qualified_name"] == "project:deploy-check"

    run_db(_test, monkeypatch, before=_BEFORE_CATALOG + _old_skills_sql())


# --- Installs and what each run loads ---


async def _import(pool: Any, *names: str) -> dict[str, str]:
    """Machine items ``kind:name`` with one file each, through the import's own storage; their ids by name."""
    from looper.agents.library import item_sha256
    from looper.db import library as db_library

    items = []
    for label in names:
        kind, name = label.split(":")
        path = ".claude-plugin/plugin.json" if kind == "plugin" else "SKILL.md"
        text = f"{name} body\n"
        files = [{"path": path, "content": text, "sha256": _sha(text), "executable": False}]
        items.append({"kind": kind, "name": name, "source": "~/x", "version": "", "sha256": item_sha256(files),
                      "size_bytes": len(text), "file_count": 1, "files": files})
    await db_library.replace_snapshot(items, pool=pool)
    return {r["name"]: r["id"] for r in await pool.fetch("SELECT id, name FROM library_items")}


async def _projects(*names: str) -> list[str]:
    ids = []
    for name in names:
        status, body = await _call("POST", "/api/projects", {"name": name})
        assert status == 201, body
        ids.append(body["project_id"])
    return ids


async def _loaded(root: Path, role: str, project_id: str | None = None) -> list[str]:
    from looper.agents.library import load_installed

    library = await load_installed(root, role, project_id)
    assert library.error is None
    return [f"{item.plugin}:{item.name}" if item.kind.value == "skill" else item.plugin for item in library.items]


def test_each_run_loads_what_is_installed_for_its_role_globally_or_in_its_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(pool: Any) -> None:
        ids = await _import(pool, "plugin:demo@market", "skill:alpha", "skill:beta", "skill:gamma")
        calc, docs = await _projects("calc", "docs")
        root = tmp_path / "runtime"
        for role in ("manager", "planner", "worker", "reviewer"):
            assert await _loaded(root, role, calc) == []  # imported, installed nowhere
        # A global worker-only skill reaches Workers, not the Planner or the Manager.
        status, body = await _call("POST", "/api/v1/installs/create", {"item_id": ids["alpha"], "scope": "global",
                                                             "roles": ["worker"]})
        assert status == 200 and body["install"]["roles"] == ["worker"]
        # A global plugin for everyone; a skill installed in calc only.
        assert (await _call("POST", "/api/v1/installs/create", {"item_id": ids["demo@market"], "scope": "global"}))[0] == 200
        assert (await _call("POST", "/api/v1/installs/create", {"item_id": ids["beta"], "scope": "project",
                                                      "project_id": calc}))[0] == 200
        assert await _loaded(root, "worker", calc) == ["demo", "personal:alpha", "personal:beta"]
        assert await _loaded(root, "worker", docs) == ["demo", "personal:alpha"]
        assert await _loaded(root, "worker") == ["demo", "personal:alpha"]  # a run without a project
        assert await _loaded(root, "planner", calc) == ["demo", "personal:beta"]
        assert await _loaded(root, "reviewer", docs) == ["demo"]
        assert await _loaded(root, "manager") == ["demo"]
        # A skill installed globally and in the project loads once.
        assert (await _call("POST", "/api/v1/installs/create", {"item_id": ids["alpha"], "scope": "project",
                                                      "project_id": calc, "roles": ["planner"]}))[0] == 200
        assert await _loaded(root, "planner", calc) == ["demo", "personal:alpha", "personal:beta"]
        assert await _loaded(root, "worker", calc) == ["demo", "personal:alpha", "personal:beta"]

    run_db(_test, monkeypatch)


def test_uninstalling_disabling_or_dropping_a_role_takes_the_item_out_of_the_next_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(pool: Any) -> None:
        ids = await _import(pool, "skill:alpha", "skill:beta")
        (calc,) = await _projects("calc")
        root = tmp_path / "runtime"
        _, a = await _call("POST", "/api/v1/installs/create", {"item_id": ids["alpha"], "scope": "global"})
        _, b = await _call("POST", "/api/v1/installs/create", {"item_id": ids["beta"], "scope": "project", "project_id": calc})
        assert await _loaded(root, "worker", calc) == ["personal:alpha", "personal:beta"]

        status, body = await _call("POST", "/api/v1/installs/update", {"install_id": b['install']['install_id'], "enabled": False})
        assert status == 200 and body["install"]["enabled"] is False
        assert await _loaded(root, "worker", calc) == ["personal:alpha"]
        status, body = await _call("POST", "/api/v1/installs/update", {"install_id": a['install']['install_id'], "roles": ["planner"]})
        assert status == 200 and body["item"]["installs"][0]["roles"] == ["planner"]
        assert await _loaded(root, "worker", calc) == []
        assert await _loaded(root, "planner", calc) == ["personal:alpha"]
        status, body = await _call("POST", "/api/v1/installs/delete", {"install_id": a['install']['install_id']})
        assert status == 200 and body["item"]["installs"] == []
        assert await _loaded(root, "planner", calc) == []
        # The item stays in the catalog, and can be installed again.
        _, catalog = await _call("POST", "/api/v1/catalog_items/list", {})
        assert {i["name"]: len(i["installs"]) for i in catalog["items"]} == {"alpha": 0, "beta": 1}
        assert (await _call("POST", "/api/v1/installs/create", {"item_id": ids["alpha"], "scope": "global"}))[0] == 200

    run_db(_test, monkeypatch)


def test_the_install_api_refuses_what_an_install_cannot_be(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        ids = await _import(pool, "skill:alpha")
        (calc,) = await _projects("calc")
        _, own = await _call("POST", "/api/v1/project_skills/add", {"project_id": calc, "skill": {"name": "deploy-check", "description": "Deploy.", "content": "Reply OK."}})
        own_id = own["skill"]["skill_id"]
        refused = [
            ({"item_id": ids["alpha"], "scope": "project", "project_id": calc, "roles": ["manager"]}, 400,
             "cannot include the manager"),
            ({"item_id": ids["alpha"], "scope": "global", "roles": []}, 400, "does not match the declared schema"),
            ({"item_id": ids["alpha"], "scope": "global", "roles": ["admin"]}, 400, "does not match the declared schema"),
            ({"item_id": ids["alpha"], "scope": "everywhere"}, 400, "does not match the declared schema"),
            ({"item_id": ids["alpha"], "scope": "project"}, 400, "needs a project_id"),
            ({"item_id": ids["alpha"], "scope": "global", "project_id": calc}, 400, "no project_id"),
            ({"item_id": ids["alpha"], "scope": "global", "enabled": "yes"}, 400, "does not match the declared schema"),
            ({"item_id": ids["alpha"], "scope": "global", "where": "x"}, 400, "does not match the declared schema"),
            ({"item_id": own_id, "scope": "global"}, 400, "installed in projects only"),
            ({"item_id": "lib-000000000000", "scope": "global"}, 404, "no catalog item"),
            ({"item_id": ids["alpha"], "scope": "project", "project_id": "proj-nope"}, 404, "not found"),
            ({"item_id": own_id, "scope": "project", "project_id": calc}, 409, "installed there already"),
        ]
        for body, status, reason in refused:
            got, answer = await _call("POST", "/api/v1/installs/create", body)
            assert (got, reason in answer.get("error", "")) == (status, True), (body, answer)
        assert (await _call("POST", "/api/v1/installs/create", {"item_id": ids["alpha"], "scope": "global"}))[0] == 200
        status, answer = await _call("POST", "/api/v1/installs/create", {"item_id": ids["alpha"], "scope": "global"})
        assert status == 409 and "installed there already" in answer["error"]
        _, view = await _call("POST", "/api/v1/installs/project", {"project_id": calc})
        install_id = next(e["install_id"] for e in view["installs"] if e["item"]["item_id"] == own_id)
        status, answer = await _call("POST", "/api/v1/installs/update", {"install_id": install_id, "roles": ["manager", "worker"]})
        assert status == 400 and "cannot include the manager" in answer["error"]
        assert (await _call("POST", "/api/v1/installs/update", {"install_id": 'inst-000000000000', "enabled": True}))[0] == 404
        assert (await _call("POST", "/api/v1/installs/delete", {"install_id": 'inst-000000000000'}))[0] == 404
        assert (await _call("POST", "/api/v1/catalog_items/get", {"item_id": 'lib-000000000000'}))[0] == 404
        assert (await _call("POST", "/api/v1/installs/project", {"project_id": 'proj-nope'}))[0] == 404
        assert await pool.fetchval("SELECT count(*) FROM library_installs") == 2

    run_db(_test, monkeypatch)


def test_the_catalog_and_a_projects_view_show_where_each_item_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        ids = await _import(pool, "plugin:demo@market", "skill:alpha", "skill:mgr-only")
        calc, docs = await _projects("calc", "docs")
        await _call("POST", "/api/v1/installs/create", {"item_id": ids["demo@market"], "scope": "global"})
        await _call("POST", "/api/v1/installs/create", {"item_id": ids["mgr-only"], "scope": "global", "roles": ["manager"]})
        await _call("POST", "/api/v1/installs/create", {"item_id": ids["alpha"], "scope": "project", "project_id": calc,
                                              "roles": ["worker", "reviewer"]})
        await _call("POST", "/api/v1/project_skills/add", {"project_id": calc, "skill": {"name": "deploy-check", "description": "Deploy.", "content": "Reply OK."}})

        status, catalog = await _call("POST", "/api/v1/catalog_items/list", {})
        # The roles an install may name, the ones a task's Planner defines last; an install names those only when
        # asked to (demo's names the four built-in roles).
        assert status == 200 and catalog["roles"] == ["manager", *TASK_ROLES, "task_role"]
        by_name = {i["name"]: i for i in catalog["items"]}
        assert [(i["kind"], i["qualified_name"], i["origin"]) for i in catalog["items"]] == [
            ("plugin", "demo", "machine"), ("skill", "personal:alpha", "machine"),
            ("skill", "project:deploy-check", "project"), ("skill", "personal:mgr-only", "machine")]
        assert by_name["deploy-check"]["owner_project_name"] == "calc"
        assert [(s["scope"], s["project_name"], s["roles"]) for s in by_name["alpha"]["installs"]] == [
            ("project", "calc", ["worker", "reviewer"])]
        assert catalog["installed_count"] == 4
        status, detail = await _call("POST", "/api/v1/catalog_items/get", {"item_id": ids['alpha']})
        assert status == 200 and detail["skill_md"] == "alpha body\n"
        assert detail["files"] == [{"path": "SKILL.md", "size_bytes": 11, "executable": False}]

        # A project sees its own installs, then the global ones that reach its task roles (not the manager-only one).
        _, view = await _call("POST", "/api/v1/installs/project", {"project_id": calc})
        assert [(e["item"]["qualified_name"], e["scope"], e["inherited"]) for e in view["installs"]] == [
            ("personal:alpha", "project", False), ("project:deploy-check", "project", False),
            ("demo", "global", True)]
        assert (view["owned_count"], view["task_roles"]) == (1, [*TASK_ROLES, "task_role"])
        assert next(e for e in view["installs"] if e["item"]["qualified_name"] == "demo")["roles"] == [
            "manager", *TASK_ROLES]
        _, other = await _call("POST", "/api/v1/installs/project", {"project_id": docs})
        assert [e["item"]["qualified_name"] for e in other["installs"]] == ["demo"]

    run_db(_test, monkeypatch)
