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
        assert await intent_routing.route(envelope(text), platform=object()) is None
    run_db(check, monkeypatch)


@pytest.mark.parametrize("text", ["建I1", "建 i-0001", " 建\u3000I-0001\u30002 "])
def test_build_grammar_calls_the_service_with_direct_chat_origin(monkeypatch, text):
    async def check(pool):
        iid = await seed(pool)
        build = AsyncMock(return_value={"intent": {"title": "Synthetic task"}, "task_id": "task-synthetic"})
        monkeypatch.setattr(intents, "build", build)
        platform = object()
        result = await intent_routing.route(envelope(text), platform=platform)
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
        result = await intent_routing.route(envelope("驳回 I-1", channel=channel, sender=sender,
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
        result = await intent_routing.route(envelope("驳回\u3000不需要处理", quote="om_quote"), platform=object())
        assert result.reply == "I-0001 已驳回。"
        row = await pool.fetchrow("SELECT * FROM intents WHERE id=$1", iid)
        assert (row["state"], row["reject_reason"], row["handled_via"]) == ("rejected", "不需要处理", "lark")
    run_db(check, monkeypatch)


def test_explicit_id_wins_and_unknown_id_is_handled(monkeypatch):
    async def check(pool):
        iid = await seed(pool)
        await pool.execute("INSERT INTO intent_posts (intent_id,kind,channel_id,chat,text,state,external_id) "
                           "VALUES ($1,'push','channel-lark',$2,'Push','sent','om_quote')", iid, stream.DM)
        result = await intent_routing.route(envelope("驳回 I-42", quote="om_quote"), platform=object())
        assert result.reply == "没有 I-0042 这条意图。"
        assert await pool.fetchval("SELECT state FROM intents") == "pending"
    run_db(check, monkeypatch)


def test_dashboard_operator_and_default_off(monkeypatch):
    async def check(pool):
        await seed(pool)
        result = await intent_routing.route(envelope("驳回 I-1", channel="web"), platform=object())
        assert result.reply == "I-0001 已驳回。"
        await pool.execute("DELETE FROM system_settings WHERE key='intents.enabled'")
        assert await intent_routing.route(envelope("建 I-1"), platform=object()) is None
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
        result = await intent_routing.route(envelope(f"建 I-{digits}", sender=sender), platform=object())
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
        result = await intent_routing.route(envelope("驳回", channel="telegram", sender="700001", quote="42"),
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
        result = await intent_routing.route(envelope("建 I-1"), platform=object())
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
        first = await intent_routing.route(envelope("建 I-1"), platform=platform)
        task = await pool.fetchval("SELECT task_id FROM intents WHERE id=$1", iid)
        assert first.reply == f"任务 {task} 已建，但启动请求失败，请重试「建 I-0001」。"
        second = await intent_routing.route(envelope("建 I-1"), platform=platform)
        assert second.reply == f"I-0001 已经建过任务 {task}。"
        assert publish.await_count == 2
        refused = await intent_routing.route(envelope("驳回 I-1"), platform=platform)
        assert refused.reply == f"I-0001 已经建过任务 {task}。" and publish.await_count == 2
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
    run_db(check, monkeypatch, timeout=30)


def test_unbound_project_candidate_and_directory_errors_are_code_replies(monkeypatch):
    from looper.db import projects

    async def check(pool):
        iid = await seed(pool)
        platform = object()
        result = await intent_routing.route(envelope("建 I-1"), platform=platform)
        assert result.reply == "请到意图页选择项目。"
        project = await projects.create_project(name="Synthetic project", work_dir="")
        await pool.execute("INSERT INTO tasks(id,project_id,title,requirement,work_dir,status) "
                           "VALUES ('task-candidate',$1,'Synthetic','Synthetic','/tmp/synthetic','completed')",
                           project["id"])
        await pool.execute("INSERT INTO intent_posts(intent_id,kind,channel_id,chat,text,state,candidates) "
                           "VALUES ($1,'push','channel-lark',$2,'Synthetic push','sent',$3)",
                           iid, stream.DM, [project["id"]])
        result = await intent_routing.route(envelope("建 I-1"), platform=platform)
        assert result.reply == "项目：这个会话没有绑定项目。候选：1. Synthetic project\n回复「建 I-0001 <编号>」"
        result = await intent_routing.route(envelope("建 I-1 2"), platform=platform)
        assert result.reply == "没有第 2 个候选项目。"
        result = await intent_routing.route(envelope("建 I-1 1"), platform=platform)
        assert result.reply == "项目 Synthetic project 没有默认目录，请到意图页建任务。"
    run_db(check, monkeypatch, timeout=30)
