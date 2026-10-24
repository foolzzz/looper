"""The catalog's import from this machine, and its items' installs across a re-import, against a real PostgreSQL.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards. The operator home
is synthetic (``library_fixtures``).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from library_fixtures import fake_claude_home, write

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return fake_claude_home(tmp_path / "home")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _items(pool: Any) -> dict[str, dict[str, Any]]:
    rows = await pool.fetch("SELECT * FROM library_items")
    return {row["name"]: dict(row) for row in rows}


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


async def _call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    if path.startswith(("/api/v1/catalog_sources/", "/api/v1/catalog_items/", "/api/v1/installs/", "/api/v1/project_skills/")):
        from tests.test_catalog_db import _call as catalog_call
        return await catalog_call(method, path, body)

    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    raw = body if isinstance(body, bytes) else (b"" if body is None else json.dumps(body).encode())
    writer = _Writer()
    await server._route_api(method, path, {}, raw, writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_an_import_stores_the_snapshot(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import library

    async def _test(pool: Any) -> None:
        status, view = await _call("POST", "/api/v1/catalog_sources/import_machine")
        assert status == 200
        items = await _items(pool)
        assert sorted((r["kind"], r["name"]) for r in items.values()) == [
            ("plugin", "demo@market"), ("skill", "alpha"), ("skill", "beta")]
        demo = items["demo@market"]
        assert (demo["source"], demo["version"], demo["origin"]) == (
            "~/.claude/plugins/cache/market/demo/1.2.0", "1.2.0", "machine")
        files = {r["path"]: dict(r) for r in await pool.fetch(
            "SELECT path, content, sha256, executable FROM library_files WHERE item_id = $1", demo["id"])}
        assert files["hooks/start.sh"]["executable"] and files["hooks/start.sh"]["content"].startswith("#!/bin/sh")
        assert all(f["sha256"] == _sha(f["content"]) for f in files.values())
        assert demo["file_count"] == len(files)
        assert demo["size_bytes"] == sum(len(f["content"].encode()) for f in files.values())
        assert demo["sha256"] == library.item_sha256(list(files.values()))
        assert "assets/logo.png" not in files and not any(p.startswith(".git/") for p in files)

        # The view lists the items, installed nowhere, and the import, with what was left out.
        assert [(i["kind"], i["name"], i["origin"], i["installs"]) for i in view["items"]] == [
            ("plugin", "demo@market", "machine", []), ("skill", "alpha", "machine", []),
            ("skill", "beta", "machine", [])]
        report = view["last_import"]
        assert (report["plugins"], report["skills"], sorted(report["added"])) == (
            1, 2, ["plugin:demo@market", "skill:alpha", "skill:beta"])
        assert {"item": "demo@market", "path": "assets/logo.png", "reason": "binary",
                "size_bytes": 17} in report["skipped"]
        assert view["installed_count"] == 0
        assert await pool.fetchval("SELECT count(*) FROM library_installs") == 0
        status, again = await _call("POST", "/api/v1/catalog_items/list", {})
        assert status == 200 and again["items"] == view["items"] and again["last_import"] == report

    run_db(_test, monkeypatch)


def test_a_reimport_replaces_changed_items_keeps_their_installs_and_leaves_project_skills_alone(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(pool: Any) -> None:
        await _call("POST", "/api/v1/catalog_sources/import_machine")
        before = await _items(pool)
        for name in ("alpha", "demo@market", "beta"):
            status, _ = await _call("POST", "/api/v1/installs/create", {"item_id": before[name]["id"], "scope": "global",
                                                              "roles": ["worker"]})
            assert status == 200
        status, project = await _call("POST", "/api/projects", {"name": "calc"})
        status, _ = await _call("POST", "/api/v1/project_skills/add", {"project_id": project['project_id'], "skill": {"name": "alpha", "description": "The project's own alpha.", "content": "Reply OWN."}})
        assert status == 200

        (home / "skills" / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\nReply ALPHA-v2.\n")
        os.unlink(home / "skills" / "beta")
        write(home / "skills" / "gamma" / "SKILL.md", "---\nname: gamma\n---\nReply GAMMA.\n")
        status, view = await _call("POST", "/api/v1/catalog_sources/import_machine")
        assert status == 200
        machine = {r["name"]: dict(r) for r in await pool.fetch("SELECT * FROM library_items WHERE origin = 'machine'")}
        assert sorted(machine) == ["alpha", "demo@market", "gamma"]
        # A changed item keeps its id and its installs; its files are the new ones.
        assert machine["alpha"]["id"] == before["alpha"]["id"]
        assert machine["alpha"]["sha256"] != before["alpha"]["sha256"]
        content = await pool.fetchval(
            "SELECT content FROM library_files WHERE item_id = $1 AND path = 'SKILL.md'", machine["alpha"]["id"])
        assert content.endswith("Reply ALPHA-v2.\n")
        installed = {r["item_id"]: list(r["roles"]) for r in await pool.fetch("SELECT * FROM library_installs")}
        assert installed[machine["alpha"]["id"]] == installed[machine["demo@market"]["id"]] == ["worker"]
        # An unchanged item is left as it was; a new one is installed nowhere; a removed one goes with its install.
        assert {k: machine["demo@market"][k] for k in ("id", "sha256", "imported_at")} == {
            k: before["demo@market"][k] for k in ("id", "sha256", "imported_at")}
        assert machine["gamma"]["id"] not in installed and before["beta"]["id"] not in installed
        report = view["last_import"]
        assert (report["added"], report["updated"], report["removed"], report["unchanged"]) == (
            ["skill:gamma"], ["skill:alpha"], ["skill:beta"], 1)
        assert await pool.fetchval(
            "SELECT count(*) FROM library_files WHERE item_id = $1", before["beta"]["id"]) == 0
        # The project's own alpha is not this machine's: the import left it and its install alone.
        own = await pool.fetchrow("SELECT * FROM library_items WHERE origin = 'project'")
        assert (own["name"], own["description"]) == ("alpha", "The project's own alpha.")
        assert own["id"] in installed and await pool.fetchval("SELECT count(*) FROM library_items") == 4

    run_db(_test, monkeypatch)


def test_an_import_over_the_cap_changes_nothing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import library

    async def _test(pool: Any) -> None:
        await _call("POST", "/api/v1/catalog_sources/import_machine")
        before = await pool.fetch("SELECT id, sha256 FROM library_items ORDER BY id")
        write(home / "skills" / "huge" / "SKILL.md", "x" * 2_000)
        monkeypatch.setattr(library, "LIBRARY_MAX_BYTES", 1_500)
        status, body = await _call("POST", "/api/v1/catalog_sources/import_machine")
        assert status == 400 and body["error"] == "invalid credential request"
        assert await pool.fetch("SELECT id, sha256 FROM library_items ORDER BY id") == before

    run_db(_test, monkeypatch)


def test_agents_load_the_installed_items_from_the_database(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.library import SKILLS_PLUGIN, load_installed

    async def _test(pool: Any) -> None:
        _, view = await _call("POST", "/api/v1/catalog_sources/import_machine")
        for item in view["items"]:
            if item["name"] != "beta":
                await _call("POST", "/api/v1/installs/create", {"item_id": item["item_id"], "scope": "global"})
        runtime = tmp_path / "runtime"
        loaded = await load_installed(runtime, "reviewer")
        assert [i["name"] for i in loaded.record()["items"]] == ["demo@market", "alpha"]
        plugin, skills = (Path(d) for d in loaded.plugin_dirs)
        assert (plugin / "hooks" / "start.sh").read_text() == "#!/bin/sh\necho started\n"
        assert os.access(plugin / "hooks" / "start.sh", os.X_OK)
        assert skills.name == SKILLS_PLUGIN and [p.name for p in (skills / "skills").iterdir()] == ["alpha"]
        assert (skills / "skills" / "alpha" / "SKILL.md").read_text().endswith("Reply ALPHA-OK.\n")

    run_db(_test, monkeypatch)
