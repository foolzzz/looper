"""Intent operations retain their operator boundary and frozen dashboard contracts."""

from datetime import datetime, timezone

import pytest

from looper.agents import api_v1
from looper.agents.bus import MessageBus
from looper.agents.message import MessageType, TASK_ROUTER_ADDRESS
from looper.db import projects
from tests import leads_support as leads, subscription_support as stream
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_intents_read_and_prepare_while_off_but_never_for_clients(monkeypatch, tmp_path):
    async def check(pool):
        await stream.source(pool)
        event = await stream.add(pool, "Synthetic request", sent_at=datetime.now(timezone.utc))
        await leads.intent(pool, [event], title='<at user_id="all">ALL</at> Synthetic title')
        async with v1_api(tmp_path, pool) as call:
            for operation, body in (("intents.list", {}), ("intents.get", {"intent_id": "I-1"}),
                ("intent_types.list", {}), ("intent_types.history", {"key": "dev_task"}),
                ("intents.stats", {}), ("intents.settings_get", {}),
                ("intent_bindings.list", {}), ("intent_alert_senders.list", {})):
                status, answer = await call(operation, body)
                assert status == 200, answer
                schema = next(op.result for op in api_v1.operations() if op.operation_id == operation)
                api_v1.validate_arguments(schema, answer["result"])
                assert "<at" not in str(answer)
                status, answer = await call(operation, body, client=True)
                assert status == 403, answer
            for action in ("build", "reject"):
                status, answer = await call(f"intents.{action}", {"intent_id": "I-1"})
                assert status == 409, answer
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("action,body", [("intents.list", {"state": ["bad"]}),
    ("intents.list", {"limit": 0}), ("intents.stats", {"days": 366}),
    ("intents.reject", {"intent_id": "I-1", "reason": "x" * 201}),
    ("intent_types.create", {"key": "custom", "name": "x", "guidance": "g", "priority": "bad"}),
    ("intents.settings_put", {"enabled": "true"}), ("intents.update", {"intent_id": "I-1"}),
    ("intent_types.save", {"key": "dev_task"})])
def test_invalid_arguments_are_rejected_before_service(monkeypatch, tmp_path, action, body):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call(action, body)
            assert status == 400, answer
    run_db(check, monkeypatch, timeout=30)


def test_type_versions_and_operator_reject_round_trip(monkeypatch, tmp_path):
    async def check(pool):
        await stream.source(pool)
        event = await stream.add(pool, "Synthetic request", sent_at=datetime.now(timezone.utc))
        await leads.intent(pool, [event])
        async with v1_api(tmp_path, pool) as call:
            for action, body in (("intent_types.create", {"key": "custom", "name": "Custom",
                    "guidance": "Synthetic guidance", "priority": "normal"}),
                ("intent_types.save", {"key": "custom", "name": "Updated"}),
                ("intent_types.rollback", {"key": "custom", "version": 1}),
                ("intent_types.archive", {"key": "custom"}),
                ("intents.settings_put", {"enabled": True}),
                ("intents.reject", {"intent_id": "I-1", "reason": "Already handled"})):
                status, answer = await call(action, body)
                assert status == 200, answer
                schema = next(op.result for op in api_v1.operations() if op.operation_id == action)
                api_v1.validate_arguments(schema, answer["result"])
            status, answer = await call("intents.get", {"intent_id": "I-1"})
            assert answer["result"]["intent"]["state"] == "rejected"
            status, answer = await call("intent_types.archive", {"key": "dev_task"})
            assert status == 409
    run_db(check, monkeypatch, timeout=30)


def test_every_intent_operation_rejects_api_client_identity(monkeypatch, tmp_path):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            operations = [op for op in api_v1.operations() if op.operation_id.split(".")[0] in
                          ("intents", "intent_types", "intent_bindings", "intent_alert_senders", "intent_group_reads")]
            assert len(operations) == 26
            for operation in operations:
                assert operation.operator_only
                status, answer = await call(operation.operation_id, {}, client=True)
                assert status == 403, (operation.operation_id, answer)
    run_db(check, monkeypatch, timeout=30)


def test_dashboard_build_records_dashboard_origin_and_queues_the_real_start_path(monkeypatch, tmp_path):
    async def check(pool):
        await stream.source(pool)
        event = await stream.add(pool, "Synthetic request", sent_at=datetime.now(timezone.utc))
        iid = await leads.intent(pool, [event])
        project = await projects.create_project(name="Synthetic project", work_dir=str(tmp_path))
        sent = []
        original = MessageBus.publish

        async def capture(self, message):
            if message.message_type is MessageType.TASK_START:
                sent.append(message)
            return await original(self, message)

        monkeypatch.setattr(MessageBus, "publish", capture)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("intents.settings_put", {"enabled": True})
            assert status == 200, answer
            status, answer = await call("intents.update", {"intent_id": "I-1", "project_id": project["id"]})
            assert status == 200, answer
            status, answer = await call("intents.build", {"intent_id": "I-1"})
            assert status == 200, answer
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/intents/build").result, answer["result"])
            result = answer["result"]
            assert result["intent"]["state"] == "tasked"
            assert result["intent"]["handled_via"] == "dashboard"
            task = await pool.fetchrow("SELECT * FROM tasks WHERE id=$1", result["task_id"])
            assert task["origin_channel_id"] == "" and task["origin_reply_route"] is None
            assert task["chat_id"] is None and task["project_id"] == project["id"]
            assert "意图 I-0001" in task["requirement"]
            assert len(sent) == 1 and sent[0].target == TASK_ROUTER_ADDRESS
            assert sent[0].payload["task_id"] == result["task_id"]
            assert await pool.fetchval("SELECT task_id FROM intents WHERE id=$1", iid) == result["task_id"]
    run_db(check, monkeypatch, timeout=30)


def test_binding_templates_and_alert_allowlist_round_trip_with_declared_schemas(monkeypatch, tmp_path):
    async def check(pool):
        await stream.source(pool)
        await stream.add(pool, "Synthetic alert", sender="app_synthetic_alert", sender_kind="app",
                         sent_at=datetime.now(timezone.utc))
        project = await projects.create_project(name="Synthetic project", work_dir=str(tmp_path))
        await pool.execute("INSERT INTO workflows(key,name,definition) VALUES ('synthetic_workflow','Synthetic','{}')")
        async with v1_api(tmp_path, pool) as call:
            calls = (("intent_bindings.put", {"chat_id": "oc_ops", "project_id": project["id"]}),
                ("intent_bindings.list", {"query": "Ops"}),
                ("intent_bindings.delete", {"chat_id": "oc_ops"}),
                ("intent_types.set_project_template", {"key": "dev_task", "project_id": project["id"],
                                                      "workflow_key": "synthetic_workflow"}),
                ("intent_types.set_project_template", {"key": "dev_task", "project_id": project["id"],
                                                      "workflow_key": None}),
                ("intent_alert_senders.put", {"app_id": "app_synthetic_alert"}),
                ("intent_alert_senders.list", {}),
                ("intent_alert_senders.delete", {"app_id": "app_synthetic_alert"}))
            answers = []
            for action, body in calls:
                status, answer = await call(action, body)
                assert status == 200, (action, answer)
                schema = next(op.result for op in api_v1.operations() if op.operation_id == action)
                api_v1.validate_arguments(schema, answer["result"])
                answers.append(answer["result"])
            assert answers[0]["bindings"][0]["project"]["id"] == project["id"]
            assert answers[1]["bindings"] == answers[0]["bindings"] and answers[2]["bindings"] == []
            assert answers[3]["type"]["projects"][0]["workflow_key"] == "synthetic_workflow"
            assert answers[4]["type"]["projects"] == []
            assert answers[5]["senders"][0]["app_id"] == "app_synthetic_alert"
            assert answers[6]["senders"][0]["messages_30d"] == 1 and answers[6]["candidates"] == []
            assert answers[7]["senders"] == []
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("state,expected", [
    ("tasked", "I-0001 已经建过任务 task-synthetic。"),
    ("rejected", "I-0001 已驳回过。"),
    ("merged", "I-0001 已合并到 I-0002，请处理那一条。"),
    ("expired", "I-0001 已过期，要建任务请回『建 I-0001』。"),
])
def test_api_conflict_names_the_existing_outcome(monkeypatch, tmp_path, state, expected):
    async def check(pool):
        await stream.source(pool)
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('intents.enabled','true')")
        project = await projects.create_project(name="Synthetic project", work_dir=str(tmp_path))
        await pool.execute("INSERT INTO tasks(id,project_id,title,requirement,work_dir,status) "
                           "VALUES ('task-synthetic',$1,'Synthetic','Synthetic',$2,'completed')",
                           project["id"], str(tmp_path))
        event = await stream.add(pool, "Synthetic request", sent_at=datetime.now(timezone.utc))
        iid = await leads.intent(pool, [event])
        if state == "merged":
            other = await leads.intent(pool, [event])
            await pool.execute("UPDATE intents SET state='merged',merged_into=$2 WHERE id=$1", iid, other)
        else:
            await pool.execute("UPDATE intents SET state=$2,handled_at=now(),handled_via='dashboard',"
                               "task_id=$3 WHERE id=$1", iid, state, "task-synthetic" if state == "tasked" else None)
        async with v1_api(tmp_path, pool) as call:
            for action in (("reject",) if state == "expired" else ("build", "reject")):
                status, answer = await call(f"intents.{action}", {"intent_id": "I-1"})
                assert status == 409, answer
                assert answer["error"] == {"code": "conflict", "message": expected}
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("with_candidates", [False, True])
def test_api_needs_project_returns_the_same_selection_instruction(monkeypatch, tmp_path, with_candidates):
    async def check(pool):
        await stream.source(pool)
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('intents.enabled','true')")
        event = await stream.add(pool, "Synthetic request", sent_at=datetime.now(timezone.utc))
        await leads.intent(pool, [event])
        if with_candidates:
            project = await projects.create_project(name="Synthetic project", work_dir=str(tmp_path))
            await pool.execute("INSERT INTO tasks(id,project_id,title,requirement,work_dir,status) "
                               "VALUES ('task-candidate',$1,'Synthetic','Synthetic',$2,'completed')",
                               project["id"], str(tmp_path))
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("intents.build", {"intent_id": "I-1"})
            assert status == 409, answer
            expected = ("项目：这个会话没有绑定项目。候选：1. Synthetic project\n回复「建 I-0001 <编号>」"
                        if with_candidates else "请到意图页选择项目。")
            assert answer["error"] == {"code": "conflict", "message": expected}
    run_db(check, monkeypatch, timeout=30)


def test_group_reads_round_trip_and_refusals(monkeypatch, tmp_path):
    from datetime import timedelta
    from looper.agents import chat_settings, cron, extraction

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(chat_settings, "_delivery_enabled", False)

    async def check(pool):
        await stream.source(pool)
        seq = await stream.add(pool, "Synthetic requirement", chat_id="oc_group", sent_at=now,
                               chat_name='<at user_id="all">ALL</at> Synthetic group')
        await stream.add(pool, "Synthetic robot", chat_id="oc_group", sent_at=now, sender_kind="app")
        await stream.add(pool, "Synthetic import", chat_id="oc_group", sent_at=now, origin="import")
        await stream.add(pool, "Old message", chat_id="oc_group", sent_at=now - timedelta(days=2))
        await leads.intent(pool, [seq], chat_id="oc_group")
        async with v1_api(tmp_path, pool) as call:
            async def ok(action, body):
                operation = f"intent_group_reads.{action}"
                status, answer = await call(operation, body)
                assert status == 200, answer
                schema = next(op.result for op in api_v1.operations() if op.operation_id == operation)
                api_v1.validate_arguments(schema, answer["result"])
                assert "<at" not in str(answer)
                return answer["result"]

            listed = await ok("list", {})
            assert listed == {"enabled": False, "delivery_enabled": False, "run_max": 200,
                              "day_max": 500, "max_groups": 10, "groups": []}
            preview = await ok("preview", {"chat_id": "oc_group"})
            assert preview["group"]["messages_24h"] == 1
            assert preview["group"]["estimate_usd"] == [round(n, 4) for n in extraction.estimate(1, 1)]
            assert (await ok("list", {}))["groups"] == []
            listed = await ok("put", {"chat_id": "oc_group"})
            group = listed["groups"][0]
            assert group["name"] == preview["group"]["name"]
            assert group["kind"] == "group" and group["readable"]
            assert group["messages_24h"] == group["intents_7d"] == 1
            assert group["read_today"] == group["skipped_today"] == 0
            before = group["read_from"]
            assert (await ok("put", {"chat_id": "oc_group"}))["groups"][0]["read_from"] == before
            day = now.astimezone(cron.zone(cron.local_zone_name())).date()
            await pool.execute("INSERT INTO intent_group_read_days(source_id,chat_id,day,read,skipped) "
                               "VALUES($1,'oc_group',$2,7,3)", stream.SOURCE, day)
            group = (await ok("list", {}))["groups"][0]
            assert group["read_today"] == 7 and group["skipped_today"] == 3
            await pool.execute("UPDATE source_chats SET extraction='excluded',extraction_excluded_at=now() WHERE chat_id='oc_group'")
            assert not (await ok("list", {}))["groups"][0]["readable"]
            await pool.execute("UPDATE source_chats SET extraction='included',extraction_excluded_at=NULL WHERE chat_id='oc_group'")
            for n in range(1, 11):
                await stream.chat(pool, f"oc_extra_{n}", kind="topic" if n == 1 else "group")
                if n < 10:
                    await ok("put", {"chat_id": f"oc_extra_{n}"})
            status, answer = await call("intent_group_reads.put", {"chat_id": "oc_extra_10"})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert "at most 10" in answer["error"]["message"]
            status, answer = await call("intent_group_reads.settings_put", {"enabled": True})
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert "delivery.enabled" in answer["error"]["message"]
            assert not (await ok("list", {}))["enabled"]
            monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
            listed = await ok("settings_put", {"enabled": True})
            assert listed["enabled"] and listed["delivery_enabled"]
            assert next(g for g in listed["groups"] if g["chat_id"] == "oc_group")["read_from"] > before
            starts = {g["chat_id"]: g["read_from"] for g in listed["groups"]}
            assert {g["chat_id"]: g["read_from"] for g in (await ok("settings_put", {"enabled": True}))["groups"]} == starts
            assert not (await ok("settings_put", {"enabled": False}))["enabled"]
            listed = await ok("delete", {"chat_id": "oc_group"})
            assert "oc_group" not in {g["chat_id"] for g in listed["groups"]}
            assert await pool.fetchval("SELECT count(*) FROM intents") == 1
            await ok("delete", {"chat_id": "oc_group"})
            for action, body in (("list", {}), ("preview", {"chat_id": "oc_extra_1"}),
                                 ("put", {"chat_id": "oc_extra_1"}), ("delete", {"chat_id": "oc_extra_1"}),
                                 ("settings_put", {"enabled": True})):
                status, answer = await call(f"intent_group_reads.{action}", body, client=True)
                assert status == 403, answer
            status, answer = await call("intent_group_reads.settings_put", {"enabled": "true"})
            assert status == 400, answer
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("case", ["p2p", "unknown", "stream_excluded", "extraction_excluded", "missing", "empty"])
def test_group_reads_reject_unfollowable_chats(monkeypatch, tmp_path, case):
    async def check(pool):
        await stream.source(pool)
        if case not in ("missing", "empty"):
            await stream.chat(pool, "oc_test", kind=case if case in ("p2p", "unknown") else "group")
            if case == "stream_excluded":
                await pool.execute("UPDATE source_chats SET inclusion='excluded',exclusion_reason='operator' WHERE chat_id='oc_test'")
            elif case == "extraction_excluded":
                await pool.execute("UPDATE source_chats SET extraction='excluded',extraction_excluded_at=now() WHERE chat_id='oc_test'")
        async with v1_api(tmp_path, pool) as call:
            for action in ("put", "preview"):
                status, answer = await call(f"intent_group_reads.{action}", {"chat_id": "" if case == "empty" else "oc_test"})
                assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await pool.fetchval("SELECT count(*) FROM intent_group_reads") == 0
    run_db(check, monkeypatch, timeout=30)



def test_enabling_group_reads_never_moves_a_group_before_its_added_at(monkeypatch):
    from datetime import timedelta
    from looper.agents import chat_settings, intents

    monkeypatch.setattr(chat_settings, "_delivery_enabled", True)

    async def check(pool):
        await stream.source(pool)
        await stream.chat(pool, "oc_added_later")
        # Model an addition newer than the enabling transaction's clock snapshot.
        added = datetime.now(timezone.utc) + timedelta(minutes=1)
        await pool.execute("INSERT INTO intent_group_reads(source_id,chat_id,added_at,read_from) "
                           "VALUES($1,'oc_added_later',$2,$2)", stream.SOURCE, added)
        result = await intents.group_read_settings_put(True, db=pool)
        assert result["enabled"]
        assert result["groups"][0]["read_from"] == added.isoformat()
    run_db(check, monkeypatch, timeout=30)



def test_turning_group_reads_off_waits_for_the_current_store_to_commit(monkeypatch):
    import asyncio
    import json
    from datetime import timedelta
    from looper.agents import extraction, intents, leads_model

    async def check(pool):
        now = datetime.now(timezone.utc)
        await stream.source(pool)
        seq = await stream.add(pool, "Synthetic requirement", chat_id="oc_writing", sent_at=now)
        await pool.execute("INSERT INTO intent_group_reads(source_id,chat_id,added_at,read_from) "
                           "VALUES($1,'oc_writing',$2,$2)", stream.SOURCE, now - timedelta(minutes=1))
        await pool.execute("INSERT INTO system_settings(key,value) VALUES('intents.group_read.enabled','true')")
        parsed = extraction.parse("intents", json.dumps({"intents": [{"type": "dev_task", "title": "Synthetic task",
                                  "summary": "Synthetic", "confidence": "high", "sources": ["m1"]}]}),
                                  {"m1": seq}, now=now, valid_days=90, enabled_types={"dev_task"}, app_seqs=(),
                                  group_seqs={seq})
        group = extraction.GroupStore(frozenset({seq}), {"oc_writing": 1}, {}, now.date(),
                                      {"group_read": 1, "group_over_cap": 0})
        closing = None
        try:
            async with pool.acquire() as conn, conn.transaction():
                counts = await extraction.store_intents(conn, parsed, source_id=stream.SOURCE, schedule_id=None,
                    run_id=None, record_id=None, now=now, more=False, group=group)
                assert counts["new"] == 1
                writer_pid = await conn.fetchval("SELECT pg_backend_pid()")
                closing = asyncio.create_task(intents.group_read_settings_put(False, db=pool))

                async def wait_for_blocked_close():
                    while not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                                                  "WHERE $1=ANY(pg_blocking_pids(pid)))", writer_pid):
                        if closing.done():
                            await closing
                            pytest.fail("closing returned before the current store committed")
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_for_blocked_close(), timeout=5)
                assert not closing.done()
                assert await leads_model.group_read_enabled(pool)
            answer = await asyncio.wait_for(closing, timeout=5)
            assert not answer["enabled"]
            assert await pool.fetchval("SELECT count(*) FROM intents WHERE via='followed_group'") == 1
        finally:
            if closing is not None and not closing.done():
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
    run_db(check, monkeypatch, timeout=20)
