"""Operator input on the Manager bus: one running result, durable acknowledgements and queue fallbacks."""
from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest

from looper.agents import chat_settings, operator_identity, session_routing
from looper.agents.claude_cli import ResidentTurn
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.manager import ReplyOutcome
from looper.agents.ops import TurnOrigin
from looper.agents.ops.service import CONFIRM, ManagerOps, Platform, _Pending
from looper.agents.runtimes import Capability
from tests.db_support import DSN, run_db, until
from tests.lark_cli_stand_in import OPEN_ID, OTHER
from tests.test_manager_session_routing_db import GROUP_A, _lark, _web, _Resident, _Rig

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


class InputResident(_Resident):
    def __init__(self) -> None:
        super().__init__("sess-main", hold=True)
        self.inputs: list[str] = []
        self.accept = True
        self.failure: Exception | None = None

    async def write_mid_turn(self, text: str) -> bool:
        if self.failure:
            raise self.failure
        if not self.accept:
            return False
        self.inputs.append(text)
        return True

    async def run_turn(self, text: str, on_event: Any, **kwargs: Any) -> ResidentTurn:
        from looper.agents.runtimes.cancel import TurnCancelled
        try:
            return await kwargs["cancel"].race(super().run_turn(text, on_event, **kwargs))
        except TurnCancelled:
            raise


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(chat_settings, "_mid_turn_input_enabled", True, raising=False)
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    async def operator(channel_id):
        return OPEN_ID
    monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
    return monkeypatch


def run_case(body, monkeypatch, tmp_path):
    async def test(pool):
        rig = _Rig(tmp_path)
        rig.main = rig.manager._sessions["main"].claude = InputResident()
        await rig.start()
        try:
            await body(rig, pool)
        finally:
            await rig.close()
    run_db(test, monkeypatch, timeout=45)


async def start_turn(rig, first=None):
    first = first or _web("first")
    await rig.bus.publish(first)
    await asyncio.wait_for(rig.main.running.wait(), 15)
    return first


async def drained(rig):
    await until(lambda: rig.bus.get_queue("manager").empty())
    await asyncio.sleep(0.1)


@pytest.mark.parametrize("routing,cross_chat", [(True, False), (True, True), (False, False)])
def test_merge_one_record_reply_destinations_and_settle_after_delivery(enabled, tmp_path, routing, cross_chat):
    enabled.setattr(session_routing, "_routing_enabled", routing)
    enabled.setattr(chat_settings, "_metrics_enabled", True)
    async def test(rig, pool):
        first = await start_turn(rig)
        second = _lark("second", "oc_synthetic_operator", OPEN_ID, group=False) if cross_chat else _web("second")
        await rig.bus.publish(second)
        await until(lambda: bool(rig.main.inputs))
        assert "[Looper] Another message from the user" in rig.main.inputs[0]
        assert rig.main.inputs[0].endswith("second")
        assert len(rig.main.turns) == 1
        assert [e for e in rig.bus.journal.undelivered() if e.target.agent_id == "manager"]  # merged input is still replayable before the reply
        rig.main.release.set()
        answer = await rig.reply(first)
        assert await rig.reply(second) == (answer if cross_chat else "（已并入上一条的回复）")
        await until(lambda: not [e for e in rig.bus.journal.undelivered() if e.target.agent_id == "manager"])
        rows = await pool.fetch("SELECT metadata FROM llm_records WHERE agent_id='manager'")
        assert len(rows) == 1
        merged = json.loads(rows[0]["metadata"])["merged"]
        assert len(merged) == 1 and merged[0]["message_id"] == second.message_id
        assert merged[0]["received_at"] == second.timestamp
        assert merged[0]["turn_metrics"]["delivery"] == "reply"
        assert merged[0]["turn_metrics"]["first_reply_ms"] is not None
    run_case(test, enabled, tmp_path)


@pytest.mark.parametrize("case", ["command", "gate_reply", "intent", "attachment", "quote", "other_sender",
                                  "other_session", "client", "confirmation", "no_capability", "no_interface",
                                  "write_false", "write_error", "write_unexpected", "admission_error",
                                  "routing_off_other_chat", "background"])
def test_ineligible_messages_stay_queued(enabled, tmp_path, case):
    async def test(rig, pool):
        if case == "routing_off_other_chat":
            enabled.setattr(session_routing, "_routing_enabled", False)
        if case == "no_interface":
            rig.main.write_mid_turn = None
        if case == "no_capability":
            runtime = rig.manager._sessions["main"].claude_runtime
            enabled.setattr(type(runtime), "capabilities", runtime.capabilities - {Capability.MID_TURN_INPUT})
        first = await start_turn(rig)
        second = _web("second")
        if case == "command":
            second.payload["text"] = "/help"
        if case == "intent":
            second.payload["text"] = "建 I-0042"
        if case == "gate_reply":
            second = _lark("second", "oc_synthetic_operator", OPEN_ID, group=False)
            second.payload["channel_context"]["reply_to"] = "om_synthetic_gate"
        if case == "attachment":
            second.payload["attachments"] = [{"state": "not_received", "kind": "file"}]
        if case == "quote":
            second.payload["quote_ref"] = {"message_id": "om_synthetic_quote"}
        if case == "other_sender":
            second = _lark("second", "oc_synthetic_other", OTHER, group=False)
        if case == "other_session":
            second = _lark("second", GROUP_A, OPEN_ID, group=True)
        if case == "client":
            second.payload["channel_context"]["client"] = {"id": "client-test", "name": "test"}
        if case == "confirmation":
            rig.manager.ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="tok")
            original_turn = rig.manager._sessions["main"].running_token.ops_turn
            rig.manager.ops._pending[original_turn.chat_key] = _Pending(
                "synthetic-confirmation", CONFIRM, {}, "synthetic impact", first.message_id,
                rig.manager.ops._clock(), {}, answer_turn=first.message_id)
        if case == "write_false":
            rig.main.accept = False
        if case == "write_error":
            rig.main.failure = BrokenPipeError("synthetic")
        if case == "write_unexpected":
            rig.main.failure = ValueError("synthetic")
        if case == "admission_error":
            def failure(_):
                raise ValueError("synthetic")
            enabled.setattr(rig.manager._mid_turn, "_pending", failure)
        if case == "routing_off_other_chat":
            second = _lark("second", "oc_synthetic_operator", OPEN_ID, group=False)
        if case == "background":
            rig.manager._sessions["main"].running_token.ops_turn.origin = TurnOrigin.COMPLETION
        await rig.bus.publish(second)
        await drained(rig)
        assert rig.main.inputs == []
        if case not in {"other_session", "other_sender"}:
            rig.collect()
            assert second.message_id not in rig.replies
        rig.main.release.set()
        await rig.reply(first)
        if case in {"write_unexpected", "admission_error"}:
            await rig.reply(second)
            assert rig.manager.is_healthy()
            third = _web("after failure")
            await rig.bus.publish(third)
            await rig.reply(third)
    run_case(test, enabled, tmp_path)


@pytest.mark.parametrize("text,command", [
    (" /NEW ", True), ("/help@synthetic_bot", True), ("/profile 1", True),
    ("/gate 2 answer", True), ("/todo", True), ("/stop", True),
    ("/tmp/report.txt", False), ("/help with this", False), ("/new project", False),
])
def test_merge_uses_exact_command_boundaries(enabled, tmp_path, text, command):
    async def test(rig, pool):
        await start_turn(rig)
        second = _web(text)
        await rig.bus.publish(second)
        await drained(rig)
        assert bool(rig.main.inputs) is not command
        rig.main.release.set()
        await rig.reply(second)
    run_case(test, enabled, tmp_path)


def test_only_three_inputs_merge_per_turn(enabled, tmp_path):
    async def test(rig, pool):
        await start_turn(rig)
        for number in range(3):
            await rig.bus.publish(_web(f"addition-{number}"))
            await until(lambda: len(rig.main.inputs) == number + 1)
        fourth = _web("fourth")
        await rig.bus.publish(fourth)
        await drained(rig)
        assert len(rig.main.inputs) == 3
        rig.main.release.set()
        await rig.reply(fourth)
        assert len(rig.main.turns) == 2
    run_case(test, enabled, tmp_path)


def test_stop_from_merged_chat_stops_original_turn(enabled, tmp_path):
    enabled.setattr(chat_settings, "_stop_enabled", True)
    async def test(rig, pool):
        first = await start_turn(rig)
        second = _lark("second", "oc_synthetic_operator", OPEN_ID, group=False)
        await rig.bus.publish(second)
        await until(lambda: bool(rig.main.inputs))
        await rig.bus.publish(_lark("/stop", "oc_synthetic_operator", OPEN_ID, group=False))
        await rig.reply(first)
        await rig.reply(second)
        assert rig.replies[first.message_id][-1].payload["outcome"] == ReplyOutcome.STOPPED.value
        assert rig.replies[second.message_id][-1].payload["outcome"] == ReplyOutcome.STOPPED.value
        assert len(rig.main.turns) == 1
    run_case(test, enabled, tmp_path)


def test_shutdown_keeps_merged_message_for_replay(enabled, tmp_path):
    async def test(rig, pool):
        first = await start_turn(rig)
        second = _web("second")
        await rig.bus.publish(second)
        await until(lambda: bool(rig.main.inputs))
        await rig.manager.stop()
        pending = {envelope.message_id for envelope in [e for e in rig.bus.journal.undelivered() if e.target.agent_id == "manager"]}
        assert {first.message_id, second.message_id} <= pending
    run_case(test, enabled, tmp_path)


def test_a_queued_command_keeps_later_input_in_order(enabled, tmp_path):
    async def test(rig, pool):
        await start_turn(rig)
        await rig.bus.publish(_web("/help"))
        second = _web("after command")
        await rig.bus.publish(second)
        await drained(rig)
        assert rig.main.inputs == []
        rig.main.release.set()
        await rig.reply(second)
        assert len(rig.main.turns) == 2 and "after command" in rig.main.turns[-1]
    run_case(test, enabled, tmp_path)


def test_real_manager_resident_path_with_scripted_cli(enabled, tmp_path):
    from looper.agents.claude_cli import ResidentClaude
    from tests.test_manager_reply import _fake_claude

    async def test(pool):
        rig = _Rig(tmp_path)
        rig.manager._sessions["main"].claude = ResidentClaude(label="synthetic-manager")
        rig.manager._config.claude_bin = _fake_claude(tmp_path, (
            "first = json.loads(sys.stdin.readline())\n"
            "print(json.dumps(dict(first, isReplay=True)), flush=True)\n"
            "print(json.dumps({'type':'system','subtype':'init','session_id':'synthetic-session'}), flush=True)\n"
            "open('ready', 'w').close()\n"
            "second = json.loads(sys.stdin.readline())\n"
            "open('inputs.json', 'w').write(json.dumps([first, second]))\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'synthetic-session',"
            "'result':'handled first and second'}), flush=True)\n"
            "sys.stdin.read()\n"
        ))
        await rig.start()
        try:
            first, second = _web("first"), _web("second")
            await rig.bus.publish(first)
            await until(lambda: (tmp_path / "ready").exists()
                        and rig.manager._claude._user_turn.started)
            await rig.bus.publish(second)
            assert await rig.reply(first) == "handled first and second"
            assert await rig.reply(second) == "（已并入上一条的回复）"
            inputs = json.loads((tmp_path / "inputs.json").read_text())
            assert len(inputs) == 2 and inputs[1]["message"]["content"].endswith("second")
            rows = await pool.fetch("SELECT metadata FROM llm_records WHERE agent_id='manager'")
            assert len(rows) == 1
            await until(lambda: not [e for e in rig.bus.journal.undelivered() if e.target.agent_id == "manager"])
            metadata = json.loads(await pool.fetchval("SELECT metadata FROM llm_records WHERE agent_id='manager'"))
            assert metadata["merged"][0]["message_id"] == second.message_id
        finally:
            await rig.close()
    run_db(test, enabled, timeout=45)


@pytest.mark.parametrize("timing", ["buffered", "drain", "kernel_before_write", "kernel_after_write"])
def test_buffered_result_queues_followup_once_without_merging_or_settling(enabled, tmp_path, timing):
    """Drive the real Manager/read loop while controlling exactly when stdout is consumed."""
    from looper.agents.claude_cli import ResidentClaude
    from tests.test_manager_reply import _fake_claude

    async def test(pool):
        kernel = timing.startswith("kernel_")
        result_during_drain = timing in {"drain", "kernel_after_write"}
        read_end, write_end = socket.socketpair()
        loop = asyncio.get_running_loop()
        rig = _Rig(tmp_path)
        resident = ResidentClaude(label="synthetic-manager-race")
        rig.manager._sessions["main"].claude = resident
        rig.manager._config.claude_bin = _fake_claude(tmp_path, (
            "import pathlib, time\n"
            "def read_input():\n"
            "    message = json.loads(sys.stdin.readline())\n"
            "    with open('consumed.jsonl', 'a') as log: log.write(json.dumps(message) + '\\n')\n"
            "    print(json.dumps(dict(message, isReplay=True)), flush=True)\n"
            "    print(json.dumps({'type':'system','subtype':'init','session_id':'synthetic-race'}), flush=True)\n"
            "read_input()\n"
            "if not pathlib.Path('ready').exists():\n"
            "    pathlib.Path('ready').touch()\n"
            "    while not pathlib.Path('read-followup').exists(): time.sleep(0.01)\n"
            "    read_input()\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'session_id':'synthetic-race','result':'handled queued followup'}), flush=True)\n"
            "sys.stdin.read()\n"
        ))
        await rig.start()
        try:
            first, second = _web("race-original"), _web("race-followup")
            await rig.bus.publish(first)
            await until(lambda: (tmp_path / "ready").exists() and resident._user_turn.started)
            running = rig.manager._mid_turn._turns[first.message_id]
            process, output = resident._process, resident._output
            original_pid = resident.pid
            result = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                "session_id": "synthetic-race", "result": "original only"}).encode() + b"\n"
            if kernel:
                # An actual selector read callback, not a pre-scheduled task wake-up.
                loop.add_reader(read_end, lambda: output._stdout.feed_data(read_end.recv(4096)))
                await until(lambda: output._stdout._waiter is not None)
            release_result = asyncio.Event()
            dispatch_event = output._dispatch
            async def dispatch(event):
                if event["type"] == "result":
                    await release_result.wait()
                await dispatch_event(event)
            enabled.setattr(output, "_dispatch", dispatch)
            writer = process.stdin
            writes = []
            class Writer:
                def __getattr__(self, name):
                    return getattr(writer, name)

                def write(self, line):
                    writes.append(json.loads(line))
                    writer.write(line)
                    if timing == "kernel_after_write":
                        write_end.sendall(result)

                async def drain(self):
                    if timing == "drain":
                        release_result.set()
                        await until(lambda: not running.accepting)
                    resumed = []
                    loop.call_soon(resumed.append, True)
                    await writer.drain()
                    if kernel:
                        assert not resumed  # short line: real drain did not yield
            process.stdin = Writer()
            write = resident.write_mid_turn
            async def followup(text):
                # The result is already in stdout before B's admission check. In
                # the drain case the reader deliberately cannot finish until B writes.
                if timing == "kernel_before_write":
                    assert output._stdout._waiter is not None
                    write_end.sendall(result)
                elif not kernel:
                    output._stdout.feed_data(result)
                if timing != "drain":
                    release_result.set()
                return await write(text)
            enabled.setattr(resident, "write_mid_turn", followup)
            queued = []
            dispatch_message = rig.manager._router.dispatch
            def queue(session, envelope):
                if envelope.message_id == second.message_id:
                    queued.append(envelope.message_id)
                dispatch_message(session, envelope)
            enabled.setattr(rig.manager._router, "dispatch", queue)
            # Hold the queued turn before it sends or settles anything.
            queued_turn, proceed = asyncio.Event(), asyncio.Event()
            handle = rig.manager._handle
            async def held_handle(envelope):
                if envelope.message_id == second.message_id:
                    queued_turn.set()
                    await proceed.wait()
                return await handle(envelope)
            enabled.setattr(rig.manager, "_handle", held_handle)
            statuses = []
            send_status = rig.manager._send_status
            async def status(envelope, text, **kwargs):
                if envelope.message_id == second.message_id:
                    statuses.append(text)
                await send_status(envelope, text, **kwargs)
            enabled.setattr(rig.manager, "_send_status", status)
            await rig.bus.publish(second)
            assert await rig.reply(first) == "original only"
            assert running.inputs == []  # the already-produced answer cannot cover B
            await asyncio.wait_for(queued_turn.wait(), 5)
            rig.collect()
            assert queued == [second.message_id]
            assert rig.manager._mid_turn.parent(second.message_id) == second.message_id
            assert second.message_id not in rig.replies
            assert "已并入当前回复" not in statuses
            assert second.message_id in {e.message_id for e in rig.bus.journal.undelivered()}
            assert len(writes) == int(result_during_drain)
            if result_during_drain:
                assert process.returncode is not None  # ambiguous pipe is gone before dispatch resumes
            (tmp_path / "read-followup").touch()
            proceed.set()
            assert await rig.reply(second) == "handled queued followup"
            assert queued == [second.message_id]
            assert (resident.pid != original_pid) is result_during_drain
            consumed = [json.loads(line) for line in (tmp_path / "consumed.jsonl").read_text().splitlines()]
            assert len(consumed) == 2 and "race-followup" in consumed[1]["message"]["content"]
            await until(lambda: not [e for e in rig.bus.journal.undelivered() if e.target.agent_id == "manager"])
            rows = await pool.fetch("SELECT metadata FROM llm_records WHERE agent_id='manager'")
            assert len(rows) == 2 and all(not json.loads(row["metadata"]).get("merged") for row in rows)
        finally:
            loop.remove_reader(read_end)
            read_end.close()
            write_end.close()
            await rig.close()
    run_db(test, enabled, timeout=45)


def test_stop_while_merged_write_is_draining_stops_original(enabled, tmp_path):
    enabled.setattr(chat_settings, "_stop_enabled", True)
    async def test(rig, pool):
        first = await start_turn(rig)
        written, drain = asyncio.Event(), asyncio.Event()
        write = rig.main.write_mid_turn
        async def blocked_write(text):
            result = await write(text)
            written.set()
            await drain.wait()
            return result
        rig.main.write_mid_turn = blocked_write
        second = _lark("second", "oc_synthetic_operator", OPEN_ID, group=False)
        await rig.bus.publish(second)
        await written.wait()
        parent = rig.manager.turn_for(first.message_id)
        await rig.bus.publish(_lark("/stop", "oc_synthetic_operator", OPEN_ID, group=False))
        await until(lambda: parent.cancel.requested, timeout=1)
        drain.set()
        await rig.reply(first)
        await rig.reply(second)
        assert rig.replies[second.message_id][-1].payload["outcome"] == "stopped"
    run_case(test, enabled, tmp_path)


def test_withdrawn_queued_input_is_not_merged(enabled, tmp_path):
    enabled.setattr(chat_settings, "_stop_enabled", True)
    async def test(rig, pool):
        await start_turn(rig)
        second = _web("withdrawn")
        checking, release = asyncio.Event(), asyncio.Event()
        operator = rig.manager._mid_turn._operator
        async def held_identity(envelope):
            checking.set()
            await release.wait()
            return await operator(envelope)
        enabled.setattr(rig.manager._mid_turn, "_operator", held_identity)
        await rig.bus.publish(second)
        await checking.wait()
        await rig.manager.stop_turn(second.message_id, via="dashboard")
        release.set()
        await drained(rig)
        assert rig.main.inputs == []
        rig.main.release.set()
        await rig.reply(second)
        assert rig.replies[second.message_id][-1].payload["outcome"] == "stopped"
        assert len(rig.main.turns) == 1
    run_case(test, enabled, tmp_path)


def test_stopped_additions_do_not_replay_if_shutdown_precedes_reply(enabled, tmp_path):
    enabled.setattr(chat_settings, "_stop_enabled", True)
    async def test(rig, pool):
        first = await start_turn(rig)
        second = _web("second")
        await rig.bus.publish(second)
        await until(lambda: bool(rig.main.inputs))
        await rig.manager.stop_turn(first.message_id, via="dashboard")
        await rig.manager.stop()
        pending = {e.message_id for e in rig.bus.journal.undelivered()}
        assert first.message_id not in pending and second.message_id not in pending
    run_case(test, enabled, tmp_path)


@pytest.mark.parametrize("disable_before_retry", [False, True])
def test_resume_retry_preserves_accepted_inputs(enabled, tmp_path, disable_before_retry):
    from dataclasses import replace
    async def test(rig, pool):
        session = rig.manager._sessions["main"]
        session.session_id, session.session_runtime, session.session_loaded = "lost-session", "claude-code", True
        run = rig.main.run_turn
        async def loses_resume(*args, **kwargs):
            result = await run(*args, **kwargs)
            return replace(result, session_started=False) if len(rig.main.turns) == 1 else result
        rig.main.run_turn = loses_resume
        first = await start_turn(rig)
        second = _web("second")
        await rig.bus.publish(second)
        await until(lambda: bool(rig.main.inputs))
        if disable_before_retry:
            enabled.setattr(chat_settings, "_mid_turn_input_enabled", False)
        rig.main.release.set()
        await rig.reply(first)
        await rig.reply(second)
        assert len(rig.main.turns) == 2 and rig.main.inputs[0] in rig.main.turns[-1]
        await until(lambda: not rig.manager._mid_turn._parents)
        rows = await pool.fetch("SELECT metadata FROM llm_records WHERE agent_id='manager' ORDER BY created_at")
        assert json.loads(rows[-1]["metadata"])["merged"][0]["message_id"] == second.message_id
    run_case(test, enabled, tmp_path)


@pytest.mark.parametrize("channel", ["lark", "telegram"])
def test_same_chat_bots_receive_one_reply_and_merged_ack_metrics(enabled, tmp_path, channel):
    from datetime import datetime, timezone
    from looper.agents import turn_metrics

    enabled.setattr(chat_settings, "_metrics_enabled", True)
    async def test(rig, pool):
        collect = rig.collect
        telegram = channel_address("telegram", "channel-telegram")
        def collect_all():
            collect()
            queue = rig.bus.get_queue(telegram.agent_id)
            while not queue.empty():
                envelope = queue.get_nowait()
                if envelope.message_type is MessageType.AGENT_REPLY:
                    rig.replies.setdefault(envelope.reply_to, []).append(envelope)
        rig.collect = collect_all
        def message(text):
            if channel == "lark":
                return _lark(text, "oc_synthetic_operator", OPEN_ID, group=False)
            return MessageEnvelope.create(source=telegram, target=MANAGER_ADDRESS,
                message_type=MessageType.USER_MESSAGE, payload={"text": text,
                "channel_context": {"chat_id": "101", "user_id": "101", "message_id": text}})
        if channel == "telegram":
            async def operator(_): return "101"
            enabled.setattr(operator_identity, "operator_sender_id", operator)
        first = await start_turn(rig, message("first"))
        second = message("second")
        key = second.source.agent_id, second.payload["channel_context"]["message_id"]
        turn_metrics.ack(*key, datetime.now(timezone.utc))
        await rig.bus.publish(second)
        await until(lambda: bool(rig.main.inputs))
        rig.main.release.set()
        await rig.reply(first)
        await until(lambda: not [e for e in rig.bus.journal.undelivered() if e.target.agent_id == "manager"])
        rig.collect()
        assert second.message_id not in rig.replies  # one final reply in this chat
        metadata = json.loads(await pool.fetchval("SELECT metadata FROM llm_records WHERE agent_id='manager'"))
        metrics = metadata["merged"][0]["turn_metrics"]
        assert metrics["ack"] == "reaction" and metrics["ack_ms"] is not None
        assert metrics["first_reply_ms"] is not None and not turn_metrics.ack_board.contains(*key)
    run_case(test, enabled, tmp_path)


def test_cross_chat_reply_redacts_original_attachment_paths(enabled, tmp_path):
    async def test(rig, pool):
        first = await start_turn(rig)
        attachment = str(tmp_path / "chat-attachments" / "synthetic" / "private file.txt")
        first.payload["attachments"] = [{"path": attachment}]
        rig.main.reply_text = "read " + attachment
        second = _lark("second", "oc_synthetic_operator", OPEN_ID, group=False)
        await rig.bus.publish(second)
        await until(lambda: bool(rig.main.inputs))
        rig.main.release.set()
        await rig.reply(first)
        assert attachment not in await rig.reply(second)
    run_case(test, enabled, tmp_path)


def test_hidden_delivery_followup_does_not_replay_consumed_additions(enabled, tmp_path):
    from dataclasses import replace

    from looper.agents.delivery_guard import FOLLOWUP_MESSAGE

    enabled.setattr(chat_settings, "_delivery_guard_enabled", True)

    async def test(rig, pool):
        run_turn = rig.main.run_turn

        async def empty_first(text, on_event, **kwargs):
            result = await run_turn(text, on_event, **kwargs)
            if len(rig.main.turns) == 1:
                return replace(result, result_event={**result.result_event, "result": ""})
            return result

        enabled.setattr(rig.main, "run_turn", empty_first)
        first = await start_turn(rig)
        second = _web("already consumed addition")
        await rig.bus.publish(second)
        await until(lambda: bool(rig.main.inputs))
        rig.main.release.set()
        assert await rig.reply(first) == "sess-main 回复 2"
        assert await rig.reply(second) == "（已并入上一条的回复）"
        assert rig.main.turns[1:] == [FOLLOWUP_MESSAGE]
        await until(lambda: not rig.manager._mid_turn._turns)

    run_case(test, enabled, tmp_path)
