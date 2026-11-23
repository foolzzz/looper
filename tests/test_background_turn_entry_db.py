"""Shared background entry remains stoppable before any asynchronous input loading."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, lanes, lanes_settings
from looper.agents.managers import ManagerFleet
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import TurnOrigin
from looper.agents.supervisor import AgentSupervisor
from looper.agents.tiers import background_turn
from looper.agents.tiers.turn_token import TurnStopped
from tests.api_manager_support import _manager, _message
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("origin", [TurnOrigin.IDLE_CHECK, TurnOrigin.COMPLETION])
@pytest.mark.parametrize("arrival", ["user", "stop", "before_child", "queued"])
def test_shared_entry_preempts_input_loading_without_a_model_call(monkeypatch, tmp_path, origin, arrival):
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)

    async def check(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            agent = chat.manager
            state = {}
            loading = asyncio.Event()
            release = asyncio.Event()
            target = MessageEnvelope.create(source=channel_address("lark"), target=MANAGER_ADDRESS,
                message_type=MessageType.IDLE_CHECK_DUE, payload={"channel_context": {
                    "chat_id": "ou_background_synthetic", "chat_type": "p2p", "sender_id": "ou_background_synthetic"}})

            async def message(token, context):
                loading.set()
                await release.wait()  # the shared entry must make even plain loaders cancellable
                return "Synthetic input"

            create_task = asyncio.create_task
            def intercept_child(work, **kwargs):
                monkeypatch.setattr(background_turn.asyncio, "create_task", create_task)
                # run() has claimed its work but its child has not taken a single step yet.
                assert agent._sessions["main"].running_token is state["token"]
                assert lanes.preempt(agent._sessions["main"], ops=agent.ops)
                return create_task(work, **kwargs)

            if arrival == "before_child":
                monkeypatch.setattr(background_turn.asyncio, "create_task", intercept_child)
            if arrival == "queued":
                # A caller may have awaited admission or candidate reads before entering run().
                agent._router.dispatch(agent._sessions["main"], _message("Already waiting"))
            pending = create_task(background_turn.run(agent, origin=origin, message=message,
                deadline_seconds=.2 if arrival == "queued" else 5, fallback=AsyncMock(), target=target, state=state))
            try:
                if arrival not in {"before_child", "queued"}:
                    await asyncio.wait_for(loading.wait(), 5)
                    if arrival == "user":
                        await lanes.route_arrival(agent, agent._sessions["main"], _message("Answer first"))
                    else:
                        fleet = ManagerFleet(platform=chat.ops.platform,
                            supervisor=AgentSupervisor(chat.bus, tmp_path), runtime_root=tmp_path,
                            url="http://127.0.0.1:1", config=agent._config, broadcaster=None)
                        fleet._agents[agent.agent_id] = agent
                        for channel, sender in [("channel-lark-other", "ou_background_synthetic"),
                                                ("channel-lark", "ou_other_synthetic")]:
                            summary = await fleet.stop_chat(channel, "oc_actual_synthetic", via="lark", sender_id=sender)
                            assert summary.matched == 0
                        summary = await fleet.stop_chat("channel-lark", "oc_actual_synthetic", via="lark",
                                                       sender_id="ou_background_synthetic")
                        assert summary.matched == summary.stopping == 1
                result = await asyncio.wait_for(pending, 5)
                assert not result.timed_out
                with pytest.raises(TurnStopped):
                    result.value()
                assert chat.model.requests == []
                assert not agent._turns.live()
                assert agent._sessions["main"].running_token is None
                assert not agent._turn_lock.locked()
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    run_db(check, monkeypatch, timeout=30)
