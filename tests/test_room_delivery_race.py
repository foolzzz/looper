"""A waiting inbox must not race a room message already being pushed to its run."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import room as module
from looper.agents.project_execution import RoomLimits
from looper.agents.room import MemberKind, RoomDelivery, RoomService, Seat
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.tiers.worker import WorkerConfig, WorkerPool, _Reminders


@pytest.mark.parametrize('kind,push', [('question', 'accepted'), ('interface', 'accepted'),
                                       ('question', 'rejected'), ('question', 'none')])
def test_inbox_wait_resolves_a_concurrent_push_before_returning_messages(tmp_path, monkeypatch, kind, push):
    async def run():
        seat = Seat('worker@s1', 'worker', MemberKind.WORK, run='worker-1')
        row = {'id': 41, 'sender_seat': 'fe_rd@s2', 'kind': kind, 'body': 'Which file?',
               'audience': [seat.key] if kind == 'question' else [], 'audience_all': kind == 'interface'}
        entered, release, queried = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        room = RoomService(worker_pool=pool)
        monkeypatch.setattr(room, '_context', AsyncMock(return_value=SimpleNamespace(limits=RoomLimits())))
        monkeypatch.setattr(module.db_room, 'last_read', AsyncMock(return_value=0))
        mark = AsyncMock()
        monkeypatch.setattr(module.db_room, 'mark_read', mark)

        async def messages(task, *, after=0):
            queried.set()
            return [row] if after < row['id'] else []

        monkeypatch.setattr(module.db_room, 'messages', messages)

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return DeliveryResult.ACCEPTED if push == 'accepted' else DeliveryResult.REJECTED

        state = _Reminders(tools_running=1, next_delivery=AsyncMock(side_effect=lambda after:
            RoomDelivery('reminder', 41, (41,), True, 0) if after < 41 else None))
        sending = None
        if push != 'none':
            pool._reminders[seat.run] = state
            pool._inputs[seat.run] = SimpleNamespace(send=send)
            sending = asyncio.create_task(pool.remind(seat.run))
            await entered.wait()  # INSERT is visible, but stdin acceptance has not advanced the cursor.
        waiting = asyncio.create_task(room.inbox('task', seat, wait_seconds=5))
        try:
            await queried.wait()
            release.set()
            result = await waiting
            if push == 'accepted':
                assert result['messages'] == [] and result['waited']
                assert result['delivered_message_ids'] == [41]
                mark.assert_not_awaited()
            else:
                assert [message['id'] for message in result['messages']] == [41]
                assert 'delivered_message_ids' not in result
                mark.assert_awaited_once_with('task', seat.key, 41)
        finally:
            release.set()
            waiting.cancel()
            await asyncio.gather(waiting, *([sending] if sending else []), return_exceptions=True)
            pool._drop_reminders(seat.run)

    asyncio.run(asyncio.wait_for(run(), 3))
