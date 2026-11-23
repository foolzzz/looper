"""Deprecation, restore, the displayed skill budget, and a run that skips a deprecated skill.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db, until
from tests.test_skill_versions_db import _MAIN_DEFAULT_PATH, _default_path_bytes, _skill

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

_PLUGIN = "578d74b686aa9709b39efa146d180db0e5c45f914df31d5896b763761416f73d"
_ITEM_SHA = "9233045e8b40d7c828c578b7d1f49dd0a9aa1dd19b36ae7881c4e9da13e1fbb0"
_RECORD_SHA = "2307cfe1d5cb6b95e254fcdda04b3fcee50ad3f5f721330dd1fecb88cf7449e9"
_HEADING = "## Your step and its report (required; code checks the report)"


def _expected_path(role: str) -> bytes:
    shown = json.loads(_MAIN_DEFAULT_PATH)
    shown["record"]["role"] = role
    return json.dumps(shown, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _claude_body(seen: Path) -> str:
    target = json.dumps(str(seen))
    return f"""
import json, sys
flag = next(f for f in ("--append-system-prompt-file", "--system-prompt-file") if f in sys.argv)
record = {{"argv": sys.argv[1:], "prompt": open(sys.argv[sys.argv.index(flag) + 1], encoding="utf-8").read()}}
open({target}, "w", encoding="utf-8").write(json.dumps(record))
json.loads(sys.stdin.readline())
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
    "result": json.dumps({{"status": "completed", "changed_files": {{}}, "summary": "ok"}})}}), flush=True)
"""


async def _quiet(pool: Any, project_id: str) -> None:
    await pool.execute(
        "UPDATE projects SET agent_room = 'off', vault = 'off', operator_notes = 'off' WHERE id = $1",
        project_id,
    )


async def _drive(tmp_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool
    from tests.fake_claude import fake_claude

    tmp_path.mkdir(parents=True, exist_ok=True)
    seen = tmp_path / "seen.json"
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    bus = MessageBus(runtime_root=tmp_path / "bus")
    workers = WorkerPool(
        config=WorkerConfig(claude_bin=fake_claude(tmp_path / "claude", _claude_body(seen))),
        runtime_root=tmp_path / "runtime",
    )
    results: list[Any] = []
    bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
    await workers.start(bus)
    await asyncio.sleep(0)
    try:
        await bus.publish(MessageEnvelope.create(
            source=planner_address("task-r"), target=worker_address("worker-2"),
            message_type=MessageType.WORK_ITEM,
            payload={**payload, "context": {"work_dir": str(work)}},
        ))
        await until(lambda: results, "the run's result", timeout=45)
    finally:
        await workers.stop()
        await bus.shutdown()
    assert results[0].payload.get("status") == "completed", results[0].payload
    return json.loads(seen.read_text(encoding="utf-8"))


def _library(metadata: Any) -> dict[str, Any]:
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert isinstance(metadata, dict) and "library" in metadata, metadata
    return metadata["library"]


def test_migration_adds_lifecycle_without_changing_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.agents import project_skills
    from looper.db import projects
    from looper.db.connection import SCHEMA_PATH
    from looper.db.migrations import skill_lifecycle as migration

    async def _test(pool: Any) -> None:
        assert "lifecycle TEXT" not in SCHEMA_PATH.read_text(encoding="utf-8")
        project = await projects.create_project(name="calc", pool=pool)
        added = await project_skills.add_skill(project["id"], _skill("deploy-check"))
        skill_id = added["skill"]["skill_id"]
        before = await pool.fetch("SELECT id, sha256 FROM library_items ORDER BY id")
        async with pool.acquire() as conn:
            assert not await migration.pending(conn)
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'skill_lifecycle'")
        async with pool.acquire() as conn:
            assert not await migration.pending(conn)
        await pool.execute("ALTER TABLE library_items DROP COLUMN lifecycle")
        async with pool.acquire() as conn, conn.transaction():
            assert await migration.pending(conn)
            await migration.apply(conn, sql="", state="current")
            assert not await migration.pending(conn)
        assert await pool.fetch("SELECT id, sha256 FROM library_items ORDER BY id") == before
        assert await pool.fetchval("SELECT lifecycle FROM library_items WHERE id = $1", skill_id) == "active"
        with pytest.raises(asyncpg.CheckViolationError):
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("UPDATE library_items SET lifecycle = 'retired' WHERE id = $1", skill_id)
        assert await pool.fetchval("SELECT lifecycle FROM library_items WHERE id = $1", skill_id) == "active"
        async with pool.acquire() as conn, conn.transaction():
            await migration.apply(conn, sql="", state="current")
        assert await pool.fetch("SELECT id, sha256 FROM library_items ORDER BY id") == before

    run_db(_test, monkeypatch)


def test_an_active_catalog_loads_the_same_record_and_launch_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Nothing deprecated: each role's load, the worker prompt and the launch stay the pinned default."""
    from looper.agents import project_skills
    from looper.agents.claude_cli import library_args
    from looper.agents.library import load_installed
    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        await _quiet(pool, project["id"])
        added = await project_skills.add_skill(project["id"], _skill("deploy-check"))
        assert added["skill"]["lifecycle"] == "active"
        assert await pool.fetchval(
            "SELECT sha256 FROM library_items WHERE id = $1", added["skill"]["skill_id"]) == _ITEM_SHA

        for role in ("planner", "worker", "reviewer"):
            runtime = tmp_path / role
            loaded = await load_installed(runtime, role, project["id"])
            record = loaded.record()
            assert record is not None
            assert loaded.skipped_deprecated == () and loaded.mcp_config is None and loaded.error is None
            assert record["sha256"] == _RECORD_SHA and record["role"] == role
            assert set(record) == {"sha256", "role", "project_id", "items"}
            assert set(record["items"][0]) == {"kind", "name", "sha256", "plugin", "scope"}
            assert "lifecycle" not in json.dumps(record) and "deprecated" not in json.dumps(record)
            args = library_args(loaded)
            assert _default_path_bytes(record, args, runtime, project["id"]) == _expected_path(role)
            assert any(part.endswith(f"{_PLUGIN}/project") for part in args)

        work = tmp_path / "run"
        await tasks.create_task(
            task_id="task-r", requirement="do it", project_id=project["id"], work_dir=str(work),
            status="running", pool=pool)
        seen = await _drive(work, {
            "task_id": "step-s2", "parent_task_id": "task-r", "step_key": "s2", "instruction": "do it",
        })
        assert _HEADING in seen["prompt"]
        assert "lifecycle" not in seen["prompt"] and "deprecated" not in seen["prompt"].lower()
        plugin_dirs = [seen["argv"][index + 1] for index, arg in enumerate(seen["argv"]) if arg == "--plugin-dir"]
        assert any(f"/{_PLUGIN}/project" in path for path in plugin_dirs)
        assert await pool.fetchval(
            "SELECT count(*) FROM activity WHERE task_id = 'task-r' AND summary LIKE 'Deprecated skills skipped%'") == 0
        metadata = await pool.fetchval(
            "SELECT metadata FROM llm_records WHERE task_id = 'task-r' AND agent_id = 'worker-2'")
        library = _library(metadata)
        assert library["sha256"] == _RECORD_SHA
        assert [item["name"] for item in library["items"]] == ["deploy-check"]
        assert "lifecycle" not in json.dumps(library)

    run_db(_test, monkeypatch, timeout=120)


def test_context_budget_counts_only_the_skills_a_run_would_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import project_skills
    from looper.agents.project_skills import ProjectSkillNotFound
    from looper.agents.skill_lifecycle import context_budget, partition_deprecated
    from looper.db import projects

    rows = (
        {"name": "kept", "lifecycle": "active", "description": "d"},
        {"name": "old", "lifecycle": "deprecated"},
        {"name": "old", "lifecycle": "deprecated"},
        {"name": "", "lifecycle": "deprecated"},
        {"name": "mystery", "lifecycle": "other"},
        {"name": "plain", "description": "x"},
    )
    kept, skipped = partition_deprecated(rows)
    assert skipped == ("old",)
    assert [row["name"] for row in kept] == ["kept", "mystery", "plain"]
    assert all("lifecycle" not in row for row in kept)
    assert rows[0]["lifecycle"] == "active"

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        added = await project_skills.add_skill(project["id"], _skill("deploy-check"))
        skill_id = added["skill"]["skill_id"]
        await project_skills.add_skill(project["id"], _skill("off-one", enabled=False))
        await pool.execute(
            "INSERT INTO library_items (id, kind, name, sha256, size_bytes, file_count, origin, description) "
            "VALUES ('plugin-big', 'plugin', 'big-plugin', 'plugin-sha', 1, 0, 'machine', $1)", "p" * 9000)
        await pool.execute(
            "INSERT INTO library_installs (id, item_id, scope, project_id, roles) "
            "VALUES ('inst-plugin', 'plugin-big', 'project', $1, ARRAY['planner', 'worker', 'reviewer'])",
            project["id"])
        description = "d" * (8000 - len("deploy-check"))
        await pool.execute("UPDATE library_items SET description = $2 WHERE id = $1", skill_id, description)
        exact = await context_budget(project["id"])
        assert exact["limit"] == 8000 and exact["counts_plugin_skills"] is False
        assert exact["roles"] == {
            role: {"chars": 8000, "skills": 1, "over": False} for role in ("planner", "worker", "reviewer")}
        await pool.execute(
            "UPDATE library_items SET description = $2 WHERE id = $1", skill_id, description + "x")
        over = await context_budget(project["id"])
        assert over["roles"]["worker"] == {"chars": 8001, "skills": 1, "over": True}
        from looper.agents.skill_lifecycle import deprecate

        await deprecate(project["id"], skill_id)
        empty = await context_budget(project["id"])
        assert empty["roles"]["planner"] == {"chars": 0, "skills": 0, "over": False}
        with pytest.raises(ProjectSkillNotFound):
            await context_budget("project-missing")

    run_db(_test, monkeypatch)


def test_deprecate_and_restore_are_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import project_skills
    from looper.agents.project_skills import ProjectSkillError
    from looper.agents.skill_lifecycle import deprecate, restore
    from looper.db import projects

    async def _stamp(pool: Any, skill_id: str) -> tuple[Any, ...]:
        row = await pool.fetchrow(
            "SELECT sha256, lifecycle, updated_at FROM library_items WHERE id = $1", skill_id)
        versions = await pool.fetchval("SELECT count(*) FROM skill_versions WHERE item_id = $1", skill_id)
        installs = await pool.fetch("SELECT id, roles, enabled FROM library_installs WHERE item_id = $1 ORDER BY id",
                                    skill_id)
        return row["sha256"], row["lifecycle"], row["updated_at"], versions, installs

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        other = await projects.create_project(name="docs", pool=pool)
        added = await project_skills.add_skill(project["id"], _skill("deploy-check"))
        skill_id = added["skill"]["skill_id"]
        from looper.agents import catalog
        await catalog.install({"item_id": skill_id, "scope": "project", "project_id": other["id"], "roles": ["worker"]})
        before = await _stamp(pool, skill_id)
        with pytest.raises(ProjectSkillError, match="reason must be a string"):
            await deprecate(project["id"], skill_id, reason=None)  # type: ignore[arg-type]
        with pytest.raises(ProjectSkillError, match="at most 500"):
            await deprecate(project["id"], skill_id, reason="x" * 501)
        assert await _stamp(pool, skill_id) == before

        first = await deprecate(project["id"], skill_id, reason="  too broad  ")
        assert first["skill"]["lifecycle"] == "deprecated"
        assert first["affected_projects"] == [
            {"project_id": project["id"], "project_name": "calc"},
            {"project_id": other["id"], "project_name": "docs"},
        ]
        assert "too broad" not in json.dumps(first, default=str)
        stamped = await _stamp(pool, skill_id)
        assert stamped[0] == before[0] and stamped[1] == "deprecated" and stamped[2] != before[2]
        assert stamped[3] == before[3] == 1 and stamped[4] == before[4]
        assert await pool.fetchval(
            "SELECT count(*) FROM skill_versions WHERE item_id = $1 AND reason <> ''", skill_id) == 0
        second = await deprecate(project["id"], skill_id, reason="again")
        assert second["skill"] == first["skill"] and second["affected_projects"] == first["affected_projects"]
        assert await _stamp(pool, skill_id) == stamped

        restored = await restore(project["id"], skill_id)
        assert restored["skill"]["lifecycle"] == "active"
        assert restored["affected_projects"] == first["affected_projects"]
        live = await _stamp(pool, skill_id)
        assert live[1] == "active" and live[2] != stamped[2] and live[3] == 1
        again = await restore(project["id"], skill_id)
        assert again["skill"] == restored["skill"] and again["affected_projects"] == restored["affected_projects"]
        assert await _stamp(pool, skill_id) == live

    run_db(_test, monkeypatch)


def test_update_and_rollback_keep_a_deprecated_skill_deprecated(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import project_skills, skill_versions
    from looper.agents.skill_lifecycle import deprecate
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        added = await project_skills.add_skill(project["id"], _skill("deploy-check", content="one\n"))
        skill_id = added["skill"]["skill_id"]
        await deprecate(project["id"], skill_id, reason="pause")
        updated = await project_skills.update_skill(project["id"], skill_id, {"content": "two\n"})
        assert updated["skill"]["lifecycle"] == "deprecated"
        row = await pool.fetchrow("SELECT skill_version, lifecycle FROM library_items WHERE id = $1", skill_id)
        assert (row["skill_version"], row["lifecycle"]) == (2, "deprecated")
        rolled = await skill_versions.rollback(project["id"], skill_id, 1)
        assert rolled["version"] == 3 and rolled["skill"]["lifecycle"] == "deprecated"
        assert await pool.fetchval("SELECT lifecycle FROM library_items WHERE id = $1", skill_id) == "deprecated"
        assert await pool.fetchval("SELECT count(*) FROM skill_versions WHERE item_id = $1", skill_id) == 3

    run_db(_test, monkeypatch)


def test_delete_of_a_deprecated_skill_leaves_a_tombstone(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import project_skills
    from looper.agents.skill_lifecycle import deprecate
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        added = await project_skills.add_skill(project["id"], _skill("deploy-check"))
        skill_id = added["skill"]["skill_id"]
        await deprecate(project["id"], skill_id)
        await project_skills.delete_skill(project["id"], skill_id)
        assert await pool.fetchval("SELECT count(*) FROM library_items WHERE id = $1", skill_id) == 0
        assert await pool.fetchval("SELECT count(*) FROM library_files WHERE item_id = $1", skill_id) == 0
        assert await pool.fetchval("SELECT count(*) FROM library_installs WHERE item_id = $1", skill_id) == 0
        rows = await pool.fetch("SELECT tombstone_at FROM skill_versions WHERE item_id = $1", skill_id)
        assert rows and all(row["tombstone_at"] is not None for row in rows)

    run_db(_test, monkeypatch)


def test_a_role_that_names_a_deprecated_skill_skips_it_and_still_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import project_skills
    from looper.agents.skill_lifecycle import deprecate, restore
    from looper.agents.library import load_installed
    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        await _quiet(pool, project["id"])
        kept = await project_skills.add_skill(project["id"], _skill("kept"))
        old = await project_skills.add_skill(project["id"], _skill("old"))
        old_id = old["skill"]["skill_id"]
        await pool.execute("UPDATE library_installs SET roles = ARRAY['task_role'] WHERE item_id = $1", old_id)
        await deprecate(project["id"], old_id)
        work = tmp_path / "run"
        await tasks.create_task(
            task_id="task-r", requirement="do it", project_id=project["id"], work_dir=str(work),
            status="running", pool=pool)
        seen = await _drive(work, {
            "task_id": "step-s2", "parent_task_id": "task-r", "step_key": "s2", "instruction": "do it",
            "task_role": {
                "id": "fe_rd", "kind": "work", "version": 1, "role_prompt": "You build the UI.",
                "harness": None, "profile_id": None, "items": [old_id],
            },
        })
        assert _HEADING in seen["prompt"]
        lines = [row["summary"] for row in await pool.fetch(
            "SELECT summary FROM activity WHERE task_id = 'task-r' AND summary LIKE 'Deprecated skills skipped%'")]
        assert lines == ["Deprecated skills skipped (old)"]
        library = _library(await pool.fetchval(
            "SELECT metadata FROM llm_records WHERE task_id = 'task-r' AND agent_id = 'worker-2'"))
        assert [item["name"] for item in library["items"]] == ["kept"]
        skipped = await load_installed(tmp_path / "after", "worker", project["id"], (old_id,))
        assert [item.name for item in skipped.items] == ["kept"] and skipped.skipped_deprecated == ("old",)
        await restore(project["id"], old_id)
        loaded = await load_installed(tmp_path / "restored", "worker", project["id"], (old_id,))
        assert sorted(item.name for item in loaded.items) == ["kept", "old"]
        assert loaded.skipped_deprecated == ()

    run_db(_test, monkeypatch, timeout=120)
