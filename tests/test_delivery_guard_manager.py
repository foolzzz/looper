"""Delivery guard on the Manager's real resident stream, with database writes observed."""

import asyncio
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings
from looper.agents.delivery_guard import FAILURE_NOTICE, FOLLOWUP_MESSAGE, REMINDER_MESSAGE
from tests.test_manager_reply import _ask, _session_with
from looper.agents.claude_cli import ResidentClaude
from looper.agents.delivery_guard import DeliveryDebt, GuardState
from looper.agents.runtimes import Capability
from looper.agents.tiers.turn_token import TurnToken


@pytest.mark.parametrize("ending", ["consumed", "cancelled", "dequeued"])
def test_pending_reminder_does_not_block_event_handling(tmp_path, ending):
    from looper.agents.delivery_guard_manager import observe, finish

    async def run():
        resident = ResidentClaude(label="delivery-guard")
        token = TurnToken("message", None, "web", "chat", datetime.now(timezone.utc))
        recorder = SimpleNamespace(annotate=AsyncMock())
        prepared = SimpleNamespace(launch=SimpleNamespace(
            profile=SimpleNamespace(runtime=SimpleNamespace(capabilities=frozenset({Capability.MID_TURN_INPUT}))),
            runtime=SimpleNamespace(hosts_runs=False)), turn=SimpleNamespace(debt=DeliveryDebt.OWED),
            run=SimpleNamespace(recorder=recorder), delivery_followup=0)
        events = asyncio.Queue()
        progress = asyncio.Event()
        state = GuardState(DeliveryDebt.OWED)

        async def handle():
            nonlocal state
            while (event := await events.get()) is not None:
                if event.get("type") != "assistant":
                    continue
                for block in event.get("message", {}).get("content", []):
                    state = await observe(state, block, prepared=prepared,
                                          manager=SimpleNamespace(_claude=resident), token=token)
                    if block.get("type") == "text":
                        progress.set()

        script = """
import json, sys
import pathlib, subprocess
def out(event):
    print(json.dumps(event), flush=True)
first = json.loads(sys.stdin.readline())
out(dict(first, isReplay=True))
out({'type':'assistant','message':{'content':[{'type':'tool_use','id':str(i)} for i in range(6)]}})
reminder = json.loads(sys.stdin.readline())
out({'type':'assistant','message':{'content':[{'type':'text','text':'Progress.'}]}})
sys.stdin.readline()
if ENDING == 'consumed':
    out(dict(reminder, isReplay=True))
out({'type':'result','subtype':'success','is_error':False,'result':'Done.',
     'user_message_uuids':[first['uuid']] + ([reminder['uuid']] if ENDING == 'consumed' else [])})
if ENDING != 'consumed':
    if ENDING == 'dequeued':
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        out({'type':'command_lifecycle','command_uuid':reminder['uuid'],'state':'started'})
        out(dict(reminder, isReplay=True))
    control = json.loads(sys.stdin.readline())
    assert control['request'] == {'subtype':'cancel_async_message', 'message_uuid':reminder['uuid']}
    out({'type':'control_response','response':{'subtype':'success','request_id':control['request_id'],
         'response':{'cancelled':ENDING == 'cancelled','message_uuid':reminder['uuid']}}})
    if ENDING == 'dequeued':
        control = json.loads(sys.stdin.readline())
        request = control['request']
        assert request['subtype'] == 'interrupt' and request['cancel_queued'] is True
        if not request.get('spare_background_tasks'):
            child.terminate()
            child.wait()
        assert child.poll() is None, 'interrupt ended background work'
        pathlib.Path('background-survived').touch()
        out({'type':'control_response','response':{'subtype':'success','request_id':control['request_id'],
             'response':{'cancelled':[]}}})
        out({'type':'result','subtype':'error_during_execution','is_error':True,'result':'',
             'user_message_uuids':[reminder['uuid']]})
        child.terminate()
        child.wait()
sys.stdin.read()
"""
        script = "ENDING = " + repr(ending) + "\n" + script
        await resident.start([sys.executable, "-u", "-c", script], cwd=str(tmp_path), signature=1)
        process = resident._process
        handling = asyncio.create_task(handle())
        task = asyncio.create_task(resident.run_turn("hello", events.put, cancel=token.cancel))
        try:
            # The child withholds the receipt until the later progress event is handled.
            await asyncio.wait_for(progress.wait(), 5)
            recorder.annotate.assert_not_awaited()
            process.stdin.write(b"release\n")
            await process.stdin.drain()
            result = await asyncio.wait_for(task, 5)
            await events.put(None)
            await handling
            await finish(token)
            assert result.result_event["result"] == "Done."
            assert resident._process is process and process.returncode is None
            if ending == "dequeued":
                assert (tmp_path / "background-survived").exists()
        finally:
            await finish(token)
            handling.cancel()
            task.cancel()
            await resident.stop(kill=True)
            await asyncio.gather(handling, task, return_exceptions=True)

    asyncio.run(run())


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", True)


def _script(endings):
    return (
        "endings = " + repr(endings) + "\n"
        "for index, raw in enumerate(sys.stdin):\n"
        "    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'synthetic-session'}), flush=True)\n"
        "    if index == 0:\n"
        "        print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','input':{}}]}}), flush=True)\n"
        "        print(json.dumps({'type':'assistant','parent_tool_use_id':'nested','message':{'content':[{'type':'text','text':'Internal.'}]}}), flush=True)\n"
        "    ending = endings[min(index, len(endings)-1)]\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': ending}), flush=True)\n"
    )


@pytest.mark.parametrize("endings,expected,records", [
    (["", "Recovered."], "Recovered.", 2),
    (["", "", "Recovered."], "Recovered.", 3),
    (["", "", ""], FAILURE_NOTICE, 3),
])
def test_empty_success_runs_hidden_followups_in_the_same_session(agent_db, endings, expected, records):
    chat = _session_with(agent_db, _script(endings), ["hello"])
    assert [reply.payload["text"] for reply in chat["replies"]] == [expected]
    assert len(chat["runs"]) == records
    for index, record in enumerate(chat["runs"][1:], 1):
        assert record["input_text"] == FOLLOWUP_MESSAGE
    annotations = [call.args[1]["delivery_guard"] for call in agent_db.add_metadata.await_args_list
                   if "delivery_guard" in call.args[1]]
    assert annotations == [{"followup": n, "for": chat["sent"][0].message_id} for n in range(1, records)]
    if expected == FAILURE_NOTICE:
        assert chat["replies"][0].payload["error"] == "no reply after follow-ups"


@pytest.mark.parametrize("first", [True, False])
def test_errors_do_not_retry_and_followup_errors_use_the_fixed_notice(agent_db, first):
    error = "print(json.dumps({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True}), flush=True)\n"
    body = (_script([""]).split("for index", 1)[0] + "for index, raw in enumerate(sys.stdin):\n"
            "    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's'}), flush=True)\n"
            + ("    " + error if first else
               "    if index:\n        " + error +
               "    else:\n        print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':''}), flush=True)\n"))
    answer = _ask(body)
    assert (answer["text"] == FAILURE_NOTICE) is not first
    assert agent_db.start_record.await_count == (1 if first else 2)


def test_reminders_join_one_resident_turn_and_text_resets_the_streak(agent_db):
    # The fake waits for each queued input and echoes it as a replay while the original turn is running.
    body = """
raw = json.loads(sys.stdin.readline())
def out(event):
    print(json.dumps(event), flush=True)
def tools(count):
    for i in range(count):
        out({'type':'assistant','message':{'content':[{'type':'tool_use','id':str(i),'name':'Read','input':{}}]}})
def reminder():
    import select
    assert select.select([sys.stdin], [], [], 5)[0], 'no reminder received'
    queued = json.loads(sys.stdin.readline())
    assert queued['message']['content'] == REMINDER
    out({'type':'user','isReplay':True,'uuid':queued.get('uuid'),'message':queued['message']})
out({'type':'user','isReplay':True,'uuid':raw['uuid'],'message':raw['message']})
out({'type':'system','subtype':'init','session_id':'s'})
tools(5)
out({'type':'assistant','parent_tool_use_id':'nested','message':{'content':[{'type':'tool_use','name':'Read','input':{}}] * 6}})
out({'type':'assistant','message':{'content':[{'type':'text','text':'Progress.'}]}})
tools(5)
import select
assert not select.select([sys.stdin], [], [], 0.1)[0], 'text did not reset the streak'
tools(1)
reminder()
tools(6)
reminder()
tools(12)
out({'type':'result','subtype':'success','is_error':False,'result':'Done.'})
"""
    answer = _ask("REMINDER = " + repr(REMINDER_MESSAGE) + "\n" + body)
    assert answer["text"] == "Done."
    assert agent_db.start_record.await_count == agent_db.finish_record.await_count == 1
    guards = [call.args[1]["delivery_guard"] for call in agent_db.add_metadata.await_args_list
              if "delivery_guard" in call.args[1]]
    assert guards == [{"reminders": 1}, {"reminders": 2}]


@pytest.mark.parametrize("session_kind,capable", [
    ("resident", False), ("keeper", True), ("runner", True),
])
def test_reminders_require_the_capability_and_the_verified_session_kind(session_kind, capable):
    from looper.agents.delivery_guard_manager import observe
    from looper.agents.kept_run import KeptProcess
    from looper.agents.runtimes.runner.session import RunnerSession

    if session_kind == "runner":
        session = object.__new__(RunnerSession)
    else:
        session = ResidentClaude(label="synthetic")
        session._process = object.__new__(KeptProcess if session_kind == "keeper" else asyncio.subprocess.Process)
    session.write_mid_turn = AsyncMock()
    caps = frozenset({Capability.MID_TURN_INPUT}) if capable else frozenset()
    prepared = SimpleNamespace(launch=SimpleNamespace(
        profile=SimpleNamespace(runtime=SimpleNamespace(capabilities=caps)), runtime=SimpleNamespace(hosts_runs=False)),
        turn=SimpleNamespace(debt=DeliveryDebt.OWED))

    async def run():
        state = GuardState(DeliveryDebt.OWED)
        for _ in range(24):
            state = await observe(state, {"type": "tool_use"}, prepared=prepared,
                                  manager=SimpleNamespace(_claude=session), token=SimpleNamespace())
        session.write_mid_turn.assert_not_awaited()

    asyncio.run(run())


def test_autonomous_turn_is_exempt_with_guard_enabled(agent_db):
    from claude_stream_replay import replay_body

    chat = _session_with(agent_db, replay_body("background_agent"), ["start a background agent", "7*6?"])
    assert len(chat["runs"]) == 3
    assert sum(run["metadata"].get("autonomous", False) for run in chat["runs"]) == 1
    assert not any("delivery_guard" in call.args[1] for call in agent_db.add_metadata.await_args_list)


@pytest.mark.parametrize("ending", ["exit", "cap", "unsuccessful"])
def test_exit_and_spend_cap_keep_the_existing_error_without_followups(agent_db, ending):
    from looper.agents.claude_cli import BUDGET_SUBTYPE

    body = {"exit": "sys.exit(2)",
            "cap": "print(json.dumps({'type':'result','subtype':" + repr(BUDGET_SUBTYPE)
                   + ", 'is_error': True}), flush=True)",
            "unsuccessful": "print(json.dumps({'type':'result','subtype':'incomplete','is_error':False}), flush=True)"}[ending]
    answer = _ask("sys.stdin.readline()\n" + body)
    assert answer["text"].startswith("Manager 出错，没能完成这次回复：")
    assert agent_db.start_record.await_count == 1


@pytest.mark.parametrize("answer", ["Recovered.", ""])
def test_recovery_keeps_the_tools_existing_reply_blocks(monkeypatch, agent_db, answer):
    from looper.agents.tiers.manager import ManagerAgent

    prepare = ManagerAgent._prepare_turn

    async def with_block(self, *args, **kwargs):
        prepared = await prepare(self, *args, **kwargs)
        prepared.turn.blocks.append("Tool result.")
        return prepared

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", with_block)
    reply = _ask(_script(["", answer]))
    assert reply["text"] == (answer or FAILURE_NOTICE) + "\n\nTool result."


@pytest.mark.parametrize("problem,records", [("tools_down", 0), ("refused", 1)])
def test_unavailable_tools_and_refused_tokens_never_start_recovery(monkeypatch, agent_db, problem, records):
    from looper.agents.tiers.manager import ManagerAgent

    if problem == "tools_down":
        monkeypatch.setattr(ManagerAgent, "_tools_problem", AsyncMock(return_value="Synthetic tools unavailable"))
        agent_db.store.sessions["manager"] = {}  # no runtime starts or saves a session on this path
    else:
        prepare = ManagerAgent._prepare_turn

        async def refused(self, *args, **kwargs):
            prepared = await prepare(self, *args, **kwargs)
            prepared.turn.refused = True
            return prepared

        monkeypatch.setattr(ManagerAgent, "_prepare_turn", refused)
    chat = _session_with(agent_db, _script([""]), ["hello"])
    (reply,) = chat["replies"]
    assert reply.payload["text"] != FAILURE_NOTICE
    assert len(chat["runs"]) == records
