"""Operator-bound configuration previews exercise real transactions and v1 dispatch."""
from uuid import UUID, uuid4

import pytest

from tests.db_support import run_db, v1_api


def rule(**fields):
    return {"enabled": True, "position": 1, "intent_type": "any", "scope": {"kind": "all"},
            "conditions": [], "action": "push_for_confirmation", "project_id": None,
            "template_key": None, **fields}


async def change(service, conn, command, *, generation=None):
    from looper.agents.api_v1 import OPERATOR
    if generation is None:
        generation = (await service.settings_get(principal=OPERATOR, db=conn))["generation"]
    preview = await service.preview(generation, command, principal=OPERATOR, db=conn)
    result = await service.confirm(preview["preview_id"], preview["required_acceptance"], principal=OPERATOR, db=conn)
    return result, preview


def test_rule_versions_reorder_delete_and_rollback_are_append_only(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        async with pool.acquire() as conn:
            first, _ = await change(service, conn, {"kind": "rule_create", "rule": rule()})
            rid = first["rules"][0]["id"]
            second, _ = await change(service, conn, {"kind": "rule_create", "rule": rule(position=2)})
            other = second["rules"][1]["id"]
            ordered, _ = await change(service, conn, {"kind": "rule_reorder", "ids": [other, rid]})
            assert [r["id"] for r in ordered["rules"]] == [other, rid]
            assert [r["position"] for r in ordered["rules"]] == [1, 2]
            current = next(r for r in ordered["rules"] if r["id"] == rid)
            disabled, _ = await change(service, conn, {"kind": "rule_disable", "rule_id": rid,
                                                       "expected_version": current["version"]})
            current = next(r for r in disabled["rules"] if r["id"] == rid)
            rolled, _ = await change(service, conn, {"kind": "rule_rollback", "rule_id": rid,
                "expected_version": current["version"], "target_version": 1})
            current = next(r for r in rolled["rules"] if r["id"] == rid)
            assert current["enabled"] is True and current["position"] == 2
            history = await service.history(rid, principal=OPERATOR, db=conn)
            assert [r["version"] for r in history["versions"]] == list(range(1, current["version"] + 1))
            assert history["versions"][0]["position"] == 1
            assert history["versions"][-1]["rolled_back_from"] == 1
            await change(service, conn, {"kind": "rule_delete", "rule_id": rid,
                                        "expected_version": current["version"]})
            assert await service.history(rid, principal=OPERATOR, db=conn)
            listing = await service.list_rules(principal=OPERATOR, db=conn)
            assert listing["fallback"]["action"] == "push_for_confirmation"
            assert listing["fallback"]["position"] > listing["rules"][-1]["position"]
    run_db(check, monkeypatch)


def test_rule_changes_require_operator_bound_preview_and_fresh_versions(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR, Principal
        from looper.agents.ops import OpError
        async with pool.acquire() as conn:
            for principal in (Principal("client", "synthetic"), True, {"operator": True}, None):
                with pytest.raises(OpError) as refused:
                    await service.preview(0, {"kind": "rule_create", "rule": rule()}, principal=principal, db=conn)
                assert refused.value.code.value == "forbidden"
            preview = await service.preview(0, {"kind": "rule_create", "rule": rule()}, principal=OPERATOR, db=conn)
            assert await conn.fetchval("SELECT count(*) FROM intent_rules") == 0
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_config") == 0
            with pytest.raises(OpError):
                await service.confirm(str(uuid4()), [], principal=OPERATOR, db=conn)
            result = await service.confirm(preview["preview_id"], [], principal=OPERATOR, db=conn)
            assert result["generation"] == 1
            assert await service.confirm(preview["preview_id"], [], principal=OPERATOR, db=conn) == result
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_config_audit") == 1
            rid = result["rules"][0]["id"]
            command = {"kind": "rule_update", "rule_id": rid, "expected_version": 1, "fields": {"enabled": False}}
            stale = await service.preview(1, command, principal=OPERATOR, db=conn)
            await change(service, conn, command)
            with pytest.raises(OpError) as conflict:
                await service.confirm(stale["preview_id"], [], principal=OPERATOR, db=conn)
            assert conflict.value.code.value == "conflict"
            expiring = await service.preview(2, {"kind": "settings", "enabled": False}, principal=OPERATOR, db=conn)
            await conn.execute("UPDATE intent_rule_previews SET expires_at=now()-interval '1 minute' WHERE id=$1", UUID(expiring["preview_id"]))
            with pytest.raises(OpError) as expired:
                await service.confirm(expiring["preview_id"], [], principal=OPERATOR, db=conn)
            assert expired.value.code.value == "expired"
    run_db(check, monkeypatch)


def test_rule_management_audit_failure_rolls_back(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        async with pool.acquire() as conn:
            preview = await service.preview(0, {"kind": "rule_create", "rule": rule()}, principal=OPERATOR, db=conn)
            await conn.execute("CREATE FUNCTION reject_rule_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit unavailable'; END $$; CREATE TRIGGER reject_audit BEFORE INSERT ON intent_rule_config_audit FOR EACH ROW EXECUTE FUNCTION reject_rule_audit()")
            with pytest.raises(Exception, match="audit unavailable"):
                await service.confirm(preview["preview_id"], [], principal=OPERATOR, db=conn)
            for table in ("intent_rules", "intent_rule_versions", "intent_rule_config", "intent_rule_config_audit"):
                assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0
            assert await conn.fetchval("SELECT consumed_at FROM intent_rule_previews") is None
    run_db(check, monkeypatch)


def test_project_auto_cap_has_one_versioned_owner(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        from looper.db import projects
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO projects(id,name,work_dir) VALUES('review','Review','/synthetic/review')")
            from looper.agents.projects import project_view
            old = project_view(await projects.get_project("review", pool=conn))
            assert "auto_task_daily_cap" not in old and "auto_task_config_version" not in old
            command = {"kind": "project_cap", "project_id": "review", "expected_config_version": 0, "auto_task_daily_cap": 50}
            preview = await service.preview(0, command, principal=OPERATOR, db=conn)
            await conn.execute("UPDATE projects SET auto_task_config_version=1 WHERE id='review'")
            with pytest.raises(OpError) as conflict:
                await service.confirm(preview["preview_id"], [], principal=OPERATOR, db=conn)
            assert conflict.value.code.value == "conflict"
            await change(service, conn, {**command, "expected_config_version": 1})
            view = await service.list_rules(principal=OPERATOR, db=conn)
            assert view["projects"] == [{"id": "review", "name": "Review", "auto_task_daily_cap": 50, "config_version": 2}]
            with pytest.raises(ValueError):
                await projects.update_project("review", execution={"auto_task_daily_cap": 99}, pool=conn)
    run_db(check, monkeypatch)


def test_rule_enable_preconditions_and_external_action_acceptance(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        async with pool.acquire() as conn:
            initial = await service.settings_get(principal=OPERATOR, db=conn)
            assert initial["enabled"] is False and initial["runtime_ready"] is False
            assert initial["adapter"] is None and initial["generation"] == 0
            for settings in ({}, {"intents.enabled": "true"}, {"intents.enabled": "true", "delivery.enabled": "true"}):
                for key, value in settings.items():
                    await conn.execute("INSERT INTO system_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key,value)
                with pytest.raises(OpError) as conflict:
                    await service.preview(0, {"kind": "settings", "enabled": True}, principal=OPERATOR, db=conn)
                assert conflict.value.code.value == "conflict"
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_config") == 0
    run_db(check, monkeypatch)


def test_every_rule_operation_rejects_client_and_declares_inside(monkeypatch, tmp_path):
    async def check(pool):
        from looper.agents import api_v1
        from looper.agents.action_levels import ActionLevel
        from looper.agents.v1_domains.intent_rules import OPERATIONS
        async with v1_api(tmp_path, pool) as call:
            for op in OPERATIONS:
                assert op.operator_only and not op.cli and op.level == ActionLevel.INSIDE
                assert op.request["additionalProperties"] is False
                status, answer = await call(op.operation_id, {}, client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden"
            status, answer = await call("intent_rules.list")
            assert status == 200 and answer["result"]["rules"] == []
            status, answer = await call("intent_rules.preview", {"expected_generation": 0,
                "change": {"kind": "rule_create", "rule": rule()}})
            assert status == 200
            token = answer["result"]["preview_id"]
            status, answer = await call("intent_rules.confirm", {"preview_id": token, "acceptance": []})
            assert status == 200 and answer["result"]["generation"] == 1
            status, _ = await call("intent_rules.confirm", {"preview_id": token, "acceptance": [], "change": {}})
            assert status == 400
            status, answer = await call("intent_rule_hits.stats")
            assert status == 200 and answer["result"]["available"] is False
    run_db(check, monkeypatch)


def test_template_definition_and_source_authorization_are_fresh_references(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO workflows(key,name,definition) VALUES('review','Review','{}')")
            contract = {"input_kind": "verified_mr", "comment_mode": "direct",
                "approve_requires_confirmation": True, "withdraw_requires_confirmation": True,
                "allow_merge": False, "allow_reviewer_changes": False}
            command = {"kind": "template_contract", "workflow_key": "review", "expected_contract_version": 0,
                       "contract": contract}
            preview = await service.preview(0, command, principal=OPERATOR, db=conn)
            await conn.execute("UPDATE workflows SET definition='{\"requirement\":\"changed\"}' WHERE key='review'")
            with pytest.raises(OpError) as refused:
                await service.confirm(preview["preview_id"], preview["required_acceptance"], principal=OPERATOR, db=conn)
            assert refused.value.code.value == "conflict"
            result, _ = await change(service, conn, command)
            listing = await service.list_rules(principal=OPERATOR, db=conn)
            assert listing["templates"][0]["version"] == 1
            assert listing["templates"][0]["allow_merge"] is False
            assert result["generation"] == 1
    run_db(check, monkeypatch)


@pytest.mark.parametrize("invalid", [
    {"kind": "rule_create", "rule": rule(intent_type="missing")},
    {"kind": "rule_create", "rule": rule(project_id="missing")},
    {"kind": "rule_create", "rule": rule(template_key="missing")},
    {"kind": "rule_create", "rule": rule(scope={"kind": "named_chats", "chats": [{"source_id": "missing", "chat_id": "missing"}]})},
    {"kind": "rule_create", "rule": rule(intent_type="mr_review", action="create_task", project_id="missing", template_key="missing", conditions=[{"kind": "internal_mr_open", "allowed_domains": ["review.example.invalid"]}])},
    {"kind": "rule_reorder", "ids": []},
    {"kind": "settings", "runtime_ready": True},
])
def test_rule_schema_rejects_unsafe_or_unresolved_configuration(monkeypatch, invalid):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        async with pool.acquire() as conn:
            with pytest.raises(OpError) as refused:
                await service.preview(0, invalid, principal=OPERATOR, db=conn)
            assert refused.value.code.value == "invalid"
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_previews") == 0
    run_db(check, monkeypatch)


def test_capability_readiness_and_risk_acceptance_are_not_writable_settings(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service, leads_model
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        monkeypatch.setattr(leads_model, "RULE_CLASSIFICATION_AVAILABLE", True)
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO system_settings(key,value) VALUES('intents.enabled','true'),('delivery.enabled','true')")
            preview = await service.preview(0, {"kind": "settings", "enabled": True}, principal=OPERATOR,
                                            db=conn, executor_available=True)
            assert preview["required_acceptance"]
            assert "verification_unavailable" in str(preview["impact"])
            with pytest.raises(OpError) as refused:
                await service.confirm(preview["preview_id"], [], principal=OPERATOR, db=conn, executor_available=True)
            assert refused.value.code.value == "invalid"
            result = await service.confirm(preview["preview_id"], preview["required_acceptance"],
                principal=OPERATOR, db=conn, executor_available=True)
            assert result["settings"]["enabled"] is True
            monkeypatch.setattr(leads_model, "RULE_CLASSIFICATION_AVAILABLE", False)
            current = await service.settings_get(principal=OPERATOR, db=conn, executor_available=True)
            assert current["runtime_ready"] is False
    run_db(check, monkeypatch)


def test_rule_hit_range_requires_timezone_and_bounded_window(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        for since, until in (("2026-01-01T00:00:00", "2026-01-02T00:00:00Z"),
                             ("2026-01-01T00:00:00Z", "2026-05-01T00:00:00Z"),
                             ("2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z")):
            with pytest.raises(OpError) as invalid:
                await service.hit_stats(principal=OPERATOR, db=pool, since=since, until=until)
            assert invalid.value.code.value == "invalid"
    run_db(check, monkeypatch)


def test_discovery_targets_and_chat_references_are_bound_to_current_authorization(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        from tests import subscription_support as stream
        await stream.source(pool)
        await stream.lark_targets(pool)
        await stream.chat(pool, "oc_rule_review")
        await pool.execute("UPDATE channel_configs SET config=config || jsonb_build_object('operator_sender_id',$1::text) WHERE channel_id='channel-lark'", stream.DM)
        command = {"kind": "rule_create", "rule": rule(scope={"kind": "named_chats", "chats": [
            {"source_id": stream.SOURCE, "chat_id": "oc_rule_review"}]})}
        preview = await service.preview(0, command, principal=OPERATOR, db=pool)
        await pool.execute("UPDATE source_chats SET extraction='excluded' WHERE source_id=$1 AND chat_id='oc_rule_review'", stream.SOURCE)
        with pytest.raises(OpError) as conflict:
            await service.confirm(preview["preview_id"], [], principal=OPERATOR, db=pool)
        assert conflict.value.code.value == "conflict"
        discovery = {"id": "project-source", "system_id": "review-system", "enabled": True,
            "private_target": {"channel_id": "channel-lark", "chat": "oc_rule_review"}, "migration_state": "old_disabled"}
        with pytest.raises(OpError) as invalid:
            await service.preview(0, {"kind": "settings", "discovery_sources": [discovery]}, principal=OPERATOR, db=pool)
        assert invalid.value.code.value == "invalid"
        discovery["private_target"]["chat"] = stream.DM
        preview = await service.preview(0, {"kind": "settings", "discovery_sources": [discovery]}, principal=OPERATOR, db=pool)
        await pool.execute("UPDATE channel_configs SET config=config || '{\"operator_sender_id\":\"different-operator\"}' WHERE channel_id='channel-lark'")
        with pytest.raises(OpError) as conflict:
            await service.confirm(preview["preview_id"], preview["required_acceptance"], principal=OPERATOR, db=pool)
        assert conflict.value.code.value == "conflict"
    run_db(check, monkeypatch)
