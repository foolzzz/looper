"""Portable Manager lanes settings use the import transaction and require an operator."""

import json

import pytest

from looper.agents import config_transfer, lanes_settings
from looper.db import system_settings
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _export(pool, summary=None):
    return json.loads("".join([chunk async for chunk in config_transfer.export_chunks(
        pool, include_projects=False, include_secrets=False, summary=summary)]))


@pytest.mark.parametrize("enabled", [None, False, True])
def test_export_summary_only_includes_lanes_when_enabled(enabled):
    async def check(pool):
        if enabled is not None:
            await system_settings.put_settings({lanes_settings.KEY: json.dumps(enabled)}, pool=pool)
        summary = config_transfer.ExportSummary()
        document = await _export(pool, summary)
        if enabled is not None:
            assert document["settings"]["manager_lanes"] == {"enabled": enabled}
        else:
            assert "manager_lanes" not in document["settings"]
        if enabled:
            assert summary.counts["settings.manager_lanes"] == 1
        else:
            assert "settings.manager_lanes" not in summary.counts

    run_db(check)


def test_lanes_config_preview_apply_export_and_reset():
    async def check(pool):
        assert "manager_lanes" not in (await _export(pool))["settings"]
        document = {"format": config_transfer.FORMAT, "settings": {"manager_lanes": {"enabled": True}}}
        async with pool.acquire() as conn:
            preview = await config_transfer.run_import(conn, document, apply=False)
        assert preview.view()["counts"]["settings.manager_lanes"]["added"] == 1
        assert await system_settings.get_settings([lanes_settings.KEY], pool=pool) == {}
        async with pool.acquire() as conn:
            await config_transfer.run_import(conn, document, apply=True)
        assert await system_settings.get_settings([lanes_settings.KEY], pool=pool) == {
            lanes_settings.KEY: "true"}
        saved = await _export(pool)
        assert saved["settings"]["manager_lanes"] == {"enabled": True}
        assert saved["settings"].get("chat", {}) == {}
        async with pool.acquire() as conn:
            unchanged = await config_transfer.run_import(conn, document, apply=False)
        assert unchanged.view()["counts"]["settings.manager_lanes"]["unchanged"] == 1
        document["settings"]["manager_lanes"]["enabled"] = None
        async with pool.acquire() as conn:
            await config_transfer.run_import(conn, document, apply=True)
        assert await system_settings.get_settings([lanes_settings.KEY], pool=pool) == {}
        assert "manager_lanes" not in (await _export(pool))["settings"]

    run_db(check)


@pytest.mark.parametrize("entry", [{"enabled": "true"}, {"enabled": 1}, {"enabled": True, "extra": False}, {}])
def test_invalid_portable_setting_is_skipped_without_writes(entry):
    async def check(pool):
        document = {"format": config_transfer.FORMAT, "settings": {"manager_lanes": entry}}
        async with pool.acquire() as conn:
            plan = await config_transfer.run_import(conn, document, apply=True)
        assert plan.view()["counts"]["settings.manager_lanes"]["skipped"] == 1
        assert await system_settings.get_settings([lanes_settings.KEY], pool=pool) == {}

    run_db(check)
