"""Operator card-button controls and portable configuration on a disposable database."""

import json

import pytest

from looper.agents import card_actions
from looper.agents.config_transfer import Action, export_chunks, run_import
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
KEY = card_actions.ENABLED_KEY


def test_operator_switch_round_trip_and_null_restores_absent_default(monkeypatch, tmp_path):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            for action, body, expected in (
                ("get", {}, False), ("update", {"enabled": True}, True), ("get", {}, True),
                ("update", {"enabled": False}, False), ("get", {}, False),
                ("update", {"enabled": True}, True), ("update", {"enabled": None}, False),
                ("get", {}, False),
            ):
                status, answer = await call(f"card_actions.{action}", body)
                assert status == 200 and answer["result"] == {"enabled": expected}, answer
                assert await card_actions.enabled() is expected
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", KEY) is None

    run_db(test, monkeypatch)


@pytest.mark.parametrize("action", ["get", "update"])
def test_api_client_cannot_read_or_write_switch(monkeypatch, tmp_path, action):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call(f"card_actions.{action}", {"enabled": True} if action == "update" else {},
                                        client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", KEY) is None

    run_db(test, monkeypatch)


@pytest.mark.parametrize("body", [{}, {"enabled": "true"}, {"enabled": 1}, {"enabled": True, "extra": False}])
def test_invalid_update_leaves_switch_absent(monkeypatch, tmp_path, body):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("card_actions.update", body)
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert not await card_actions.enabled()

    run_db(test, monkeypatch)


def test_portable_switch_preserves_absence_and_round_trips_explicit_values(monkeypatch):
    async def test(pool):
        async def export():
            async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
                return json.loads("".join([chunk async for chunk in export_chunks(
                    conn, include_projects=False, include_secrets=False)]))

        assert KEY not in (await export())["settings"].get("system", {})
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('manager.persona', $1)",
                           '{"name":"Assistant"}')
        for value in (True, False, None):
            document = {"format": "looper-config/v4", "settings": {"system": {KEY: value}}}
            before = await card_actions.enabled()
            async with pool.acquire() as conn:
                preview = await run_import(conn, document, apply=False)
                assert not preview.applied and all(change.action is not Action.SKIPPED for change in preview.changes)
                assert await card_actions.enabled() is before
                await run_import(conn, document, apply=True)
            assert await card_actions.enabled() is (value is True)
            saved = (await export())["settings"].get("system", {})
            assert saved["manager.persona"] == {"name": "Assistant"}
            if value is None:
                assert KEY not in saved
                assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", KEY) is None
            else:
                assert saved[KEY] is value
        async with pool.acquire() as conn:
            plan = await run_import(conn, {"format": "looper-config/v4", "settings": {
                "system": {KEY: "true"}}}, apply=True)
        assert any(change.key == KEY and change.action is Action.SKIPPED for change in plan.changes)
        assert not await card_actions.enabled()

    run_db(test, monkeypatch)


@pytest.mark.parametrize("route", ["import_preview", "import_upload"])
@pytest.mark.parametrize("value", [True, False, None])
def test_client_import_of_switch_is_refused(monkeypatch, tmp_path, route, value):
    async def test(pool):
        document = {"format": "looper-config/v4", "settings": {"system": {KEY: value}}}
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call(f"config.{route}",
                {"document": document} if route == "import_preview" else document, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", KEY) is None

    run_db(test, monkeypatch)


def test_operator_import_applies_switch_immediately(monkeypatch, tmp_path):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("config.import_preview", {"document": {
                "format": "looper-config/v4", "settings": {"system": {KEY: True}}}})
            assert status == 200, answer
            assert not await card_actions.enabled()
            status, answer = await call("config.import_apply", {"import_id": answer["result"]["import_id"]})
            assert status == 200, answer
            status, answer = await call("card_actions.get")
            assert status == 200 and answer["result"] == {"enabled": True}, answer

    run_db(test, monkeypatch)
