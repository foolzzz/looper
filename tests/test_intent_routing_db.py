"""Intent commands are exact operator actions, resolved before the Manager model."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import intent_routing, intents
from looper.agents.message import AgentAddress, MessageEnvelope, MessageType, manager_address
from tests import leads_support as leads, subscription_support as stream
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def envelope(text, *, channel="lark", sender=stream.DM, group=False, quote=None, client=None):
    chat = "oc_syntheticgroup" if group else "oc_syntheticdm" if channel == "lark" else sender
    context = {"chat_id": chat, "sender_id": sender,
               "chat_type": "group" if group else "p2p", "message_id": "om_command"}
    if quote:
        context["reply_to_message_id" if channel == "telegram" else "reply_to"] = quote
    if client:
        context["client"] = {"id": client, "name": "Test client"}
    return MessageEnvelope.create(source=AgentAddress("channel", f"channel-{channel}", channel),
        target=manager_address("manager"), message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": context})


async def route(*args, **kwargs):
    result = await intent_routing.route(*args, **kwargs)
    if result is not None and getattr(result, "action", None) is not None:
        return intent_routing.RouteResult(await result.action())
    return result


async def seed(pool):
    await stream.source(pool)
    await stream.lark_targets(pool)
    await pool.execute("UPDATE channel_configs SET config = config || $1::jsonb "
                       "WHERE channel_id = 'channel-lark'", '{"operator_sender_id":"' + stream.DM + '"}')
    await pool.execute("INSERT INTO system_settings (key,value) VALUES ('intents.enabled','true')")
    event = await stream.add(pool, "Synthetic request", sent_at=datetime.now(timezone.utc))
    return await leads.intent(pool, [event])


@pytest.mark.parametrize("text", ["建", "建 2", "请建 I-0001", "我说建 I-0001", "建 I-1 后开始", "建 I-1 100"])
def test_unquoted_bare_and_non_commands_are_left_to_the_model(monkeypatch, text):
    async def check(pool):
        await seed(pool)
        assert await route(envelope(text), platform=object()) is None
    run_db(check, monkeypatch)


@pytest.mark.parametrize("text", ["建I1", "建 i-0001", " 建\u3000I-0001\u30002 "])
def test_build_grammar_calls_the_service_with_direct_chat_origin(monkeypatch, text):
    async def check(pool):
        iid = await seed(pool)
        build = AsyncMock(return_value={"intent": {"title": "Synthetic task"}, "task_id": "task-synthetic"})
        monkeypatch.setattr(intents, "build", build)
        platform = object()
        result = await route(envelope(text), platform=platform)
        assert result.reply == "已建任务 task-synthetic：Synthetic task，已开始。"
        args = build.call_args
        assert args.args == (iid,)
        assert args.kwargs["platform"] is platform
        assert args.kwargs["origin"]["chat"] == "oc_syntheticdm"
        assert args.kwargs["origin"]["group"] is False
        assert args.kwargs["candidate"] == (2 if "2" in text else None)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("group,sender,channel,client", [(True, stream.DM, "lark", None),
    (False, "ou_other_synthetic", "lark", None), (False, None, "web", "client-synthetic")])
def test_only_the_operator_may_handle_intents(monkeypatch, group, sender, channel, client):
    async def check(pool):
        await seed(pool)
        result = await route(envelope("驳回 I-1", channel=channel, sender=sender,
                                                    group=group, client=client), platform=object())
        assert result.reply == "意图只能由你本人在私聊或意图页处理。"
        assert await pool.fetchval("SELECT state FROM intents") == "pending"
    run_db(check, monkeypatch)


@pytest.mark.parametrize("progress", [False, True])
def test_quote_resolves_push_or_progress_and_rejects_with_reason(monkeypatch, progress):
    async def check(pool):
        iid = await seed(pool)
        posted = iid
        if progress:
            event = await stream.add(pool, "Synthetic progress", sent_at=datetime.now(timezone.utc))
            posted = await leads.intent(pool, [event], state="merged", merged_into=iid)
        await pool.execute("INSERT INTO intent_posts (intent_id,kind,channel_id,chat,text,state,external_id) "
                           "VALUES ($1,$2,'channel-lark',$3,'Synthetic push','sent','om_quote')",
                           posted, "progress" if progress else "push", stream.DM)
        result = await route(envelope("驳回\u3000不需要处理", quote="om_quote"), platform=object())
        assert result.reply == "I-0001 已驳回。"
        row = await pool.fetchrow("SELECT * FROM intents WHERE id=$1", iid)
        assert (row["state"], row["reject_reason"], row["handled_via"]) == ("rejected", "不需要处理", "lark")
    run_db(check, monkeypatch)


def test_explicit_id_wins_and_unknown_id_is_handled(monkeypatch):
    async def check(pool):
        iid = await seed(pool)
        await pool.execute("INSERT INTO intent_posts (intent_id,kind,channel_id,chat,text,state,external_id) "
                           "VALUES ($1,'push','channel-lark',$2,'Push','sent','om_quote')", iid, stream.DM)
        result = await route(envelope("驳回 I-42", quote="om_quote"), platform=object())
        assert result.reply == "没有 I-0042 这条意图。"
        assert await pool.fetchval("SELECT state FROM intents") == "pending"
    run_db(check, monkeypatch)


def test_dashboard_operator_and_default_off(monkeypatch):
    async def check(pool):
        await seed(pool)
        result = await route(envelope("驳回 I-1", channel="web"), platform=object())
        assert result.reply == "I-0001 已驳回。"
        await pool.execute("DELETE FROM system_settings WHERE key='intents.enabled'")
        assert await route(envelope("建 I-1"), platform=object()) is None
    run_db(check, monkeypatch)


@pytest.mark.parametrize("enabled", [False, True])
def test_manager_keeps_disabled_commands_for_the_model_and_handles_enabled_replies(
        monkeypatch, tmp_path, agent_db, enabled):
    from looper.agents import leads_model
    from looper.agents.gate_routing import NOT_A_REPLY
    from tests.test_manager_new_session import _web
    from tests.test_manager_stop import _Session, _manager, _replies, _with_ops

    async def check():
        session = _Session(hold=False)
        manager, bus, queue = _manager(tmp_path, session)
        _with_ops(manager)
        manager._route_to_gate = AsyncMock(return_value=NOT_A_REPLY)
        manager._persist_user = AsyncMock()
        routed = AsyncMock(return_value=intent_routing.RouteResult("I-0001 已驳回。"))
        monkeypatch.setattr(intent_routing, "route", routed)
        if enabled:
            monkeypatch.setattr(leads_model, "intents_enabled", AsyncMock(return_value=True))
        await manager._handle(_web("驳回 I-1"))
        if enabled:
            routed.assert_awaited_once()
            assert not session.turns
            assert _replies(queue)[-1]["text"] == "I-0001 已驳回。"
        else:
            routed.assert_not_awaited()
            assert len(session.turns) == 1 and "驳回 I-1" in session.turns[0]
        await bus.shutdown()
    asyncio.run(check())


def test_intent_preparation_preserves_the_manager_stop_protocol():
    from tests.test_manager_stop_faults import test_preparation_awaits_only_through_steps

    test_preparation_awaits_only_through_steps()


@pytest.mark.parametrize("sender", [stream.DM, "ou_other_synthetic"])
def test_oversized_ids_validate_identity_before_not_found(monkeypatch, sender):
    async def check(pool):
        await seed(pool)
        digits = "9" * 5000
        result = await route(envelope(f"建 I-{digits}", sender=sender), platform=object())
        assert result.reply == (f"没有 I-{digits} 这条意图。" if sender == stream.DM
                                else "意图只能由你本人在私聊或意图页处理。")
    run_db(check, monkeypatch)


def test_telegram_quote_uses_its_own_bot_and_message_reference(monkeypatch):
    async def check(pool):
        iid = await seed(pool)
        await pool.execute("UPDATE channel_configs SET config = config || '{\"operator_sender_id\":\"700001\"}'::jsonb "
                           "WHERE channel_id='channel-telegram'")
        await pool.execute("INSERT INTO intent_posts (intent_id,kind,channel_id,chat,text,state,external_id) "
                           "VALUES ($1,'push','channel-telegram','700001','Synthetic push','sent','42')", iid)
        result = await route(envelope("驳回", channel="telegram", sender="700001", quote="42"),
                                            platform=object())
        assert result.reply == "I-0001 已驳回。"
        assert await pool.fetchval("SELECT handled_via FROM intents") == "telegram"
    run_db(check, monkeypatch)


@pytest.mark.parametrize("state", ["tasked", "rejected", "merged"])
def test_terminal_states_have_frozen_replies(monkeypatch, state):
    from looper.db import projects

    async def check(pool):
        iid = await seed(pool)
        if state == "tasked":
            project = await projects.create_project(name="Synthetic project", work_dir="/tmp/synthetic")
            await pool.execute("INSERT INTO tasks (id,project_id,title,requirement,work_dir,status) "
                               "VALUES ('task-synthetic',$1,'Synthetic','Synthetic','/tmp/synthetic','completed')",
                               project["id"])
            await pool.execute("UPDATE intents SET state='tasked',task_id='task-synthetic',handled_at=now(),"
                               "handled_via='lark' WHERE id=$1", iid)
            expected = "I-0001 已经建过任务 task-synthetic。"
        elif state == "rejected":
            await intents.reject(iid, via="lark")
            expected = "I-0001 已驳回过。"
        else:
            event = await stream.add(pool, "Synthetic second", sent_at=datetime.now(timezone.utc))
            target = await leads.intent(pool, [event])
            await pool.execute("UPDATE intents SET state='merged',merged_into=$2 WHERE id=$1", iid, target)
            expected = "I-0001 已合并到 I-0002，请处理那一条。"
        result = await route(envelope("建 I-1"), platform=object())
        assert result.reply == expected
    run_db(check, monkeypatch)


def test_build_retries_the_existing_task_after_start_delivery_fails(monkeypatch, tmp_path):
    from looper.db import projects

    async def check(pool):
        iid = await seed(pool)
        project = await projects.create_project(name="Synthetic project", work_dir=str(tmp_path))
        await pool.execute("UPDATE intents SET project_id=$2 WHERE id=$1", iid, project["id"])
        publish = AsyncMock(side_effect=[OSError("synthetic bus failure"), None])
        platform = SimpleNamespace(bus=SimpleNamespace(publish=publish))
        first = await route(envelope("建 I-1"), platform=platform)
        task = await pool.fetchval("SELECT task_id FROM intents WHERE id=$1", iid)
        assert first.reply == f"任务 {task} 已建，但启动请求失败，请重试「建 I-0001」。"
        second = await route(envelope("建 I-1"), platform=platform)
        assert second.reply == f"I-0001 已经建过任务 {task}。"
        assert publish.await_count == 2
        refused = await route(envelope("驳回 I-1"), platform=platform)
        assert refused.reply == f"I-0001 已经建过任务 {task}。" and publish.await_count == 2
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
    run_db(check, monkeypatch, timeout=30)


def test_unbound_project_candidate_and_directory_errors_are_code_replies(monkeypatch):
    from looper.db import projects

    async def check(pool):
        iid = await seed(pool)
        platform = object()
        result = await route(envelope("建 I-1"), platform=platform)
        assert result.reply == "请到意图页选择项目。"
        project = await projects.create_project(name="Synthetic project", work_dir="")
        await pool.execute("INSERT INTO tasks(id,project_id,title,requirement,work_dir,status) "
                           "VALUES ('task-candidate',$1,'Synthetic','Synthetic','/tmp/synthetic','completed')",
                           project["id"])
        # A direct command can arrive before the first scheduled push.
        result = await route(envelope("建 I-1"), platform=platform)
        assert "1. Synthetic project" in result.reply
        result = await route(envelope("建 I-1 1"), platform=platform)
        assert result.reply == "项目 Synthetic project 没有默认目录，请到意图页建任务。"
        await pool.execute("INSERT INTO intent_posts(intent_id,kind,channel_id,chat,text,state,candidates) "
                           "VALUES ($1,'push','channel-lark',$2,'Synthetic push','sent',$3)",
                           iid, stream.DM, [project["id"]])
        result = await route(envelope("建 I-1"), platform=platform)
        assert result.reply == "项目：这个会话没有绑定项目。候选：1. Synthetic project\n回复「建 I-0001 <编号>」"
        result = await route(envelope("建 I-1 2"), platform=platform)
        assert result.reply == "没有第 2 个候选项目。"
        result = await route(envelope("建 I-1 1"), platform=platform)
        assert result.reply == "项目 Synthetic project 没有默认目录，请到意图页建任务。"
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("error", ["deleted_candidate", "profile", "requirement", "value"])
def test_build_errors_reply_without_creating_a_task(monkeypatch, tmp_path, error):
    from looper.agents import projects, workflows

    async def check(pool):
        iid = await seed(pool)
        await pool.execute("INSERT INTO projects(id,name,work_dir) VALUES('project-candidate','Synthetic',$1)", str(tmp_path))
        await pool.execute("UPDATE intents SET project_id='project-candidate' WHERE id=$1", iid)
        if error == "deleted_candidate":
            await pool.execute("INSERT INTO intent_posts(intent_id,kind,channel_id,chat,text,candidates,state) "
                               "VALUES($1,'push','channel-lark',$2,'Synthetic',ARRAY['project-candidate'],'sent')", iid, stream.DM)
            await pool.execute("DELETE FROM projects WHERE id='project-candidate'")
        elif error == "profile":
            await pool.execute("INSERT INTO workflows(key,name,definition) VALUES('wf','wf','{}')")
            await intents.type_save("dev_task", default_workflow="wf")
            async def refuse(*args, **kwargs):
                raise workflows.WorkflowError("synthetic unavailable Agent Profile")
            monkeypatch.setattr(workflows, "check_profiles", refuse)
        elif error == "requirement":
            await pool.execute("UPDATE source_chats SET name=$1", "x" * (projects.TASK_REQUIREMENT_LIMIT + 1))
        else:
            monkeypatch.setattr(projects, "create_task_core", AsyncMock(side_effect=ValueError("synthetic validation failure")))
        platform = SimpleNamespace(bus=SimpleNamespace(publish=AsyncMock()))
        result = await route(envelope(f"建 I-{iid:04d}" + (" 1" if error == "deleted_candidate" else "")), platform=platform)
        assert result.reply
        if error == "deleted_candidate":
            assert result.reply == "第 1 个候选项目已删除。"
        else:
            assert result.reply.startswith("无法建任务：")
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT state FROM intents WHERE id=$1", iid) == "pending"
        platform.bus.publish.assert_not_awaited()
    run_db(check, monkeypatch)


def test_reject_expired_intent_gives_chinese_build_instruction(monkeypatch):
    async def check(pool):
        iid = await seed(pool)
        async with pool.acquire() as conn, conn.transaction():
            await intents.store.transition(conn, iid, "expired", via="system", now=datetime.now(timezone.utc))
        result = await route(envelope("驳回 I-1"), platform=object())
        assert result.reply == "I-0001 已过期，要建任务请回『建 I-0001』。"
    run_db(check, monkeypatch)


def test_deleted_candidates_keep_the_original_push_numbers(monkeypatch, tmp_path):
    async def check(pool):
        iid = await seed(pool)
        await pool.execute("INSERT INTO projects(id,name,work_dir) VALUES('gone','Gone',$1),('kept','Kept',$1)", str(tmp_path))
        await pool.execute("INSERT INTO intent_posts(intent_id,kind,channel_id,chat,text,candidates,state) "
                           "VALUES($1,'push','channel-lark',$2,'Synthetic',ARRAY['gone','kept'],'sent')", iid, stream.DM)
        await pool.execute("DELETE FROM projects WHERE id='gone'")
        platform = SimpleNamespace(bus=SimpleNamespace(publish=AsyncMock()))
        result = await route(envelope("建 I-1"), platform=platform)
        assert "1. （已删除）  2. Kept" in result.reply
        result = await route(envelope("建 I-1 2"), platform=platform)
        assert "已建任务" in result.reply
        assert await pool.fetchval("SELECT project_id FROM tasks") == "kept"
    run_db(check, monkeypatch)


@pytest.mark.parametrize("command", ["建 I-1", "驳回 I-1"])
def test_intent_route_prepares_without_writes(monkeypatch, tmp_path, command):
    async def check(pool):
        iid = await seed(pool)
        await pool.execute("INSERT INTO projects(id,name,work_dir) VALUES('prepared','Prepared',$1)", str(tmp_path))
        await pool.execute("UPDATE intents SET project_id='prepared' WHERE id=$1", iid)
        platform = SimpleNamespace(bus=SimpleNamespace(publish=AsyncMock()))
        final = AsyncMock()
        monkeypatch.setattr(intents, "_final", final)
        result = await intent_routing.route(envelope(command), platform=platform)
        assert await pool.fetchval("SELECT state FROM intents WHERE id=$1", iid) == "pending"
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        platform.bus.publish.assert_not_awaited()
        final.assert_not_awaited()
        assert result.action is not None
        reply = await result.action()
        assert "已建任务" in reply if command.startswith("建") else "已驳回" in reply
        final.assert_awaited_once_with(iid)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("command", ["建 I-1", "驳回 I-1"])
@pytest.mark.parametrize("interruption", ["stop", "timeout"])
def test_manager_stop_or_timeout_before_acting_never_writes_intents(
        monkeypatch, tmp_path, agent_db, command, interruption):
    from looper.agents import chat_settings, leads_model
    from looper.agents.gate_routing import NOT_A_REPLY
    from looper.agents.tiers.manager import StopResult
    from looper.agents.tiers.turn_token import TurnToken
    from tests.test_manager_new_session import _web
    from tests.test_manager_stop import _Session, _manager, _replies, _with_ops

    async def check(pool):
        await seed(pool)
        monkeypatch.setattr(chat_settings, "_stop_enabled", True)
        monkeypatch.setattr(leads_model, "intents_enabled", AsyncMock(return_value=True))
        manager, bus, queue = _manager(tmp_path, _Session(hold=False))
        _with_ops(manager)
        manager._route_to_gate = AsyncMock(return_value=NOT_A_REPLY)
        write = AsyncMock(return_value={"intent": {"title": "Synthetic"}, "task_id": "task-synthetic"})
        monkeypatch.setattr(intents, "build" if command.startswith("建") else "reject", write)
        reached, release = asyncio.Event(), asyncio.Event()
        async def persist(*args):
            reached.set()
            await release.wait()
        manager._persist_user = persist
        keep = TurnToken.keep
        if interruption == "timeout":
            async def short_timeout(self, name, aw, timeout, **kwargs):
                return await keep(self, name, aw, 0.02 if name == "persist_user_message" else timeout, **kwargs)
            monkeypatch.setattr(TurnToken, "keep", short_timeout)
        message = _web(command)
        handling = asyncio.create_task(manager._handle(message))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            if interruption == "stop":
                assert await manager.stop_turn(message.message_id, via="web") is StopResult.STOPPING
                release.set()
            await asyncio.wait_for(handling, 5)
            (reply,) = _replies(queue)
            assert reply["outcome"] == ("stopped" if interruption == "stop" else "failed")
            write.assert_not_awaited()
            assert await pool.fetchval("SELECT state FROM intents") == "pending"
        finally:
            release.set()
            if not handling.done():
                handling.cancel()
            await asyncio.gather(handling, return_exceptions=True)
            await bus.shutdown()
    run_db(check, monkeypatch)
