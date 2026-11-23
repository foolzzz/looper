"""Manager metrics through a real fake-runtime turn and PostgreSQL recorder."""
import asyncio
import json
from datetime import datetime, timezone
from dataclasses import replace
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from fake_claude import fake_claude
from manager_mcp_stand_in import manager_endpoint
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("delivery,channel,replayed,disable_mid", [
    ("reply","web",False,False), ("empty","web",False,False), ("failed","web",False,False),
    ("over_cap","web",False,False), ("stopped","web",False,False), ("reply","lark",False,False),
    ("reply","web",True,False), ("reply","web",False,True),
    ("reply","lark_pending",False,False), ("reply","lark_none",False,False),
    ("db_failure","web",False,False)])
def test_manager_turn_records_delivery(delivery, channel, replayed, disable_mid, monkeypatch, tmp_path):
    from looper.agents import chat_settings
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    write_failure = delivery == "db_failure"
    if write_failure:
        delivery = "reply"

    async def test(pool):
        if write_failure:
            from looper.agents.claude_cli import ClaudeRunRecorder
            annotate = ClaudeRunRecorder.annotate
            async def fail_metrics(recorder, metadata):
                if "turn_metrics" in metadata:
                    raise OSError("synthetic metrics write unavailable")
                await annotate(recorder, metadata)
            monkeypatch.setattr(ClaudeRunRecorder, "annotate", fail_metrics)
        monkeypatch.setattr(chat_settings, "_metrics_enabled", True, raising=False)
        text = "hello" if delivery in {"reply", "stopped"} else ""
        result = {"type": "result", "subtype": "error_max_budget_usd" if delivery == "over_cap" else "success",
                  "is_error": delivery in {"failed", "over_cap"}, "result": text, "session_id": "metrics-session"}
        body = "import sys,json\nfor line in sys.stdin:\n"
        body += " print(json.dumps({'type':'system','subtype':'init','session_id':'metrics-session'}),flush=True)\n"
        body += f" print(json.dumps({{'type':'assistant','message':{{'content':[{{'type':'text','text':{text!r}}}]}}}}),flush=True)\n"
        body += f" print(json.dumps({result!r}),flush=True)\n"
        manager = ManagerAgent(config=ManagerConfig(claude_bin=fake_claude(tmp_path / "runtime", body), work_dir=tmp_path), runtime_root=tmp_path)
        if disable_mid or delivery == "stopped":
            converse = manager._converse
            async def end(token, prepared):
                result = await converse(token, prepared)
                if disable_mid:
                    chat_settings.set_metrics_enabled(False)
                if delivery == "stopped":
                    from looper.agents.tiers.turn_token import TurnStopped
                    raise TurnStopped()
                return result
            monkeypatch.setattr(manager, "_converse", end)
        bus = MessageBus(runtime_root=tmp_path)
        manager._bus = bus
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path):
            envelope = MessageEnvelope.create(source=channel_address("web" if channel == "web" else "lark"), target=manager_address("manager"),
                message_type=MessageType.USER_MESSAGE, payload={"text":"hi", "channel_context":{"session_id":"chat-metrics"}}, correlation_id="metrics-turn")
            if channel != "web":
                envelope.payload["channel_context"] = {"chat_id":"chat-metrics","chat_type":"p2p","sender_id":"operator-metrics","message_id":"message-metrics"}
                from looper.agents.channels.status_line import Reactions
                if channel == "lark":
                    await Reactions(channel="channel-lark", react=AsyncMock())._call("chat-metrics", "message-metrics")
                elif channel == "lark_none":
                    from looper.agents import turn_metrics
                    turn_metrics.ack_board.ack("channel-lark", "message-metrics", None)
                else:
                    from looper.agents import turn_metrics
                    board = turn_metrics.AckBoard()
                    board.ack("channel-lark", "message-metrics", datetime.now(timezone.utc))
                    for index in range(2000):
                        board.ack("channel-lark", str(index), None)
                    monkeypatch.setattr(turn_metrics, "ack_board", board)
            if replayed:
                envelope = replace(envelope, timestamp="2020-01-01T00:00:00+00:00")
            await manager._handle(envelope)
            from looper.agents import turn_metrics
            await asyncio.gather(*turn_metrics._pending)
            await manager.stop()
        await bus.shutdown()
        metadata = json.loads(await pool.fetchval("SELECT metadata::text FROM llm_records WHERE agent_id = 'manager' ORDER BY id DESC LIMIT 1"))
        if write_failure:
            assert "turn_metrics" not in metadata
            reply = [item for item in bus.get_queue("channel-web")._queue if item.message_type is MessageType.AGENT_REPLY]
            assert reply[-1].payload["text"] == "hello"
            return
        metrics = metadata["turn_metrics"]
        assert set(metrics) == {"v", "origin", "chat", "received_at", "ack", "ack_ms", "first_reply_ms", "tool_calls", "delivery", "replayed", "queued_behind"}
        assert metrics["queued_behind"] == "none"
        assert metrics["delivery"] == delivery
        assert metrics["chat"] == ("dashboard" if channel == "web" else "direct")
        assert metrics["ack"] == {"web":"status", "lark":"reaction", "lark_pending":"pending", "lark_none":"none"}[channel]
        if channel in {"web", "lark"}:
            assert metrics["ack_ms"] >= 0
        else:
            assert metrics["ack_ms"] is None
        assert metrics["origin"] == "user" and metrics["replayed"] is replayed
        assert (metrics["first_reply_ms"] is not None) is (delivery in {"reply", "stopped"})
    run_db(test, monkeypatch, timeout=90)


@pytest.mark.parametrize("group,sender,targets", [(True,"operator",["group-metrics"]), (True,"other",[]), (False,"operator",["direct-metrics","operator"])])
def test_operator_reply_marks_only_the_operator_chat(group, sender, targets, monkeypatch, tmp_path):
    from looper.agents import operator_identity
    from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import proactive_deliveries

    async def test(pool):
        marker = AsyncMock()
        monkeypatch.setattr(proactive_deliveries,"mark_replied",marker)
        monkeypatch.setattr(operator_identity,"operator_sender_id",AsyncMock(return_value="operator"))
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path),runtime_root=tmp_path)
        envelope = MessageEnvelope.create(source=channel_address("lark","lark-metrics"),target=manager_address("manager"),message_type=MessageType.USER_MESSAGE,
            payload={"text":"hi","channel_context":{"chat_id":"group-metrics" if group else "direct-metrics", "chat_type":"group" if group else "p2p","sender_id":sender,"reply_to":"quoted-metrics"}})
        await manager._mark_metrics_replied(envelope)
        assert [call.args[1] for call in marker.await_args_list] == targets
        assert all(call.args[2] == "quoted-metrics" for call in marker.await_args_list)
    run_db(test,monkeypatch,timeout=30)


@pytest.mark.parametrize("enabled", [False, True])
def test_completion_metrics_only_when_enabled(enabled, monkeypatch, tmp_path):
    from datetime import timedelta
    from looper.agents import chat_settings, turn_metrics
    from looper.agents.completion_service import CompletionService
    from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
    from looper.db import projects, tasks
    from tests.api_manager_support import USAGE, _manager
    from tests.fake_model_api import ResponseScript, text_events

    async def test(pool):
        monkeypatch.setattr(chat_settings,"_metrics_enabled",enabled)
        monkeypatch.setattr(chat_settings,"_completion_turns",chat_settings.CompletionTurns.DM_ONLY)
        async with _manager(pool,tmp_path,monkeypatch,[ResponseScript(events=text_events("Delivered.",usage=USAGE))]) as chat:
            project = await projects.create_project(name="metrics",work_dir=str(tmp_path),pool=pool)
            await tasks.create_task(task_id="metrics-task",requirement="synthetic",project_id=project["id"],work_dir=str(tmp_path),status="completed",origin_thread="metrics-thread",pool=pool)
            envelope = MessageEnvelope.create(source=TASK_ROUTER_ADDRESS,target=MANAGER_ADDRESS,message_type=MessageType.GOAL_STATUS,
                payload={"task_id":"metrics-task","goal_id":"metrics-goal","status":"completed","summary":"Delivered."},
                metadata={"origin_thread":"metrics-thread","origin_channel_id":"channel-web"})
            await chat.manager._handle(envelope)
            service = CompletionService(bus=chat.bus,manager_running=lambda _: True,running_managers=lambda:["manager"])
            await service.run_once(now=datetime.now(timezone.utc)+timedelta(seconds=61))
            queue = chat.bus.get_queue("manager")
            while not queue.empty():
                await chat.manager._handle(queue.get_nowait())
            await asyncio.gather(*turn_metrics._pending)
            metadata = json.loads(await pool.fetchval("SELECT metadata::text FROM llm_records WHERE agent_id='manager' ORDER BY id DESC LIMIT 1"))
            assert ("turn_metrics" in metadata) is enabled
            if enabled:
                assert metadata["turn_metrics"] == {"origin":"completion","tool_calls":0,"delivery":"reply"}
    run_db(test,monkeypatch,timeout=90)


def test_a_resume_retry_records_each_model_turn_separately(monkeypatch, tmp_path):
    from looper.agents import chat_settings, turn_metrics
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import agent_sessions

    async def test(pool):
        monkeypatch.setattr(chat_settings,"_metrics_enabled",True)
        await agent_sessions.save_session("manager","gone",{},runtime_type="claude-code",pool=pool)
        body = """import sys,json
if '--resume' in sys.argv:
 sys.stdin.readline()
 print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'call-resume','name':'Read','input':{}}]}}),flush=True)
 print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'errors':['No conversation found with session ID: gone']}),flush=True)
 sys.exit(0)
for line in sys.stdin:
 print(json.dumps({'type':'system','subtype':'init','session_id':'fresh'}),flush=True)
 print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'hello','session_id':'fresh'}),flush=True)
"""
        manager = ManagerAgent(config=ManagerConfig(claude_bin=fake_claude(tmp_path/"runtime",body),work_dir=tmp_path),runtime_root=tmp_path)
        bus = MessageBus(runtime_root=tmp_path)
        manager._bus = bus
        async with manager_endpoint(manager,bus,SimpleNamespace(has_planner=lambda _:False),tmp_path):
            envelope = MessageEnvelope.create(source=channel_address("web"),target=manager_address("manager"),message_type=MessageType.USER_MESSAGE,payload={"text":"hi","channel_context":{"session_id":"retry-chat"}})
            await manager._handle(envelope)
            await asyncio.gather(*turn_metrics._pending)
            await manager.stop()
        await bus.shutdown()
        records = [json.loads(row["metadata"]) for row in await pool.fetch("SELECT metadata::text AS metadata FROM llm_records WHERE agent_id='manager' ORDER BY id")]
        assert [row["turn_metrics"]["delivery"] for row in records] == ["failed","reply"]
        assert [row["turn_metrics"]["tool_calls"] for row in records] == [1,0]
        assert records[0]["turn_metrics"]["first_reply_ms"] is None
        assert records[1]["turn_metrics"]["first_reply_ms"] is not None
    run_db(test,monkeypatch,timeout=90)
