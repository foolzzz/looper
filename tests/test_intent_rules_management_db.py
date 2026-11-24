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
    from looper.agents import intent_rules_model as model
    from looper.agents.v1_domains.intent_rules import OPERATIONS
    model.validate_schema(preview, next(op.result for op in OPERATIONS if op.operation_id == "intent_rules.preview"))
    result = await service.confirm(preview["preview_id"], preview["required_acceptance"], principal=OPERATOR, db=conn)
    model.validate_schema(result, next(op.result for op in OPERATIONS if op.operation_id == "intent_rules.confirm"))
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


@pytest.mark.parametrize("kind", ["rule_create", "settings", "project_cap", "template_contract"])
def test_rule_management_audit_failure_rolls_back(monkeypatch, kind):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO projects(id,name) VALUES('audit-project','Audit project')")
            await conn.execute("INSERT INTO workflows(key,name,definition) VALUES('audit-review','Audit review','{}')")
            command = {"rule_create": {"kind": "rule_create", "rule": rule()},
                "settings": {"kind": "settings", "enabled": False},
                "project_cap": {"kind": "project_cap", "project_id": "audit-project", "expected_config_version": 0,
                    "auto_task_daily_cap": 50},
                "template_contract": {"kind": "template_contract", "workflow_key": "audit-review",
                    "expected_contract_version": 0, "contract": {"input_kind": "verified_mr", "comment_mode": "direct",
                        "approve_requires_confirmation": True, "withdraw_requires_confirmation": True,
                        "allow_merge": False, "allow_reviewer_changes": False}}}[kind]
            preview = await service.preview(0, command, principal=OPERATOR, db=conn)
            await conn.execute("CREATE FUNCTION reject_rule_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit unavailable'; END $$; CREATE TRIGGER reject_audit BEFORE INSERT ON intent_rule_config_audit FOR EACH ROW EXECUTE FUNCTION reject_rule_audit()")
            with pytest.raises(Exception, match="audit unavailable"):
                await service.confirm(preview["preview_id"], [], principal=OPERATOR, db=conn)
            for table in ("intent_rules", "intent_rule_versions", "intent_rule_config", "intent_rule_config_audit"):
                assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0
            assert await conn.fetchval("SELECT consumed_at FROM intent_rule_previews") is None
            assert await conn.fetchval("SELECT count(*) FROM intent_rule_template_contracts") == 0
            assert tuple(await conn.fetchrow("SELECT auto_task_daily_cap,auto_task_config_version FROM projects")) == (None, 0)
            assert await conn.fetchval("SELECT value FROM system_settings WHERE key='intents.rules.enabled'") is None
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
            assert preview["diff"]["reference_before"]["auto_task_daily_cap"] is None
            assert preview["diff"]["reference_after"]["auto_task_daily_cap"] == 50
            await conn.execute("UPDATE projects SET auto_task_config_version=1 WHERE id='review'")
            with pytest.raises(OpError) as conflict:
                await service.confirm(preview["preview_id"], [], principal=OPERATOR, db=conn)
            assert conflict.value.code.value == "conflict"
            await change(service, conn, {**command, "expected_config_version": 1})
            view = await service.list_rules(principal=OPERATOR, db=conn)
            assert view["projects"] == [{"id": "review", "name": "Review", "auto_task_daily_cap": 50, "config_version": 2}]
            from datetime import datetime, timezone
            from looper.agents import chat_settings
            monkeypatch.setattr(chat_settings, "_timezone", "Pacific/Honolulu")
            monkeypatch.setattr(service, "_now", lambda: datetime(2026, 11, 23, 5, tzinfo=timezone.utc))
            stats = await service.hit_stats(principal=OPERATOR, db=conn)
            assert stats["projects"] == [{"project_id": "review", "local_day": "2026-11-22",
                "timezone": "Pacific/Honolulu", "used": None, "cap": 50}]
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
            assert preview["diff"]["reference_before"] is None
            assert preview["diff"]["reference_after"]["comment_mode"] == "direct"
            await conn.execute("UPDATE workflows SET definition='{\"planner_instructions\":\"changed\"}' WHERE key='review'")
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
    {"kind": "rule_reorder", "ids": ["00112233-4455-4677-8899-aabbccddeeff"]},
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
        await pool.execute("UPDATE channel_configs SET enabled=true,config=config || jsonb_build_object('operator_sender_id',$1::text) WHERE channel_id='channel-lark'", stream.DM)
        command = {"kind": "rule_create", "rule": rule(scope={"kind": "named_chats", "chats": [
            {"source_id": stream.SOURCE, "chat_id": "oc_rule_review"}]})}
        preview = await service.preview(0, command, principal=OPERATOR, db=pool)
        await pool.execute("UPDATE source_chats SET extraction='excluded',extraction_excluded_at=now() WHERE source_id=$1 AND chat_id='oc_rule_review'", stream.SOURCE)
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
        await pool.execute("UPDATE channel_configs SET enabled=true,config=config || '{\"operator_sender_id\":\"different-operator\"}' WHERE channel_id='channel-lark'")
        with pytest.raises(OpError) as conflict:
            await service.confirm(preview["preview_id"], preview["required_acceptance"], principal=OPERATOR, db=pool)
        assert conflict.value.code.value == "conflict"
    run_db(check, monkeypatch)


def test_group_id_cannot_be_declared_as_discovery_operator_private_target(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        from tests import subscription_support as stream
        await stream.lark_targets(pool)
        await pool.execute("UPDATE channel_configs SET enabled=true,config=config || '{\"operator_sender_id\":\"oc_group\"}' WHERE channel_id='channel-lark'")
        source = {"id": "source-one", "system_id": "review", "enabled": True,
            "private_target": {"channel_id": "channel-lark", "chat": "oc_group"}, "migration_state": "old_disabled"}
        with pytest.raises(OpError) as invalid:
            await service.preview(0, {"kind": "settings", "discovery_sources": [source]}, principal=OPERATOR, db=pool)
        assert invalid.value.code.value == "invalid"
    run_db(check, monkeypatch)


def test_unknown_workflow_definition_cannot_be_attested_as_verified_input(monkeypatch):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        await pool.execute("INSERT INTO workflows(key,name,definition) VALUES('bad-review','Bad review','{\"inputs\":\"arbitrary expression\"}')")
        contract = {"input_kind": "verified_mr", "comment_mode": "direct", "approve_requires_confirmation": True,
                    "withdraw_requires_confirmation": True, "allow_merge": False, "allow_reviewer_changes": False}
        with pytest.raises(OpError) as invalid:
            await service.preview(0, {"kind": "template_contract", "workflow_key": "bad-review",
                "expected_contract_version": 0, "contract": contract}, principal=OPERATOR, db=pool)
        assert invalid.value.code.value == "invalid"
    run_db(check, monkeypatch)


def test_prepared_mr_rules_share_project_cap_and_require_current_external_action_acceptance(monkeypatch):
    async def check(pool):
        from looper.agents import extraction, intent_rules as service, leads_model
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        from tests import subscription_support as stream
        await stream.source(pool)
        await stream.chat(pool, "oc_review_authorized")
        await stream.lark_targets(pool)
        await pool.execute("UPDATE channel_configs SET enabled=true,config=config || jsonb_build_object('operator_sender_id',$1::text) WHERE channel_id='channel-lark'", stream.DM)
        await pool.execute("INSERT INTO projects(id,name,work_dir) VALUES('review','Review','/synthetic/review')")
        await pool.execute("INSERT INTO workflows(key,name,definition) VALUES('review','Review','{}')")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.enabled','true'),('delivery.enabled','true')")
        await change(service, pool, {"kind": "project_cap", "project_id": "review", "expected_config_version": 0,
                                     "auto_task_daily_cap": 50})
        await change(service, pool, {"kind": "template_contract", "workflow_key": "review", "expected_contract_version": 0,
            "contract": {"input_kind": "verified_mr", "comment_mode": "direct", "approve_requires_confirmation": True,
                         "withdraw_requires_confirmation": True, "allow_merge": False, "allow_reviewer_changes": False}})
        base = [{"kind": "internal_mr_open", "allowed_domains": ["review.example.invalid"]},
                {"kind": "mr_repository_in_allowlist", "repository_ids": ["repository-one"]}]
        for position, condition in enumerate(("operator_is_reviewer", "addressed_to_operator", None), 1):
            scope = {"kind": "all"} if condition else {"kind": "named_chats", "chats": [
                {"source_id": stream.SOURCE, "chat_id": "oc_review_authorized"}]}
            await change(service, pool, {"kind": "rule_create", "rule": rule(position=position, intent_type="mr_review",
                action="create_task", project_id="review", template_key="review", scope=scope,
                conditions=base + ([{"kind": condition}] if condition else []))})
        assert "mr_review" not in [row["key"] for row in await extraction.intent_types(pool)]
        view = await service.list_rules(principal=OPERATOR, db=pool)
        assert [row["position"] for row in view["rules"]] == [1, 2, 3]
        assert view["projects"][0]["auto_task_daily_cap"] == 50
        assert {row["project_id"] for row in view["rules"]} == {"review"}
        assert next(row for row in view["configurable_types"] if row["key"] == "mr_review")["available_when_rules_on"]
        assert all("guidance" not in row for row in view["configurable_types"])
        monkeypatch.setattr(leads_model, "RULE_CLASSIFICATION_AVAILABLE", True)
        source = {"id": "discovery", "system_id": "review", "enabled": True,
            "private_target": {"channel_id": "channel-lark", "chat": stream.DM}, "migration_state": "unconfigured"}
        with pytest.raises(OpError, match="discovery_migrated"):
            await service.preview(5, {"kind": "settings", "enabled": True, "discovery_sources": [source]},
                principal=OPERATOR, db=pool, executor_available=True)
        # Seven more enabled custom types would leave no room for the prepared built-in.
        for n in range(7):
            await pool.execute("INSERT INTO intent_types(key,version) VALUES($1,1)", f"capacity_{n}")
            await pool.execute("INSERT INTO intent_type_versions(type_key,version,name,guidance,priority,edited_via) "
                               "VALUES($1,1,'Capacity','Synthetic request','normal','api')", f"capacity_{n}")
        with pytest.raises(OpError, match="type_capacity"):
            await service.preview(5, {"kind": "settings", "enabled": True},
                principal=OPERATOR, db=pool, executor_available=True)
        await pool.execute("UPDATE intent_types SET enabled=false WHERE key LIKE 'capacity_%'")
        preview = await service.preview(5, {"kind": "settings", "enabled": True},
            principal=OPERATOR, db=pool, executor_available=True)
        assert "external_comments" in preview["required_acceptance"]
        assert preview["impact"]["ordered_rules"] == view["rules"]
        assert preview["impact"]["projects"] == [{"id": "review", "auto_task_daily_cap": 50}]
        assert preview["impact"]["verification"] == "verification_unavailable"
        with pytest.raises(OpError):
            await service.confirm(preview["preview_id"], ["reviewer_can_be_added"],
                principal=OPERATOR, db=pool, executor_available=True)
        result = await service.confirm(preview["preview_id"], preview["required_acceptance"],
            principal=OPERATOR, db=pool, executor_available=True)
        assert result["generation"] == 6 and result["settings"]["enabled"] is True
        # Missing objects must never prevent the operator from stopping automatic admission.
        await pool.execute("DELETE FROM workflows WHERE key='review'")
        stopped = await service.preview(6, {"kind": "settings", "enabled": False}, principal=OPERATOR, db=pool)
        result = await service.confirm(stopped["preview_id"], [], principal=OPERATOR, db=pool)
        assert result["settings"]["enabled"] is False
    run_db(check, monkeypatch)


def test_generic_configuration_surfaces_cannot_write_or_export_rule_settings(monkeypatch, tmp_path):
    async def check(pool):
        import json
        from looper.agents import config_transfer, intent_rules as service
        await pool.execute("INSERT INTO projects(id,name) VALUES('review','Review')")
        await change(service, pool, {"kind": "project_cap", "project_id": "review", "expected_config_version": 0,
                                     "auto_task_daily_cap": 50})
        async with v1_api(tmp_path, pool) as call:
            for action, body in (("project_admin.update", {"project_id": "review", "fields": {"auto_task_daily_cap": 99}}),
                    ("/api/v1/ops/settings/set", {"values": {"intents.rules.enabled": True}})):
                status, answer = await call(action, body)
                assert status == 400 and answer["error"]["code"] == "invalid"
        async with pool.acquire() as conn, conn.transaction():
            exported = json.loads("".join([part async for part in config_transfer.export_chunks(
                conn, include_projects=True, include_secrets=False)]))
            serialized = json.dumps(exported)
            assert "auto_task_daily_cap" not in serialized and "auto_task_config_version" not in serialized
            assert "intents.rules.enabled" not in serialized and "intent_rule_previews" not in serialized
            with pytest.raises(config_transfer.ConfigFileError, match="intent_rules.preview"):
                await config_transfer.import_config(conn, {"format": "looper-config/v4", "settings": {
                    "system": {"intents.rules.enabled": "true", "intent_rule_config.adapter": {"executable": "bad"}}},
                    "projects": [{"name": "Review", "auto_task_daily_cap": 99,
                                  "execution": {"auto_task_daily_cap": 99}}]}, apply=True)
            await config_transfer.import_config(conn, {"format": "looper-config/v4",
                "projects": [{"name": "Review", "auto_task_daily_cap": 99,
                              "execution": {"auto_task_daily_cap": 99}}]}, apply=True)
        assert tuple(await pool.fetchrow("SELECT auto_task_daily_cap,auto_task_config_version FROM projects")) == (50, 1)
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key='intents.rules.enabled'") is None
        assert await pool.fetchval("SELECT generation FROM intent_rule_config") == 1
    run_db(check, monkeypatch)


@pytest.mark.parametrize("kind", ["rule_create", "settings", "project_cap", "template_contract"])
def test_confirmation_does_not_lock_source_or_dependency_catalog_rows(monkeypatch, kind):
    async def check(pool):
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        await pool.execute("INSERT INTO projects(id,name) VALUES('review','Review')")
        await pool.execute("INSERT INTO workflows(key,name,definition) VALUES('review','Review','{}')")
        command = {
            "rule_create": {"kind": "rule_create", "rule": rule()},
            "settings": {"kind": "settings", "enabled": False},
            "project_cap": {"kind": "project_cap", "project_id": "review",
                "expected_config_version": 0, "auto_task_daily_cap": 50},
            "template_contract": {"kind": "template_contract", "workflow_key": "review",
                "expected_contract_version": 0, "contract": {"input_kind": "verified_mr", "comment_mode": "direct",
                    "approve_requires_confirmation": True, "withdraw_requires_confirmation": True,
                    "allow_merge": False, "allow_reviewer_changes": False}},
        }[kind]
        preview = await service.preview(0, command, principal=OPERATOR, db=pool)
        prepare = service._prepare
        inspected = False

        async def inspect(conn, *args, **kwargs):
            nonlocal inspected
            # RowShareLock is acquired even for an empty SELECT ... FOR SHARE.
            locks = await conn.fetch("SELECT locktype,mode,relation::regclass::text AS table_name FROM pg_locks "
                                     "WHERE pid=pg_backend_pid() AND granted")
            assert any(row["locktype"] == "advisory" for row in locks)
            assert any(row["table_name"] == "intent_rule_config" and row["mode"] == "RowShareLock" for row in locks)
            locked_tables = {row["table_name"] for row in locks if row["mode"] == "RowShareLock"}
            assert "projects" in locked_tables and "workflows" in locked_tables
            assert not locked_tables.intersection({"source_chats", "event_sources", "channel_configs", "system_settings", "intent_types"})
            inspected = True
            return await prepare(conn, *args, **kwargs)

        monkeypatch.setattr(service, "_prepare", inspect)
        result = await service.confirm(preview["preview_id"], [], principal=OPERATOR, db=pool)
        assert inspected and result["generation"] == 1
    run_db(check, monkeypatch)


def test_automatic_rules_require_existing_project_cap_and_share_it(monkeypatch, tmp_path):
    async def check(pool):
        import asyncpg
        from looper.agents import intent_rules as service
        from looper.agents.api_v1 import OPERATOR
        from looper.agents.ops import OpError
        await pool.execute("INSERT INTO projects(id,name,work_dir) VALUES('review','Review',$1)", str(tmp_path))
        await pool.execute("INSERT INTO workflows(key,name,definition) VALUES('review','Review','{}')")
        await change(service, pool, {"kind": "template_contract", "workflow_key": "review",
            "expected_contract_version": 0, "contract": {"input_kind": "verified_mr", "comment_mode": "direct",
                "approve_requires_confirmation": True, "withdraw_requires_confirmation": True,
                "allow_merge": False, "allow_reviewer_changes": False}})
        automatic = rule(intent_type="mr_review", action="create_task", project_id="review", template_key="review",
            conditions=[{"kind": "internal_mr_open", "allowed_domains": ["review.example.invalid"]}])
        assert await pool.fetchval("SELECT auto_task_daily_cap FROM projects WHERE id='review'") is None
        with pytest.raises(OpError, match="positive daily cap"):
            await service.preview(1, {"kind": "rule_create", "rule": automatic}, principal=OPERATOR, db=pool)
        for cap in (0, -1, True, "50"):
            with pytest.raises(OpError) as refused:
                await service.preview(1, {"kind": "project_cap", "project_id": "review",
                    "expected_config_version": 0, "auto_task_daily_cap": cap}, principal=OPERATOR, db=pool)
            assert refused.value.code.value == "invalid"
        for cap in (0, -1):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute("UPDATE projects SET auto_task_daily_cap=$1 WHERE id='review'", cap)
        await change(service, pool, {"kind": "project_cap", "project_id": "review",
            "expected_config_version": 0, "auto_task_daily_cap": 50})
        await change(service, pool, {"kind": "rule_create", "rule": automatic})
        result, preview = await change(service, pool, {"kind": "rule_create", "rule": {**automatic, "position": 2}})
        assert len(result["rules"]) == 2 and {row["project_id"] for row in result["rules"]} == {"review"}
        assert preview["impact"]["projects"] == [{"id": "review", "auto_task_daily_cap": 50}]
        assert (await service.list_rules(principal=OPERATOR, db=pool))["projects"] == [
            {"id": "review", "name": "Review", "auto_task_daily_cap": 50, "config_version": 1}]
        # Clearing the shared cap must fail while any automatic rule still references it.
        with pytest.raises(OpError, match="positive daily cap"):
            await service.preview(4, {"kind": "project_cap", "project_id": "review",
                "expected_config_version": 1, "auto_task_daily_cap": None}, principal=OPERATOR, db=pool)
    run_db(check, monkeypatch)
