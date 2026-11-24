"""Synthetic operator turns for the Manager memory tool's integration tests."""
import json

from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from looper.agents.ops import Turn
from looper.agents.ops.service import ManagerOps, Platform
from tests.memory_support import configure_write, synthetic_vault

STATEMENT = '以后周报都先写结论，再写细节。'
ARGS = {'kind': 'preference', 'statement': STATEMENT, 'quote': STATEMENT, 'keywords': ['周报', '结论']}


async def setup(pool, tmp_path):
    repo = synthetic_vault(tmp_path)
    settings = await configure_write(repo, pool)
    raw = json.loads(await pool.fetchval("SELECT value FROM system_settings WHERE key='memory_write'"))
    raw['sources'] = {'manager': True}
    await pool.execute("UPDATE system_settings SET value=$1 WHERE key='memory_write'", json.dumps(raw))
    return repo, settings


def ops():
    result = ManagerOps(Platform(bus=None, task_router=None), url='http://127.0.0.1:9', token='synthetic')
    result._working_at = result._clock()
    return result


def turn(text=STATEMENT, message_id='m-1', **fields):
    envelope = MessageEnvelope.create(source=channel_address('web'), target=manager_address('manager'),
        message_type=MessageType.USER_MESSAGE, payload={'text': text, 'channel_context': {'session_id': 'memory-chat'}})
    return Turn(channel='web', chat='memory-chat', message_id=message_id, envelope=envelope, **fields)


async def propose(service, current, **arguments):
    binding = f'memory-{id(current)}'
    service.begin_turn(current, binding=binding)
    try:
        return await service.call('looper_memory_propose', {**ARGS, **arguments}, binding=binding)
    finally:
        service.end_turn(current)
