"""Project-skill versions against PostgreSQL: append, rollback, tombstones, and a run that does not see them.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _skill(name: str, **extra: Any) -> dict[str, Any]:
    content = extra.pop("content", f"Reply {name}.\n")
    return {"name": name, "description": f"Use for {name}.", "content": content, **extra}


def _result(status: int, body: dict[str, Any]) -> dict[str, Any]:
    assert status == 200 and body["status"] == "ok", body
    return body["result"]


def _matches(operation_id: str, result: dict[str, Any]) -> None:
    from looper.agents.api_v1 import validate_arguments
    from looper.agents.v1_domains.project_skills import OPERATIONS

    operation = next(op for op in OPERATIONS if op.operation_id == operation_id)
    validate_arguments(
        {"type": "object", "properties": {"result": operation.result}, "required": ["result"]},
        {"result": result},
    )


def test_a_new_project_skill_is_version_one_and_other_origins_are_not(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import catalog_sources, project_skills
    from looper.agents.project_skills import skill_body, skill_file, skill_md
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        added = await project_skills.add_skill(project["id"], _skill("deploy-check"))
        skill_id = added["skill"]["skill_id"]
        version = await pool.fetchrow("SELECT * FROM skill_versions WHERE item_id = $1", skill_id)
        body = skill_body(skill_file("deploy-check", "Use for deploy-check.", "Reply deploy-check.\n"),
                          "deploy-check", "Use for deploy-check.")
        _, sha = skill_md("deploy-check", "Use for deploy-check.", "Reply deploy-check.\n")
        assert version["version"] == 1 and version["author_kind"] == "operator" and version["author_ref"] is None
        assert version["reason"] == "" and version["content"] == body and not version["content"].startswith("---")
        item = await pool.fetchrow("SELECT sha256, skill_version FROM library_items WHERE id = $1", skill_id)
        assert item["skill_version"] == 1 and item["sha256"] == version["sha256"] == sha

        await catalog_sources.create_skill(
            {"name": "settings-skill", "description": "Use for settings-skill.", "content": "Do the thing.\n"})
        assert await pool.fetchval("SELECT count(*) FROM skill_versions") == 1
        settings = await pool.fetchrow(
            "SELECT skill_version FROM library_items WHERE origin = 'ui' AND name = 'settings-skill'")
        assert settings["skill_version"] is None

    run_db(_test, monkeypatch)


def test_update_moves_the_pointer_in_one_transaction_and_a_failure_rolls_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import project_skills
    from looper.agents.project_skills import skill_md
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        added = await project_skills.add_skill(project["id"], _skill("deploy-check", content="one\n"))
        skill_id = added["skill"]["skill_id"]
        await project_skills.update_skill(project["id"], skill_id, {"content": "two\n"})
        _, sha = skill_md("deploy-check", "Use for deploy-check.", "two\n")
        item = await pool.fetchrow("SELECT sha256, skill_version FROM library_items WHERE id = $1", skill_id)
        assert item["skill_version"] == 2 and item["sha256"] == sha
        assert await pool.fetchval("SELECT sha256 FROM skill_versions WHERE item_id = $1 AND version = 2", skill_id) == sha

        async def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("stopped")

        monkeypatch.setattr("looper.db.library.update_owned_skill", _boom)
        with pytest.raises(RuntimeError, match="stopped"):
            await project_skills.update_skill(project["id"], skill_id, {"content": "three\n"})
        after = await pool.fetchrow("SELECT sha256, skill_version FROM library_items WHERE id = $1", skill_id)
        assert dict(after) == dict(item)
        assert await pool.fetchval("SELECT count(*) FROM skill_versions WHERE item_id = $1", skill_id) == 2

    run_db(_test, monkeypatch)


def test_rollback_appends_the_old_content_under_a_new_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.library import load_installed
    from looper.db import library as db_library
    from looper.db import projects

    async def _test(pool: Any) -> None:
        zeta = await projects.create_project(name="zeta", pool=pool)
        alpha = await projects.create_project(name="alpha", pool=pool)
        async with v1_api(tmp_path, pool) as call:
            added = _result(*await call("project_skills.add", {"project_id": zeta["id"], "skill": _skill("notes", content="one\n")}))
            skill_id = added["skill"]["skill_id"]
            assert (await call("project_skills.update", {
                "project_id": zeta["id"], "skill_id": skill_id, "fields": {"content": "two\n"}}))[0] == 200
            async with pool.acquire() as conn:
                await db_library.insert_install(
                    conn, item_id=skill_id, scope="project", project_id=alpha["id"], roles=["worker"], enabled=True)
            old = await pool.fetchrow(
                "SELECT content, sha256 FROM skill_versions WHERE item_id = $1 AND version = 1", skill_id)
            result = _result(*await call("project_skills.rollback", {
                "project_id": zeta["id"], "skill_id": skill_id, "version": 1}))
        _matches("project_skills.rollback", result)
        assert result["version"] == 3
        assert [project["project_name"] for project in result["affected_projects"]] == ["alpha", "zeta"]
        new = await pool.fetchrow(
            "SELECT content, sha256, author_kind, author_ref, reason FROM skill_versions "
            "WHERE item_id = $1 AND version = 3", skill_id)
        assert new["author_kind"] == "rollback" and new["author_ref"] == "1" and new["reason"] == ""
        assert new["content"] == old["content"] and new["sha256"] == old["sha256"]
        item = await pool.fetchrow("SELECT sha256, skill_version FROM library_items WHERE id = $1", skill_id)
        assert item["sha256"] == old["sha256"] and item["skill_version"] == 3
        loaded = await load_installed(tmp_path / "runtime", "worker", zeta["id"])
        assert loaded.items[0].sha256 == old["sha256"]
        assert "skill_version" not in json.dumps(loaded.record())

    run_db(_test, monkeypatch)


def test_rollback_refuses_the_current_version_a_missing_one_a_hash_mismatch_and_a_taken_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        async with v1_api(tmp_path, pool) as call:
            added = _result(*await call("project_skills.add", {
                "project_id": project["id"], "skill": _skill("alpha", content="one\n")}))
            skill_id = added["skill"]["skill_id"]
            assert (await call("project_skills.update", {
                "project_id": project["id"], "skill_id": skill_id, "fields": {"name": "beta"}}))[0] == 200

            async def _refuse(version: int, status_code: int, message: str) -> None:
                before = await pool.fetchrow(
                    "SELECT name, sha256, skill_version FROM library_items WHERE id = $1", skill_id)
                count = await pool.fetchval("SELECT count(*) FROM skill_versions WHERE item_id = $1", skill_id)
                status, body = await call("project_skills.rollback", {
                    "project_id": project["id"], "skill_id": skill_id, "version": version})
                assert status == status_code and body["error"]["message"] == message, body
                after = await pool.fetchrow(
                    "SELECT name, sha256, skill_version FROM library_items WHERE id = $1", skill_id)
                assert dict(after) == dict(before)
                assert await pool.fetchval("SELECT count(*) FROM skill_versions WHERE item_id = $1", skill_id) == count

            await _refuse(2, 400, "that version is already current")
            await _refuse(9, 400, "no version 9")
            await pool.execute(
                "UPDATE skill_versions SET sha256 = 'not-the-hash' WHERE item_id = $1 AND version = 1", skill_id)
            await _refuse(1, 400, "the stored version does not match its content hash")
            assert (await call("project_skills.add", {"project_id": project["id"], "skill": _skill("alpha")}))[0] == 200
            status, body = await call("project_skills.rollback", {
                "project_id": project["id"], "skill_id": skill_id, "version": 1})
            assert status == 409, body
            assert await pool.fetchval("SELECT name FROM library_items WHERE id = $1", skill_id) == "beta"
            assert await pool.fetchval("SELECT count(*) FROM skill_versions WHERE item_id = $1", skill_id) == 2

    run_db(_test, monkeypatch)


def test_versions_returns_history_and_a_diff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        async with v1_api(tmp_path, pool) as call:
            added = _result(*await call("project_skills.add", {
                "project_id": project["id"], "skill": _skill("notes", content="one\n")}))
            skill_id = added["skill"]["skill_id"]
            assert (await call("project_skills.update", {
                "project_id": project["id"], "skill_id": skill_id, "fields": {"content": "two\n"}}))[0] == 200
            listed = _result(*await call("project_skills.versions", {
                "project_id": project["id"], "skill_id": skill_id}))
            _matches("project_skills.versions", listed)
            assert listed["current_version"] == 2
            assert [version["version"] for version in listed["versions"]] == [1, 2]
            assert all("content" not in version for version in listed["versions"])
            assert listed["diff"] is None
            diff = _result(*await call("project_skills.versions", {
                "project_id": project["id"], "skill_id": skill_id, "from_version": 1, "to_version": 2}))
            assert "-one\n" in diff["diff"]["content"] and "+two\n" in diff["diff"]["content"]
            status, body = await call("project_skills.versions", {
                "project_id": project["id"], "skill_id": skill_id, "from_version": 1})
            assert status == 400 and body["error"]["message"] == "from_version and to_version are given together"
            status, body = await call("project_skills.versions", {
                "project_id": project["id"], "skill_id": "skill-missing"})
            assert status == 404, body

    run_db(_test, monkeypatch)


def test_a_client_cannot_read_versions_or_roll_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.v1_domains.project_skills import OPERATIONS
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        async with v1_api(tmp_path, pool) as call:
            added = _result(*await call("project_skills.add", {
                "project_id": project["id"], "skill": _skill("notes", content="one\n")}))
            skill_id = added["skill"]["skill_id"]
            assert (await call("project_skills.update", {
                "project_id": project["id"], "skill_id": skill_id, "fields": {"content": "two\n"}}))[0] == 200
            body = {"project_id": project["id"], "skill_id": skill_id}
            for action, arguments in (
                ("project_skills.versions", body),
                ("project_skills.rollback", {**body, "version": 1}),
            ):
                status, answer = await call(action, arguments, client=True)
                assert status == 403 and answer["error"] == {
                    "code": "forbidden", "message": "operator identity is required"}, answer
            assert await pool.fetchval("SELECT skill_version FROM library_items WHERE id = $1", skill_id) == 2
            assert await pool.fetchval("SELECT count(*) FROM skill_versions WHERE item_id = $1", skill_id) == 2
            listed = _result(*await call("project_skills.versions", body))
            assert listed["current_version"] == 2
            rolled = _result(*await call("project_skills.rollback", {**body, "version": 1}))
            assert rolled["version"] == 3
        fresh = {op.operation_id: op for op in OPERATIONS}
        assert fresh["project_skills.versions"].operator_only and fresh["project_skills.rollback"].operator_only

    run_db(_test, monkeypatch)


def test_delete_leaves_a_tombstone_and_project_delete_does_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import project_skills
    from looper.db import projects

    async def _gone(pool: Any, skill_id: str) -> None:
        assert await pool.fetchval("SELECT count(*) FROM library_items WHERE id = $1", skill_id) == 0
        assert await pool.fetchval("SELECT count(*) FROM library_files WHERE item_id = $1", skill_id) == 0
        assert await pool.fetchval("SELECT count(*) FROM library_installs WHERE item_id = $1", skill_id) == 0
        rows = await pool.fetch("SELECT tombstone_at FROM skill_versions WHERE item_id = $1", skill_id)
        assert rows and all(row["tombstone_at"] is not None for row in rows)

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        added = await project_skills.add_skill(project["id"], _skill("deploy-check"))
        skill_id = added["skill"]["skill_id"]
        await project_skills.delete_skill(project["id"], skill_id)
        await _gone(pool, skill_id)

        doomed = await projects.create_project(name="doomed", pool=pool)
        owned = await project_skills.add_skill(doomed["id"], _skill("notes"))
        assert await projects.delete_project(doomed["id"], pool=pool)
        await _gone(pool, owned["skill"]["skill_id"])

    run_db(_test, monkeypatch)


def test_tombstones_are_purged_ninety_days_after_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.skill_versions import purge_tombstones

    async def _test(pool: Any) -> None:
        now = datetime(2030, 6, 1, tzinfo=timezone.utc)
        ancient = now - timedelta(days=200)

        async def _row(item_id: str, tombstone: datetime | None) -> None:
            await pool.execute(
                "INSERT INTO skill_versions (item_id, version, name, description, content, sha256, author_kind, "
                "reason, created_at, tombstone_at) VALUES ($1, 1, 'n', 'd', 'body\n', 'sha', 'operator', '', $2, $3)",
                item_id, ancient, tombstone)

        await _row("old", now - timedelta(days=91))
        await _row("recent", now - timedelta(days=1))
        await _row("boundary", now - timedelta(days=90))
        await _row("living", None)
        async with pool.acquire() as conn, conn.transaction():
            removed = await purge_tombstones(conn, now=now)
        assert removed == 1
        left = {row["item_id"] for row in await pool.fetch("SELECT item_id FROM skill_versions")}
        assert left == {"recent", "boundary", "living"}

    run_db(_test, monkeypatch)


def test_migration_backfill_keeps_sha256_and_skips_other_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.project_skills import skill_body, skill_file, skill_md
    from looper.db import projects
    from looper.db.connection import SCHEMA_PATH
    from looper.db.migrations import skill_versions as migration

    async def _test(pool: Any) -> None:
        assert "CREATE TABLE IF NOT EXISTS skill_versions" not in SCHEMA_PATH.read_text(encoding="utf-8")
        project = await projects.create_project(name="calc", pool=pool)
        text = skill_file("legacy", "Use for legacy.", "Keep this.\n")
        machine = skill_file("machine-skill", "Use for machine-skill.", "Machine.\n")
        await pool.execute(
            "INSERT INTO library_items (id, kind, name, sha256, size_bytes, file_count, origin, owner_project_id, "
            "description) VALUES ('skill-legacy', 'skill', 'legacy', 'unchanged-sha', $1, 1, 'project', $2, "
            "'Use for legacy.')", len(text.encode()), project["id"])
        await pool.execute(
            "INSERT INTO library_files (item_id, path, content, sha256, executable) "
            "VALUES ('skill-legacy', 'SKILL.md', $1, 'file-sha', false)", text)
        await pool.execute(
            "INSERT INTO library_items (id, kind, name, sha256, size_bytes, file_count, origin, description) "
            "VALUES ('skill-machine', 'skill', 'machine-skill', 'machine-sha', $1, 1, 'machine', "
            "'Use for machine-skill.')", len(machine.encode()))
        await pool.execute(
            "INSERT INTO library_files (item_id, path, content, sha256, executable) "
            "VALUES ('skill-machine', 'SKILL.md', $1, 'machine-file', false)", machine)
        await pool.execute(
            "INSERT INTO library_items (id, kind, name, sha256, size_bytes, file_count, origin, owner_project_id, "
            "description) VALUES ('skill-bare', 'skill', 'bare', 'bare-sha', 0, 0, 'project', $1, 'Use for bare.')",
            project["id"])

        async with pool.acquire() as conn:
            assert not await migration.pending(conn)
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'skill_versions_entomb' AND NOT tgisinternal)")
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'skill_versions'")
        await pool.execute("DROP TRIGGER IF EXISTS skill_versions_entomb ON library_items")
        await pool.execute("DROP FUNCTION IF EXISTS skill_versions_entomb()")
        await pool.execute("DROP TABLE IF EXISTS skill_versions")
        await pool.execute("ALTER TABLE library_items DROP COLUMN IF EXISTS skill_version")

        async with pool.acquire() as conn, conn.transaction():
            assert await migration.pending(conn)
            await migration.apply(conn, sql="", state="current")
            assert not await migration.pending(conn)

        legacy = await pool.fetchrow(
            "SELECT name, sha256, size_bytes, skill_version FROM library_items WHERE id = 'skill-legacy'")
        assert (legacy["name"], legacy["sha256"], legacy["size_bytes"], legacy["skill_version"]) == (
            "legacy", "unchanged-sha", len(text.encode()), 1)
        version = await pool.fetchrow(
            "SELECT version, sha256, author_kind, author_ref, reason, content FROM skill_versions "
            "WHERE item_id = 'skill-legacy'")
        _, real = skill_md("legacy", "Use for legacy.", "Keep this.\n")
        assert real != "unchanged-sha"
        assert version["version"] == 1 and version["sha256"] == "unchanged-sha"
        assert version["author_kind"] == "operator" and version["author_ref"] is None and version["reason"] == "imported"
        assert version["content"] == skill_body(text, "legacy", "Use for legacy.")
        assert not version["content"].startswith("---")
        assert await pool.fetchval("SELECT skill_version FROM library_items WHERE id = 'skill-machine'") is None
        assert await pool.fetchval("SELECT count(*) FROM skill_versions WHERE item_id = 'skill-machine'") == 0
        assert await pool.fetchval("SELECT skill_version FROM library_items WHERE id = 'skill-bare'") is None
        assert await pool.fetchval("SELECT count(*) FROM skill_versions WHERE item_id = 'skill-bare'") == 0
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'skill_versions_entomb' AND NOT tgisinternal)")
        count = await pool.fetchval("SELECT count(*) FROM skill_versions")
        async with pool.acquire() as conn, conn.transaction():
            await migration.apply(conn, sql="", state="current")
        assert await pool.fetchval("SELECT count(*) FROM skill_versions") == count

    run_db(_test, monkeypatch)


# The whole default run for this skill on origin/main: library record, launch arguments, and the
# files under ``.local/library``. The project id and the runtime root are the only parts that vary.
_MAIN_DEFAULT_PATH = (
    '{"args":["--plugin-dir","<runtime>/.local/library/578d74b686aa9709b39efa146d180db0e5c45f914df31d5896b763761416f73d/project"],'
    '"files":{"578d74b686aa9709b39efa146d180db0e5c45f914df31d5896b763761416f73d/project/.claude-plugin/plugin.json":'
    '"{\\n  \\"name\\": \\"project\\",\\n  \\"description\\": \\"This project\'s own skills, managed by Looper.\\"\\n}\\n",'
    '"578d74b686aa9709b39efa146d180db0e5c45f914df31d5896b763761416f73d/project/skills/deploy-check":'
    '"link:../../../9233045e8b40d7c828c578b7d1f49dd0a9aa1dd19b36ae7881c4e9da13e1fbb0/deploy-check",'
    '"9233045e8b40d7c828c578b7d1f49dd0a9aa1dd19b36ae7881c4e9da13e1fbb0/deploy-check/SKILL.md":'
    '"---\\nname: deploy-check\\ndescription: \\"Use for deploy-check.\\"\\n---\\n\\nReply deploy-check.\\n"},'
    '"record":{"items":[{"kind":"skill","name":"deploy-check","plugin":"project","scope":"project",'
    '"sha256":"9233045e8b40d7c828c578b7d1f49dd0a9aa1dd19b36ae7881c4e9da13e1fbb0"}],'
    '"project_id":"<project>","role":"worker",'
    '"sha256":"2307cfe1d5cb6b95e254fcdda04b3fcee50ad3f5f721330dd1fecb88cf7449e9"}}'
)


def _default_path_bytes(record: dict[str, Any], args: list[str], runtime: Path, project_id: str) -> bytes:
    root = runtime.absolute() / ".local" / "library"
    files: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                files[rel] = "link:" + os.readlink(path)
            elif path.is_file():
                files[rel] = path.read_text(encoding="utf-8")
    prefix = str(runtime.absolute())
    shown = json.loads(json.dumps(record).replace(project_id, "<project>"))
    shown_args = [part.replace(prefix, "<runtime>") for part in args]
    return json.dumps(
        {"record": shown, "args": shown_args, "files": files},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()


def test_a_run_record_and_launch_args_omit_the_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import project_skills
    from looper.agents.claude_cli import library_args
    from looper.agents.library import item_directory, load_installed
    from looper.agents.project_skills import skill_file, skill_md
    from looper.agents.v1_domains.project_skills import OPERATIONS
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        added = await project_skills.add_skill(project["id"], _skill("deploy-check"))
        assert "skill_version" not in json.dumps(added, default=str)
        skill_id = added["skill"]["skill_id"]
        _, sha = skill_md("deploy-check", "Use for deploy-check.", "Reply deploy-check.\n")
        item = await pool.fetchrow("SELECT sha256, skill_version FROM library_items WHERE id = $1", skill_id)
        assert item["sha256"] == sha and item["skill_version"] == 1

        loaded = await load_installed(tmp_path / "runtime", "worker", project["id"])
        record = loaded.record()
        assert record is not None and set(record) == {"sha256", "role", "project_id", "items"}
        assert "skill_version" not in json.dumps(record)
        view = record["items"][0]
        assert set(view) == {"kind", "name", "sha256", "plugin", "scope"}
        assert view["plugin"] == "project" and view["scope"] == "project" and view["sha256"] == sha
        args = library_args(loaded)
        assert len(args) == 2 and args[0] == "--plugin-dir" and Path(args[1]).name == "project"
        materialized = item_directory(tmp_path / "runtime", loaded.items[0]) / "SKILL.md"
        assert materialized.read_text(encoding="utf-8") == skill_file(
            "deploy-check", "Use for deploy-check.", "Reply deploy-check.\n")
        assert _default_path_bytes(record, args, tmp_path / "runtime", project["id"]) == _MAIN_DEFAULT_PATH.encode()
        fresh = {op.operation_id: op for op in OPERATIONS}
        assert fresh["project_skills.versions"].tool is None and fresh["project_skills.rollback"].tool is None

    run_db(_test, monkeypatch)
