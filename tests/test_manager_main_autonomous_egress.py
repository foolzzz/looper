"""Autonomous output stays in the operator's main conversation unless its recipient is the operator."""

from dataclasses import replace

import pytest

from looper.agents import session_routing
from looper.agents.agent_settings import Launch
from looper.agents.bus import MessageBus
from looper.agents.claude_cli import ResidentTurn
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.system_prompt import SystemPrompt
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import run_with_channel_configs


def _last(audience):
    if audience == "none":
        return None
    web = audience in ("operator_web", "client")
    context = ({"session_id": "synthetic-thread"} if web else {
        "chat_id": "oc_synthetic_group" if audience == "group" else "oc_synthetic_dm",
        "chat_type": "group" if audience == "group" else "p2p",
        "sender_id": "ou_synthetic_operator" if audience == "operator_dm" else "ou_synthetic_other"})
    if audience == "client":
        context["client"] = {"id": "synthetic-client", "name": "Synthetic client"}
    return MessageEnvelope.create(source=channel_address("web" if web else "lark"),
        target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": "Run background work", "channel_context": context})


@pytest.mark.parametrize("routing", [False, True], ids=["routing-off", "routing-on"])
@pytest.mark.parametrize("audience", ["operator_dm", "operator_web", "group", "other_dm", "client", "none"])
def test_main_autonomous_output_only_reaches_the_operator(monkeypatch, tmp_path, routing, audience):
    _check(monkeypatch, tmp_path, routing, audience, routed=False)


@pytest.mark.parametrize("audience", ["group", "other_dm"])
def test_routed_autonomous_output_still_reaches_its_own_chat(monkeypatch, tmp_path, audience):
    _check(monkeypatch, tmp_path, True, audience, routed=True)


def _check(monkeypatch, tmp_path, routing, audience, *, routed):
    async def check(pool):
        monkeypatch.setattr(session_routing, "_routing_enabled", routing)
        await pool.execute("UPDATE channel_configs SET config=$1::jsonb WHERE channel_id='channel-lark'",
                           '{"operator_sender_id":"ou_synthetic_operator"}')
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus = MessageBus(runtime_root=tmp_path)
        last = _last(audience)
        token = None
        if routed:
            kind = session_routing.SessionKind.GROUP if audience == "group" else session_routing.SessionKind.DM
            route = session_routing.SessionKey(kind, "channel-lark", last.payload["channel_context"]["chat_id"])
            session = replace(manager._session_now(), key=route.text, route=route)
            token = manager._current.set(session)
        try:
            run = await manager._autonomous_turns([], SystemPrompt("Synthetic test prompt"), Launch())(last)
            result = {"type": "result", "subtype": "success", "is_error": False, "result": "Background complete"}
            await run.event(result)
            await run.end(ResidentTurn(result_event=result))
            replies = ([item for item in await bus.drain_queue(last.source.agent_id)
                        if item.message_type is MessageType.AGENT_REPLY] if last else [])
            allowed = routed or audience in ("operator_dm", "operator_web")
            assert [item.payload["text"] for item in replies] == (["Background complete"] if allowed else [])
            records = await pool.fetch("SELECT id,output_text FROM llm_records")
            assert len(records) == 1 and records[0]["output_text"] == "Background complete"
            events = await pool.fetch("SELECT kind,state,reason,chat,record_id,finished_at FROM manager_events")
            if not allowed and last is not None:
                assert [(row["kind"], row["state"], row["reason"]) for row in events] == [
                    ("autonomous", "skipped", "withheld_not_operator")]
                assert events[0]["chat"] == last.payload["channel_context"].get("chat_id", "synthetic-thread")
                assert events[0]["record_id"] == records[0]["id"]
                assert events[0]["finished_at"] is not None
            else:
                assert events == []
        finally:
            if token is not None:
                manager._current.reset(token)
            await manager.stop()
            await bus.shutdown()

    run_with_channel_configs(check, monkeypatch)
