"""Recognition is offered only to the operator and begins only after a confirmed preview."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from looper.agents import intent_recognition
from looper.agents.ops import Call, ErrorCode, OpError, Turn
from looper.agents.ops import intents_recognize as tool
from looper.agents.ops.service import ManagerOps, Platform
from tests.db_support import run_with_channel_configs
from tests.subscription_support import SOURCE, chat, source


async def _call(ops, turn, name, arguments):
    binding = f"b-{turn.message_id}"
    ops.begin_turn(turn, binding=binding)
    try:
        return await ops.call(name, arguments, binding=binding)
    finally:
        ops.end_turn(turn)


@pytest.mark.parametrize("channel", ["lark", "web"])
def test_recognize_tool_runs_only_in_the_operators_turns_and_asks_first(monkeypatch, channel):
    async def test(pool):
        await source(pool)
        await chat(pool, "oc_synthetic", name="Synthetic product")
        await pool.execute("INSERT INTO system_settings (key,value) VALUES "
                           "('intents.enabled','true'),('intents.on_request.enabled','true')")
        await pool.execute("UPDATE channel_configs SET config = "
                           "'{\"operator_sender_id\":\"ou_synthetic\",\"allowed_open_ids\":[\"ou_synthetic\"]}' "
                           "WHERE channel_id='channel-lark'")
        ran = []
        bounds = {"range_from": "2026-11-01T00:00:00+00:00", "range_to": "2026-11-08T00:00:00+00:00"}

        async def preview(**fields):
            return {**bounds, "chat": {"chat_id": fields["chat_id"], "name": "Synthetic product"},
                    "events": 42, "effective_from": bounds["range_from"], "truncated": None,
                    "estimate_usd": [0.01, 0.05], "schedule_id": "s"}

        async def begin(**fields):
            ran.append(fields)
            return {"recognition": {"id": "R-1"}}

        monkeypatch.setattr(intent_recognition, "preview", preview)
        monkeypatch.setattr(intent_recognition, "service", lambda: SimpleNamespace(begin=begin))
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok")
        ops._working_at = ops._clock()
        for turn in (Turn(channel="lark", channel_id="channel-lark", chat="oc_group", sender="ou_synthetic",
                          group=True, message_id="group"),
                     Turn(channel="lark", channel_id="channel-lark", chat="oc_dm", sender="ou_other", message_id="other"),
                     Turn(channel="web", chat="web", client_id="api-client", message_id="api"),
                     Turn(channel="web", chat="web")):
            assert tool.TOOL_NAME not in {item["name"] for item in await ops.current_listing(turn)}
            answer = await _call(ops, turn, tool.TOOL_NAME, {"action": "preview", "chat": "Synthetic product"})
            assert answer["error"]["code"] == "forbidden", answer
        own = Turn(channel=channel, channel_id="channel-lark" if channel == "lark" else None,
                   chat="oc_dm" if channel == "lark" else "web", sender="ou_synthetic", message_id="ask")
        assert tool.TOOL_NAME in {item["name"] for item in await ops.current_listing(own)}
        asked = await _call(ops, own, tool.TOOL_NAME, {"action": "start", "chat": "Synthetic product"})
        assert asked["status"] == "needs_confirmation" and ran == []
        assert "42" in own.blocks[0] and "$0.01–$0.05" in own.blocks[0] and "上限 $1" in own.blocks[0]
        confirming = Turn(channel=own.channel, channel_id=own.channel_id, chat=own.chat, sender=own.sender,
                          message_id="confirm")
        confirmed = await _call(ops, confirming, "looper_confirm", {"confirmation_id": asked["confirmation_id"]})
        assert confirmed["status"] == "ok", confirmed
        assert len(ran) == 1 and ran[0] == {
            "source_id": SOURCE, "chat_id": "oc_synthetic", "since": datetime(2026, 11, 1, tzinfo=timezone.utc),
            "until": datetime(2026, 11, 8, tzinfo=timezone.utc), "requested_via": "manager",
            "reply": ("channel-lark", "oc_dm") if channel == "lark" else None}
        await pool.execute("UPDATE system_settings SET value='false' WHERE key='intents.on_request.enabled'")
        assert tool.TOOL_NAME not in {item["name"] for item in await ops.current_listing(own)}

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("guard", ["dispatch", "handler"])
def test_recognize_tool_operator_guards_work_independently(monkeypatch, guard):
    async def test(pool):
        await pool.execute("INSERT INTO system_settings (key,value) VALUES "
                           "('intents.enabled','true'),('intents.on_request.enabled','true')")
        await pool.execute("UPDATE channel_configs SET config=$1::jsonb WHERE channel_id='channel-lark'",
                           '{"operator_sender_id":"ou_synthetic"}')
        result = {"recognition": {"id": "R-1"}}

        async def status(recognition):
            return result

        async def unchecked_handler(call):
            return result

        monkeypatch.setattr(intent_recognition, "status", status)
        if guard == "dispatch":
            # Bypass the handler's check so only ManagerOps can refuse the call.
            monkeypatch.setattr(tool, "TOOL", replace(tool.TOOL, run=unchecked_handler))
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok")
        ops._working_at = ops._clock()
        arguments = {"action": "status", "recognition": "R-1"}
        for turn in (Turn(channel="lark", channel_id="channel-lark", chat="oc_group", sender="ou_synthetic",
                          group=True, message_id="group"),
                     Turn(channel="lark", channel_id="channel-lark", chat="oc_dm", sender="ou_other", message_id="other"),
                     Turn(channel="web", chat="web", client_id="api-client", message_id="api"),
                     Turn(channel="web", chat="web")):
            if guard == "dispatch":
                answer = await _call(ops, turn, tool.TOOL_NAME, arguments)
                assert answer["error"]["code"] == "forbidden", answer
            else:
                # Direct execution models confirmation, which bypasses ManagerOps.call's audience check.
                with pytest.raises(OpError) as refused:
                    await tool.run(Call(arguments, turn, ops.platform))
                assert refused.value.code is ErrorCode.FORBIDDEN
        own = Turn(channel="web", chat="web", message_id="operator")
        if guard == "dispatch":
            assert await _call(ops, own, tool.TOOL_NAME, arguments) == {"status": "ok", "result": result}
        else:
            assert await tool.run(Call(arguments, own, ops.platform)) == result

    run_with_channel_configs(test, monkeypatch)


def test_recognize_tool_refuses_ambiguous_chats_and_invalid_times(monkeypatch):
    async def test(pool):
        await source(pool)
        await chat(pool, "oc_one", name="Synthetic one")
        await chat(pool, "oc_two", kind="topic", name="Synthetic two")
        await pool.execute("INSERT INTO system_settings (key,value) VALUES "
                           "('intents.enabled','true'),('intents.on_request.enabled','true')")
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok")
        ops._working_at = ops._clock()
        for arguments in ({"action": "preview", "chat": "Synthetic"},
                          {"action": "preview", "chat": "oc_one", "since": "yesterday"},
                          {"action": "preview", "chat": "oc_one", "since": "2026-11-01"},
                          {"action": "status", "recognition": "R-no"}):
            answer = await _call(ops, Turn(channel="web", chat="c", message_id="ask"), tool.TOOL_NAME, arguments)
            assert answer["error"]["code"] == "invalid", answer
            if arguments.get("chat") == "Synthetic":
                assert "Synthetic one" in answer["error"]["message"] and "Synthetic two" in answer["error"]["message"]
        for recognition in ("R-999", 999):
            missing = await _call(ops, Turn(channel="web", chat="c", message_id="status"), tool.TOOL_NAME,
                                  {"action": "status", "recognition": recognition})
            assert missing["error"]["code"] == "not_found", missing

    run_with_channel_configs(test, monkeypatch)
