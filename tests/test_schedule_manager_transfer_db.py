"""Portable schedule owners retain immutable Manager identities on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import config_transfer as transfer
from looper.db import connection
from looper.db.connection import ensure_schema
from tests.db_support import DSN, fresh_schema, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _document(*, owner: str = "manager-review", **schedule: Any) -> dict[str, Any]:
    return {"format": transfer.V4_FORMAT, "managers": [{"id": owner, "name": owner,
            "system_prompt": "Handle synthetic review tasks.", "enabled": False}], "projects": [{
                "name": "Example", "schedules": [{"name": "Nightly", "cron": "0 9 * * *",
                    "timezone": "UTC", "requirement": "Review example changes.", "manager": owner, **schedule}]}]}


def test_preview_schedule_owner_resolves_imported_manager_without_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        before = {table: list(await pool.fetch(f"SELECT * FROM {table} ORDER BY 1"))
                  for table in ("managers", "projects", "schedules")}
        async with pool.acquire() as conn:
            plan = await transfer.run_import(conn, _document(), apply=False)
        change = next(change for change in plan.changes if change.section == "project.schedules")
        assert change.action is transfer.Action.ADDED
        for table, rows in before.items():
            assert list(await pool.fetch(f"SELECT * FROM {table} ORDER BY 1")) == rows

    run_db(test, monkeypatch, timeout=20)


def test_schedule_owner_roundtrip_uses_full_identity_and_target_project(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        async with pool.acquire() as conn:
            await transfer.run_import(conn, _document(owner="manager-manager"), apply=True)
        source = await pool.fetchrow("SELECT id, project_id, manager_id, enabled FROM schedules")
        assert (source["manager_id"], source["enabled"]) == ("manager-manager", False)
        async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            document = json.loads("".join([chunk async for chunk in transfer.export_chunks(
                conn, include_projects=True, include_secrets=False)]))
        assert document["format"] == transfer.V4_FORMAT
        assert document["projects"][0]["schedules"][0]["manager"] == "manager-manager"
        async with fresh_schema() as target:
            await ensure_schema(target)
            async with target.acquire() as conn:
                await transfer.run_import(conn, {"format": transfer.V4_FORMAT,
                                                 "projects": [{"name": "Example"}]}, apply=True)
                await transfer.run_import(conn, document, apply=True)
            stored = await target.fetchrow("SELECT project_id, manager_id, enabled FROM schedules")
            assert stored["project_id"] != source["project_id"]
            assert (stored["manager_id"], stored["enabled"]) == ("manager-manager", False)

    run_db(test, monkeypatch, timeout=30)


def test_omitted_schedule_owner_defaults_and_roundtrips(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        document = _document()
        del document["managers"]
        del document["projects"][0]["schedules"][0]["manager"]
        async with pool.acquire() as conn:
            await transfer.run_import(conn, document, apply=True)
        assert await pool.fetchval("SELECT manager_id FROM schedules") == "manager"
        async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            exported = json.loads("".join([chunk async for chunk in transfer.export_chunks(
                conn, include_projects=True, include_secrets=False)]))
        assert exported["format"] == transfer.V4_FORMAT
        assert set(exported["projects"][0]["schedules"][0]) == {
            "name", "cron", "timezone", "requirement", "overlap", "max_parallel_runs", "catch_up",
            "run_timeout_seconds", "run_budget_usd", "gate_command", "gate_timeout_seconds",
            "daily_budget_usd", "notify", "notify_target", "profiles", "task_limits"}

    run_db(test, monkeypatch, timeout=20)


@pytest.mark.parametrize("deleted", [False, True])
def test_explicit_unresolved_owner_is_reported_without_defaulting(
    monkeypatch: pytest.MonkeyPatch, deleted: bool,
) -> None:
    async def test(pool: Any) -> None:
        if deleted:
            await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled, deleted_at) "
                               "VALUES ('manager-review', 'Review', 'Handle examples.', false, now())")
        document = _document()
        del document["managers"]
        async with pool.acquire() as conn:
            plan = await transfer.run_import(conn, document, apply=True)
        change = next(change for change in plan.changes if change.section == "project.schedules")
        assert change.action is transfer.Action.SKIPPED and "conflict" in change.reason
        assert "manager-review" in change.reason
        assert await pool.fetchval("SELECT count(*) FROM schedules") == 0

    run_db(test, monkeypatch, timeout=20)


def test_same_name_different_owner_preserves_planner_session_and_history(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        async with pool.acquire() as conn:
            await transfer.run_import(conn, _document(), apply=True)
        schedule_id = await pool.fetchval("SELECT id FROM schedules")
        await pool.execute("UPDATE schedules SET planner_session_id = 'synthetic-session' WHERE id = $1", schedule_id)
        await pool.execute("INSERT INTO schedule_runs (id, schedule_id, trigger, status, scheduled_for) "
                           "VALUES ('srun-example', $1, 'manual', 'skipped_budget', now())", schedule_id)
        before = await pool.fetchrow("SELECT * FROM schedules WHERE id = $1", schedule_id)
        async with pool.acquire() as conn:
            plan = await transfer.run_import(conn, _document(owner="manager-other"), apply=True)
        change = next(change for change in plan.changes if change.section == "project.schedules")
        assert change.action is transfer.Action.SKIPPED and "immutable" in change.reason
        assert await pool.fetchrow("SELECT * FROM schedules WHERE id = $1", schedule_id) == before
        assert await pool.fetchval("SELECT schedule_id FROM schedule_runs WHERE id = 'srun-example'") == schedule_id

    run_db(test, monkeypatch, timeout=20)


def test_changed_schedule_is_disabled_and_retains_target_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        async with pool.acquire() as conn:
            await transfer.run_import(conn, _document(), apply=True)
        await pool.execute("UPDATE schedules SET enabled = true, next_fire_at = now()")
        document = _document(requirement="Review the updated example changes.")
        del document["managers"]  # Resolve an existing nondeleted target Manager.
        async with pool.acquire() as conn:
            plan = await transfer.run_import(conn, document, apply=True)
        change = next(change for change in plan.changes if change.section == "project.schedules")
        assert change.action is transfer.Action.CHANGED and change.fields == ("requirement",)
        row = await pool.fetchrow("SELECT manager_id, enabled, next_fire_at, requirement FROM schedules")
        assert tuple(row) == ("manager-review", False, None, "Review the updated example changes.")

    run_db(test, monkeypatch, timeout=20)


def test_templates_only_owner_cannot_import_free_form_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        document = _document()
        document["managers"][0]["task_policy"] = "templates_only"
        async with pool.acquire() as conn:
            plan = await transfer.run_import(conn, document, apply=True)
        change = next(change for change in plan.changes if change.section == "project.schedules")
        assert change.action is transfer.Action.SKIPPED and "template" in change.reason
        assert await pool.fetchval("SELECT count(*) FROM schedules") == 0

    run_db(test, monkeypatch, timeout=20)


def test_import_locks_existing_schedule_before_rewriting_its_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        async with pool.acquire() as conn:
            await transfer.run_import(conn, _document(), apply=True)
        # The schema is migrated already; incidental migration DDL locks must not hide row-lock ordering.
        monkeypatch.setattr(connection, "ensure_schema", AsyncMock())
        schedule_id = await pool.fetchval("SELECT id FROM schedules")
        document = _document(requirement="Review updated examples.")
        document["managers"][0]["description"] = "Updated review metadata"
        async with pool.acquire() as firing, pool.acquire() as importing, pool.acquire() as observer:
            pending = None
            try:
                async with firing.transaction():
                    await firing.fetchrow("SELECT id FROM schedules WHERE id = $1 FOR UPDATE", schedule_id)
                    pending = asyncio.create_task(transfer.run_import(importing, document, apply=True))
                    # Wait for the actual row-lock wait, so the test checks ordering rather than CPU timing.
                    async with asyncio.timeout(5):
                        while not await observer.fetchval("SELECT $1 = ANY(pg_blocking_pids($2))",
                                                           firing.get_server_pid(), importing.get_server_pid()):
                            await asyncio.sleep(0.01)
                    # This is firing's next lock. NOWAIT exposes an import holding the owner in reverse order.
                    await firing.fetchrow("SELECT id FROM managers WHERE id = 'manager-review' FOR SHARE NOWAIT")
                plan = await asyncio.wait_for(pending, timeout=10)
                assert next(change for change in plan.changes if change.section == "project.schedules").action \
                    is transfer.Action.CHANGED
                assert await observer.fetchval("SELECT description FROM managers WHERE id = 'manager-review'") \
                    == "Updated review metadata"
            finally:
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    run_db(test, monkeypatch, timeout=25)


def test_changed_import_rechecks_target_owner_policy_after_initial_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        async with pool.acquire() as conn:
            await transfer.run_import(conn, _document(), apply=True)
        # Isolate row-level ownership checks from ensure_schema's transaction-wide DDL locks.
        monkeypatch.setattr(connection, "ensure_schema", AsyncMock())
        before = await pool.fetchrow("SELECT * FROM schedules")
        original = transfer._Importer.managers

        async def change_policy(importer, entries):
            await original(importer, entries)
            await pool.execute("UPDATE managers SET task_policy = 'templates_only' WHERE id = 'manager-review'")

        monkeypatch.setattr(transfer._Importer, "managers", change_policy)
        document = _document(requirement="Updated example review.")
        del document["managers"]
        async with pool.acquire() as conn:
            plan = await transfer.run_import(conn, document, apply=True)
        change = next(change for change in plan.changes if change.section == "project.schedules")
        assert change.action is transfer.Action.SKIPPED and "template" in change.reason
        assert await pool.fetchrow("SELECT * FROM schedules") == before

    run_db(test, monkeypatch, timeout=20)
