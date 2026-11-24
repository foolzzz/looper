"""A waiting inbox must not race a room message already being pushed to its run."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import room as module
from looper.agents.project_execution import RoomLimits
from looper.agents.room import MemberKind, RoomDelivery, RoomService, Seat
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.tiers.worker import WorkerConfig, WorkerPool, _Reminders


@pytest.mark.parametrize('kind,push,cleanup,later_status', [
    ('question', 'accepted', None, False), ('interface', 'accepted', None, False),
    ('question', 'rejected', None, False), ('question', 'none', None, False),
    ('question', 'accepted', 'send', False), ('question', 'unknown', 'send', False),
    ('question', 'accepted', 'query', False), ('question', 'unknown', 'query', False),
    ('question', 'rejected', 'query', False),
    ('question', 'accepted', 'query', True), ('question', 'unknown', 'query', True),
    ('question', 'accepted', 'context', False)])
def test_inbox_wait_resolves_a_concurrent_push_before_returning_messages(
    tmp_path, monkeypatch, kind, push, cleanup, later_status,
):
    async def run():
        seat = Seat('worker@s1', 'worker', MemberKind.WORK, run='worker-1')
        row = {'id': 41, 'sender_seat': 'fe_rd@s2', 'kind': kind, 'body': 'Which file?',
               'audience': [seat.key] if kind == 'question' else [], 'audience_all': kind == 'interface'}
        status = {'id': 42, 'sender_seat': 'qa@s3', 'kind': 'status', 'body': 'tests green',
                  'audience': [], 'audience_all': True}
        rows = [row, status] if later_status else [row]
        entered, release, queried = asyncio.Event(), asyncio.Event(), asyncio.Event()
        release_query, query_returned = asyncio.Event(), asyncio.Event()
        context_entered, release_context = asyncio.Event(), asyncio.Event()
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        room = RoomService(worker_pool=pool)

        async def context(*args, **kwargs):
            if cleanup == 'context':
                context_entered.set()
                await release_context.wait()
            return SimpleNamespace(limits=RoomLimits())

        monkeypatch.setattr(room, '_context', context)
        read = 0

        async def mark_read(task, key, through):
            nonlocal read
            read = max(read, through)

        monkeypatch.setattr(module.db_room, 'last_read', AsyncMock(side_effect=lambda *args: read))
        mark = AsyncMock(side_effect=mark_read)
        monkeypatch.setattr(module.db_room, 'mark_read', mark)

        async def messages(task, *, after=0):
            queried.set()
            if cleanup == 'query':
                await release_query.wait()
                query_returned.set()
            return [r for r in rows if r['id'] > after]

        monkeypatch.setattr(module.db_room, 'messages', messages)

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'accepted': DeliveryResult.ACCEPTED, 'unknown': DeliveryResult.UNKNOWN,
                    'rejected': DeliveryResult.REJECTED}[push]

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
            await (context_entered if cleanup == 'context' else queried).wait()
            if cleanup:
                pool._drop_reminders(seat.run)
            release_context.set()
            if cleanup == 'query':
                # Drop the run before inbox can join remind(), with stdin acceptance still pending.
                release_query.set()
                await query_returned.wait()
            release.set()
            result = await waiting
            if push in ('accepted', 'unknown'):
                assert [message['id'] for message in result['messages']] == ([42] if later_status else [])
                assert result['waited']
                assert result['delivered_message_ids'] == [41]
                mark.assert_not_awaited()
            else:
                assert [message['id'] for message in result['messages']] == [41]
                assert 'delivered_message_ids' not in result
                mark.assert_awaited_once_with('task', seat.key, 41)
            if cleanup and push in ('accepted', 'unknown'):
                # An unconfirmed push is excluded only for this consumer, not its next run.
                next_run = await room.inbox('task', replace(seat, run='worker-2'))
                assert [message['id'] for message in next_run['messages']] == ([41, 42] if later_status else [41])
        finally:
            release_context.set()
            release_query.set()
            release.set()
            waiting.cancel()
            await asyncio.gather(waiting, *([sending] if sending else []), return_exceptions=True)
            pool._drop_reminders(seat.run)

    asyncio.run(asyncio.wait_for(run(), 3))


def _pull_push_room(tmp_path, monkeypatch):
    seat = Seat('worker@s1', 'worker', MemberKind.WORK, run='worker-1')
    row = {'id': 41, 'sender_seat': 'fe_rd@s2', 'kind': 'question', 'body': 'Which file?',
           'audience': [seat.key], 'audience_all': False}
    pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
    room = RoomService(worker_pool=pool)
    monkeypatch.setattr(room, '_context', AsyncMock(return_value=SimpleNamespace(limits=RoomLimits())))
    read = 0

    async def last_read(*args):
        return read

    async def mark_read(task, key, through):
        nonlocal read
        read = max(read, through)

    async def messages(task, *, after=0):
        return [row] if after < 41 else []

    async def next_delivery(after):
        return RoomDelivery('reminder', 41, (41,), True, 0) if max(after, read) < 41 else None

    monkeypatch.setattr(module.db_room, 'last_read', last_read)
    monkeypatch.setattr(module.db_room, 'mark_read', mark_read)
    monkeypatch.setattr(module.db_room, 'messages', messages)
    state = _Reminders(tools_running=1, next_delivery=next_delivery)
    pool._reminders[seat.run] = state
    return room, pool, seat, state


@pytest.mark.parametrize('wait_seconds', [0, 5])
def test_inbox_commits_its_read_before_a_push_can_start(tmp_path, monkeypatch, wait_seconds):
    async def run():
        room, pool, seat, state = _pull_push_room(tmp_path, monkeypatch)
        marking, release_mark, flushing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        mark_read = module.db_room.mark_read

        async def slow_mark(*args):
            marking.set()
            await release_mark.wait()
            await mark_read(*args)

        class SendLock(asyncio.Lock):
            async def acquire(self):
                if asyncio.current_task() is pushing:
                    flushing.set()
                return await super().acquire()

        monkeypatch.setattr(module.db_room, 'mark_read', slow_mark)
        state.lock = SendLock()
        send = AsyncMock(return_value=DeliveryResult.ACCEPTED)
        pool._inputs[seat.run] = SimpleNamespace(send=send)
        # The inbox's own remind is rate limited; a retry becomes eligible during mark_read.
        state.last_at = time.monotonic()
        pushing = None
        reading = asyncio.create_task(room.inbox('task', seat, wait_seconds=wait_seconds))
        try:
            await marking.wait()
            state.last_at = float('-inf')
            pushing = asyncio.create_task(pool.remind(seat.run))
            await flushing.wait()  # The push has either acquired the lock or blocked on the reader.
            release_mark.set()
            result = await reading
            await pushing
            assert [message['id'] for message in result['messages']] == [41]
            send.assert_not_awaited()
        finally:
            release_mark.set()
            reading.cancel()
            await asyncio.gather(reading, *([pushing] if pushing else []), return_exceptions=True)
            pool._drop_reminders(seat.run)

    asyncio.run(asyncio.wait_for(run(), 3))


def test_plain_inbox_waits_for_a_push_in_flight(tmp_path, monkeypatch):
    async def run():
        room, pool, seat, state = _pull_push_room(tmp_path, monkeypatch)
        entered, release, queried = asyncio.Event(), asyncio.Event(), asyncio.Event()
        messages = module.db_room.messages

        async def query(*args, **kwargs):
            # Resume the controller after this reader has reached its send-lock/return boundary.
            asyncio.get_running_loop().call_soon(queried.set)
            return await messages(*args, **kwargs)

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return DeliveryResult.ACCEPTED

        monkeypatch.setattr(module.db_room, 'messages', query)
        pool._inputs[seat.run] = SimpleNamespace(send=send)
        pushing = asyncio.create_task(pool.remind(seat.run))
        reading = None
        try:
            await entered.wait()
            reading = asyncio.create_task(room.inbox('task', seat))
            await queried.wait()
            release.set()
            result = await reading
            await pushing
            assert state.through == 41
            assert result['messages'] == []
        finally:
            release.set()
            if reading:
                reading.cancel()
            await asyncio.gather(pushing, *([reading] if reading else []), return_exceptions=True)
            pool._drop_reminders(seat.run)

    asyncio.run(asyncio.wait_for(run(), 3))
