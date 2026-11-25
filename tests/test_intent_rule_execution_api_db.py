"""The execution operations retain the same authenticated operator boundary."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.db_support import run_db, v1_api
from tests.intent_rule_execution_support import URL, configure, fact


@pytest.mark.parametrize("operation,body", [
    ("discovery_submit", {"discovery_source_id":"discovery","source_ref":"request","system_id":"review-system","mr_url":URL}),
    ("discovery_get", {"discovery_id":1}),
    ("discovery_build", {"discovery_id":1,"project_id":"review","template_key":"review"}),
    ("discovery_reject", {"discovery_id":1}),
    ("revoke", {"execution_id":1}),
    ("claim_revision", {"task_id":"task-synthetic","system_id":"review-system","mr_url":URL,"expected_revision":"head-a"}),
    ("migration_preview", {"discovery_source_id":"discovery","entries":[{"task_id":"task-synthetic","system_id":"review-system","mr_url":URL}]}),
    ("migration_confirm", {"preview_id":"00000000-0000-4000-8000-000000000001"}),
])
def test_execution_operations_reject_regular_clients(monkeypatch, tmp_path, operation, body):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, result = await call("intent_rules."+operation, body, client=True)
            assert status == 403, result
        assert await pool.fetchval("SELECT count(*) FROM intent_rule_discoveries") == 0
    run_db(check, monkeypatch)


def test_discovery_and_execution_have_closed_http_readback(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0,result=fact()))
        monkeypatch.setattr(engine, "_INSTALLED", service)
        async with v1_api(tmp_path,pool) as call:
            status, result = await call("intent_rules.discovery_submit", {"discovery_source_id":"discovery",
                "source_ref":"request","system_id":"review-system","mr_url":URL})
            assert status == 200, result
            iid = result["result"]["discovery_id"]
            await service.run_once()
            status, result = await call("intent_rules.discovery_get", {"discovery_id":iid})
            assert status == 200, result
            record = result["result"]
            assert record["execution"]["quota"]["units"] == 1
            from looper.agents import intent_rules as management
            direct = await management.get_hit(record["hit_id"],principal=OPERATOR,db=pool)
            assert direct["execution"]
            status, detail = await call("intent_rule_hits.get", {"hit_id":record["hit_id"]})
            assert status == 200, detail
            assert detail["result"]["execution"]["id"] == record["execution"]["id"]
            status, revoked = await call("intent_rules.revoke", {"execution_id":record["execution"]["id"]})
            assert status == 200 and revoked["result"]["state"] == "stopped", revoked
    run_db(check, monkeypatch)


def test_revoke_prepare_is_read_only_and_available_with_rules_off(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine, intent_routing
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.message import AgentAddress, MessageEnvelope, MessageType, manager_address
        await configure(pool, tmp_path)
        service = engine.IntentRuleService(db=pool, verifier=lambda *_a, **_kw: asyncio.sleep(0,result=fact()))
        await service.submit_discovery("discovery","request","review-system",URL,principal=OPERATOR)
        await service.run_once()
        task_id = await pool.fetchval("SELECT task_id FROM intent_rule_executions")
        await pool.execute("UPDATE system_settings SET value='false' WHERE key IN ('intents.enabled','intents.rules.enabled')")
        envelope = MessageEnvelope.create(source=AgentAddress("channel","channel-web","web"),target=manager_address("manager"),
            message_type=MessageType.USER_MESSAGE,payload={"text":"撤销 "+task_id,"channel_context":{}})
        assert await intent_routing.is_bound_revoke("撤销 "+task_id)
        assert not await intent_routing.is_bound_revoke("撤销 task-unknown")
        route = await intent_routing.route(envelope,platform=SimpleNamespace(bus=None))
        assert route and route.action is not None
        assert await pool.fetchval("SELECT revoke_state FROM intent_rule_executions") == "none"
        assert await pool.fetchval("SELECT status FROM tasks") == "init"
        assert "已停止" in await route.action()
        assert await pool.fetchval("SELECT status FROM tasks") == "cancelled"
    run_db(check,monkeypatch)


def test_unknown_historical_reason_has_explicit_readback(monkeypatch,tmp_path):
    async def check(pool):
        from looper.agents import intent_rule_engine as engine
        from looper.agents.api_v1 import OPERATOR
        await configure(pool,tmp_path)
        service=engine.IntentRuleService(db=pool,verifier=lambda *_a,**_kw:asyncio.sleep(0,result=fact()))
        await service.submit_discovery("discovery","request","review-system",URL,principal=OPERATOR)
        await service.run_once()
        await pool.execute("UPDATE intent_rule_hits SET reason='future_unrecognized_code'")
        async with v1_api(tmp_path,pool) as call:
            status,result=await call("intent_rule_hits.list",{})
            assert status==200,result
            assert result["result"]["hits"][0]["reason"]=="unknown"
        assert await pool.fetchval("SELECT count(*) FROM tasks")==1
    run_db(check,monkeypatch)
