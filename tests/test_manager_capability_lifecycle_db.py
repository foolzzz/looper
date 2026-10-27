"""Capability refresh and deletion admission retain the actual resident and conversation."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from looper.agents import catalog_sources, manager_installs, managers_api, projects
from looper.agents.bus import MessageBus
from looper.agents.http_server import AgentHTTPServer
from looper.agents.managers import ManagerFleet
from looper.agents.message import MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Turn
from looper.agents.ops.service import Platform
from looper.agents.supervisor import AgentSupervisor
from looper.agents.tiers.manager import ManagerConfig
from tests.db_support import run_db, until, call_v1
from tests.fake_claude import fake_claude


@asynccontextmanager
async def lifecycle(root, *, quiescent=True, background_events=None):
    if background_events is None:
        background_events = [] if quiescent else [{'type':'system','subtype':'task_started','task_id':'example-job'}]
    binary = fake_claude(root / 'resident', f'BACKGROUND_EVENTS = {background_events!r}\n' + '''import json, sys
for line in sys.stdin:
    print(json.dumps({'type':'command_lifecycle','command_uuid':json.loads(line)['uuid'],'state':'started'}), flush=True)
    print(json.dumps({'type':'system','subtype':'init','session_id':'example-session'}), flush=True)
    for event in BACKGROUND_EVENTS:
        print(json.dumps(event), flush=True)
    print(json.dumps({'type':'result','subtype':'success','is_error':False,
                      'session_id':'example-session','result':'Reply.'}), flush=True)
''')
    bus = MessageBus(runtime_root=root)
    supervisor = AgentSupervisor(bus, root)
    router = SimpleNamespace(has_planner=lambda _: False)
    fleet = ManagerFleet(platform=Platform(bus=bus, task_router=router), supervisor=supervisor,
                         runtime_root=root, url='http://127.0.0.1:1',
                         config=ManagerConfig(claude_bin=binary, work_dir=root), broadcaster=None)
    row = (await managers_api.create_manager({'key':'example','name':'Example','system_prompt':'Help.'}))['manager']
    await fleet.load()
    await supervisor.start_all()
    agent = fleet.get(row['id'])
    server = AgentHTTPServer(host='127.0.0.1', port=0, web_adapter=SimpleNamespace(),
                             task_router=router, supervisor=supervisor, manager=agent,
                             managers=fleet, runtime_root=root)
    await server.start()
    try:
        yield agent, fleet, bus, server
    finally:
        await server.stop()
        await fleet.stop()
        await supervisor.stop_all()
        await bus.shutdown()


def message(agent, text='hello'):
    return MessageEnvelope.create(source=channel_address('web'), target=agent.agent_address,
                                  message_type=MessageType.USER_MESSAGE, payload={'text':text})


@pytest.mark.parametrize('quiescent', [False, True])
def test_pending_capabilities_repeat_until_reset_and_records_keep_actual_launch(tmp_path, monkeypatch, quiescent):
    async def check(pool):
        async with lifecycle(tmp_path, quiescent=quiescent) as (agent, fleet, bus, server):
            # Model tools are orthogonal to this resident protocol regression.
            agent.ops = None
            async def ask(text='hello'):
                await agent._handle(message(agent, text))
                replies = [e.payload['text'] for e in bus.get_queue('channel-web')._queue
                           if e.message_type == MessageType.AGENT_REPLY]
                if text != '/new':
                    metadata = await pool.fetchval('SELECT metadata FROM llm_records WHERE agent_id=$1 ORDER BY created_at DESC LIMIT 1', agent.agent_id)
                    assert json.loads(metadata).get('library') == agent._process.launch.library.record()
                return replies[-1]
            await ask()
            initial = agent._process.launch.library.sha256
            item = (await catalog_sources.create_skill({'name':'example','description':'Greeting example.','content':'Say hello.'}))['item']
            for installing in (True, False):
                change = manager_installs.put_install if installing else manager_installs.delete_install
                revert = manager_installs.delete_install if installing else manager_installs.put_install
                running = agent._process.launch.library.sha256
                await change(agent.agent_id, item['item_id'])
                for _ in range(2):
                    assert ('/new' in await ask()) is (not quiescent)
                    assert (agent._process.launch.library.sha256 == running) is (not quiescent)
                if not quiescent:
                    await revert(agent.agent_id, item['item_id'])
                    assert '/new' not in await ask()  # the desired configuration returned to the running hash
                    await change(agent.agent_id, item['item_id'])
                    await ask('/new')
                    assert '/new' not in await ask()
                assert (agent._process.launch.library.sha256 != initial) is installing
    run_db(check, monkeypatch, timeout=45)


@pytest.mark.parametrize('background_events', [
    pytest.param([{'type':'system','subtype':'task_started','task_id':'example-job'}], id='tracked-job'),
    pytest.param([{'type':'assistant','message':{'content':[{
        'type':'tool_use','id':'example-tool','name':'Bash','input':{'run_in_background':True}}]}}],
        id='untracked-launch'),
    pytest.param([{'type':'system','subtype':'task_started'}], id='unknown-job-state'),
    pytest.param([], id='idle'),
])
def test_public_delete_requires_runtime_quiescence_after_reply(tmp_path, monkeypatch, background_events):
    async def check(pool):
        async with lifecycle(tmp_path, background_events=background_events) as (agent, fleet, bus, server):
            agent.ops = None  # Exercise the resident without invoking model tools.
            await bus.publish(message(agent))
            await until(lambda: agent._handling is None and not agent._turns.live())
            replies = [e for e in bus.get_queue('channel-web')._queue
                       if e.message_type == MessageType.AGENT_REPLY]
            assert len(replies) == 1 and replies[0].payload['text'] == 'Reply.'
            assert not agent.busy and bus.pending_count(agent.agent_id) == 0
            resident, pid, ops = agent._claude, agent._claude.pid, fleet.ops(agent.agent_id)
            session = await pool.fetchrow('SELECT * FROM agent_sessions WHERE agent_id=$1', agent.agent_id)
            assert session is not None and pid is not None

            status, _ = await call_v1(server, '/api/v1/managers/delete', {'manager_id':agent.agent_id})
            if not background_events:
                assert status == 200
                assert fleet.get(agent.agent_id) is None and not resident.started
            else:
                assert status == 409
                assert fleet.get(agent.agent_id) is agent and agent.is_healthy()
                assert resident.started and resident.pid == pid
                assert fleet.ops(agent.agent_id) is ops and ops.authorized('Bearer ' + ops._token)
                assert await pool.fetchval('SELECT deleted_at FROM managers WHERE id=$1', agent.agent_id) is None
                assert await pool.fetchrow('SELECT * FROM agent_sessions WHERE agent_id=$1', agent.agent_id) == session
                await bus.publish(message(agent, 'continue'))
                await until(lambda: len([e for e in bus.get_queue('channel-web')._queue
                                        if e.message_type == MessageType.AGENT_REPLY]) == 2)
                assert resident.pid == pid
    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize('phase', ['preparing', 'executing', 'disconnected'])
def test_public_delete_preserves_turn_and_admitted_task_creation(tmp_path, monkeypatch, phase):
    async def check(pool):
        async with lifecycle(tmp_path) as (agent, fleet, bus, server):
            await pool.execute("INSERT INTO projects (id,name,work_dir) VALUES ('example','Example',$1)", str(tmp_path))
            entered, release, finish_turn = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original = projects.task_workspace
            async def workspace(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)
            monkeypatch.setattr(projects, 'task_workspace', workspace)
            ops, issued = agent.ops, asyncio.Event()
            async def answer(envelope, token):
                turn = Turn(channel='web', chat='example', message_id=envelope.message_id)
                ops.begin_turn(turn, 'example-binding')
                issued.set()
                if phase == 'preparing':
                    entered.set()
                    await release.wait()
                elif phase == 'disconnected':
                    await finish_turn.wait()
                else:
                    await until(lambda: bool(turn.blocks))
                ops.end_turn(turn)
                await agent._send_reply(envelope, 'Reply.')
            monkeypatch.setattr(agent, '_answer_user_message', answer)
            request = message(agent)
            settlements, settle = [], bus.journal.mark_delivered
            monkeypatch.setattr(bus.journal, 'mark_delivered', lambda key: (settlements.append(key), settle(key))[-1])
            await bus.publish(request)
            await issued.wait()
            token = agent.turn_for(request.message_id)
            if phase == 'preparing':
                assert not agent.busy and bus.pending_count(agent.agent_id) == 0
            else:
                body = json.dumps({'requirement':'Example work','title':'Example','project':'Example',
                                   'workspace_mode':'in_place'}).encode()
                reader, writer = await asyncio.open_connection('127.0.0.1', server._port)
                writer.write((f'POST /api/agent/v1/ops/looper_task_create HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                              f'Authorization: Bearer {ops._token}\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body)
                await writer.drain()
                await entered.wait()
                if phase == 'disconnected':
                    writer.close()
                    await writer.wait_closed()
                    finish_turn.set()  # execution outlives both its turn and its client
                    await until(lambda: agent._handling is None)
            status, result = await call_v1(server, '/api/v1/managers/delete', {'manager_id':agent.agent_id})
            assert status == 409 and 'wait' in result['error']
            assert fleet.get(agent.agent_id) is agent and fleet.ops(agent.agent_id) is ops
            assert ops.authorized('Bearer ' + ops._token)
            assert agent.turn_for(request.message_id) is token or phase == 'disconnected'
            release.set()
            if phase != 'preparing':
                if phase != 'disconnected':
                    assert b'"ok"' in await asyncio.wait_for(reader.read(), 5)
                    writer.close()
                    await writer.wait_closed()
                await until(lambda: bool(ops._turn is None) and not ops.executing)
                assert await pool.fetchval('SELECT count(*) FROM tasks WHERE manager_id=$1', agent.agent_id) == 1
            await until(lambda: agent._handling is None)
            replies = [e for e in bus.get_queue('channel-web')._queue if e.message_type == MessageType.AGENT_REPLY]
            assert len(replies) == 1
            assert settlements.count(request.message_id) == 1
            assert request.message_id not in {e.message_id for e in bus.journal.undelivered()}
    run_db(check, monkeypatch, timeout=20)
