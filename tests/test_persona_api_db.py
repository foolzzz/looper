"""manager_persona get/update, an unreadable row, and configuration export/import.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from looper.agents import persona
from looper.agents.config_transfer import Action, export_chunks, run_import
from looper.agents.persona import PERSONA_KEY, Persona
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _export(pool: Any) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        text = "".join([chunk async for chunk in export_chunks(
            conn, include_projects=False, include_secrets=False)])
    return json.loads(text)


def test_get_update_round_trip_null_clears_and_empty_deletes_the_row(monkeypatch, tmp_path) -> None:
    async def test(pool: Any) -> None:
        async with v1_api(tmp_path, pool) as call:
            status, body = await call("manager_persona.get")
            assert status == 200 and body["result"] == {"name": "", "address": "", "tone": "", "notes": ""}

            status, body = await call("manager_persona.update", {"name": "管家", "address": "阿星"})
            assert status == 200 and body["result"]["name"] == "管家" and body["result"]["address"] == "阿星"
            persona.set_current(Persona())
            await persona.load()
            assert persona.current() == Persona(name="管家", address="阿星")
            stored = await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", PERSONA_KEY)
            assert stored == '{"address":"阿星","name":"管家"}'

            status, body = await call("manager_persona.update", {"address": None})
            assert status == 200 and body["result"] == {"name": "管家", "address": "", "tone": "", "notes": ""}

            status, body = await call("manager_persona.update", {"name": "   "})
            assert status == 200 and body["result"]["name"] == ""
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", PERSONA_KEY) is None

    run_db(test, monkeypatch, timeout=60)


def test_a_field_over_its_schema_and_a_total_over_600_are_refused(monkeypatch, tmp_path) -> None:
    async def test(pool: Any) -> None:
        async with v1_api(tmp_path, pool) as call:
            status, body = await call("manager_persona.update", {"name": "n" * 21})
            assert status == 400 and body["error"]["message"] == "the request does not match the declared schema"

            status, body = await call("manager_persona.update", {
                "name": "n" * 20, "address": "a" * 20, "tone": "t" * 200, "notes": "x" * 400})
            assert status == 400 and "600" in body["error"]["message"]
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", PERSONA_KEY) is None

            for action in ("manager_persona.get", "manager_persona.update"):
                status, body = await call(action, {"name": "管家"}, client=True)
                assert status == 403 and body["error"]["message"] == "operator identity is required"
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", PERSONA_KEY) is None

    run_db(test, monkeypatch, timeout=60)


def test_an_unreadable_persona_warns_once_and_renders_nothing(monkeypatch, caplog) -> None:
    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2)", PERSONA_KEY, "{")
        with caplog.at_level("WARNING", logger="looper.agents.persona"):
            await persona.load()
            await persona.load()
            assert persona.current() == Persona() and persona.section(persona.current()) == []
            await persona.update({"name": "管家"})
            await pool.execute("UPDATE system_settings SET value = $1 WHERE key = $2", "{", PERSONA_KEY)
            await persona.load()
        warnings = [record for record in caplog.records if record.message == "manager.persona is unreadable"]
        assert len(warnings) == 2
        assert persona.current() == Persona()

    run_db(test, monkeypatch, timeout=30)


def test_export_round_trips_and_a_bad_persona_is_skipped(monkeypatch) -> None:
    async def test(pool: Any) -> None:
        await persona.update({"name": "管家", "address": "阿星", "tone": "  "})
        exported = await _export(pool)
        assert exported["settings"]["system"]["manager.persona"] == {"name": "管家", "address": "阿星"}

        await persona.update({"name": "别人", "address": None})
        async with pool.acquire() as conn:
            plan = await run_import(conn, exported, apply=True)
        await persona.load()
        assert persona.current() == Persona(name="管家", address="阿星")
        assert any(change.section == "settings.system" and change.key == PERSONA_KEY
                   and change.action is Action.CHANGED for change in plan.changes)

        exported["settings"]["system"] = {"manager.persona": {"name": "n" * 21}, "extra": True}
        async with pool.acquire() as conn:
            refused = await run_import(conn, exported, apply=True)
        assert any(change.section == "settings.system" and change.key == PERSONA_KEY
                   and change.action is Action.SKIPPED and "20" in change.reason for change in refused.changes)
        assert any(change.section == "settings.system" and change.key == "extra"
                   and change.action is Action.SKIPPED for change in refused.changes)
        await persona.load()
        assert persona.current() == Persona(name="管家", address="阿星")

    run_db(test, monkeypatch, timeout=60)
