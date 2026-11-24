"""The operator-only project switch: default off, shown only when on, migration 1860."""

from __future__ import annotations

import json

import asyncpg
import pytest

from looper.agents import config_transfer
from looper.agents.projects import project_view
from looper.agents.v1_domains.memory_closeout import OPERATIONS
from looper.db import migrations, projects
from looper.db.migrations import memory_closeout as migration
from tests.db_support import DSN, run_db, v1_api
from tests.test_memory_closeout import BASELINE, CANONICAL_AT, _sha, export_entry, off_row

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_operations_are_operator_only_and_default_off(tmp_path, monkeypatch):
    async def run(pool):
        for operation in OPERATIONS:
            assert operation.operator_only and len(operation.summary) <= 120
            assert operation.description.startswith(operation.summary)
        project = await projects.create_project(name="Reports", pool=pool)
        async with v1_api(tmp_path, pool) as call:
            for action in ("get", "update"):
                body = {"project_id": project["id"]}
                if action == "update":
                    body["memory_closeout"] = "on"
                status, answer = await call(f"memory_closeout.{action}", body, client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden", answer
            status, answer = await call("memory_closeout.get", {"project_id": project["id"]})
            assert status == 200 and answer["result"] == {"memory_closeout": "off"}, answer
            assert (await projects.get_project(project["id"], pool=pool))["memory_closeout"] is None
            for mode, stored in (("on", "on"), ("off", None)):
                status, answer = await call("memory_closeout.update",
                                            {"project_id": project["id"], "memory_closeout": mode})
                assert status == 200 and answer["result"] == {"memory_closeout": mode}, answer
                status, answer = await call("memory_closeout.get", {"project_id": project["id"]})
                assert answer["result"] == {"memory_closeout": mode}
                assert (await projects.get_project(project["id"], pool=pool))["memory_closeout"] == stored
            status, answer = await call("memory_closeout.get", {"project_id": "missing"})
            assert status == 404, answer
            status, answer = await call("memory_closeout.update",
                                        {"project_id": "missing", "memory_closeout": "on"})
            assert status == 404, answer

    run_db(run, monkeypatch)


def test_project_view_and_export_show_it_only_when_on(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Off", description="", work_dir="", background="", pool=pool)
        live = await projects.get_project(project["id"], pool=pool)
        assert "memory_closeout" not in project_view(live)
        canonical = off_row()
        row = {**live, "id": canonical["id"], "created_at": CANONICAL_AT, "updated_at": CANONICAL_AT}
        assert "memory_closeout" not in project_view(row)
        assert _sha(project_view(row)) == BASELINE["sha256"]["project_view_off"]
        assert _sha(export_entry(row)) == BASELINE["sha256"]["export_entry_off"]
        async with pool.acquire() as conn:
            exported = json.loads("".join([chunk async for chunk in config_transfer.export_chunks(
                conn, include_projects=True, include_secrets=False)]))
        assert "memory_closeout" not in exported["projects"][0]["execution"]

        await projects.set_memory_closeout(project["id"], "on", pool=pool)
        shown = project_view(await projects.get_project(project["id"], pool=pool))
        assert shown["memory_closeout"] == "on"
        async with pool.acquire() as conn:
            exported_on = json.loads("".join([chunk async for chunk in config_transfer.export_chunks(
                conn, include_projects=True, include_secrets=False)]))
        assert exported_on["projects"][0]["execution"]["memory_closeout"] == "on"

        async with pool.acquire() as conn, conn.transaction():
            kept = await config_transfer.import_config(conn, {"format": config_transfer.FORMAT, "projects": [
                {"name": project["name"]}]}, apply=True)
            assert not any(change.action is config_transfer.Action.SKIPPED and change.section == "projects"
                           for change in kept.changes)
        assert (await projects.get_project(project["id"], pool=pool))["memory_closeout"] == "on"
        document = {"format": config_transfer.FORMAT, "projects": [
            {"name": project["name"], "execution": {"memory_closeout": "off"}}]}
        async with pool.acquire() as conn, conn.transaction():
            await config_transfer.import_config(conn, document, apply=True)
        assert (await projects.get_project(project["id"], pool=pool))["memory_closeout"] is None
        document["projects"][0]["execution"]["memory_closeout"] = "on"
        document["projects"][0]["name"] = "Imported on"
        async with pool.acquire() as conn, conn.transaction():
            await config_transfer.import_config(conn, document, apply=True)
        assert await pool.fetchval("SELECT memory_closeout FROM projects WHERE name = 'Imported on'") == "on"
        async with pool.acquire() as conn:
            skipped = await config_transfer.import_config(conn, {"format": config_transfer.FORMAT, "projects": [
                {"name": "Bad value", "execution": {"memory_closeout": "yes"}}]}, apply=False)
        error = next(change for change in skipped.changes if change.section == "projects")
        assert error.action is config_transfer.Action.SKIPPED and "memory_closeout" in error.reason

    run_db(run, monkeypatch)


def test_migration_uses_its_reserved_order_and_check(monkeypatch):
    async def run(pool):
        found = next(item for item in migrations.discover() if item.name == "memory_closeout")
        assert (found.order, found.pending_order) == (1860, 1860)
        assert migration.order == migration.pending_order == 1860
        assert await pool.fetchval("""
            SELECT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'projects' AND column_name = 'memory_closeout')""")
        async with pool.acquire() as conn:
            assert not await migration.pending(conn)
            await migration.apply(conn, sql="", state="")
            assert not await migration.pending(conn)
        project = await projects.create_project(name="Checked", pool=pool)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE projects SET memory_closeout = 'yes' WHERE id = $1", project["id"])

    run_db(run, monkeypatch)
