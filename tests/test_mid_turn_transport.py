"""Native resident input admission, without calling a model or another runtime."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.claude_cli import ResidentClaude, _Output, _UserTurn
from looper.agents.runtimes.cancel import TurnCancel


class _Writer:
    def __init__(self) -> None:
        self.lines: list[bytes] = []
        self.closing = False
        self.error: Exception | None = None
        self.after_write: Any = None

    def is_closing(self) -> bool:
        return self.closing

    def write(self, line: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.lines.append(line)

    async def drain(self) -> None:
        if self.after_write is not None:
            self.after_write()


def _running() -> tuple[ResidentClaude, _Writer, _UserTurn]:
    resident = ResidentClaude(label="synthetic-mid-turn")
    writer = _Writer()
    resident._process = SimpleNamespace(stdin=writer, returncode=None)
    turn = _UserTurn("original", "first", "original-tag", None, asyncio.get_running_loop().create_future(),
                     started=True)
    resident._user_turn = turn
    resident._cancel = TurnCancel.never()
    resident._cancel.sent()
    resident._output = SimpleNamespace(turn=turn, ended=False, discard=False)
    return resident, writer, turn


@pytest.mark.parametrize("state", [
    "idle", "not_sent", "not_started", "answered", "stopping", "cancelled", "exited", "closing",
    "output_ended", "discarded", "different_turn",
])
def test_input_is_refused_unless_the_original_turn_can_still_take_it(state: str) -> None:
    async def run() -> None:
        resident, writer, turn = _running()
        if state == "idle":
            resident._user_turn = None
        elif state == "not_sent":
            resident._cancel.prompt_sent = False
        elif state == "not_started":
            turn.started = False
        elif state == "answered":
            turn.done.set_result({"type": "result"})
        elif state == "stopping":
            turn.stopping = True
        elif state == "cancelled":
            resident._cancel.request()
        elif state == "exited":
            resident._process.returncode = 0
        elif state == "closing":
            writer.closing = True
        elif state == "output_ended":
            resident._output.ended = True
        elif state == "discarded":
            resident._output.discard = True
        elif state == "different_turn":
            resident._output.turn = None
        assert await resident.write_mid_turn("second") is False
        assert writer.lines == []

    asyncio.run(run())


def test_a_result_during_drain_reserves_the_uuid_for_the_next_turn() -> None:
    async def run() -> None:
        resident, writer, turn = _running()

        async def observe(event):
            pass

        turn.on_event = observe
        output = resident._output = _Output(resident, asyncio.StreamReader(), None)
        output.turn = turn
        output._marks_turns = True

        async def drain():
            await output._dispatch({"type": "result", "user_message_uuids": [turn.uuid]})

        writer.drain = drain
        assert await resident.write_mid_turn("补充\ntext", message_id="B") is False
        message = json.loads(writer.lines[0])
        uuid = message.pop("uuid")
        assert message == {"type": "user", "message": {"role": "user", "content": "补充\ntext"}}
        assert output.queued["B"].uuid == uuid
        assert resident._process is not None and not output.discard
        assert await resident.write_mid_turn("too late") is False
        assert len(writer.lines) == 1

    asyncio.run(run())


def test_unconfirmed_write_is_bound_when_generation_was_not_yet_observed(monkeypatch) -> None:
    async def run() -> None:
        resident, writer, turn = _running()
        written = asyncio.Event()
        at_result, release_result = asyncio.Event(), asyncio.Event()
        retired = []

        async def retire(**kwargs):
            retired.append(kwargs)
            resident._process = None

        async def observe(event):
            at_result.set()
            await release_result.wait()

        monkeypatch.setattr(resident, "stop", retire)
        turn.on_event = observe
        output = resident._output = _Output(resident, asyncio.StreamReader(), None)
        output.turn = turn
        output._marks_turns = True
        writer.after_write = written.set
        pending = asyncio.create_task(resident.write_mid_turn("B", message_id="B"))
        await written.wait()
        assert not pending.done()  # stdin.write/drain is no consumption acknowledgement
        dispatch = asyncio.create_task(output._dispatch({"type": "result", "result": "A only"}))
        await at_result.wait()
        assert not output.discard and not pending.done()
        release_result.set()
        await dispatch
        assert await pending is False
        assert not output.discard and retired == []
        assert output.queued["B"].uuid == json.loads(writer.lines[0])["uuid"]
        assert len(writer.lines) == 1

    asyncio.run(run())


@pytest.mark.parametrize("confirmation", ["replay", "result", "other_input", "stop", "eof"])
def test_only_runtime_consumption_confirms_a_written_input(monkeypatch, confirmation) -> None:
    async def run() -> None:
        resident, writer, turn = _running()
        written = asyncio.Event()
        retired = []

        async def observe(event):
            pass

        async def retire(**kwargs):
            retired.append(kwargs)
            resident._process = None

        monkeypatch.setattr(resident, "stop", retire)
        turn.on_event = observe
        output = resident._output = _Output(resident, asyncio.StreamReader(), None)
        output.turn = turn
        output._marks_turns = True
        # The top-level tool wait is open even while a subagent generates output.
        await output._dispatch({"type": "stream_event", "event": {"type": "message_start"}})
        await output._dispatch({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tool"}]}})
        await output._dispatch({"type": "stream_event", "parent_tool_use_id": "tool",
                                "event": {"type": "message_start"}})
        writer.after_write = written.set
        pending = asyncio.create_task(resident.write_mid_turn("B", message_id="B"))
        await written.wait()
        assert not pending.done()
        uuid = json.loads(writer.lines[0])["uuid"]
        if confirmation == "replay":
            await output._dispatch({"type": "user", "isReplay": True, "uuid": uuid})
        elif confirmation == "stop":
            async def cancel_receipt():
                request = json.loads(writer.lines[-1])
                assert request["request"] == {"subtype": "cancel_async_message", "message_uuid": uuid}
                await output._dispatch({"type": "control_response", "response": {
                    "subtype": "success", "request_id": request["request_id"],
                    "response": {"cancelled": True}}})

            writer.drain = cancel_receipt
            resident._cancel.request()
            await output._dispatch({"type": "result", "user_message_uuids": []})
        elif confirmation == "eof":
            output.drop_turn()
        else:
            # Neither a queued lifecycle nor another input's replay is a receipt.
            await output._dispatch({"type": "command_lifecycle", "command_uuid": uuid, "state": "queued"})
            await output._dispatch({"type": "user", "isReplay": True, "uuid": "other"})
            assert not pending.done()
            await output._dispatch({"type": "result", "user_message_uuids": [
                uuid if confirmation == "result" else "other"]})
        accepted = confirmation in {"replay", "result"}
        assert await pending is accepted
        assert retired == ([{"kill": True}] if confirmation == "eof" else [])

    asyncio.run(run())


@pytest.mark.parametrize("result_at", ["before_write", "after_write"])
def test_kernel_buffered_result_with_immediate_drain_refuses_input(monkeypatch, result_at) -> None:
    """Poll must wake the transport and then the blocked reader before admission decides."""
    async def run() -> None:
        resident, _, turn = _running()
        client, peer = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=client)
        result = b'{"type":"result","subtype":"success","result":"original only"}\n'
        writes, retired = [], []

        async def observe(event):
            pass

        turn.on_event = observe
        output = resident._output = _Output(resident, reader, None)
        output.turn = turn
        output._marks_turns = True
        process = resident._process

        class Writer:
            def is_closing(self):
                return writer.is_closing()

            def write(self, line):
                writes.append(line)
                writer.write(line)
                if result_at == "after_write":
                    peer.sendall(result)

            async def drain(self):
                # Assert this real short-line drain does not suspend.
                resumed = []
                asyncio.get_running_loop().call_soon(resumed.append, True)
                await writer.drain()
                assert not resumed

        async def retire(**kwargs):
            retired.append(kwargs)
            process.returncode = -9
            resident._process = None
            writer.close()
            await writer.wait_closed()

        process.stdin = Writer()
        monkeypatch.setattr(resident, "stop", retire)
        output.read()
        try:
            await asyncio.sleep(0)  # the real read loop blocks waiting for OS data
            assert reader._waiter is not None and not reader._buffer
            if result_at == "before_write":
                peer.sendall(result)  # kernel bytes, no feed_data/call_soon wake-up
            assert await resident.write_mid_turn("B", message_id="B") is False
            assert turn.result_received
            assert len(writes) == 1  # unread output is resolved by ownership, not OS scheduling
            assert retired == []
            assert output.queued["B"].uuid == json.loads(writes[0])["uuid"]
            assert process.returncode is None and not writer.is_closing()
        finally:
            peer.close()
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(output._task, 5)

    asyncio.run(run())


def test_a_broken_input_pipe_is_reported_to_the_owner(monkeypatch) -> None:
    async def run() -> None:
        resident, writer, _ = _running()
        async def retire(**kwargs):
            resident._process = None
        monkeypatch.setattr(resident, "stop", retire)
        writer.error = BrokenPipeError()
        with pytest.raises(BrokenPipeError):
            await resident.write_mid_turn("second")
        assert writer.lines == []

    asyncio.run(run())


def test_consumption_receipt_prevents_retry_even_if_drain_then_fails() -> None:
    async def run() -> None:
        resident, writer, turn = _running()

        async def observe(event):
            pass

        turn.on_event = observe
        output = resident._output = _Output(resident, asyncio.StreamReader(), None)
        output.turn = turn
        output._marks_turns = True

        async def drain():
            uuid = json.loads(writer.lines[0])["uuid"]
            await output._dispatch({"type": "user", "isReplay": True, "uuid": uuid})
            raise BrokenPipeError("after consumption")

        writer.drain = drain
        assert await resident.write_mid_turn("B") is True
        assert len(writer.lines) == 1 and not output.discard

    asyncio.run(run())


@pytest.mark.parametrize("event", [
    {"type": "stream_event", "event": {"type": "message_start", "message": {"role": "assistant"}}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "A only"}]}},
])
def test_final_generation_closes_admission_before_result_or_observer(event) -> None:
    async def run() -> None:
        resident, writer, turn = _running()
        entered, release = asyncio.Event(), asyncio.Event()

        async def observe(event):
            entered.set()
            await release.wait()

        turn.on_event = observe
        output = resident._output = _Output(resident, asyncio.StreamReader(), None)
        output.turn = turn
        output._marks_turns = True
        dispatch = asyncio.create_task(output._dispatch(event))
        try:
            await entered.wait()
            assert not turn.result_received and not turn.done.done()
            assert await resident.write_mid_turn("B", message_id="B") is False
            assert writer.lines == []
        finally:
            release.set()
            await dispatch

    asyncio.run(run())


def test_native_input_joins_one_resident_turn_and_keeps_its_result(tmp_path: Path) -> None:
    """The subprocess reads two stdin inputs and emits exactly one result for run_turn."""
    from looper.agents.runtimes import MidTurnInput

    async def run() -> None:
        resident = ResidentClaude(label="synthetic-mid-turn")
        script = (
            "import json, sys\n"
            "first = json.loads(sys.stdin.readline())\n"
            "print(json.dumps(dict(first, isReplay=True)), flush=True)\n"
            "second = json.loads(sys.stdin.readline())\n"
            "print(json.dumps(dict(second, isReplay=True)), flush=True)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': "
            "first['message']['content'] + ' + ' + second['message']['content']}), flush=True)\n"
            "sys.stdin.read()\n"
        )
        command = [sys.executable, "-u", "-c", script]
        await resident.start(command, cwd=str(tmp_path), signature=("synthetic", 1))
        started = asyncio.Event()
        seen: list[dict[str, Any]] = []

        async def observe(event: dict[str, Any]) -> None:
            seen.append(event)
            if event.get("isReplay"):
                started.set()

        try:
            assert isinstance(resident, MidTurnInput)
            task = asyncio.create_task(resident.run_turn("first", observe))
            await asyncio.wait_for(started.wait(), 5)
            assert await resident.write_mid_turn("second") is True
            result = await asyncio.wait_for(task, 5)
            assert result.result_event["result"] == "first + second"
            assert len([event for event in seen if event["type"] == "result"]) == 1
            assert resident.command == command and resident._signature == ("synthetic", 1)
            assert await resident.write_mid_turn("third") is False
        finally:
            await resident.stop(kill=True)

    asyncio.run(run())


def test_a_result_closes_input_before_its_event_callback_finishes(tmp_path: Path) -> None:
    async def run() -> None:
        resident = ResidentClaude(label="synthetic-mid-turn-result")
        script = (
            "import json, sys\n"
            "message = json.loads(sys.stdin.readline())\n"
            "print(json.dumps(dict(message, isReplay=True)), flush=True)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'done'}), flush=True)\n"
            "sys.stdin.read()\n"
        )
        await resident.start([sys.executable, "-u", "-c", script], cwd=str(tmp_path), signature=1)
        at_result, release = asyncio.Event(), asyncio.Event()

        async def observe(event: dict[str, Any]) -> None:
            if event["type"] == "result":
                at_result.set()
                await release.wait()

        task = asyncio.create_task(resident.run_turn("first", observe))
        try:
            await asyncio.wait_for(at_result.wait(), 5)
            assert await resident.write_mid_turn("too late") is False
        finally:
            release.set()
            await asyncio.wait_for(task, 5)
            await resident.stop(kill=True)

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["fold", "tool_result"])
def test_fold_and_top_level_tool_result_close_admission_until_next_tool_use(boundary) -> None:
    async def run() -> None:
        resident, writer, turn = _running()

        async def observe(event):
            pass

        turn.on_event = observe
        output = resident._output = _Output(resident, asyncio.StreamReader(), None)
        output.turn = turn
        if boundary == "fold":
            written = asyncio.Event()
            writer.after_write = written.set
            pending = asyncio.create_task(resident.write_mid_turn("B", message_id="B"))
            await written.wait()
            await output._dispatch({"type": "user", "isReplay": True, "uuid": json.loads(writer.lines[-1])["uuid"]})
            assert await pending
        else:
            await output._dispatch({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool"}]}})
        count = len(writer.lines)
        assert not await resident.write_mid_turn("C", message_id="C")
        assert len(writer.lines) == count
        await output._dispatch({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "next"}]}})

        async def consume():
            await output._dispatch({"type": "user", "isReplay": True, "uuid": json.loads(writer.lines[-1])["uuid"]})

        writer.drain = consume
        assert await resident.write_mid_turn("C", message_id="C")
        assert len(writer.lines) == count + 1

    asyncio.run(run())


def test_unconfirmed_input_claims_its_native_next_turn_without_rewriting_or_killing_background_work(tmp_path) -> None:
    import os

    async def run() -> None:
        resident = ResidentClaude(label="synthetic-next-turn")
        script = '''
import json, os, pathlib, subprocess, sys
first = json.loads(sys.stdin.readline())
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
pathlib.Path('child.pid').write_text(str(child.pid))
print(json.dumps(dict(first, isReplay=True)), flush=True)
print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'t1','input':{'run_in_background':True}}]}}), flush=True)
second = json.loads(sys.stdin.readline())
print(json.dumps({'type':'result','result':'A only','user_message_uuids':[first['uuid']]}), flush=True)
print(json.dumps(dict(second, isReplay=True)), flush=True)
print(json.dumps({'type':'system','subtype':'init'}), flush=True)
print(json.dumps({'type':'result','result':'B next turn','user_message_uuids':[second['uuid']]}), flush=True)
third = json.loads(sys.stdin.readline())
pathlib.Path('third.json').write_text(json.dumps(third))
print(json.dumps(dict(third, isReplay=True)), flush=True)
print(json.dumps({'type':'result','result':'C only','user_message_uuids':[third['uuid']]}), flush=True)
sys.stdin.read()
'''
        await resident.start([sys.executable, "-u", "-c", script], cwd=str(tmp_path), signature=1)
        process = resident._process
        tool = asyncio.Event()

        async def observe(event):
            if event.get("type") == "assistant":
                tool.set()

        first = asyncio.create_task(resident.run_turn("A", observe))
        try:
            await asyncio.wait_for(tool.wait(), 5)
            assert not await resident.write_mid_turn("B injected", message_id="B")
            assert (await asyncio.wait_for(first, 5)).result_event["result"] == "A only"
            events = []

            async def second_event(event):
                events.append(event)

            second = await asyncio.wait_for(resident.run_turn("B normal prompt", second_event,
                                                              tag=SimpleNamespace(message_id="B")), 5)
            assert second.result_event["result"] == "B next turn"
            assert len([event for event in events if event.get("type") == "result"]) == 1
            assert resident._process is process and process.returncode is None
            os.kill(int((tmp_path / "child.pid").read_text()), 0)
            third = await asyncio.wait_for(resident.run_turn("C", second_event), 5)
            assert third.result_event["result"] == "C only"
            assert json.loads((tmp_path / "third.json").read_text())["message"]["content"] == "C"
            assert resident._process is process
        finally:
            await resident.stop(kill=True)

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [True, False])
def test_stop_with_pending_input_withdraws_uuid_then_interrupts_and_preserves_resident(tmp_path, cancelled) -> None:
    from looper.agents.runtimes import StopMethod

    async def run() -> None:
        resident = ResidentClaude(label="synthetic-stop-input")
        script = '''
import json, pathlib, sys
first = json.loads(sys.stdin.readline())
print(json.dumps(dict(first, isReplay=True)), flush=True)
print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'t1'}]}}), flush=True)
second = json.loads(sys.stdin.readline())
print(json.dumps({'type':'system','subtype':'informational','content':'B queued'}), flush=True)
cancel = json.loads(sys.stdin.readline())
assert cancel['request'] == {'subtype':'cancel_async_message','message_uuid':second['uuid']}
print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':cancel['request_id'],'response':{'cancelled':CANCELLED,'message_uuid':second['uuid']}}}), flush=True)
interrupt = json.loads(sys.stdin.readline())
assert interrupt['request']['subtype'] == 'interrupt'
if not CANCELLED: assert interrupt['request']['cancel_queued'] is True
print(json.dumps({'type':'result','result':'','user_message_uuids':[first['uuid']]}), flush=True)
third = json.loads(sys.stdin.readline())
assert third['message']['content'] == 'C'
print(json.dumps(dict(third,isReplay=True)), flush=True)
print(json.dumps({'type':'result','result':'C survived','user_message_uuids':[third['uuid']]}), flush=True)
sys.stdin.read()
'''.replace('CANCELLED', repr(cancelled))
        await resident.start([sys.executable, "-u", "-c", script], cwd=str(tmp_path), signature=1)
        process = resident._process
        tool, queued = asyncio.Event(), asyncio.Event()
        cancel = TurnCancel()

        async def observe(event):
            if event.get("type") == "assistant":
                tool.set()
            if event.get("content") == "B queued":
                queued.set()

        first = asyncio.create_task(resident.run_turn("A", observe, cancel=cancel))
        try:
            await asyncio.wait_for(tool.wait(), 5)
            pending = asyncio.create_task(resident.write_mid_turn("B", message_id="B"))
            await asyncio.wait_for(queued.wait(), 5)
            cancel.request()
            result = await asyncio.wait_for(first, 10)
            assert result.stopped is StopMethod.CONTROL
            assert not await pending
            assert resident._process is process and process.returncode is None
            third = await asyncio.wait_for(resident.run_turn("C", observe), 5)
            assert third.result_event["result"] == "C survived"
            assert resident._process is process
        finally:
            await resident.stop(kill=True)

    asyncio.run(run())



def test_unconfirmed_drain_failure_withdraws_the_written_uuid_before_retry() -> None:
    async def run() -> None:
        resident, writer, turn = _running()

        async def observe(event):
            pass

        turn.on_event = observe
        output = resident._output = _Output(resident, asyncio.StreamReader(), None)
        output.turn = turn
        first_drain = True

        async def drain():
            nonlocal first_drain
            if first_drain:
                first_drain = False
                raise BrokenPipeError("drain failed after write")
            request = json.loads(writer.lines[-1])
            assert request["request"] == {
                "subtype": "cancel_async_message", "message_uuid": json.loads(writer.lines[0])["uuid"]}
            await output._dispatch({"type": "control_response", "response": {
                "subtype": "success", "request_id": request["request_id"],
                "response": {"cancelled": True}}})

        writer.drain = drain
        with pytest.raises(BrokenPipeError):
            await resident.write_mid_turn("B", message_id="B")
        assert len(writer.lines) == 2
        assert not turn.input_receipts and not output.queued and not output.discard
        assert resident._process is not None

    asyncio.run(run())


def test_stop_after_original_result_withdraws_reserved_next_input_without_killing(tmp_path) -> None:
    from looper.agents.runtimes import StopMethod

    async def run() -> None:
        resident = ResidentClaude(label="synthetic-stop-after-result")
        script = '''
import json, sys
first = json.loads(sys.stdin.readline())
print(json.dumps(dict(first, isReplay=True)), flush=True)
print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'t1'}]}}), flush=True)
second = json.loads(sys.stdin.readline())
print(json.dumps({'type':'result','result':'A only','user_message_uuids':[first['uuid']]}), flush=True)
cancel = json.loads(sys.stdin.readline())
assert cancel['request'] == {'subtype':'cancel_async_message','message_uuid':second['uuid']}
print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':cancel['request_id'],'response':{'cancelled':True,'message_uuid':second['uuid']}}}), flush=True)
third = json.loads(sys.stdin.readline())
assert third['message']['content'] == 'C'
print(json.dumps(dict(third,isReplay=True)), flush=True)
print(json.dumps({'type':'result','result':'C survived','user_message_uuids':[third['uuid']]}), flush=True)
sys.stdin.read()
'''
        await resident.start([sys.executable, "-u", "-c", script], cwd=str(tmp_path), signature=1)
        process = resident._process
        tool = asyncio.Event()
        cancel = TurnCancel()

        async def observe(event):
            if event.get("type") == "assistant":
                tool.set()
            if event.get("type") == "result":
                asyncio.get_running_loop().call_soon(cancel.request)

        first = asyncio.create_task(resident.run_turn("A", observe, cancel=cancel))
        try:
            await asyncio.wait_for(tool.wait(), 5)
            pending = asyncio.create_task(resident.write_mid_turn("B", message_id="B"))
            result = await asyncio.wait_for(first, 10)
            assert result.stopped is StopMethod.CONTROL
            assert not await pending
            assert not resident._output.queued
            assert resident._process is process and process.returncode is None

            async def ignore(event):
                pass

            third = await asyncio.wait_for(resident.run_turn("C", ignore), 5)
            assert third.result_event["result"] == "C survived"
            assert resident._process is process
        finally:
            await resident.stop(kill=True)

    asyncio.run(run())


@pytest.mark.parametrize("proof", ["cancelled_receipt", "native_result"])
def test_completed_original_waits_for_gentle_interrupt_proof_before_retiring(proof) -> None:
    async def run() -> None:
        resident, writer, turn = _running()
        written, interrupt_written = asyncio.Event(), asyncio.Event()

        async def observe(event):
            pass

        turn.on_event = observe
        output = resident._output = _Output(resident, asyncio.StreamReader(), None)
        output.turn = turn
        output._marks_turns = True
        request = None
        uuid = None

        async def drain():
            nonlocal request, uuid
            message = json.loads(writer.lines[-1])
            if message["type"] == "user":
                uuid = message["uuid"]
                written.set()
            elif message["request"]["subtype"] == "cancel_async_message":
                await output._dispatch({"type": "control_response", "response": {
                    "subtype": "success", "request_id": message["request_id"],
                    "response": {"cancelled": False}}})
            else:
                assert message["request"] == {"subtype": "interrupt", "cancel_queued": True,
                                              "spare_background_tasks": True}
                request = message
                interrupt_written.set()

        writer.drain = drain
        pending = asyncio.create_task(resident.write_mid_turn("advisory"))
        await written.wait()
        await output._dispatch({"type": "result", "user_message_uuids": [turn.uuid]})
        await asyncio.wait_for(interrupt_written.wait(), 5)
        assert not pending.done()  # writing CONTROL alone is not safe settlement
        assert resident._process is not None and resident._process.returncode is None
        if proof == "cancelled_receipt":
            await output._dispatch({"type": "control_response", "response": {
                "subtype": "success", "request_id": request["request_id"],
                "response": {"still_queued": [], "cancelled": [uuid]}}})
        else:
            await output._dispatch({"type": "user", "isReplay": True, "uuid": uuid})
            await output._dispatch({"type": "result", "user_message_uuids": [uuid]})
        assert not await pending
        assert resident._process is not None and not output.discard and not output.queued

    asyncio.run(run())


def test_consumption_during_failed_drain_cleanup_prevents_a_duplicate_retry() -> None:
    async def run() -> None:
        resident, writer, turn = _running()

        async def observe(event):
            pass

        turn.on_event = observe
        output = resident._output = _Output(resident, asyncio.StreamReader(), None)
        output.turn = turn
        first_drain = True

        async def drain():
            nonlocal first_drain
            if first_drain:
                first_drain = False
                raise BrokenPipeError("drain failed before the consumption receipt")
            request = json.loads(writer.lines[-1])
            uuid = json.loads(writer.lines[0])["uuid"]
            if request["request"]["subtype"] == "cancel_async_message":
                # Claude folded B while the failed writer was withdrawing it.
                await output._dispatch({"type": "user", "isReplay": True, "uuid": uuid})
                await output._dispatch({"type": "control_response", "response": {
                    "subtype": "success", "request_id": request["request_id"],
                    "response": {"cancelled": False}}})
            else:
                assert request["request"]["subtype"] == "interrupt"
                await output._dispatch({"type": "result", "user_message_uuids": [turn.uuid, uuid]})

        writer.drain = drain
        assert await resident.write_mid_turn("B", message_id="B") is True
        assert len([line for line in writer.lines if json.loads(line)["type"] == "user"]) == 1
        assert not turn.input_receipts and not output.queued and not output.discard
        assert resident._process is not None

    asyncio.run(run())


@pytest.mark.parametrize("superseded,respond", [(False, True), (False, False), (True, False)])
def test_unclaimed_native_turn_is_settled_before_resident_reuse(tmp_path, monkeypatch, caplog, superseded, respond):
    """Abandonment withdraws B; a different caller waits without rewriting it."""
    from looper.agents import claude_cli

    monkeypatch.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.3)
    monkeypatch.setattr(claude_cli, "RESERVED_INPUT_WAIT_SEC", 0.1, raising=False)

    async def run():
        resident = ResidentClaude(label="synthetic-unclaimed")
        script = (
            "import json, sys\n"
            "def emit(item): print(json.dumps(item), flush=True)\n"
            "first = json.loads(sys.stdin.readline())\n"
            "emit(dict(first, isReplay=True))\n"
            "emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':'a'}]}})\n"
            "second = json.loads(sys.stdin.readline())\n"
            "emit({'type':'result','result':'A','user_message_uuids':[first['uuid']]})\n"
            "emit(dict(second, isReplay=True))\n"
            # A subagent's result cannot release the native parent's buffer.
            "emit({'type':'result','parent_tool_use_id':'b-child','result':'child'})\n"
            "for line in sys.stdin:\n"
            "    item = json.loads(line)\n"
            "    if item['type'] == 'control_request':\n"
            "        with open('controls.jsonl','a') as log: log.write(line)\n"
            f"        if not {respond!r}: continue\n"
            "        request = item['request']\n"
            "        emit({'type':'control_response','response':{'subtype':'success',"
            "'request_id':item['request_id'],'response':{'cancelled':False}}})\n"
            "        if request['subtype'] == 'interrupt':\n"
            "            assert request['spare_background_tasks']\n"
            "            emit({'type':'result','result':'B interrupted','user_message_uuids':[second['uuid']]})\n"
            "    else:\n"
            "        emit(dict(item, isReplay=True))\n"
            "        emit({'type':'result','result':'C','user_message_uuids':[item['uuid']]})\n"
        )
        await resident.start([sys.executable, "-u", "-c", script], cwd=str(tmp_path), signature=1)
        process = resident._process
        tool, buffered = asyncio.Event(), asyncio.Event()

        async def observe(event):
            if event.get("type") == "assistant":
                tool.set()

        async def unfolded(event):
            if event.get("parent_tool_use_id") == "b-child":
                buffered.set()

        first = asyncio.create_task(resident.run_turn("A", observe))
        try:
            await asyncio.wait_for(tool.wait(), 3)
            assert not await resident.write_mid_turn("B", message_id="B", on_unfolded=unfolded)
            await asyncio.wait_for(first, 3)
            await asyncio.wait_for(buffered.wait(), 3)
            output = resident._output
            assert output._buffering is not None and not output.queued["B"].completed.is_set()
            if superseded:
                next_turn = resident.run_turn("C", observe, tag=SimpleNamespace(message_id="C"))
                with pytest.raises(RuntimeError, match="native input B did not complete") as raised:
                    await asyncio.wait_for(next_turn, 3)
                assert type(raised.value).__name__ == "ReservedInputTimeout"
                assert "refusing to interrupt or rewrite it" in caplog.text
                assert resident._process is process and process.returncode is None
                assert output.queued["B"].text == "B" and output._buffering is not None
                assert not (tmp_path / "controls.jsonl").exists()
                return  # neither C nor a duplicate B was written; B retains its native output
            else:
                await asyncio.wait_for(resident.discard_reserved_input("B"), 3)
                if respond:
                    result = await asyncio.wait_for(resident.run_turn("C", observe), 3)
                    assert result.result_event["result"] == "C"
            assert output.queued == {} and output._buffering is None
            if respond:
                assert resident._process is process and process.returncode is None
                controls = [json.loads(line)["request"] for line in (tmp_path / "controls.jsonl").read_text().splitlines()]
                assert [item["subtype"] for item in controls] == ["cancel_async_message", "interrupt"]
            else:
                assert resident._process is None and process.returncode is not None
        finally:
            await resident.stop(kill=True)
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    asyncio.run(run())
