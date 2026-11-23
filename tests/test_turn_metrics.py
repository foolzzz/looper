"""Bounded, best-effort metrics never hold up delivery."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest


def test_ack_board_eviction_and_take():
    from looper.agents.turn_metrics import AckBoard
    board = AckBoard()
    now = datetime.now(timezone.utc)
    for n in range(2001):
        board.ack('channel', str(n), now)
    assert not board.contains('channel', '0')
    assert board.take('channel', '1') == now
    assert not board.contains('channel', '1')
    board.ack('other', '2', None)
    assert board.contains('other', '2')
    assert board.take('other', '2') is None


def test_turn_metadata_contract():
    from looper.agents.turn_metrics import AckKind, ChatKind, TurnDelivery, TurnTimes
    from looper.agents.ops import TurnOrigin
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = TurnTimes(now, ack_at=now + timedelta(milliseconds=80), ack=AckKind.REACTION,
                      first_reply_at=now + timedelta(milliseconds=125), tool_calls=2, chat=ChatKind.DIRECT)
    metadata = times.to_metadata(TurnDelivery.REPLY, TurnOrigin.USER, True)
    assert type(metadata['origin']) is str
    assert metadata == {
        'v': 1, 'origin': 'user', 'chat': 'direct', 'received_at': '2026-01-01T00:00:00Z',
        'ack': 'reaction', 'ack_ms': 80, 'first_reply_ms': 125, 'tool_calls': 2,
        'delivery': 'reply', 'replayed': True}


def test_metrics_write_is_bounded_and_failure_is_throttled(monkeypatch, caplog):
    from looper.agents import turn_metrics as metrics
    monkeypatch.setattr(metrics, "_last_warning", float("-inf"))
    async def main():
        blocker = asyncio.Event()
        monkeypatch.setattr(metrics, 'PENDING_MAX', 2)
        async def fail():
            await blocker.wait()
            raise RuntimeError('synthetic unavailable database')
        metrics.background(fail())
        metrics.background(fail())
        metrics.background(fail())
        assert len(metrics._pending) == 2
        blocker.set()
        await asyncio.gather(*metrics._pending)
    asyncio.run(main())
    assert caplog.text.count('metrics write failed') == 1


@pytest.mark.parametrize("stopped,expected", [(False, "failed"), (True, "stopped")])
def test_completion_timeout_is_a_failure_and_user_stop_stays_stopped(monkeypatch, tmp_path, stopped, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import uuid

    from looper.agents import completion_events, turn_metrics
    from looper.agents.tiers import manager as manager_module
    from looper.agents.tiers.turn_token import TurnStopped

    async def main():
        manager = manager_module.ManagerAgent(config=manager_module.ManagerConfig(work_dir=tmp_path),
                                              runtime_root=tmp_path)
        manager._bus = SimpleNamespace()
        record = AsyncMock()
        monkeypatch.setattr(manager_module.db_llm_records, 'add_metadata', record)
        monkeypatch.setattr(manager_module.db_connection, 'get_pool',
                            lambda: SimpleNamespace(fetchval=AsyncMock(return_value=0)))
        monkeypatch.setattr(manager_module.db_manager_events, 'failing', AsyncMock(return_value=False))
        monkeypatch.setattr(completion_events, 'TURN_SECONDS', 0.01)
        async def complete(rows, batch, state):
            state.update(record_id=1, token=SimpleNamespace(times=turn_metrics.TurnTimes(datetime.now(timezone.utc))))
            if stopped:
                raise TurnStopped()
            await asyncio.Event().wait()
        async def end(run, batch, state):
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        monkeypatch.setattr(manager, '_completion_turn', complete)
        fallback = AsyncMock()
        monkeypatch.setattr(manager, '_fall_back_completion', fallback)
        monkeypatch.setattr(manager, '_end_late_completion', end)
        await manager._tell_completion([], uuid.uuid4())
        await asyncio.gather(*turn_metrics._pending)
        assert record.await_args.args == (1, {'turn_metrics': {
            'origin': 'completion', 'tool_calls': 0, 'delivery': expected}})
        assert fallback.await_args.args[1] == ('turn_stopped' if stopped else 'turn_timeout')
    asyncio.run(main())


@pytest.mark.parametrize('outcome', ['success', 'failed', 'capped', 'disabled'])
def test_reaction_acknowledges_only_completed_calls(monkeypatch, outcome):
    from looper.agents import chat_settings, turn_metrics as metrics
    from looper.agents.channels import status_line
    monkeypatch.setattr(chat_settings, 'metrics_enabled', lambda: outcome != 'disabled')
    monkeypatch.setattr(metrics, 'ack_board', metrics.AckBoard())
    async def main():
        async def react(chat, message):
            if outcome == 'failed':
                raise RuntimeError('synthetic reaction failure')
        reactions = status_line.Reactions(channel='channel', react=react)
        if outcome == 'capped':
            monkeypatch.setattr(status_line, 'REACTIONS_PENDING_MAX', 0)
        reactions.add('chat', 'message')
        await asyncio.gather(*reactions._pending)
        if outcome == 'disabled':
            assert not metrics.ack_board.contains('channel', 'message')
        else:
            assert metrics.ack_board.contains('channel', 'message')
            ack = metrics.ack_board.take('channel', 'message')
            assert isinstance(ack, datetime) if outcome == 'success' else ack is None
    asyncio.run(main())
