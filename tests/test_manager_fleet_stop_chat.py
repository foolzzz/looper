"""Chat Stop fans out by bot/chat identity, outside every Manager's model queue."""

from __future__ import annotations

import asyncio
import dataclasses
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings
from looper.agents.bus import MessageBus
from looper.agents.managers import ManagerFleet, chat_stop_reply
from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from looper.agents.ops.service import Platform
from looper.agents.supervisor import AgentSupervisor
from looper.agents.tiers.manager import (
    STOP_DONE_REPLY, STOP_IDLE_REPLY, STOP_UNSUPPORTED_REPLY, ManagerConfig, default_definition,
)
from looper.agents.tiers.turn_token import TurnPhase
from tests.db_support import until
from tests.test_manager_stop import _Session


@pytest.fixture(autouse=True)
def stop_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)


@asynccontextmanager
async def fleet(root: Path):
    bus = MessageBus(runtime_root=root)
    supervisor = AgentSupervisor(bus, root)
    platform = Platform(bus=bus, task_router=None, supervisor=supervisor, runtime_root=root)
    f = ManagerFleet(platform=platform, supervisor=supervisor, runtime_root=root, url="http://127.0.0.1:1",
                     config=ManagerConfig(work_dir=root), broadcaster=None)
    a = f._build(default_definition())
    b = f._build(dataclasses.replace(default_definition(), id="manager-review", name="Review", builtin=False))
    for agent in (a, b):
        agent._bus = bus
        agent._claude = SimpleNamespace(can_interrupt=True)
        bus.subscribe_topic(MessageType.USER_MESSAGE.value, agent._receive, consumes=True)
    try:
        yield SimpleNamespace(fleet=f, bus=bus, a=a, b=b)
    finally:
        await asyncio.gather(*(task for agent in (a, b) for task in agent._stop_tasks))
        await f.stop()
        await bus.shutdown()


def message(agent: Any, text: str = "hello", *, channel: str = "telegram", channel_id: str | None = None,
            chat: str = "42", group: bool = False) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=channel_address(channel, channel_id), target=manager_address(agent.agent_id),
        message_type=MessageType.USER_MESSAGE, payload={"text": text, "channel_context": {
            "chat_id": chat, "chat_type": "group" if group else "p2p", "user_id": "7",
        }},
    )


async def command(r: Any, **kwargs: Any) -> MessageEnvelope:
    stop = message(r.a, " /STOP ", **kwargs)
    await r.bus.publish(stop)
    await asyncio.gather(*r.a._stop_tasks)
    replies = [e for e in r.bus.take_queue(stop.source.agent_id) if e.reply_to == stop.message_id]
    assert len(replies) == 1
    assert "chat_record" not in replies[0].metadata
    assert r.a.turn_for(stop.message_id) is None
    assert stop.message_id not in {e.message_id for e in r.bus.journal.undelivered()}
    return replies[0]


@pytest.mark.parametrize(("channel", "channel_id", "chat", "group"), [
    ("telegram", None, "42", False),
    ("telegram", "channel-telegram-2", "-42", True),
    ("lark", None, "oc_room", False),
    ("lark", "channel-lark-2", "oc_room", True),
    ("lark_bot", None, "oc_room", False),
    ("lark_bot", "channel-lark_bot-2", "oc_room", True),
])
def test_stop_takes_live_and_queued_turns_only_for_the_same_bot_and_chat(
    tmp_path: Path, channel: str, channel_id: str | None, chat: str, group: bool,
) -> None:
    async def run() -> None:
        async with fleet(tmp_path) as r:
            args = dict(channel=channel, channel_id=channel_id, chat=chat, group=group)
            targets, untouched = [], []
            for agent in (r.a, r.b):
                live = agent._register_turn(message(agent, **args))
                live.phase = TurnPhase.RUNNING
                targets.extend((live, agent._register_turn(message(agent, **args))))
                untouched.extend((
                    agent._register_turn(message(agent, **{**args, "chat": "other"})),
                    agent._register_turn(message(agent, **{**args, "channel_id": f"channel-{channel}-3"})),
                    agent._register_turn(message(agent, **{**args, "chat": f"other:{chat}"})),
                ))
            await command(r, **args)
            assert all(t.stop is not None and t.stop.via == channel for t in targets)
            assert all(t.stop is None for t in untouched)
            assert all(r.bus.pending_count(a.agent_id) == 0 for a in (r.a, r.b))

    asyncio.run(run())


def test_stop_is_claimed_before_both_model_turns_finish(
    tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import chat, managers

    store = AsyncMock()
    monkeypatch.setattr(chat, "save_channel_message", store)

    async def run() -> None:
        async with fleet(tmp_path) as r:
            async def row(manager_id: str):
                return {**dataclasses.asdict(r.fleet.get(manager_id)._definition), "system_prompt": "Be helpful."}

            monkeypatch.setattr(managers, "get_manager", row)
            sessions = [_Session(), _Session()]
            tasks = []
            for agent, session in zip((r.a, r.b), sessions):
                agent._claude = session
                agent.ops = None
                tasks.append(asyncio.create_task(agent._handle(message(agent))))
            try:
                await until(lambda: all(s.running.is_set() for s in sessions), timeout=5)
                await command(r)
                await asyncio.wait_for(asyncio.gather(*tasks), 5)
                assert all(len(s.turns) == 1 and "/STOP" not in s.turns[0] for s in sessions)
                assert len(agent_db.finish_record.await_args_list) == 2
                assert all(c.kwargs["status"] == "interrupted" for c in agent_db.finish_record.await_args_list)
                assert all("/STOP" not in str(c) for c in agent_db.start_record.await_args_list)
                assert store.await_count == 2 and "/STOP" not in str(store.await_args_list)
            finally:
                for session in sessions:
                    session._stopped.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("entry", ["chat", "direct"])
def test_disabled_switch_changes_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    monkeypatch.setattr(chat_settings, "_stop_enabled", False)

    async def run() -> None:
        async with fleet(tmp_path) as r:
            tokens = [a._register_turn(message(a)) for a in (r.a, r.b)]
            if entry == "chat":
                reply = (await command(r)).payload["text"]
            else:
                summary = await r.fleet.stop_chat("channel-telegram", "42", via="telegram")
                assert summary.state == "disabled"
                reply = chat_stop_reply(summary)
            assert reply == chat_settings.STOP_DISABLED_REPLY
            assert all(t.stop is None for t in tokens)

    asyncio.run(run())


@pytest.mark.parametrize("receiver_has_turn", [False, True])
def test_unsupported_receiver_still_stops_a_supported_peer(tmp_path: Path, receiver_has_turn: bool) -> None:
    async def run() -> None:
        async with fleet(tmp_path) as r:
            r.a._claude.can_interrupt = False
            own = r.a._register_turn(message(r.a)) if receiver_has_turn else None
            peer = r.b._register_turn(message(r.b))
            reply = (await command(r)).payload["text"]
            assert peer.stop is not None
            if own:
                assert own.stop is None
                assert "部分" in reply and "照常完成" in reply and reply != STOP_DONE_REPLY
            else:
                assert reply == STOP_DONE_REPLY

    asyncio.run(run())


def test_all_unsupported_keeps_the_reply_running(tmp_path: Path) -> None:
    async def run() -> None:
        async with fleet(tmp_path) as r:
            tokens = [a._register_turn(message(a)) for a in (r.a, r.b)]
            for a in (r.a, r.b):
                a._claude.can_interrupt = False
            assert (await command(r)).payload["text"] == STOP_UNSUPPORTED_REPLY
            assert all(t.stop is None for t in tokens)

    asyncio.run(run())


def test_repeated_stop_reports_already_stopping_without_changing_the_request(tmp_path: Path) -> None:
    async def run() -> None:
        async with fleet(tmp_path) as r:
            tokens = [a._register_turn(message(a)) for a in (r.a, r.b)]
            first = await r.fleet.stop_chat("channel-telegram", "42", via="telegram")
            requests = [t.stop for t in tokens]
            again = await r.fleet.stop_chat("channel-telegram", "42", via="courier")
            assert first.stopping == 2 and first.already_stopping == 0
            assert again.stopping == 0 and again.already_stopping == 2
            assert [t.stop for t in tokens] == requests
            assert chat_stop_reply(first) == chat_stop_reply(again)

    asyncio.run(run())


@pytest.mark.parametrize("race", ["settled", "failure"])
def test_one_owners_race_or_failure_does_not_miss_other_snapshot_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, race: str,
) -> None:
    async def run() -> None:
        async with fleet(tmp_path) as r:
            first = r.a._register_turn(message(r.a))
            ended, peer = [r.b._register_turn(message(r.b)) for _ in range(2)]
            original = r.a.stop_turn

            async def stop(message_id: str, *, via: str):
                # Mutate the fleet and registries after the first await; later owners must already be snapshotted.
                await asyncio.sleep(0)
                r.fleet._agents.pop(r.b.agent_id)
                if race == "failure":
                    raise RuntimeError("synthetic stop failure")
                ended.settle()
                r.b._register_turn(message(r.b))  # arrived after the command; must not be stopped
                return await original(message_id, via=via)

            monkeypatch.setattr(r.a, "stop_turn", stop)
            summary = await r.fleet.stop_chat("channel-telegram", "42", via="telegram")
            assert summary.matched == 3 and peer.stop is not None
            if race == "failure":
                assert summary.failures == [r.a.agent_id] and first.stop is None
                assert summary.stopping == 2
                assert "未能完整停止" in chat_stop_reply(summary)
                assert "synthetic stop failure" in caplog.text
            else:
                assert not summary.failures and summary.stopping == 2
                assert ended.stop is None
                assert r.b.live_for_chat("channel-telegram", "42")[-1].stop is None

    asyncio.run(run())


@pytest.mark.parametrize("can_interrupt", [True, False])
def test_empty_fleet_uses_receiver_capability_but_direct_control_is_idle(tmp_path: Path, can_interrupt: bool) -> None:
    async def run() -> None:
        async with fleet(tmp_path) as r:
            r.a._claude.can_interrupt = can_interrupt
            expected = STOP_IDLE_REPLY if can_interrupt else STOP_UNSUPPORTED_REPLY
            assert (await command(r)).payload["text"] == expected
            summary = await r.fleet.stop_chat("channel-telegram", "42", via="courier")
            assert summary.matched == 0 and chat_stop_reply(summary) == STOP_IDLE_REPLY

    asyncio.run(run())


def test_standalone_manager_keeps_its_local_stop(tmp_path: Path) -> None:
    async def run() -> None:
        async with fleet(tmp_path) as r:
            r.a.stop_chat = None
            own, peer = [a._register_turn(message(a)) for a in (r.a, r.b)]
            assert (await command(r)).payload["text"] == STOP_DONE_REPLY
            assert own.stop is not None and peer.stop is None

    asyncio.run(run())


@pytest.mark.parametrize("count", [1, 3])
def test_one_owning_manager_preserves_the_existing_acknowledgement(tmp_path: Path, count: int) -> None:
    async def run() -> None:
        async with fleet(tmp_path) as r:
            for _ in range(count):
                await r.bus.publish(message(r.a))
            reply = (await command(r)).payload["text"]
            expected = STOP_DONE_REPLY if count == 1 else "已停止当前回复，另取消了 2 条排队的消息。"
            assert reply == expected

    asyncio.run(run())


def test_a_token_without_channel_id_uses_only_its_legacy_channel_key(tmp_path: Path) -> None:
    async def run() -> None:
        async with fleet(tmp_path) as r:
            legacy = r.a._register_turn(message(r.a))
            legacy.channel_id = None
            full = r.b._register_turn(message(r.b))
            summary = await r.fleet.stop_chat("telegram", "42", via="courier")
            assert summary.stopping == 1 and legacy.stop is not None and full.stop is None

    asyncio.run(run())
