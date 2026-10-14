"""Project skills against a real PostgreSQL: the table, the API and what a run loads.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database. Each
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

DSN = os.environ.get("LOOPX_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")


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
    from loopx.db import connection

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
    from loopx.agents.http_server import AgentHTTPServer
    from loopx.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), task_router=TaskRouter(runtime_root=root),
                             supervisor=SimpleNamespace(), runtime_root=root)
    writer = _Writer()
    await server._route_api(method, path, {}, b"" if body is None else json.dumps(body).encode(), writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def _skill(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "description": f"Use for {name}.", "content": f"Reply {name.upper()}-OK.\n", **extra}


def test_the_table_holds_slugs_unique_per_project_and_goes_with_its_project(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from loopx.db import connection, projects

    async def _test(pool: Any) -> None:
        await connection.ensure_schema(pool)  # applying the schema again changes nothing
        calc = await projects.create_project(name="calc", pool=pool)
        other = await projects.create_project(name="other", pool=pool)
        insert = "INSERT INTO project_skills (id, project_id, name, description, content) VALUES ($1, $2, $3, 'd', 'c')"
        await pool.execute(insert, "skill-a", calc["id"], "deploy-check")
        await pool.execute(insert, "skill-b", other["id"], "deploy-check")  # another project may reuse the name
        assert await pool.fetchval("SELECT enabled FROM project_skills WHERE id = 'skill-a'") is True
        with pytest.raises(asyncpg.UniqueViolationError):
            await pool.execute(insert, "skill-c", calc["id"], "deploy-check")
        for bad in ("Deploy", "deploy check", "-deploy", "deploy--check", "../x", "", "x" * 65):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(insert, f"skill-{uuid.uuid4().hex[:8]}", calc["id"], bad)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute(insert, "skill-orphan", "proj-nope", "orphan")
        assert await projects.delete_project(calc["id"], pool=pool)
        assert await pool.fetchval("SELECT array_agg(id) FROM project_skills") == ["skill-b"]

    _run(_test, monkeypatch)


def test_the_api_adds_lists_toggles_renames_and_deletes_skills(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from loopx.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        base = f"/api/projects/{project['id']}/skills"
        status, added = await _call(tmp_path, "POST", base, _skill("release-notes", content="Notes · 中文\n"))
        assert status == 201 and added["skill"]["size_bytes"] == len("Notes · 中文\n".encode())
        status, deploy = await _call(tmp_path, "POST", base, _skill("deploy-check"))
        assert status == 201
        assert (await _call(tmp_path, "POST", base, _skill("deploy-check")))[0] == 409
        assert (await _call(tmp_path, "POST", base, _skill("Deploy Check")))[0] == 400
        assert (await _call(tmp_path, "POST", "/api/projects/proj-nope/skills", _skill("x")))[0] == 404

        status, view = await _call(tmp_path, "GET", base)
        assert status == 200
        assert [(s["name"], s["enabled"]) for s in view["skills"]] == [("deploy-check", True), ("release-notes", True)]
        row = await pool.fetchrow("SELECT * FROM project_skills WHERE name = 'release-notes'")
        assert (row["content"], row["description"]) == ("Notes · 中文\n", "Use for release-notes.")

        skill_id = deploy["skill"]["skill_id"]
        status, toggled = await _call(tmp_path, "PUT", f"{base}/{skill_id}", {"enabled": False})
        assert status == 200 and toggled["enabled_count"] == 1
        row = await pool.fetchrow("SELECT enabled, updated_at, created_at FROM project_skills WHERE id = $1", skill_id)
        assert row["enabled"] is False and row["updated_at"] > row["created_at"]
        assert (await _call(tmp_path, "PUT", f"{base}/{skill_id}", {"name": "release-notes"}))[0] == 409
        status, renamed = await _call(tmp_path, "PUT", f"{base}/{skill_id}", {"name": "deploy-gate", "content": "X"})
        assert status == 200 and renamed["skill"]["qualified_name"] == "project:deploy-gate"
        assert await pool.fetchval("SELECT content FROM project_skills WHERE id = $1", skill_id) == "X"
        assert (await _call(tmp_path, "PUT", f"{base}/skill-nope", {"enabled": True}))[0] == 404

        status, after = await _call(tmp_path, "DELETE", f"{base}/{skill_id}")
        assert status == 200 and after["count"] == 1
        assert (await _call(tmp_path, "DELETE", f"{base}/{skill_id}"))[0] == 404

    _run(_test, monkeypatch)


def test_concurrent_adds_cannot_both_pass_the_count_cap_or_take_one_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loopx.agents import project_skills
    from loopx.db import projects

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
        assert await pool.fetchval("SELECT count(*) FROM project_skills") == 2

    _run(_test, monkeypatch)


def test_a_run_loads_only_its_projects_enabled_skills_from_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from loopx.agents import project_skills
    from loopx.db import projects

    async def _test(pool: Any) -> None:
        calc = await projects.create_project(name="calc", pool=pool)
        other = await projects.create_project(name="other", pool=pool)
        await project_skills.add_skill(calc["id"], _skill("deploy-check"))
        await project_skills.add_skill(calc["id"], _skill("disabled-one", enabled=False))
        await project_skills.add_skill(other["id"], _skill("elsewhere"))
        runtime, work = tmp_path / "runtime", tmp_path / "work"
        work.mkdir()

        loaded = await project_skills.load_project_skills(runtime, calc)

        plugin = Path(loaded.plugin_dir or "")
        assert plugin == runtime / ".local" / "projects" / calc["id"] / "skills" / str(loaded.sha256) / "project"
        assert sorted(p.name for p in (plugin / "skills").iterdir()) == ["deploy-check"]
        skill_md = plugin / "skills" / "deploy-check" / "SKILL.md"
        assert skill_md.read_text() == ("---\nname: deploy-check\ndescription: \"Use for deploy-check.\"\n---\n\n"
                                        "Reply DEPLOY-CHECK-OK.\n")
        assert stat.S_IMODE(skill_md.stat().st_mode) == 0o444
        assert [s.name for s in loaded.skills] == ["deploy-check"] and loaded.error is None
        assert list(work.iterdir()) == []
        # The other project's run gets its own plugin, never this one.
        theirs = await project_skills.load_project_skills(runtime, other)
        assert [s.name for s in theirs.skills] == ["elsewhere"] and theirs.plugin_dir != loaded.plugin_dir
        # The view the API gives counts the same enabled skills.
        view = await project_skills.skills_view(calc["id"])
        assert (view["count"], view["enabled_count"]) == (2, 1)

    _run(_test, monkeypatch)
