"""The independent task role never changes shared Manager/API discovery."""
import json

import pytest

from looper.agents import agent_settings, task_conversations_settings, task_manager_settings
from looper.agents.v1_domains import global_settings
from tests.db_support import run_db, v1_api


OLD_ROLES = ["manager", "planner", "worker", "reviewer", "processor", "advisor", "advice_check",
             "leads_check", "outbound_check", "memory_draft", "memory_retro", "explorer"]


def test_shared_role_schemas_remain_frozen():
    assert global_settings.ROLE["enum"] == OLD_ROLES
    assert list(global_settings.AGENT_SETTINGS["properties"]["roles"]["properties"]) == OLD_ROLES


@pytest.mark.parametrize("operation", ["roles.list", "agent_settings.get"])
def test_switch_off_freezes_role_settings_and_global_role_api(monkeypatch, tmp_path, operation):
    monkeypatch.setattr(task_conversations_settings, "_enabled", False)
    async def check(db):
        await db.execute("DELETE FROM role_configs WHERE role='task_manager'")
        async with v1_api(tmp_path, db) as call:
            status, before = await call(operation)
            assert status == 200
            await task_manager_settings.seed_defaults(db)
            status, after = await call(operation)
            assert status == 200
            if operation == "roles.list":
                assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)
            else:
                assert list(after["result"]["roles"]) == OLD_ROLES
                assert list((await agent_settings.settings_view())["roles"]) == OLD_ROLES
    run_db(check, monkeypatch, timeout=30)
