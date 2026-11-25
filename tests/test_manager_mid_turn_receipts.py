"""Receipt-gated resident input must leave the Manager inbox and other sessions available."""
from __future__ import annotations

import asyncio
import json
import socket
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from looper.agents.claude_cli import ResidentClaude
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.mid_turn_input import InputState, MERGED_REPLY
from tests.db_support import DSN, run_db, until
from tests.lark_cli_stand_in import OPEN_ID
from tests.test_manager_mid_turn_input import enabled  # noqa: F401
from tests.test_manager_session_routing_db import GROUP_A, _lark, _Resident, _Rig, _web

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# A and B follow the review probe's exact ownership sequence. Datagram gates
# replace its file sleeps; later requests are answered by this same process.
SCRIPT = (
    "import pathlib, socket\n"
    "gate = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
    "gate.bind(('127.0.0.1', 0))\n"
    "pathlib.Path('input-gate.port').write_text(str(gate.getsockname()[1]))\n"
    "def emit(event): print(json.dumps(event), flush=True)\n"
    "def begin(message):\n"
    "    emit(dict(message, isReplay=True))\n"
    "    emit({'type':'system','subtype':'init','session_id':'synthetic-receipt'})\n"
    "def answer(text, uuids):\n"
    "    emit({'type':'result','subtype':'success','is_error':False,"
    "'session_id':'synthetic-receipt','result':text,'user_message_uuids':uuids})\n"
    "first = json.loads(sys.stdin.readline())\n"
    "begin(first)\n"
    "emit({'type':'assistant','message':{'content':["
    "{'type':'tool_use','id':'tool-1','name':'Bash','input':{}}]}})\n"
    "pathlib.Path('ready').touch()\n"
    "second = json.loads(sys.stdin.readline())\n"
    "pathlib.Path('got-second').touch()\n"
    "with open('gate-actions.jsonl','a') as log: log.write(json.dumps(gate.recv(32).decode()) + '\\n')\n"
    "emit(dict(second, isReplay=True))\n"
    "with open('gate-actions.jsonl','a') as log: log.write(json.dumps(gate.recv(32).decode()) + '\\n')\n"
    "emit({'type':'stream_event','event':{'type':'message_start'}})\n"
    "answer('answer A and B', [first['uuid'], second['uuid']])\n"
    "for line in sys.stdin:\n"
    "    message = json.loads(line)\n"
    "    with open('next-inputs.jsonl','a') as log: log.write(line)\n"
    "    begin(message)\n"
    "    answer('answer C', [message['uuid']])\n"
)


def _rig(tmp_path: Path) -> tuple[_Rig, ResidentClaude, list[dict[str, Any]]]:
    from tests.test_manager_reply import _fake_claude

    rig = _Rig(tmp_path)
    resident = ResidentClaude(label="synthetic-receipt")
    calls: list[dict[str, Any]] = []
    write = resident.write_mid_turn

    async def recorded(text: str, **kwargs: Any) -> bool:
        call = {"message_id": kwargs.get("message_id"), "result": None}
        calls.append(call)
        call["result"] = await write(text, **kwargs)
        return call["result"]

    resident.write_mid_turn = recorded  # type: ignore[method-assign]
    rig.manager._sessions["main"].claude = resident
    rig.manager._config.claude_bin = _fake_claude(tmp_path, SCRIPT)
    return rig, resident, calls


def _gate(tmp_path: Path, action: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as gate:
        gate.sendto(action, ("127.0.0.1", int((tmp_path / "input-gate.port").read_text())))


async def _pending(rig: _Rig, resident: ResidentClaude, tmp_path: Path):
    first, second = _web("first"), _web("second")
    await rig.bus.publish(first)
    await until(lambda: (tmp_path / "ready").exists() and resident._user_turn is not None
                and resident._user_turn.started)
    await rig.bus.publish(second)
    await until(lambda: (tmp_path / "got-second").exists())
    running = rig.manager._mid_turn._turns[first.message_id]
    assert running.pending[second.message_id].state is InputState.PENDING_CONFIRMATION
    assert not running.lock.locked()
    return first, second, running


def test_pending_fold_keeps_other_session_and_inbox_events_available(enabled, tmp_path):  # noqa: F811
    async def test(pool):
        rig, resident, calls = _rig(tmp_path)
        await rig.start()
        try:
            rig.group(GROUP_A, _Resident("sess-group"))
            first, second, running = await _pending(rig, resident, tmp_path)
            seen = set()
            handle = rig.manager._handle
            dispatch = rig.manager._router.dispatch

            async def observe(envelope):
                if envelope.payload.get("receipt_probe"):
                    seen.add(envelope.message_type)
                    return
                await handle(envelope)

            def enqueue(session, envelope):
                if envelope.payload.get("receipt_probe"):
                    # IDLE_CHECK belongs to the busy session's worker. Its
                    # arrival still has to leave the inbox immediately.
                    seen.add(envelope.message_type)
                dispatch(session, envelope)

            enabled.setattr(rig.manager, "_handle", observe)
            enabled.setattr(rig.manager._router, "dispatch", enqueue)
            other = _lark("group-question", GROUP_A, OPEN_ID, group=True)
            await rig.bus.publish(other)
            assert await rig.reply(other) == "sess-group 回复 1"
            kinds = {MessageType.WORK_RESULT, MessageType.GOAL_STATUS, MessageType.IDLE_CHECK_DUE}
            for kind in kinds:
                await rig.bus.publish(MessageEnvelope.create(
                    source=MANAGER_ADDRESS, target=MANAGER_ADDRESS, message_type=kind,
                    payload={"receipt_probe": True}))
            await until(lambda: kinds <= seen)
            assert calls == [{"message_id": second.message_id, "result": None}]
            assert running.pending[second.message_id].state is InputState.PENDING_CONFIRMATION
            assert not running.lock.locked()
            _gate(tmp_path, b"fold")
            _gate(tmp_path, b"answer")
            assert await rig.reply(first) == "answer A and B"
            assert await rig.reply(second) == MERGED_REPLY
            assert [json.loads(line) for line in (tmp_path / "gate-actions.jsonl").read_text().splitlines()] == ["fold", "answer"]
        finally:
            with suppress(OSError):
                _gate(tmp_path, b"fold")
                _gate(tmp_path, b"answer")
            await rig.close()

    run_db(test, enabled, timeout=60)


def test_same_chat_waits_for_fold_then_queues_without_restarting_resident(enabled, tmp_path):  # noqa: F811
    async def test(pool):
        rig, resident, calls = _rig(tmp_path)
        await rig.start()
        try:
            first, second, running = await _pending(rig, resident, tmp_path)
            process = resident._process
            third = _web("third")
            handled = asyncio.Event()
            handle = rig.manager._handle
            queued = []
            dispatch = rig.manager._router.dispatch

            async def marker(envelope):
                if envelope.payload.get("receipt_probe"):
                    handled.set()
                    return
                await handle(envelope)

            def enqueue(session, envelope):
                queued.append(envelope.message_id)
                dispatch(session, envelope)

            enabled.setattr(rig.manager, "_handle", marker)
            enabled.setattr(rig.manager._router, "dispatch", enqueue)
            await rig.bus.publish(third)
            await rig.bus.publish(MessageEnvelope.create(
                source=MANAGER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.WORK_RESULT,
                payload={"receipt_probe": True}))
            await asyncio.wait_for(handled.wait(), 10)
            # The inbox has passed C, but C has neither written nor been
            # queued ahead of the unresolved B settlement.
            assert third.message_id not in queued
            assert len(calls) == 1 and calls[0]["result"] is None
            assert running.pending[second.message_id].state is InputState.PENDING_CONFIRMATION
            _gate(tmp_path, b"fold")
            await until(lambda: third.message_id in queued)
            assert calls == [{"message_id": second.message_id, "result": True},
                             {"message_id": third.message_id, "result": False}]
            _gate(tmp_path, b"answer")
            assert await rig.reply(first) == "answer A and B"
            assert await rig.reply(second) == MERGED_REPLY
            assert [json.loads(line) for line in (tmp_path / "gate-actions.jsonl").read_text().splitlines()] == ["fold", "answer"]
            assert await rig.reply(third) == "answer C"
            await until(lambda: not [e for e in rig.bus.journal.undelivered()
                                    if e.target.agent_id == "manager"])
            rig.collect()
            assert len(rig.replies[third.message_id]) == 1
            assert resident._process is process and resident.pid == process.pid
            assert process.returncode is None
            assert len((tmp_path / "next-inputs.jsonl").read_text().splitlines()) == 1
        finally:
            with suppress(OSError):
                _gate(tmp_path, b"fold")
                _gate(tmp_path, b"answer")
            await rig.close()

    run_db(test, enabled, timeout=60)


def test_shutdown_cancels_pending_settlers_and_keeps_messages_replayable(enabled, tmp_path):  # noqa: F811
    async def test(pool):
        rig, resident, calls = _rig(tmp_path)
        await rig.start()
        closed = False
        try:
            first, second, running = await _pending(rig, resident, tmp_path)
            pending = running.pending[second.message_id]
            third = _web("third")
            await rig.bus.publish(third)
            await until(lambda: len(rig.manager._mid_turn._tasks) == 2)
            settlers = tuple(rig.manager._mid_turn._tasks)
            workers = tuple(rig.manager._router._workers.values())
            process = resident._process
            # No fold or answer is released: the turn and its receipt waiter
            # have to cancel together rather than wait on each other's finally.
            await asyncio.wait_for(rig.close(), 8)
            closed = True
            assert pending.settled.is_set()
            assert all(task.done() for task in (*settlers, *workers))
            assert not rig.manager._mid_turn._tasks
            assert not rig.manager._mid_turn._settlers
            assert not rig.manager._router._workers
            assert resident._process is None and process.returncode is not None
            assert calls == [{"message_id": second.message_id, "result": None}]
            undelivered = {envelope.message_id for envelope in rig.bus.journal.undelivered()
                           if envelope.target.agent_id == "manager"}
            assert {first.message_id, second.message_id, third.message_id} <= undelivered
        finally:
            if not closed:
                await resident.stop(kill=True)
                await rig.close()

    run_db(test, enabled, timeout=45)
