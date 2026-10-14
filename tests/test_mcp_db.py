"""MCP servers in the catalog against a real PostgreSQL: what each run mounts, the secrets, and the import.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_catalog_db import DSN, _call, _projects, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")

SECRET = "sk-test-0123456789"


async def _server(name: str, **fields: Any) -> dict[str, Any]:
    stdio = {"transport": "stdio", "command": "python3", "args": ["server.py"]}
    body = {"name": name, **(stdio if fields.get("transport", "stdio") == "stdio" else {}), **fields}
    status, created = await _call("POST", "/api/catalog/mcp", body)
    assert status == 201, created
    return created["item"]


async def _mounted(root: Path, role: str, project_id: str | None = None) -> tuple[list[str], dict[str, Any] | None]:
    """The argv a run of ``role`` gets from its installs, and the servers its --mcp-config file names."""
    from looper.agents.claude_cli import library_args
    from looper.agents.library import load_installed

    library = await load_installed(root, role, project_id)
    assert library.error is None
    argv = library_args(library)
    if "--mcp-config" not in argv:
        return argv, None
    path = Path(argv[argv.index("--mcp-config") + 1])
    assert path.parent == root.absolute() / ".local" / "mcp" and path.stat().st_mode & 0o777 == 0o400
    return argv, json.loads(path.read_text())["mcpServers"]


def _no_secret(payload: Any) -> None:
    assert SECRET not in json.dumps(payload, default=str)


def test_a_server_reaches_only_the_roles_and_projects_it_is_installed_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(pool: Any) -> None:
        calc, docs = await _projects("calc", "docs")
        root = tmp_path / "runtime"
        marker = await _server("marker", env={"MARKER": SECRET})
        search = await _server("search", transport="http", url="https://mcp.example.test/mcp",
                               headers={"Authorization": f"Bearer {SECRET}"})
        for role in ("manager", "planner", "worker", "reviewer"):
            assert await _mounted(root, role, calc) == ([], None)  # added, installed nowhere

        # A global worker-only server reaches Workers, not the Planner, the Reviewer or the Manager.
        status, _ = await _call("POST", "/api/installs", {"item_id": marker["item_id"], "scope": "global",
                                                          "roles": ["worker"]})
        assert status == 201
        argv, servers = await _mounted(root, "worker", docs)
        assert argv[-2] == "--mcp-config" and servers == {"marker": {
            "type": "stdio", "command": "python3", "args": ["server.py"], "env": {"MARKER": SECRET}}}
        assert (await _mounted(root, "worker"))[1] == servers  # a run without a project
        for role in ("manager", "planner", "reviewer"):
            assert await _mounted(root, role, docs) == ([], None)

        # A project install reaches only that project's task agents.
        assert (await _call("POST", "/api/installs", {"item_id": search["item_id"], "scope": "project",
                                                      "project_id": calc, "roles": ["planner", "reviewer"]}))[0] == 201
        assert sorted((await _mounted(root, "planner", calc))[1] or {}) == ["search"]
        assert (await _mounted(root, "planner", calc))[1]["search"] == {
            "type": "http", "url": "https://mcp.example.test/mcp", "headers": {"Authorization": f"Bearer {SECRET}"}}
        assert sorted((await _mounted(root, "worker", calc))[1] or {}) == ["marker"]
        assert sorted((await _mounted(root, "reviewer", calc))[1] or {}) == ["search"]
        assert await _mounted(root, "planner", docs) == ([], None)
        assert await _mounted(root, "manager") == ([], None)

        # The record names the servers, never their values; uninstalling takes the server out of the next run.
        from looper.agents.library import load_installed

        record = (await load_installed(root, "reviewer", calc)).record()
        assert record is not None and record["mcp_servers"] == ["search"]
        _no_secret(record)
        _, project = await _call("GET", f"/api/projects/{calc}/installs")
        install = next(e for e in project["installs"] if e["item"]["name"] == "search")
        _no_secret(project)
        assert (await _call("DELETE", f"/api/installs/{install['install_id']}"))[0] == 200
        assert await _mounted(root, "planner", calc) == ([], None)

    _run(_test, monkeypatch)


def test_the_api_masks_the_values_and_sending_the_mask_back_keeps_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(pool: Any) -> None:
        item = await _server("marker", env={"MARKER": SECRET, "MODE": "fast"}, description="Returns a marker.")
        assert item["mcp"] == {"transport": "stdio", "command": "python3", "args": ["server.py"],
                               "env": {"MARKER": "***", "MODE": "***"}, "url": "", "headers": {}}
        item_id, version = item["item_id"], item["sha256"]
        await _call("POST", "/api/installs", {"item_id": item_id, "scope": "global", "roles": ["worker"]})
        for path in ("/api/catalog", f"/api/catalog/{item_id}"):
            status, body = await _call("GET", path)
            assert status == 200
            _no_secret(body)

        # The mask sent back keeps the stored value; a new value or a removed name is a new version.
        status, body = await _call("PUT", f"/api/catalog/{item_id}", {
            "args": ["server.py", "--quiet"], "env": {"MARKER": "***", "MODE": "***"}})
        assert status == 200 and body["item"]["mcp"]["env"] == {"MARKER": "***", "MODE": "***"}
        _no_secret(body)
        servers = (await _mounted(tmp_path, "worker"))[1]
        assert servers == {"marker": {"type": "stdio", "command": "python3", "args": ["server.py", "--quiet"],
                                      "env": {"MARKER": SECRET, "MODE": "fast"}}}
        assert body["item"]["sha256"] != version
        status, body = await _call("PUT", f"/api/catalog/{item_id}", {"description": "Still a marker."})
        assert status == 200 and (await _mounted(tmp_path, "worker"))[1] == servers  # only the description
        status, body = await _call("PUT", f"/api/catalog/{item_id}", {"env": {"MARKER": "rotated"}})
        assert status == 200 and body["item"]["mcp"]["env"] == {"MARKER": "***"}
        assert (await _mounted(tmp_path, "worker"))[1]["marker"]["env"] == {"MARKER": "rotated"}

        # The mask stands only for a stored value.
        status, body = await _call("PUT", f"/api/catalog/{item_id}", {"env": {"TOKEN": "***"}})
        assert status == 400 and "TOKEN" in body["error"]
        # Deleting the server takes it out of the catalog and the next run.
        assert (await _call("DELETE", f"/api/catalog/{item_id}"))[0] == 200
        assert (await _call("GET", f"/api/catalog/{item_id}"))[0] == 404
        assert await _mounted(tmp_path, "worker") == ([], None)

    _run(_test, monkeypatch)


@pytest.mark.parametrize(("body", "status", "reason"), [
    ({"name": "bad name", "transport": "stdio", "command": "x"}, 400, "name"),
    ({"name": "a", "transport": "ws", "command": "x"}, 400, "transport"),
    ({"name": "a", "transport": "stdio"}, 400, "command"),
    ({"name": "a", "transport": "stdio", "command": "x", "url": "https://x.test"}, 400, "no url"),
    ({"name": "a", "transport": "http", "url": "file:///etc/passwd"}, 400, "http"),
    ({"name": "a", "transport": "sse", "url": "https://x.test", "env": {"A": "b"}}, 400, "no env"),
    ({"name": "a", "transport": "http", "url": "https://x.test", "headers": {"X": "a\nb"}}, 400, "one line"),
    ({"name": "a", "transport": "stdio", "command": "x", "env": {"1BAD": "v"}}, 400, "not usable"),
    ({"name": "a", "transport": "stdio", "command": "x", "env": {f"V{n}": "v" for n in range(51)}}, 400, "50"),
    ({"name": "a", "transport": "stdio", "command": "x", "env": {"A": "***"}}, 400, "stored value"),
    ({"name": "a", "transport": "stdio", "command": "x", "timeout": 5}, 400, "unknown field"),
    ({"name": "taken", "transport": "stdio", "command": "x"}, 409, "already"),
])
def test_adding_a_server_refuses_what_it_cannot_be(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], status: int, reason: str,
) -> None:
    async def _test(pool: Any) -> None:
        await _server("taken")
        got, answer = await _call("POST", "/api/catalog/mcp", body)
        assert got == status and reason in answer["error"], answer

    _run(_test, monkeypatch)


def test_importing_from_this_machine_reads_its_user_mcp_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import mcp

    claude_json = tmp_path / ".claude.json"

    def write(servers: dict[str, Any]) -> None:
        claude_json.write_text(json.dumps({"numStartups": 3, "mcpServers": servers,
                                           "projects": {"/x": {"mcpServers": {"local": {"command": "y"}}}}}))

    async def _test(pool: Any) -> None:
        await _server("mine")  # added here: an imported server of that name is not imported
        write({
            "marker": {"type": "stdio", "command": "python3", "args": ["m.py"], "env": {"MARKER": SECRET}},
            "docs": {"type": "http", "url": "https://docs.example.test/mcp", "headers": {"X-Key": SECRET},
                     "headersHelper": "get-headers.sh"},
            "plain": {"command": "node", "args": ["p.js"]},  # no type: stdio
            "mine": {"command": "other"},
            "bad name": {"command": "x"},
            "broken": {"type": "http"},
        })
        catalog = await mcp.import_from_machine(claude_json)
        _no_secret(catalog)
        by_name = {i["name"]: i for i in catalog["items"]}
        assert {n: (i["origin"], i["installs"]) for n, i in by_name.items()} == {
            "docs": ("machine", []), "marker": ("machine", []), "plain": ("machine", []), "mine": ("ui", [])}
        assert by_name["docs"]["mcp"]["headers"] == {"X-Key": "***"}
        assert by_name["plain"]["mcp"]["transport"] == "stdio"
        report = catalog["last_mcp_import"]
        assert report["added"] == ["mcp:docs", "mcp:marker", "mcp:plain"] and report["servers"] == 3
        assert {(s["item"], s["path"]) for s in report["skipped"]} == {
            ("bad name", ""), ("broken", ""), ("docs", "headersHelper"), ("mine", "")}
        await _call("POST", "/api/installs", {"item_id": by_name["marker"]["item_id"], "scope": "global",
                                              "roles": ["worker"]})
        assert (await _mounted(tmp_path / "rt", "worker"))[1]["marker"]["env"] == {"MARKER": SECRET}

        # A re-import updates a changed server (keeping its id and installs) and removes one that is gone.
        write({"marker": {"type": "stdio", "command": "python3", "args": ["m.py", "-v"], "env": {"MARKER": "new"}},
               "plain": {"command": "node", "args": ["p.js"]}})
        catalog = await mcp.import_from_machine(claude_json)
        report = catalog["last_mcp_import"]
        assert (report["added"], report["updated"], report["removed"], report["unchanged"]) == (
            [], ["mcp:marker"], ["mcp:docs"], 1)
        marker = next(i for i in catalog["items"] if i["name"] == "marker")
        assert marker["item_id"] == by_name["marker"]["item_id"] and len(marker["installs"]) == 1
        assert (await _mounted(tmp_path / "rt", "worker"))[1]["marker"]["args"] == ["m.py", "-v"]
        # The plugins-and-skills import leaves the MCP servers alone.
        from looper.db import library as db_library

        await db_library.replace_snapshot([], pool=pool)
        assert sorted(i["name"] for i in (await _call("GET", "/api/catalog"))[1]["items"]) == [
            "marker", "mine", "plain"]

    _run(_test, monkeypatch)


def test_a_catalog_from_before_mcp_servers_accepts_them(monkeypatch: pytest.MonkeyPatch) -> None:
    before = """
    CREATE TABLE library_items (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('plugin', 'skill')), name TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL,
        size_bytes BIGINT NOT NULL, file_count INTEGER NOT NULL,
        imported_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
    """

    async def _test(pool: Any) -> None:
        assert (await _server("marker"))["kind"] == "mcp"
        with pytest.raises(Exception, match="library_items_kind_check"):
            await pool.execute("INSERT INTO library_items (id, kind, name, sha256, size_bytes, file_count) "
                               "VALUES ('x', 'agent', 'x', 'x', 0, 0)")

    _run(_test, monkeypatch, before=before)
