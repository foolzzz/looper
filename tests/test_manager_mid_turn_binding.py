"""Native queued turns keep their actual input and bridge ownership before Manager claims them."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from looper.agents.claude_cli import ResidentClaude
from looper.agents import admission, session_routing
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


def _account_admission(monkeypatch, *, active=True):
    controller = admission.Controller()
    controller.active = active
    monkeypatch.setattr(admission, "_controller", controller)
    monkeypatch.setattr(admission, "slot", controller.slot)
    monkeypatch.setattr(controller, "_account", AsyncMock(return_value={
        "name": "Synthetic", "max_concurrency": 1, "vendor_type": "anthropic"}))
    acquire = AsyncMock(wraps=controller.acquire)
    monkeypatch.setattr(controller, "acquire", acquire)
    return controller, acquire


@pytest.mark.parametrize("routing", [True, False])
@pytest.mark.parametrize("account_admission", [False, True])
def test_native_input_binds_tools_before_claim_and_records_actual_prompt(enabled, tmp_path, routing,  # noqa: F811
                                                                       account_admission):
    enabled.setattr(session_routing, "_routing_enabled", routing)
    async def test(pool):
        controller, acquire = _account_admission(enabled, active=account_admission)
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
        wrapping_a, release_a = asyncio.Event(), asyncio.Event()
        converse = rig.manager._converse

        async def hold_wrapup(token, prepared, **kwargs):
            result = await converse(token, prepared, **kwargs)
            if token.message_id == first.message_id:
                wrapping_a.set()
                await release_a.wait()
            return result

        enabled.setattr(rig.manager, "_converse", hold_wrapup)

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
            await asyncio.wait_for(wrapping_a.wait(), 10)
            await until(lambda: (tmp_path / "native-tool").exists())
            # A has not reached release_binding yet: only unfolded can publish B.
            early_binding = json.loads(bridge_file.read_text())["turn"]
            assert early_binding == rig.manager._turns.find(second.message_id).binding
            assert not release_a.is_set()
            release_a.set()
            assert await rig.reply(first) == "answer A"
            await asyncio.wait_for(preparing_b.wait(), 10)
            if account_admission:
                assert list(controller.running) == ["manager:main"]
                assert acquire.await_count == 1
            # Longer than the real bridge's match window: B still cannot claim,
            # so a late replay cannot rescue a missing binding or expectation.
            await asyncio.sleep(MATCH_WAIT_SEC + 0.1)
            binding = json.loads(bridge_file.read_text())["turn"]
            assert binding == rig.manager._turns.find(second.message_id).binding
            assert await ops.call("looper_native_write", {}, binding=binding, call_id="b-tool") == {
                "status": "ok", "result": {"written": True}}
            assert effects == [second.message_id]
            assert not release_b.is_set()
            release_b.set()
            await until(lambda: second.message_id not in resident._output.queued)
            if account_admission:
                assert list(controller.running) == ["manager:main"]
                assert acquire.await_count == 1  # claiming the native turn reuses its session's slot
            (tmp_path / "finish-b").touch()
            assert await rig.reply(second) == "answer B"
            assert not controller.running
            rows = await pool.fetch("SELECT input_text, metadata FROM llm_records WHERE output_text='answer B'")
            assert len(rows) == 1 and rows[0]["input_text"] == injected
            assert "looper_state" not in json.loads(rows[0]["metadata"])
            first_metadata = await pool.fetchval("SELECT metadata FROM llm_records WHERE output_text='answer A'")
            assert "looper_state" in json.loads(first_metadata)
            assert not ops._expected.get(binding)  # buffered tool_use is not matched twice on claim
        finally:
            release_a.set()
            release_b.set()
            (tmp_path / "finish-a").touch()
            (tmp_path / "finish-b").touch()
            await rig.close()

    run_db(test, enabled, timeout=60)


@pytest.mark.parametrize("ending", ["completed", "preparation_failure", "stopped", "shutdown", "disabled_before_claim"])
def test_unclaimed_native_input_releases_account_slot(enabled, tmp_path, ending):  # noqa: F811
    async def test(pool):
        from looper.agents import claude_cli
        enabled.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.1)
        controller, acquire = _account_admission(enabled)
        rig = _Rig(tmp_path)
        resident = ResidentClaude(label="synthetic-native-admission")
        rig.manager._sessions["main"].claude = resident
        rig.manager._config.claude_bin = _fake_claude(tmp_path, SCRIPT)
        preparing_b, release_b = asyncio.Event(), asyncio.Event()
        probes = 0

        async def tools_problem():
            nonlocal probes
            probes += 1
            if probes == 2:
                preparing_b.set()
                await release_b.wait()
                if ending == "preparation_failure":
                    return "synthetic preparation failure"
            return None

        enabled.setattr(rig.manager, "_tools_problem", tools_problem)
        await rig.start()
        closed = False
        try:
            first, second = _web("first"), _web("second")
            await rig.bus.publish(first)
            await until(lambda: (tmp_path / "ready").exists() and resident._user_turn is not None
                        and resident._user_turn.started)
            await rig.bus.publish(second)
            await until(lambda: (tmp_path / "injected.txt").exists())
            (tmp_path / "finish-a").touch()
            assert await rig.reply(first) == "answer A"
            await preparing_b.wait()
            await until(lambda: (tmp_path / "native-tool").exists())
            assert list(controller.running) == ["manager:main"]
            assert acquire.await_count == 1
            provider = controller.running["manager:main"].provider_id
            if ending == "shutdown":
                await rig.close()
                closed = True
            elif ending == "disabled_before_claim":
                await controller.set_enabled(False)
                release_b.set()
                await until(lambda: second.message_id not in resident._output.queued)
                (tmp_path / "finish-b").touch()
                assert await rig.reply(second) == "answer B"
                assert acquire.await_count == 1
            else:
                if ending == "stopped":
                    from looper.agents.tiers.manager import StopResult
                    assert await rig.manager.stop_turn(second.message_id, via="web") is StopResult.STOPPING
                if ending == "completed":
                    (tmp_path / "finish-b").touch()
                    await until(lambda: not controller.running)
                    await controller.transition(provider, admission.QuotaFailure(None, None))
                release_b.set()
                answer = await rig.reply(second)
                await until(lambda: not controller.running)
                assert (answer == "answer B") is (ending == "completed")
                assert acquire.await_count == 1  # an already completed native result needs no new admission
            assert not controller.running
            assert not rig.manager._mid_turn._admissions
        finally:
            release_b.set()
            (tmp_path / "finish-a").touch()
            (tmp_path / "finish-b").touch()
            if not closed:
                await rig.close()
            await controller.stop()

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


@pytest.mark.parametrize("replacement", [False, True])
def test_discarded_reservation_clears_only_its_own_binding(enabled, tmp_path, replacement):  # noqa: F811
    async def test(pool):
        from looper.agents.ops import Turn

        rig = _Rig(tmp_path)
        resident = ResidentClaude(label="synthetic-discard-binding")
        rig.manager._sessions["main"].claude = resident
        rig.manager._config.claude_bin = _fake_claude(tmp_path, SCRIPT)
        bridge_file = tmp_path / "turn.json"
        ops = rig.manager.ops = ManagerOps(Platform(bus=rig.bus, task_router=None),
                                           url="http://127.0.0.1:9", turn_file=bridge_file)
        enabled.setattr(rig.manager, "_tools_problem", AsyncMock(return_value=None))
        release_a = asyncio.Event()
        converse = rig.manager._converse

        async def hold_wrapup(token, prepared, **kwargs):
            result = await converse(token, prepared, **kwargs)
            await release_a.wait()
            return result

        enabled.setattr(rig.manager, "_converse", hold_wrapup)
        await rig.start()
        try:
            first, second = _web("first"), _web("second")
            await rig.bus.publish(first)
            await until(lambda: (tmp_path / "ready").exists() and resident._user_turn is not None
                        and resident._user_turn.started)
            await rig.bus.publish(second)
            await until(lambda: (tmp_path / "injected.txt").exists())
            owner = next(owner for owner in resident._user_turn.input_owners.values()
                         if owner.message_id == second.message_id)
            (tmp_path / "finish-a").touch()
            await until(lambda: (tmp_path / "native-tool").exists())
            # The child marker can precede the parent's processing of A's result and B's start.
            await until(lambda: owner.buffering, "native input to start buffering")
            assert resident._output.queued.get(second.message_id) is owner
            binding = rig.manager._turns.find(second.message_id).binding
            assert json.loads(bridge_file.read_text())["turn"] == binding
            (tmp_path / "finish-b").touch()
            await asyncio.wait_for(owner.completed.wait(), 3)
            if replacement:
                binding = "synthetic-replacement"
                ops.register(Turn(channel="web", chat="replacement"), binding)
                ops.publish_binding(binding)
            await resident.discard_reserved_input(second.message_id)
            assert json.loads(bridge_file.read_text())["turn"] == (binding if replacement else None)
        finally:
            release_a.set()
            (tmp_path / "finish-a").touch()
            (tmp_path / "finish-b").touch()
            await rig.close()

    run_db(test, enabled, timeout=45)
