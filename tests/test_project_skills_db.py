"""Project skills against a real PostgreSQL: catalog items a project writes, the API, and what a run loads.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _fresh_schema() -> AsyncIterator[Any]:
    import asyncpg

    schema = f"loopx_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, server_settings={"search_path": schema})
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _run(test: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``test(pool)`` on a new schema that is also the server's pool."""
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)

    asyncio.run(_main())


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


async def _call(root: Path, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), task_router=TaskRouter(runtime_root=root),
                             supervisor=SimpleNamespace(), runtime_root=root)
    writer = _Writer()
    await server._route_api(method, path, {}, b"" if body is None else json.dumps(body).encode(), writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def _skill(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "description": f"Use for {name}.", "content": f"Reply {name.upper()}-OK.\n", **extra}


async def _items(pool: Any) -> dict[str, dict[str, Any]]:
    rows = await pool.fetch("SELECT * FROM library_items WHERE origin = 'project' ORDER BY name")
    return {f"{row['owner_project_id']}/{row['name']}": dict(row) for row in rows}


def test_names_are_unique_per_owner_and_a_skill_goes_with_its_project(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.agents import project_skills
    from looper.db import connection, projects

    async def _test(pool: Any) -> None:
        await connection.ensure_schema(pool)  # applying the schema again changes nothing
        calc = await projects.create_project(name="calc", pool=pool)
        other = await projects.create_project(name="other", pool=pool)
        await project_skills.add_skill(calc["id"], _skill("deploy-check"))
        await project_skills.add_skill(other["id"], _skill("deploy-check"))  # another project may reuse the name
        insert = ("INSERT INTO library_items (id, kind, name, sha256, size_bytes, file_count, origin, owner_project_id) "
                  "VALUES ($1, 'skill', $2, 'x', 1, 1, $3, $4)")
        with pytest.raises(asyncpg.UniqueViolationError):
            await pool.execute(insert, "skill-dup", "deploy-check", "project", calc["id"])
        with pytest.raises(asyncpg.CheckViolationError):  # a project-authored item has an owner, and only it
            await pool.execute(insert, "skill-orphan", "orphan", "project", None)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(insert, "lib-owned", "owned", "machine", calc["id"])
        assert await projects.delete_project(calc["id"], pool=pool)
        assert list(await _items(pool)) == [f"{other['id']}/deploy-check"]
        assert await pool.fetchval("SELECT count(*) FROM library_installs") == 1

    _run(_test, monkeypatch)


def test_the_api_writes_a_catalog_item_installed_in_the_project_then_changes_and_deletes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.library import item_sha256
    from looper.agents.project_skills import skill_file
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        base = f"/api/projects/{project['id']}/skills"
        status, added = await _call(tmp_path, "POST", base, _skill("release-notes", content="Notes · 中文\n"))
        assert status == 201 and added["skill"]["size_bytes"] == len(
            skill_file("release-notes", "Use for release-notes.", "Notes · 中文\n").encode())
        status, deploy = await _call(tmp_path, "POST", base, _skill("deploy-check", enabled=False))
        assert status == 201
        assert (await _call(tmp_path, "POST", base, _skill("deploy-check")))[0] == 409
        assert (await _call(tmp_path, "POST", "/api/projects/proj-nope/skills", _skill("x")))[0] == 404

        # Each is a catalog item owned by the project, one SKILL.md, installed there for the task roles.
        item = (await _items(pool))[f"{project['id']}/release-notes"]
        assert (item["kind"], item["origin"], item["description"]) == ("skill", "project", "Use for release-notes.")
        files = [dict(r) for r in await pool.fetch(
            "SELECT path, content, sha256, executable FROM library_files WHERE item_id = $1", item["id"])]
        assert [f["content"] for f in files] == [skill_file("release-notes", "Use for release-notes.", "Notes · 中文\n")]
        assert item["sha256"] == item_sha256(files)
        assert [(e["item"]["qualified_name"], e["roles"], e["enabled"]) for e in added["installs"]] == [
            ("project:release-notes", ["planner", "worker", "reviewer"], True)]
        assert [(e["item"]["name"], e["enabled"]) for e in deploy["installs"]] == [
            ("deploy-check", False), ("release-notes", True)]
        assert deploy["owned_count"] == 2

        skill_id = deploy["skill"]["skill_id"]
        assert (await _call(tmp_path, "PUT", f"{base}/{skill_id}", {"name": "release-notes"}))[0] == 409
        status, renamed = await _call(tmp_path, "PUT", f"{base}/{skill_id}", {"name": "deploy-gate", "content": "X"})
        assert status == 200 and renamed["skill"]["qualified_name"] == "project:deploy-gate"
        assert await pool.fetchval("SELECT content FROM library_files WHERE item_id = $1", skill_id) == skill_file(
            "deploy-gate", "Use for deploy-check.", "X")
        status, shown = await _call(tmp_path, "GET", f"{base}/{skill_id}")
        assert status == 200 and (shown["content"], shown["skill"]["description"]) == ("X\n", "Use for deploy-check.")
        # The description alone changes; the body stays.
        assert (await _call(tmp_path, "PUT", f"{base}/{skill_id}", {"description": "Gate a deploy."}))[0] == 200
        assert (await _call(tmp_path, "GET", f"{base}/{skill_id}"))[1]["content"] == "X\n"
        assert (await _call(tmp_path, "PUT", f"{base}/skill-nope", {"name": "y"}))[0] == 404

        status, after = await _call(tmp_path, "DELETE", f"{base}/{skill_id}")
        assert status == 200 and after["owned_count"] == 1 and after["deleted"] == skill_id
        assert (await _call(tmp_path, "DELETE", f"{base}/{skill_id}"))[0] == 404
        assert await pool.fetchval("SELECT count(*) FROM library_installs WHERE item_id = $1", skill_id) == 0

    _run(_test, monkeypatch)


def test_a_skill_is_read_and_changed_through_its_own_project_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        other = await projects.create_project(name="other", pool=pool)
        base = f"/api/projects/{project['id']}/skills"
        _, added = await _call(tmp_path, "POST", base, _skill("release-notes", content="Notes · 中文\n"))
        skill_id = added["skill"]["skill_id"]

        status, shown = await _call(tmp_path, "GET", f"{base}/{skill_id}")
        assert status == 200
        assert (shown["skill"]["qualified_name"], shown["content"]) == ("project:release-notes", "Notes · 中文\n")
        assert (await _call(tmp_path, "GET", f"{base}/skill-nope"))[0] == 404
        elsewhere = f"/api/projects/{other['id']}/skills/{skill_id}"
        assert (await _call(tmp_path, "GET", elsewhere))[0] == 404
        assert (await _call(tmp_path, "PUT", elsewhere, {"name": "mine"}))[0] == 404
        assert (await _call(tmp_path, "DELETE", elsewhere))[0] == 404

    _run(_test, monkeypatch)


def test_a_skill_installed_in_another_project_keeps_one_name_per_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Every project skill loads as project:<name>, so a project cannot hold two of one name."""
    from looper.db import projects

    async def _test(pool: Any) -> None:
        calc = await projects.create_project(name="calc", pool=pool)
        docs = await projects.create_project(name="docs", pool=pool)
        _, deploy = await _call(tmp_path, "POST", f"/api/projects/{calc['id']}/skills", _skill("deploy-check"))
        _, notes = await _call(tmp_path, "POST", f"/api/projects/{calc['id']}/skills", _skill("notes"))
        _, theirs = await _call(tmp_path, "POST", f"/api/projects/{docs['id']}/skills", _skill("deploy-check"))
        status, answer = await _call(tmp_path, "POST", "/api/installs", {
            "item_id": deploy["skill"]["skill_id"], "scope": "project", "project_id": docs["id"]})
        assert status == 409 and "both would load as project:deploy-check" in answer["error"]
        status, _ = await _call(tmp_path, "POST", "/api/installs", {
            "item_id": notes["skill"]["skill_id"], "scope": "project", "project_id": docs["id"], "roles": ["worker"]})
        assert status == 201
        # docs cannot now write a skill named notes, and calc cannot rename notes onto a name docs has.
        status, answer = await _call(tmp_path, "POST", f"/api/projects/{docs['id']}/skills", _skill("notes"))
        assert status == 409 and "project:notes" in answer["error"]
        await _call(tmp_path, "POST", f"/api/projects/{docs['id']}/skills", _skill("docs-only"))
        status, answer = await _call(tmp_path, "PUT", f"/api/projects/{calc['id']}/skills/{notes['skill']['skill_id']}",
                                     {"name": "docs-only"})
        assert status == 409 and "the skill is installed there" in answer["error"]
        status, answer = await _call(tmp_path, "PUT", f"/api/projects/{calc['id']}/skills/{notes['skill']['skill_id']}",
                                     {"name": "release-notes"})
        assert status == 200
        _, view = await _call(tmp_path, "GET", f"/api/projects/{docs['id']}/installs")
        assert sorted((e["item"]["qualified_name"], e["item"]["owner_project_name"], e["roles"])
                      for e in view["installs"]) == [
            ("project:deploy-check", "docs", ["planner", "worker", "reviewer"]),
            ("project:docs-only", "docs", ["planner", "worker", "reviewer"]),
            ("project:release-notes", "calc", ["worker"])]
        assert theirs["owned_count"] == 1

    _run(_test, monkeypatch)


def test_concurrent_adds_cannot_both_pass_the_count_cap_or_take_one_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import project_skills
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        results = await asyncio.gather(*(project_skills.add_skill(project["id"], _skill("same")) for _ in range(2)),
                                       return_exceptions=True)
        conflicts = [r for r in results if isinstance(r, project_skills.ProjectSkillConflict)]
        assert len(conflicts) == 1

        monkeypatch.setattr(project_skills, "PROJECT_MAX_SKILLS", 2)
        results = await asyncio.gather(project_skills.add_skill(project["id"], _skill("one")),
                                       project_skills.add_skill(project["id"], _skill("two")),
                                       return_exceptions=True)
        refused = [r for r in results if isinstance(r, project_skills.ProjectSkillError)]
        assert len(refused) == 1 and "at most 2 skills" in str(refused[0])
        assert await pool.fetchval("SELECT count(*) FROM library_items WHERE origin = 'project'") == 2

    _run(_test, monkeypatch)


def test_a_run_loads_only_the_skills_installed_and_enabled_in_its_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import project_skills
    from looper.agents.library import load_installed
    from looper.db import projects

    async def _test(pool: Any) -> None:
        calc = await projects.create_project(name="calc", pool=pool)
        other = await projects.create_project(name="other", pool=pool)
        await project_skills.add_skill(calc["id"], _skill("deploy-check"))
        await project_skills.add_skill(calc["id"], _skill("disabled-one", enabled=False))
        await project_skills.add_skill(other["id"], _skill("elsewhere"))
        runtime, work = tmp_path / "runtime", tmp_path / "work"
        work.mkdir()

        loaded = await load_installed(runtime, "worker", calc["id"])

        (plugin,) = (Path(d) for d in loaded.plugin_dirs)
        assert plugin.parent.parent == runtime.absolute() / ".local" / "library" and plugin.name == "project"
        assert sorted(p.name for p in (plugin / "skills").iterdir()) == ["deploy-check"]
        skill_md = plugin / "skills" / "deploy-check" / "SKILL.md"
        assert skill_md.read_text() == ("---\nname: deploy-check\ndescription: \"Use for deploy-check.\"\n---\n\n"
                                        "Reply DEPLOY-CHECK-OK.\n")
        assert stat.S_IMODE(skill_md.stat().st_mode) == 0o444
        assert [(i.name, i.scope) for i in loaded.items] == [("deploy-check", "project")] and loaded.error is None
        assert list(work.iterdir()) == []
        # The other project's run gets its own plugin, never this one; the Manager gets none.
        theirs = await load_installed(runtime, "planner", other["id"])
        assert [i.name for i in theirs.items] == ["elsewhere"] and theirs.plugin_dirs != loaded.plugin_dirs
        assert (await load_installed(runtime, "manager")).items == ()

    _run(_test, monkeypatch)
