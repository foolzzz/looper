"""Typed arrival decisions and process cleanup for a preempted background turn."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fake_claude import fake_claude
from looper.agents import lanes, lanes_settings, proc_tree
from looper.agents.claude_cli import ResidentClaude
from looper.agents.ops import Turn, TurnOrigin
from looper.agents.runtimes import StopMethod
from looper.agents.runtimes.cancel import StopCleanup, TurnCancel
from looper.agents.tiers.turn_token import TurnPhase, TurnToken


@pytest.mark.parametrize("origin,expected", [
    (TurnOrigin.USER, lanes.Lane.INTERACTIVE),
    (TurnOrigin.COMPLETION, lanes.Lane.BACKGROUND),
    (TurnOrigin.OWN, None),
])
def test_manager_lane(origin, expected):
    assert lanes.manager_lane(origin) is expected
    assert [int(lane) for lane in lanes.Lane] == [0, 1, 2, 3]


@pytest.mark.parametrize("origin,interrupt,merge,expected", [
    (None, False, False, lanes.Arrival.RUN),
    (None, True, True, lanes.Arrival.RUN),
    (TurnOrigin.USER, False, False, lanes.Arrival.QUEUE),
    (TurnOrigin.USER, True, False, lanes.Arrival.QUEUE),
    (TurnOrigin.USER, False, True, lanes.Arrival.MERGE),
    (TurnOrigin.USER, True, True, lanes.Arrival.MERGE),
    (TurnOrigin.COMPLETION, True, False, lanes.Arrival.PREEMPT),
    (TurnOrigin.COMPLETION, True, True, lanes.Arrival.PREEMPT),
    (TurnOrigin.COMPLETION, False, False, lanes.Arrival.QUEUE),
    (TurnOrigin.COMPLETION, False, True, lanes.Arrival.QUEUE),
    (TurnOrigin.OWN, True, False, lanes.Arrival.RUN),
])
def test_arrival_table(origin, interrupt, merge, expected):
    assert lanes.classify_arrival(origin, interrupt, merge) is expected


def _token(origin=TurnOrigin.COMPLETION):
    token = TurnToken("background", None, "web", "synthetic", datetime.now(timezone.utc))
    token.phase = TurnPhase.RUNNING
    token.ops_turn = Turn(channel="web", chat="synthetic", origin=origin)
    return token


def test_preemption_targets_its_session_and_never_settles_the_wake():
    token = _token()
    session = SimpleNamespace(running_token=token, claude=SimpleNamespace(can_interrupt=True))
    ops = Mock()
    assert lanes.preempt(session, ops=ops)
    assert token.cancel.requested and token.phase is TurnPhase.STOPPING
    assert token.stop.via == "preempt"
    assert token.cancel.cleanup is StopCleanup.REQUIRED
    ops.stop_turn.assert_called_once_with(token.ops_turn)
    assert token.live  # the owner still owes its fallback before settling the wake
    assert not lanes.preempt(session, ops=ops)
    ops.stop_turn.assert_called_once()


@pytest.mark.parametrize("origin,phase,interrupt", [
    (TurnOrigin.USER, TurnPhase.RUNNING, True),
    (TurnOrigin.OWN, TurnPhase.RUNNING, True),
    (TurnOrigin.COMPLETION, TurnPhase.ENDED, True),
    (TurnOrigin.COMPLETION, TurnPhase.STOPPING, True),
    (TurnOrigin.COMPLETION, TurnPhase.RUNNING, False),
])
def test_preemption_refuses_non_live_background_or_incapable_runtime(origin, phase, interrupt):
    token = _token(origin)
    token.phase = phase
    session = SimpleNamespace(running_token=token, claude=SimpleNamespace(can_interrupt=interrupt))
    ops = Mock()
    assert not lanes.preempt(session, ops=ops)
    ops.stop_turn.assert_not_called()
    assert not token.cancel.requested


def test_background_process_tracking_is_off_by_default(monkeypatch):
    token = _token()
    monkeypatch.setattr(lanes_settings, "enabled", lambda: False)
    lanes.prepare_background(SimpleNamespace(), token)
    assert token.cancel.cleanup is StopCleanup.NONE
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
    lanes.prepare_background(SimpleNamespace(), token)
    assert token.cancel.cleanup is StopCleanup.TRACK


def test_frequent_preemption_warns_once_per_day(monkeypatch, caplog):
    monkeypatch.setattr(lanes, "_warning_day", None)
    monkeypatch.setattr(lanes, "_daily_preemptions", 0)
    with caplog.at_level(logging.WARNING, logger="looper.agents.lanes"):
        for _ in range(25):
            session = SimpleNamespace(running_token=_token(), claude=SimpleNamespace(can_interrupt=True))
            assert lanes.preempt(session, ops=None)
    assert sum("preemptions" in record.message for record in caplog.records) == 1


_CHILDREN = '''\
import json, os, subprocess, sys
def out(event):
    print(json.dumps(event), flush=True)
for raw in sys.stdin:
    msg = json.loads(raw)
    if msg['type'] == 'control_request':
        out({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
             'session_id': 'synthetic', 'total_cost_usd': 0, 'num_turns': 1})
        continue
    text = msg['message']['content']
    out({'type': 'system', 'subtype': 'init', 'session_id': 'synthetic'})
    if text in ('old', 'background'):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 start_new_session=True)
        with open(text + '.pid', 'w') as stream:
            stream.write(str(child.pid))
        out({'type': 'stream_event', 'event': {'type': 'content_block_delta',
             'delta': {'type': 'text_delta', 'text': 'working'}}})
    if text != 'background':
        out({'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': 'synthetic', 'result': str(os.getpid()), 'total_cost_usd': 0, 'num_turns': 1})
'''


@pytest.mark.parametrize("checkpoint_available", [True, False])
def test_control_preemption_cleans_new_children_or_kills_the_resident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint_available: bool,
):
    executable = fake_claude(tmp_path / "claude", _CHILDREN)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    if not checkpoint_available:
        async def unavailable(self):
            return None
        monkeypatch.setattr(proc_tree.TurnTree, "checkpoint", unavailable)

    async def run():
        resident = ResidentClaude(label="preemption-test")
        await resident.start([executable], cwd=str(tmp_path), signature=None)
        initial_pid = resident.pid
        async def event(_):
            pass
        old = new = None
        task = None
        try:
            await asyncio.wait_for(resident.run_turn("old", event), 5)
            old = int((tmp_path / "old.pid").read_text())
            cancel = TurnCancel()
            cancel.cleanup = StopCleanup.TRACK
            task = asyncio.create_task(resident.run_turn("background", event, cancel=cancel))
            for _ in range(500):
                pid_file = tmp_path / "background.pid"
                if pid_file.exists() and pid_file.read_text():
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("the background turn did not start")
            new = int(pid_file.read_text())
            cancel.cleanup = StopCleanup.REQUIRED
            cancel.request()
            result = await asyncio.wait_for(task, 8)
            table = await asyncio.to_thread(proc_tree._snapshot)
            assert table is not None and new not in table
            if checkpoint_available:
                assert result.stopped is StopMethod.CONTROL
                assert old in table and resident.pid == initial_pid
                again = await asyncio.wait_for(resident.run_turn("next", event), 5)
                assert again.result_event['result'] == str(initial_pid)
            else:
                assert result.stopped is StopMethod.KILL
                assert not resident.started and old not in table
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await resident.stop(kill=True)
    asyncio.run(run())


def test_preempted_completion_note_and_metadata_are_once_and_bounded():
    token = _token()
    session = SimpleNamespace(stopped_notes=["first", "second", "third"])
    assert lanes.completion_metadata(token) == {}
    token.cancel.cleanup = StopCleanup.REQUIRED
    token.preempted_by = "synthetic-user-message"
    expected = ("[Looper] Before this message, your report on 2 ended task(s) was interrupted so you could "
                "answer. Looper sent their notices itself; do not report them again unless asked.")
    lanes.completion_note(session, token, 2)
    lanes.completion_note(session, token, 2)
    assert session.stopped_notes == ["second", "third", expected]
    assert lanes.completion_metadata(token) == {
        "lane": {"lane": "background", "preempted_by": "synthetic-user-message"}}
    unpreempted = _token()
    unpreempted.cancel.cleanup = StopCleanup.TRACK
    assert lanes.completion_metadata(unpreempted) == {"lane": {"lane": "background", "preempted_by": None}}


def test_route_arrival_uses_target_runtime_and_retains_mid_turn_merge_rules():
    from unittest.mock import AsyncMock
    from looper.agents.message import AgentAddress, MessageEnvelope, MessageType

    envelope = MessageEnvelope.create(source=AgentAddress(agent_type="channel", agent_id="synthetic"),
        target=AgentAddress(agent_type="manager", agent_id="manager"),
        message_type=MessageType.USER_MESSAGE, payload={"text": "hello"})

    async def run():
        ops = Mock()
        agent = SimpleNamespace(ops=ops, can_interrupt=False, _mid_turn=SimpleNamespace(merge=AsyncMock(return_value=True)))
        token = _token()
        session = SimpleNamespace(running_token=token, claude=SimpleNamespace(can_interrupt=True))
        assert not await lanes.route_arrival(agent, session, envelope)
        assert token.preempted_by == envelope.message_id
        agent._mid_turn.merge.assert_not_awaited()
        agent.can_interrupt = True
        token = _token()
        session.running_token = token
        session.claude.can_interrupt = False
        assert not await lanes.route_arrival(agent, session, envelope)
        assert not token.cancel.requested
        session.running_token = _token(TurnOrigin.USER)
        assert await lanes.route_arrival(agent, session, envelope)
        agent._mid_turn.merge.assert_awaited_once_with(session, envelope)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["unread_child", "unread_detached_child", "snapshot", "reused_child"])
def test_process_cleanup_never_signals_a_process_with_uncertain_identity(monkeypatch, failure):
    table = {100001: (1, 100001)}
    starts = {100001: "resident"}
    signals = []
    monkeypatch.setattr(proc_tree, "_snapshot", lambda: dict(table))
    monkeypatch.setattr(proc_tree, "_start_times", lambda pids: {pid: starts[pid] for pid in pids if pid in starts})
    monkeypatch.setattr(proc_tree.os, "kill", lambda pid, sig: signals.append(pid))
    process = SimpleNamespace(pid=100001, returncode=None)

    async def run():
        tree = proc_tree.TurnTree.track(process, label="synthetic")
        checkpoint = await tree.checkpoint()
        assert checkpoint == frozenset({100001})
        table[100002] = (100001, 100002)
        if failure == "reused_child":
            starts[100002] = "original child"
            await tree.refresh(force=True)
            starts[100002] = "unrelated replacement"
            table[100002] = (1, 100002)
        if failure == "unread_detached_child":
            await tree.refresh(force=True)
            table[100002] = (1, 100002)
        if failure == "snapshot":
            monkeypatch.setattr(proc_tree, "_snapshot", lambda: None)
        assert await tree.end_since(checkpoint) is (failure == "reused_child")
        assert signals == []
    asyncio.run(run())
