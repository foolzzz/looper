"""Native queued turns keep their actual input and bridge ownership before Manager claims them."""
from __future__ import annotations

import asyncio
import json

import pytest

from looper.agents.claude_cli import ResidentClaude
from looper.agents import session_routing
from looper.agents.ops import Risk, Tool
from looper.agents.ops.service import MATCH_WAIT_SEC, ManagerOps, Platform
from tests.db_support import DSN, run_db, until
from tests.test_manager_mid_turn_input import enabled  # noqa: F401
from tests.test_manager_reply import _fake_claude
from tests.test_manager_session_routing_db import _Rig, _web

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

SCRIPT = """
import pathlib

def emit(event):
    print(json.dumps(event), flush=True)

first = json.loads(sys.stdin.readline())
emit(dict(first, isReplay=True))
emit({'type':'system','subtype':'init','session_id':'synthetic-native-binding'})
emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':'a-tool','name':'Bash','input':{}}]}})
pathlib.Path('ready').touch()
second = json.loads(sys.stdin.readline())
pathlib.Path('injected.txt').write_text(second['message']['content'])
while not pathlib.Path('finish-a').exists(): time.sleep(0.01)
emit({'type':'result','subtype':'success','is_error':False,'session_id':'synthetic-native-binding',
      'result':'answer A','user_message_uuids':[first['uuid']]})
emit({'type':'command_lifecycle','command_uuid':second['uuid'],'state':'started'})
emit(dict(second, isReplay=True))
emit({'type':'system','subtype':'init','session_id':'synthetic-native-binding'})
emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':'b-tool',
      'name':'mcp__looper__looper_native_write','input':{}}]}})
pathlib.Path('native-tool').touch()
while not pathlib.Path('finish-b').exists(): time.sleep(0.01)
emit({'type':'result','subtype':'success','is_error':False,'session_id':'synthetic-native-binding',
      'result':'answer B','user_message_uuids':[second['uuid']]})
sys.stdin.read()
"""


@pytest.mark.parametrize("routing", [True, False])
def test_native_input_binds_tools_before_claim_and_records_actual_prompt(enabled, tmp_path, routing):  # noqa: F811
    enabled.setattr(session_routing, "_routing_enabled", routing)
    async def test(pool):
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('manager.stable_context', 'true')")
        rig = _Rig(tmp_path)
        resident = ResidentClaude(label="synthetic-native-binding")
        rig.manager._sessions["main"].claude = resident
        rig.manager._config.claude_bin = _fake_claude(tmp_path, SCRIPT)
        bridge_file = tmp_path / "turn.json"
        effects = []

        async def write(call):
            effects.append(call.turn.message_id)
            return {"written": True}

        ops = ManagerOps(Platform(bus=rig.bus, task_router=None), url="http://127.0.0.1:9",
                         tools=[Tool("looper_native_write", "Record a synthetic effect", {"type": "object"},
                                     Risk.WRITE, write)], turn_file=bridge_file)
        rig.manager.ops = ops
        preparing_b, release_b = asyncio.Event(), asyncio.Event()
        probes = 0

        async def tools_problem():
            nonlocal probes
            probes += 1
            if probes == 2:
                preparing_b.set()
                await release_b.wait()
            return None

        enabled.setattr(rig.manager, "_tools_problem", tools_problem)
        await rig.start()
        try:
            first, second = _web("first"), _web("second")
            await rig.bus.publish(first)
            await until(lambda: (tmp_path / "ready").exists() and resident._user_turn is not None
                        and resident._user_turn.started)
            await rig.bus.publish(second)
            await until(lambda: (tmp_path / "injected.txt").exists())
            injected = (tmp_path / "injected.txt").read_text()
            (tmp_path / "finish-a").touch()
            assert await rig.reply(first) == "answer A"
            await asyncio.wait_for(preparing_b.wait(), 10)
            await until(lambda: (tmp_path / "native-tool").exists())
            # Longer than the real bridge's match window: B still cannot claim,
            # so a late replay cannot rescue a missing binding or expectation.
            await asyncio.sleep(MATCH_WAIT_SEC + 0.1)
            binding = json.loads(bridge_file.read_text())["turn"]
            assert binding == rig.manager._turns.find(second.message_id).binding
            assert await ops.call("looper_native_write", {}, binding=binding, call_id="b-tool") == {
                "status": "ok", "result": {"written": True}}
            assert effects == [second.message_id]
            assert not release_b.is_set()
            (tmp_path / "finish-b").touch()
            release_b.set()
            assert await rig.reply(second) == "answer B"
            rows = await pool.fetch("SELECT input_text, metadata FROM llm_records WHERE output_text='answer B'")
            assert len(rows) == 1 and rows[0]["input_text"] == injected
            assert "looper_state" not in json.loads(rows[0]["metadata"])
            first_metadata = await pool.fetchval("SELECT metadata FROM llm_records WHERE output_text='answer A'")
            assert "looper_state" in json.loads(first_metadata)
            assert not ops._expected.get(binding)  # buffered tool_use is not matched twice on claim
        finally:
            release_b.set()
            (tmp_path / "finish-a").touch()
            (tmp_path / "finish-b").touch()
            await rig.close()

    run_db(test, enabled, timeout=60)


@pytest.mark.parametrize("routing", [True, False])
def test_fifo_turn_keeps_its_binding_and_completed_native_input(enabled, tmp_path, routing):  # noqa: F811
    enabled.setattr(session_routing, "_routing_enabled", routing)
    async def test(pool):
        rig = _Rig(tmp_path)
        resident = ResidentClaude(label="synthetic-fifo-binding")
        rig.manager._sessions["main"].claude = resident
        script = SCRIPT.replace("sys.stdin.read()", """
for line in sys.stdin:
    message = json.loads(line)
    emit(dict(message, isReplay=True))
    emit({'type':'system','subtype':'init','session_id':'synthetic-native-binding'})
    emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':'next-tool',
          'name':'mcp__looper__looper_native_write','input':{}}]}})
    pathlib.Path('next-tool').touch()
    while not pathlib.Path('finish-next').exists(): time.sleep(0.01)
    emit({'type':'result','subtype':'success','is_error':False,'session_id':'synthetic-native-binding',
          'result':'answer next','user_message_uuids':[message['uuid']]})
""")
        rig.manager._config.claude_bin = _fake_claude(tmp_path, script)
        bridge_file = tmp_path / "turn.json"
        effects = []

        async def write(call):
            effects.append(call.turn.message_id)
            return {"written": True}

        ops = ManagerOps(Platform(bus=rig.bus, task_router=None), url="http://127.0.0.1:9",
                         tools=[Tool("looper_native_write", "Record a synthetic effect", {"type": "object"},
                                     Risk.WRITE, write)], turn_file=bridge_file)
        rig.manager.ops = ops

        async def tools_problem():
            return None

        enabled.setattr(rig.manager, "_tools_problem", tools_problem)
        await rig.start()
        try:
            first, second, ahead = _web("first"), _web("second"), _web("queued ahead")
            prepare = rig.manager._prepare_turn

            async def after_native_result(token, envelope, **kwargs):
                if envelope.message_id == ahead.message_id:
                    await until(lambda: second.message_id in resident._output.queued
                                and resident._output.queued[second.message_id].completed.is_set())
                return await prepare(token, envelope, **kwargs)

            enabled.setattr(rig.manager, "_prepare_turn", after_native_result)
            await rig.bus.publish(first)
            await until(lambda: (tmp_path / "ready").exists() and resident._user_turn is not None
                        and resident._user_turn.started)
            await rig.bus.publish(second)
            await until(lambda: (tmp_path / "injected.txt").exists())
            rig.manager._register_turn(ahead)
            rig.manager._router.dispatch(rig.manager._sessions["main"], ahead)
            (tmp_path / "finish-a").touch()
            (tmp_path / "finish-b").touch()
            assert await rig.reply(first) == "answer A"
            await until(lambda: (tmp_path / "next-tool").exists())
            binding = json.loads(bridge_file.read_text())["turn"]
            assert binding == rig.manager._turns.find(ahead.message_id).binding
            assert await ops.call("looper_native_write", {}, binding=binding, call_id="next-tool") == {
                "status": "ok", "result": {"written": True}}
            assert effects == [ahead.message_id]
            (tmp_path / "finish-next").touch()
            assert await rig.reply(ahead) == "answer next"
            assert await rig.reply(second) == "answer B"  # cached result, without a second write
            assert json.loads(bridge_file.read_text())["turn"] is None
        finally:
            for name in ("finish-a", "finish-b", "finish-next"):
                (tmp_path / name).touch()
            await rig.close()

    run_db(test, enabled, timeout=60)
