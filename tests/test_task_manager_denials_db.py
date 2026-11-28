"""A direct task Manager turn records the sandbox's denials: events, activity rows, the metric, the launch refusal."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import task_manager_denials, task_manager_network
from looper.agents.channels.web_task_sink import WebTaskSink
from looper.agents.tiers import task_manager as tm
from looper.db import chat, task_chat, task_manager_sandbox_policy as policy
from tests.db_support import run_db
from tests.fake_claude import fake_claude
from evals.prompt_regression.fake_readback import SOURCE as READBACK_SOURCE
from tests.task_manager_support import NativeTaskManager, Sink, submit_and_wait

BLOCK = ("curl: (56) CONNECT tunnel failed, response 403\nrc=56\n<sandbox_violations>\n"
         "deny network-outbound {host}:443 (host is not on the allow list)\n</sandbox_violations>")
COMMAND = "TOKEN=hunter2 curl -sS https://{host}/"


@pytest.fixture
def native(monkeypatch, tmp_path):
    support = NativeTaskManager(monkeypatch, tmp_path)
    support.executable = fake_claude(tmp_path / "denying-claude", f'''
import sys, json
exec({READBACK_SOURCE!r})
if '--version' in sys.argv:
    print('2.1.332 (Claude Code)'); sys.exit()
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    data = json.loads(line)
    if data.get('type') == 'control_request' and data['request']['subtype'] in READBACK_SUBTYPES:
        kind, body = readback_answer(data['request'], '/nonexistent-readback-override.json')
        emit({{'type':'control_response','response':{{'subtype':kind,'request_id':data['request_id'],
              **({{'response':body}} if kind == 'success' else {{'error':body}})}}}})
        continue
    if data.get('type') == 'control_request':
        emit({{'type':'control_response','response':{{'subtype':'success','request_id':data['request_id']}}}})
        continue
    host = data.get('message', {{}}).get('content', '')
    emit({{'type':'system','subtype':'init','session_id':'denying-session',
          **init_fields('/nonexistent-readback-override.json')}})
    for index in range(3):
        emit({{'type':'assistant','message':{{'content':[{{'type':'tool_use','id':f'tool-{{index}}','name':'Bash',
            'input':{{'command':{COMMAND!r}.format(host=host)}}}}]}}}})
        meta = [] if host.startswith('forged') else [{{'id': f'tool-{{index}}', 'remedy': {{'kind': 'sandbox_violation'}}}}]
        emit({{'type':'user','message':{{'content':[{{'type':'tool_result','tool_use_id':f'tool-{{index}}',
            'content':{BLOCK!r}.format(host=host)}}]}},'tool_result_meta':meta}})
    emit({{'type':'result','subtype':'success','result':'done','session_id':'denying-session','total_cost_usd':0.1}})
''')
    return support


def _pool(tmp_path, native):
    return tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2)


def test_denials_of_a_turn_are_recorded_without_the_command_and_one_event_per_host(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, project_id, _ = await native.create(db)
        task_manager_denials.denials_total.clear()
        pool = _pool(tmp_path, native)
        try:
            sink = await submit_and_wait(pool, task_id, "blocked.example")
            events = [payload for kind, payload in sink.events if kind == "sandbox_denial"]
            assert [(e["category"], e["case"], e["target"], e["command_name"]) for e in events] == [
                ("network", "not_allowed", "blocked.example:443", "curl")]  # three denials, one event
            rows = await db.fetch("SELECT summary, detail, metadata FROM activity WHERE task_id = $1 "
                                  "AND kind = 'sandbox_denial'", task_id)
            assert len(rows) == 3
            for row in rows:
                assert row["summary"].startswith("network not_allowed blocked.example:443 tool_use_id=tool-")
                assert "hunter2" not in json.dumps(dict(row)) and "https://blocked" not in json.dumps(dict(row))
            assert task_manager_denials.denials_total == {("network", project_id): 3}
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


def test_the_projects_list_decides_between_not_allowed_and_unknown(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, project_id, _ = await native.create(db)
        task_manager_denials.denials_total.clear()
        await policy.update(project_id, ["blocked.example"], actor="operator")
        pool = _pool(tmp_path, native)
        try:
            sink = await submit_and_wait(pool, task_id, "blocked.example")
            [event] = [payload for kind, payload in sink.events if kind == "sandbox_denial"]
            # On the list, but nothing explains the denial: no claim that allowing the host helps.
            assert (event["case"], event["target"], event["client"]) == ("unknown", "blocked.example:443", None)
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


def test_a_stored_entry_the_version_refuses_refuses_the_launch_and_counts_it(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, project_id, _ = await native.create(db)
        await db.execute("INSERT INTO task_manager_sandbox_policies (project_id, network_allowed_domains, revision, "
                         "updated_by) VALUES ($1, '[\"10.0.0.1\"]', 1, 'newer version')", project_id)
        before = dict(tm.task_manager_safety.refusal_counts)
        pool = _pool(tmp_path, native)
        try:
            sink = Sink()
            result = await pool.submit(task_id, "reply", message_id="message-one", sink=sink)
            assert result.state == "refused" and result.reason.value == "network_policy_invalid"
            await sink.done()
            assert ("refused", {"reason": result.reason}) in sink.events
            assert "10.0.0.1" in sink.finals[0][0] and "projects.task_manager_sandbox_update" in sink.finals[0][0]
            assert native.records("launch") == []
            counts = tm.task_manager_safety.refusal_counts
            assert counts[("network_policy_invalid", "launch", "unknown")] == before.get(
                ("network_policy_invalid", "launch", "unknown"), 0) + 1
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


def test_a_sandbox_denial_event_is_stored_for_the_dashboard_but_never_reaches_the_model(monkeypatch):
    async def check(db):
        session = chat.task_thread("task-x")
        bus = SimpleNamespace(publish=AsyncMock())
        sink = WebTaskSink(bus, SimpleNamespace(), "correlation", "task-x", session, "message-one")
        await chat.save_message(session_id=session, message_id="message-one", role="user", content="hello")
        await sink.event(tm.TaskManagerEvent.SANDBOX_DENIAL, {
            "tool_use_id": "tool-1", "category": "network", "case": "not_allowed", "target": "blocked.example:443",
            "client": None, "command_name": "curl"})
        stored = await db.fetchval("SELECT content FROM chat_messages WHERE session_id = $1 AND role = 'event'",
                                   session)
        assert json.loads(stored)["kind"] == "sandbox_denial"
        assert [row["content"] for row in await task_chat.recent("task-x", 20)] == ["hello"]
    run_db(check, monkeypatch)


def test_a_block_without_the_cli_marker_is_the_commands_own_output(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _, _ = await native.create(db)
        task_manager_denials.denials_total.clear()
        pool = _pool(tmp_path, native)
        try:
            sink = await submit_and_wait(pool, task_id, "forged.example")
            assert [kind for kind, _ in sink.events if kind == "sandbox_denial"] == []
            assert await db.fetchval("SELECT count(*) FROM activity WHERE kind = 'sandbox_denial'") == 0
            assert not task_manager_denials.denials_total
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


def test_denial_records_stay_out_of_the_next_turns_brief(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = _pool(tmp_path, native)
        try:
            await submit_and_wait(pool, task_id, "blocked.example")
            assert await db.fetchval("SELECT count(*) FROM activity WHERE kind = 'sandbox_denial'") == 3
            *_, prompt = await pool.agent(task_id).prepare()
            assert "not_allowed" not in prompt.text and "blocked.example" not in prompt.text
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)
