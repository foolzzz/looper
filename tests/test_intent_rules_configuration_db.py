"""Generic configuration transfer cannot substitute for rule preview and confirmation."""

import json

import pytest

from looper.agents import config_imports, config_transfer
from tests.db_support import run_db, v1_api


def documents():
    return [
        {"settings": {"system": {"intents.rules.enabled": "true"}}},
        {"settings": {"system": {"intent_rule_config": {"enabled": True}}}},
        {"settings": {"system": {"intent_rule_config.adapter": {"executable": "example"}}}},
        {"settings": {"intent_rules": [{"action": "create_task"}]}},
        {"intent_rules": [{"action": "create_task"}]},
    ]


async def create_rule(pool):
    from looper.agents import intent_rules as service
    from looper.agents.api_v1 import OPERATOR

    preview = await service.preview(0, {"kind": "rule_create", "rule": {
        "enabled": True, "position": 1, "intent_type": "any", "scope": {"kind": "all"},
        "conditions": [], "action": "push_for_confirmation", "project_id": None, "template_key": None}},
        principal=OPERATOR, db=pool)
    await service.confirm(preview["preview_id"], preview["required_acceptance"], principal=OPERATOR, db=pool)


@pytest.mark.parametrize("fields", documents())
@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
def test_rule_keys_cannot_be_staged_via_generic_import(monkeypatch, tmp_path, fields, client):
    async def check(pool):
        document = {"format": config_transfer.FORMAT, **fields}
        async with v1_api(tmp_path, pool) as call:
            for operation, body in (("config.import_preview", {"document": document}),
                                    ("config.import_upload", document)):
                status, result = await call(operation, body, client=client)
                assert status == 400, result
                assert result["error"]["code"] == "invalid"
        assert await pool.fetchval("SELECT count(*) FROM intent_rules") == 0
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_config") == 0
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_previews") == 0
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key='intents.rules.enabled'") is None
    run_db(check, monkeypatch)


@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
@pytest.mark.parametrize("fields", documents()[:-1])
def test_staged_generic_import_rechecks_rule_keys_at_execution(monkeypatch, tmp_path, fields, client):
    async def check(pool):
        baseline = {"format": config_transfer.FORMAT, "settings": {"scheduler": {"min_interval_seconds": 73}}}
        async with v1_api(tmp_path, pool) as call:
            status, staged = await call("config.import_preview", {"document": baseline}, client=client)
            assert status == 200, staged
            import_id = staged["result"]["import_id"]
            operation, body = "config.import_apply", {"import_id": import_id}
            if client:
                status, pending = await call(operation, body, client=True)
                assert status == 202, pending
                operation, body = "confirm", {"confirmation_id": pending["confirmation_id"]}
            path = tmp_path / config_imports.DIRNAME / f"{import_id}.json"
            path.write_text(json.dumps({**baseline, "settings": {**baseline["settings"], **fields["settings"]}}))
            status, refused = await call(operation, body, client=client)
            assert status == 400, refused
            assert refused["error"]["code"] == "invalid"
            assert not path.exists()
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_config") == 0
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key='intents.rules.enabled'") is None
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key='scheduler.min_interval_seconds'") is None
    run_db(check, monkeypatch)


@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
def test_generic_settings_endpoints_refuse_rule_keys(monkeypatch, tmp_path, client):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        await create_rule(pool)
        before = await service.list_rules(principal=OPERATOR, db=pool)
        async with v1_api(tmp_path, pool) as call:
            for operation, body in (
                ("/api/v1/ops/settings/set", {"values": {"intents.rules.enabled": True}}),
                ("/api/v1/ops/settings/set", {"values": {"intent_rules": [{"action": "create_task"}]}}),
                ("/api/v1/ops/settings/set", {"values": {"intent_rules": [
                    {"id": before["rules"][0]["id"], "enabled": False}]}}),
                ("agent_settings.update_system", {"fields": {"intents.rules.enabled": True}}),
                ("chat_settings.update", {"intents.rules.enabled": True}),
            ):
                status, refused = await call(operation, body, client=client)
                assert status in (400, 403), refused
                assert refused["error"]["code"] in ("invalid", "forbidden")
        assert await service.list_rules(principal=OPERATOR, db=pool) == before
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key='intents.rules.enabled'") is None
    run_db(check, monkeypatch)


def test_export_excludes_configured_rules_and_enabled_switch(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        await create_rule(pool)
        await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.rules.enabled','true')")
        before = await service.list_rules(principal=OPERATOR, db=pool)
        assert len(before["rules"]) == 1
        assert before["settings"]["enabled"] is True
        async with v1_api(tmp_path, pool) as call:
            for client in (False, True):
                status, exported = await call("config.export", {}, client=client)
                assert status == 200, exported
                document = exported["result"]["document"]
                serialized = json.dumps(document)
                assert "intents.rules.enabled" not in serialized
                assert "intent_rules" not in serialized
                assert "intent_rule_config" not in serialized
                # The ordinary document remains a usable generic import.
                status, staged = await call("config.import_preview", {"document": document}, client=client)
                assert status == 200, staged
        assert await service.list_rules(principal=OPERATOR, db=pool) == before
    run_db(check, monkeypatch)
