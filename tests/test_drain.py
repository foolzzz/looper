"""Drain admission, deadline reporting and operator API boundaries."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1
from looper.agents.drain import DrainPhase, DrainScope, DrainState
from looper.agents.tiers.worker import WorkerConfig, WorkerPool, WorkerProcess
from tests.test_api_v1_auth import CLIENT_TOKEN, TOKEN, _bearer, _send, _serving
from tests.test_api_v1_auth import clients as clients  # noqa: F401


@pytest.mark.parametrize("scope,seconds", [(DrainScope.TURNS, 180), (DrainScope.RUNS, 1200)])
def test_deadline_reports_without_aborting_and_restart_starts_serving(scope, seconds):
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    drain = DrainState(clock=lambda: now)
    runs = [{"worker_id": "w1", "task_id": "t1", "role": "worker", "since": now.isoformat(), "keepable": False}]
    drain.add_source(lambda: {"runs": runs})
    initial = drain.begin(scope)
    assert initial["phase"] == DrainPhase.DRAINING
    assert datetime.fromisoformat(initial["deadline"]) - now == timedelta(seconds=seconds)
    now += timedelta(seconds=seconds)
    expired = drain.snapshot()
    assert expired["deadline_passed"] and expired["waiting"]["runs"] == runs
    assert expired["phase"] == "draining" and drain.draining
    runs.clear()
    assert drain.snapshot()["phase"] == "drained"
    assert drain.draining  # DRAINED still holds admission.
    assert DrainState().snapshot()["phase"] == "serving"


@pytest.mark.parametrize("blocker,value", [("manager_turns", ["manager"]), ("planner_turns", ["planner-t1"]),
                                          ("gates", 1), ("waits", 1), ("consults", 1), ("starting", 1)])
def test_each_control_plane_activity_blocks_drain(blocker, value):
    drain = DrainState()
    waiting = {blocker: value}
    drain.add_source(lambda: waiting)
    assert drain.begin(DrainScope.RUNS)["phase"] == "draining"
    waiting.clear()
    assert drain.snapshot()["phase"] == "drained"


def test_cancel_wakes_the_held_worker_slot(tmp_path, monkeypatch):
    from looper.agents.tiers import worker

    monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=2))

    async def run():
        drain = DrainState()
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path, drain=drain)
        first = WorkerProcess("w1", "s1", "p1")
        second = WorkerProcess("w2", "s2", "p1")
        async with pool.slot_for(first):
            drain.begin(DrainScope.RUNS)
            entered = asyncio.Event()

            async def held():
                async with pool.slot_for(second):
                    entered.set()

            pending = asyncio.create_task(held())
            try:
                await asyncio.sleep(0)
                assert second.status == "pending" and not entered.is_set()
                assert drain.snapshot()["waiting"]["runs"][0]["worker_id"] == "w1"
                drain.cancel()
                await asyncio.wait_for(pending, 1)
                assert entered.is_set()
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.usefixtures("clients")
@pytest.mark.parametrize("blocked", [False, True])
def test_lost_drain_polls_resume_held_turns_and_runs_without_releasing_at_deadline(tmp_path, monkeypatch, blocked):
    from looper.agents.tiers import worker
    from looper.agents.v1_domains import drain as domain

    monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=2))
    monkeypatch.setattr(domain, "DRAIN_POLL_TIMEOUT_SECONDS", 0.2, raising=False)

    async def run():
        async with _serving(tmp_path, listen=True) as server:
            drain = server._drain
            pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path, drain=drain)
            drain.add_source(lambda: {"gates": int(blocked)})
            code, _, initial = await _send(server, "POST /api/v1/system/drain", _bearer(TOKEN),
                '{"scope":"runs","timeout_seconds":0.05}')
            assert code == 200
            phase = "draining" if blocked else "drained"
            assert initial["result"]["phase"] == phase
            turn = asyncio.create_task(drain.wait_serving())

            async def held_run():
                async with pool.slot_for(WorkerProcess("held", "step", "project")):
                    return "admitted"

            pending = asyncio.create_task(held_run())
            try:
                await asyncio.sleep(0.12)
                code, _, status = await _send(server, "POST /api/v1/system/drain_status", _bearer(TOKEN), "{}")
                assert code == 200 and status["result"]["deadline_passed"]
                assert status["result"]["phase"] == phase
                # Polling renews the lease beyond its original expiry, even after the stop deadline.
                await asyncio.sleep(0.12)
                assert drain.snapshot()["phase"] == phase
                assert not turn.done() and not pending.done()
                # No more polls: the timer must wake both wait paths without another HTTP request.
                await asyncio.wait_for(asyncio.gather(turn, pending), 1)
                assert pending.result() == "admitted"
                assert drain.snapshot()["phase"] == "serving"
                assert drain.snapshot()["deadline"] is None
            finally:
                turn.cancel()
                pending.cancel()
                await asyncio.gather(turn, pending, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.usefixtures("clients")
def test_operator_drain_api_and_generated_cli_contract(tmp_path):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            for name in ("drain", "drain_status", "drain_cancel"):
                code, _, _ = await _send(server, f"POST /api/v1/system/{name}", _bearer(CLIENT_TOKEN),
                                         json.dumps({"scope": "runs"}) if name == "drain" else "{}")
                assert code == 403
            code, _, _ = await _send(server, "POST /api/v1/system/drain", _bearer(TOKEN), '{"scope":"bad"}')
            assert code == 400
            code, _, answer = await _send(server, "POST /api/v1/system/drain", _bearer(TOKEN),
                                           '{"scope":"turns","timeout_seconds":1}')
            assert code == 200 and answer["status"] == "ok" and answer["result"]["phase"] == "drained"
            code, _, answer = await _send(server, "POST /api/v1/system/drain_status", _bearer(TOKEN), "{}")
            assert code == 200 and answer["result"]["scope"] == "turns"
            assert not any(answer["result"]["waiting"].values())
            code, _, answer = await _send(server, "POST /api/v1/system/drain_cancel", _bearer(TOKEN), "{}")
            assert code == 200 and answer["result"]["phase"] == "serving"
            assert answer["result"]["since"] is None and answer["result"]["deadline"] is None
        document = api_v1.document()
        for name in ("drain", "drain_status", "drain_cancel"):
            operations = document["paths"][f"/system/{name}"]
            assert set(operations) == {"post"}
            assert "200" in operations["post"]["responses"]
            assert "202" not in operations["post"]["responses"]
        assert set(document["paths"]["/system/status"]) == {"post"}
        from looper import loopercli
        commands = {(head, tail, operation.method) for head, tail, _, _, operation
                    in loopercli.generated_operations(document)}
        assert {("system", "drain", "POST"), ("system", "drain-status", "POST"),
                ("system", "drain-cancel", "POST")} <= commands

    asyncio.run(run())


@pytest.mark.usefixtures("clients")
@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_drain_refuses_non_post_methods_like_existing_system_operations(tmp_path, method):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            code, _, answer = await _send(server, f"{method} /api/v1/system/check", _bearer(TOKEN))
            assert code in (404, 405)
            for name in ("drain", "drain_status", "drain_cancel", "status"):
                refused, _, body = await _send(server, f"{method} /api/v1/system/{name}", _bearer(TOKEN))
                assert refused == code
                assert body == answer
    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf")])
def test_invalid_timeout_leaves_serving(timeout):
    drain = DrainState()
    with pytest.raises(ValueError):
        drain.begin(DrainScope.RUNS, timeout)
    assert not drain.draining


def test_immediate_redrain_keeps_previously_woken_turns_held():
    async def run():
        drain = DrainState()
        drain.begin(DrainScope.RUNS)
        waiter = asyncio.create_task(drain.wait_serving())
        try:
            await asyncio.sleep(0)
            drain.cancel()
            drain.begin(DrainScope.TURNS)
            await asyncio.sleep(0)
            assert not waiter.done()
            drain.cancel()
            await asyncio.wait_for(waiter, 1)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("channel,group", [("telegram", False), ("lark", False),
                                          ("lark", True), ("lark_bot", True)])
@pytest.mark.parametrize("restart_before_delivery", [False, True])
def test_held_chat_reply_survives_restart(agent_db, tmp_path, monkeypatch, channel, group,
                                         restart_before_delivery):
    from types import SimpleNamespace

    from looper.agents.bus import MessageBus
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.telegram import TelegramChannelAdapter
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import chat
    from tests.db_support import until
    from tests.test_manager_stop import _Session

    saved = AsyncMock()
    monkeypatch.setattr(chat, "save_channel_message", saved)
    channel_id = f"channel-{channel}"
    chat_id = 99 if channel == "telegram" else "oc_chat"
    sent, replied = AsyncMock(return_value=SimpleNamespace(message_id=42)), AsyncMock(return_value="om_answer")
    transport = SimpleNamespace(send=sent, reply=replied, edit=AsyncMock(), react=AsyncMock(),
                                names=AsyncMock(return_value={}), aclose=AsyncMock())

    def adapter(bus):
        allowlist = SenderAllowlist(channel_id, frozenset({"7", "ou_sender"}))
        if channel == "telegram":
            result = TelegramChannelAdapter(allowlist=allowlist)
            result._app = SimpleNamespace(bot=SimpleNamespace(send_message=sent, edit_message_text=AsyncMock()))
        else:
            result = LarkChannelAdapter(allowlist=allowlist, channel_type=channel, transport=transport,
                                        bot_open_id="ou_bot", runtime_root=tmp_path)
        result._bus, result._running = bus, True
        return result

    async def run():
        bus = MessageBus(runtime_root=tmp_path)
        await bus.start()
        drain = DrainState()
        drain.begin(DrainScope.TURNS)
        held = asyncio.Event()
        wait = drain.wait_serving

        async def waiting():
            held.set()
            await wait()

        monkeypatch.setattr(drain, "wait_serving", waiting)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path, drain=drain)
        inbound = adapter(bus)
        await manager.start(bus)
        try:
            if channel == "telegram":
                update = SimpleNamespace(update_id=1, effective_chat=SimpleNamespace(id=chat_id),
                                         effective_user=SimpleNamespace(id=7, username="sender"),
                                         message=SimpleNamespace(text="hello", message_id=41, reply_to_message=None))
                await inbound._on_message(update, None)
            else:
                await inbound._handle_lark_event({"type": "im.message.receive_v1", "chat_id": chat_id,
                    "chat_type": "group" if group else "p2p", "sender_id": "ou_sender", "sender_type": "user",
                    "message_type": "text", "message_id": "om_question", "content": "hello",
                    "mentions": [{"id": "ou_bot", "key": "@bot", "name": "Bot"}] if group else []})
            await asyncio.wait_for(held.wait(), 2)
            assert saved.await_count == 0  # held before the Manager starts the turn
            (question,) = bus.journal.undelivered()
        finally:
            await manager.stop()
            await inbound._reactions.stop()
            await bus.shutdown()

        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._claude = _Session(hold=False)
        await bus.start()
        await manager.start(bus)
        try:
            await until(lambda: question.message_id not in {e.message_id for e in bus.journal.undelivered()},
                        timeout=5)
            assert len(manager._claude.turns) == 1
        finally:
            await manager.stop()
        if restart_before_delivery:
            await bus.shutdown()
            bus = MessageBus(runtime_root=tmp_path)
            await bus.start()  # only the pending answer remains, not the user's message

        outbound = adapter(bus)  # no routing state from the previous channel instance
        assert not outbound._correlation_chat
        outbound._outbound_task = asyncio.create_task(outbound._outbound_loop())
        try:
            await until(lambda: not bus.journal.undelivered(), timeout=5)
            answers = [call.kwargs for call in saved.await_args_list if call.kwargs["role"] == "assistant"]
            assert len(answers) == 1 and answers[0]["delivery_failure"] is None
            deliveries = replied.await_args_list if group else sent.await_args_list
            final = [call for call in deliveries if call.kwargs.get("markdown", call.kwargs.get("text")) == "好的"]
            assert len(final) == 1
            if not restart_before_delivery:
                assert len(deliveries) > 1  # the resumed turn's status also reached the chat
            if group:
                assert all(call.args == ("om_question",) for call in deliveries)
                sent.assert_not_awaited()  # never broadcast the group reply
            else:
                assert final[0].kwargs["chat_id"] == chat_id
            # An autonomous answer must carry the same route even after the cache is lost again.
            outbound._correlation_chat.clear()
            if group:
                outbound._group_message.clear()
            manager._bus = bus
            await manager._send_own_reply(question, "background result")
            await until(lambda: not bus.journal.undelivered(), timeout=5)
            own = replied.await_args_list if group else sent.await_args_list
            assert len([call for call in own if call.kwargs.get("markdown", call.kwargs.get("text"))
                        == "background result"]) == 1
        finally:
            outbound._outbound_task.cancel()
            await asyncio.gather(outbound._outbound_task, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 15))


def test_replayed_manager_message_waits_for_http_readiness(agent_db, tmp_path, monkeypatch):
    from looper.agents.bus import MessageBus
    from looper.agents.message import AgentAddress, MessageEnvelope, MessageType
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    async def run():
        bus = MessageBus(tmp_path)
        await bus.start()
        ready, handled = asyncio.Event(), asyncio.Event()
        envelope = MessageEnvelope.create(source=AgentAddress("channel", "channel-web"),
            target=AgentAddress("manager", "manager"), message_type=MessageType.USER_MESSAGE,
            payload={"text": "held during restart"})
        await bus.publish(envelope)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path, http_ready=ready), runtime_root=tmp_path)
        monkeypatch.setattr(manager, "_handle", AsyncMock(side_effect=lambda item: handled.set()))
        await manager.start(bus)
        try:
            await asyncio.sleep(0)
            assert not handled.is_set()
            assert envelope.message_id in {item.message_id for item in bus.journal.undelivered()}
            ready.set()
            await asyncio.wait_for(handled.wait(), 2)
            manager._handle.assert_awaited_once_with(envelope)
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(run())
