"""Unfolded native input owns only its result and cannot starve later Manager turns."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from looper.agents.claude_cli import ResidentClaude
from tests.db_support import DSN, run_db, until
from tests.test_manager_mid_turn_input import enabled  # noqa: F401
from tests.test_manager_session_routing_db import _Rig, _web

SERVE_REST = (
    "for line in sys.stdin:\n"
    "    item = json.loads(line)\n"
    "    open('later-inputs.jsonl','a').write(line)\n"
    "    if item.get('type') == 'control_request':\n"
    "        print(json.dumps({'type':'control_response','response':{'subtype':'success',"
    "'request_id':item['request_id'],'response':{'cancelled': False}}}), flush=True)\n"
    "        continue\n"
    "    uuid = item.get('uuid')\n"
    "    print(json.dumps({'type':'command_lifecycle','command_uuid':uuid,'state':'started'}), flush=True)\n"
    "    print(json.dumps({'type':'system','subtype':'init','session_id':'synthetic-session'}), flush=True)\n"
    "    print(json.dumps(dict(item, isReplay=True)), flush=True)\n"
    "    print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'synthetic-session',"
    "'result':'answer ' + item['message']['content'][-14:],'user_message_uuids':[uuid]}), flush=True)\n"
)


def _manager_rig(tmp_path: Path, script: str) -> tuple[_Rig, ResidentClaude, list[dict[str, Any]]]:
    from tests.test_manager_reply import _fake_claude

    rig = _Rig(tmp_path)
    resident = ResidentClaude(label="synthetic-manager")
    calls: list[dict[str, Any]] = []
    original = resident.write_mid_turn

    async def recorded(text: str, **kwargs: Any) -> bool:
        entry: dict[str, Any] = {"tail": text[-24:], "result": None}
        calls.append(entry)
        entry["result"] = await original(text, **kwargs)
        return entry["result"]

    resident.write_mid_turn = recorded  # type: ignore[method-assign]
    rig.manager._sessions["main"].claude = resident
    rig.manager._config.claude_bin = _fake_claude(tmp_path, script)
    return rig, resident, calls


SCRIPT_UNFOLDED = (
    "import pathlib\n"
    "first = json.loads(sys.stdin.readline())\n"
    "print(json.dumps(dict(first, isReplay=True)), flush=True)\n"
    "print(json.dumps({'type':'system','subtype':'init','session_id':'synthetic-session'}), flush=True)\n"
    "print(json.dumps({'type':'assistant','session_id':'synthetic-session','message':{'content':["
    "{'type':'tool_use','id':'t1','name':'Bash','input':{}}]}}), flush=True)\n"
    "open('ready','w').close()\n"
    "second = json.loads(sys.stdin.readline())\n"
    "open('got-second','w').close()\n"
    "while not pathlib.Path('go').exists(): time.sleep(0.01)\n"
    "print(json.dumps({'type':'stream_event','session_id':'synthetic-session','event':{'type':'message_start'}}), flush=True)\n"
    "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'synthetic-session',"
    "'result':'answer A','user_message_uuids':[first.get('uuid')]}), flush=True)\n"
    "print(json.dumps({'type':'command_lifecycle','command_uuid':second['uuid'],'state':'started'}), flush=True)\n"
    "print(json.dumps({'type':'system','subtype':'init','session_id':'synthetic-session'}), flush=True)\n"
    "print(json.dumps(dict(second, isReplay=True)), flush=True)\n"
    "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'synthetic-session',"
    "'result':'native B answer','user_message_uuids':[second['uuid']]}), flush=True)\n"
    + SERVE_REST
)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("case", ["claimed", "stopped_before_claim", "failed_before_claim", "item_queued_ahead"])
def test_unfolded_input_reservation(enabled, tmp_path, case):  # noqa: F811
    """An unconfirmed B continues by its uuid. Does a later turn still get its own events?"""
    async def test(pool):
        rig, resident, calls = _manager_rig(tmp_path, SCRIPT_UNFOLDED)
        gate = asyncio.Event()
        probes = []

        async def tools_problem():
            probes.append(1)
            if case == "stopped_before_claim" and len(probes) == 2:
                await gate.wait()  # B's preparation, before run_turn can claim its reserved turn
            if case == "failed_before_claim" and len(probes) == 2:
                return "synthetic tool probe failure"
            return None

        rig.manager._tools_problem = tools_problem
        await rig.start()
        try:
            first, second, third = _web("first"), _web("second"), _web("third")
            await rig.bus.publish(first)
            await until(lambda: (tmp_path / "ready").exists() and resident._user_turn is not None
                        and resident._user_turn.started)
            pid = resident._process.pid
            await rig.bus.publish(second)
            await until(lambda: (tmp_path / "got-second").exists())
            ahead = None
            if case == "item_queued_ahead":
                # Stands in for a completion batch the router enqueues on this session during A (FIFO, lanes off).
                ahead = _web("queued-ahead")
                rig.manager._register_turn(ahead)
                rig.manager._router.dispatch(rig.manager._sessions["main"], ahead)
            (tmp_path / "go").touch()
            a_reply = await rig.reply(first)
            outcome: dict[str, Any] = {"A": a_reply}
            if case == "stopped_before_claim":
                await until(lambda: len(probes) >= 2)
                outcome["stop"] = str(await rig.manager.stop_turn(second.message_id, via="web"))
                gate.set()
            if ahead is not None:
                try:
                    outcome["ahead"] = await rig.reply(ahead, timeout=8)
                except AssertionError:
                    outcome["ahead"] = "NO REPLY in 8 s"
            try:
                outcome["B"] = await rig.reply(second, timeout=8)
            except AssertionError:
                outcome["B"] = "NO REPLY in 8 s"
            if case != "item_queued_ahead":
                await rig.bus.publish(third)
                try:
                    outcome["C"] = await rig.reply(third, timeout=8)
                except AssertionError:
                    outcome["C"] = "NO REPLY in 8 s"
            rig.collect()
            output = resident._output
            outcome.update(
                B_replies=[e.payload.get("text") for e in rig.replies.get(second.message_id, [])],
                calls=calls, same_pid=resident._process is not None and resident._process.pid == pid,
                reserved=list(output.queued) if output is not None else None,
                buffering=bool(output is not None and output._buffering is not None),
                later_inputs=len((tmp_path / "later-inputs.jsonl").read_text().splitlines())
                if (tmp_path / "later-inputs.jsonl").exists() else 0)
            assert "NO REPLY" not in str(outcome), outcome
            assert outcome["reserved"] == [] and not outcome["buffering"], outcome
            assert outcome["same_pid"], outcome
            assert len(outcome["B_replies"]) == 1, outcome
            if case in {"claimed", "item_queued_ahead"}:
                assert outcome["B"] == "native B answer"
                assert outcome["later_inputs"] == 1  # C or the ahead item only; B was never sent twice
        finally:
            gate.set()
            (tmp_path / "go").touch()
            await rig.close()
    run_db(test, enabled, timeout=90)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("times_out", [False, True])
def test_fifo_item_waits_for_native_input_without_repeating_effects(enabled, tmp_path, times_out):  # noqa: F811
    from looper.agents import chat_settings, claude_cli, completion_events, lanes_settings, session_routing
    from looper.db import manager_events, projects, tasks

    enabled.setattr(session_routing, "_routing_enabled", False)
    enabled.setattr(lanes_settings, "_enabled", False)
    enabled.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    enabled.setattr(claude_cli, "RESERVED_INPUT_WAIT_SEC", 0.1 if times_out else 2, raising=False)
    enabled.setattr(claude_cli, "INTERRUPT_GRACE_SEC", 0.2)
    # The resident commits B to its transcript and performs a tool effect before
    # the FIFO's earlier completion item gets its turn. It can still receive Stop.
    script = SCRIPT_UNFOLDED.split("print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'synthetic-session',"
                                   "'result':'native B answer'")[0] + r"""
import select
with open('transcript.jsonl', 'a') as log: log.write(json.dumps(second) + '\n')
with open('effects', 'a') as log: log.write('B\n')
pathlib.Path('native-running').touch()
while not pathlib.Path('finish-native').exists():
    if not select.select([sys.stdin], [], [], 0.01)[0]: continue
    item = json.loads(sys.stdin.readline())
    assert item['type'] == 'control_request'
    emit = lambda event: print(json.dumps(event), flush=True)
    emit({'type':'control_response','response':{'subtype':'success','request_id':item['request_id'],
          'response':{'cancelled':False}}})
    if item['request']['subtype'] == 'interrupt': break
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'synthetic-session',
      'result':'native B answer','user_message_uuids':[second['uuid']]}), flush=True)
for line in sys.stdin:
    item = json.loads(line)
    assert item['type'] == 'user'
    with open('transcript.jsonl', 'a') as log: log.write(line)
    if 'second' in item['message']['content']:
        with open('effects', 'a') as log: log.write('B\n')
    print(json.dumps(dict(item, isReplay=True)), flush=True)
    print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'synthetic-session',
          'result':'later answer','user_message_uuids':[item['uuid']]}), flush=True)
"""

    async def test(pool):
        rig, resident, _ = _manager_rig(tmp_path, script)
        project = await projects.create_project(name="synthetic", work_dir=str(tmp_path), pool=pool)
        await tasks.create_task(task_id="synthetic-completed", requirement="synthetic", project_id=project["id"],
                                title="Synthetic task", status="completed", work_dir=str(tmp_path), pool=pool)
        settling = asyncio.Event()
        settle = resident.settle_reserved_inputs

        async def observed_settlement(**kwargs):
            if resident._output is not None and resident._output.queued:
                settling.set()
            await settle(**kwargs)

        enabled.setattr(resident, "settle_reserved_inputs", observed_settlement)
        await rig.start()
        release = None
        try:
            first, second = _web("first"), _web("second")
            await rig.bus.publish(first)
            await until(lambda: (tmp_path / "ready").exists() and resident._user_turn is not None
                        and resident._user_turn.started)
            await rig.bus.publish(second)
            await until(lambda: (tmp_path / "got-second").exists())
            from looper.agents.message import MessageEnvelope, MessageType, TASK_ROUTER_ADDRESS, MANAGER_ADDRESS

            ended = MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS,
                message_type=MessageType.GOAL_STATUS,
                payload={"task_id": "synthetic-completed", "status": "completed", "summary": "Synthetic result"})
            recorded = await completion_events.record_web_end("manager", ended, "web-chat-1", "Synthetic fallback")
            assert recorded.row["state"] == "pending"
            batch = uuid.uuid4()
            rows = await manager_events.claim("manager", "channel-web", "web-chat-1", batch)
            assert len(rows) == 1
            rig.manager._router.dispatch_completion(completion_events.wake(rig.manager.agent_address,
                completion_events.Target("manager", "channel-web", "web-chat-1")), rows, batch, None)
            (tmp_path / "go").touch()
            await until(lambda: (tmp_path / "native-running").exists())
            await asyncio.wait_for(settling.wait(), 5)

            async def finish_native():
                await asyncio.sleep(0.4)
                (tmp_path / "finish-native").touch()

            release = asyncio.create_task(finish_native())
            assert await rig.reply(first) == "answer A"
            assert await rig.reply(second) == "native B answer"
            row = await pool.fetchrow("SELECT state, reason FROM manager_events WHERE id=$1", recorded.row["id"])
            assert (row["state"], row["reason"]) == (("fallback", "turn_failed") if times_out else ("delivered", None))
            assert (tmp_path / "effects").read_text().splitlines() == ["B"]
            assert len((tmp_path / "transcript.jsonl").read_text().splitlines()) == (1 if times_out else 2)
        finally:
            (tmp_path / "finish-native").touch()
            if release is not None:
                await release
            await rig.close()

    run_db(test, enabled, timeout=45)
