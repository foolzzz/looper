"""Project knowledge directories stay confined without changing agent-visible projects."""

import pytest

from looper.agents import knowledge_directory, projects as core, vault
from tests.db_support import run_db, v1_api
from looper.db import projects


@pytest.mark.parametrize("value", ["../notes", "notes/../other", "/notes", ".notes", "notes/.private", "", "notes//other", "C:/notes"])
def test_directory_rejects_unsafe_relative_paths(tmp_path, value):
    with pytest.raises(ValueError, match="knowledge_dir"):
        knowledge_directory.validate(vault.VaultConfig(tmp_path, (), None), value)


@pytest.mark.parametrize("escape", [False, True])
def test_directory_rejects_all_symlink_components(tmp_path, escape):
    root = tmp_path / "vault"
    root.mkdir()
    target = tmp_path / "outside" if escape else root / "real"
    target.mkdir()
    (root / "linked").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        knowledge_directory.validate(vault.VaultConfig(root, (), None), "linked/notes")


def test_directory_accepts_missing_real_directory_and_null(tmp_path):
    config = vault.VaultConfig(tmp_path, (), None)
    assert knowledge_directory.validate(config, "projects/example") == "projects/example"
    assert knowledge_directory.validate(config, None) is None


@pytest.mark.parametrize("value", ["file", "file/notes"])
def test_directory_rejects_file_components(tmp_path, value):
    (tmp_path / "file").write_text("Original")
    with pytest.raises(ValueError, match="directories"):
        knowledge_directory.validate(vault.VaultConfig(tmp_path, (), None), value)


def test_project_admin_reads_writes_clears_and_returns_400(tmp_path, monkeypatch):
    config = vault.VaultConfig(tmp_path, (), None)
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)

    async def load():
        return config, None

    monkeypatch.setattr(vault, "load", load)

    async def run(pool):
        project = await projects.create_project(name="Example")
        assert project["knowledge_dir"] is None
        async with v1_api(tmp_path, pool) as call:
            status, result = await call("project_admin.update", {"project_id": project["id"], "fields": {"knowledge_dir": "projects/example"}})
            assert status == 200, result
            assert result["result"]["project"]["knowledge_dir"] == "projects/example"
            stored = await projects.get_project(project["id"])
            assert stored["knowledge_dir"] == "projects/example"
            assert "knowledge_dir" not in core.project_view(stored)
            from looper.agents.v1_domains.projects import COMPACT_PROJECT
            assert "knowledge_dir" not in COMPACT_PROJECT["properties"]
            status, result = await call("project_admin.list")
            assert status == 200 and result["result"]["projects"][0]["knowledge_dir"] == "projects/example"
            for value in ("../notes", "/notes", ".private", "linked/notes"):
                status, result = await call("project_admin.update", {"project_id": project["id"], "fields": {"knowledge_dir": value}})
                assert status == 400 and "knowledge_dir" in result["error"]["message"], result
            status, result = await call("project_admin.update", {"project_id": project["id"], "fields": {"knowledge_dir": None}})
            assert status == 200 and result["result"]["project"].get("knowledge_dir") is None, result
            status, result = await call("project_admin.create", {"name": "Created", "knowledge_dir": "projects/created"})
            assert status == 200 and result["result"]["project"]["knowledge_dir"] == "projects/created", result
    run_db(run, monkeypatch)


def test_directory_migration_is_ordered_and_adds_missing_column(monkeypatch):
    from looper.db import migrations
    from looper.db.migrations import project_knowledge_dir as migration

    ordered = migrations.discover()
    assert migration.order == migration.pending_order == 1970
    assert sum(item.order == migration.order for item in ordered) == 1
    assert ordered.index(migration) > next(i for i, item in enumerate(ordered) if item.name == "memory_lessons")

    async def run(pool):
        async with pool.acquire() as conn:
            assert not await migration.pending(conn)
            await conn.execute("ALTER TABLE projects DROP COLUMN knowledge_dir")
            await conn.execute("DELETE FROM schema_migrations WHERE name=$1", migration.name)
            assert await migration.pending(conn)
            await migration.apply(conn, sql="", state={})
            assert not await migration.pending(conn)
            await migration.apply(conn, sql="", state={})
            project = await projects.create_project(name="Migrated", pool=conn)
            assert project["knowledge_dir"] is None
    run_db(run, monkeypatch)


def test_setting_requires_configured_vault_but_clearing_does_not(tmp_path, monkeypatch):
    async def load():
        return None, None
    monkeypatch.setattr(vault, "load", load)

    async def run(pool):
        project = await projects.create_project(name="Example", knowledge_dir="notes")
        async with v1_api(tmp_path, pool) as call:
            status, result = await call("project_admin.update", {"project_id": project["id"], "fields": {"knowledge_dir": "notes"}})
            assert status == 400, result
            status, result = await call("project_admin.update", {"project_id": project["id"], "fields": {"knowledge_dir": "other"}})
            assert status == 400 and "vault" in result["error"]["message"], result
            status, result = await call("project_admin.update", {"project_id": project["id"], "fields": {"knowledge_dir": None}})
            assert status == 200 and result["result"]["project"].get("knowledge_dir") is None, result
    run_db(run, monkeypatch)
