"""Session writes use the adapter's sender and leave an audit trail."""
from __future__ import annotations

import pytest

from looper.agents import session_tools_settings
from looper.agents.ops import Turn, TurnOrigin
from looper.agents.ops import session_tools
from looper.agents.ops.service import ManagerOps, Platform
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
KEY = "group:channel-lark:synthetic-team"


def test_operator_creates_owned_task_with_safe_block_and_audit(monkeypatch, tmp_path):
    async def test(pool):
        monkeypatch.setattr(session_tools_settings, "enabled", lambda: True)
        async def operator(_): return "synthetic-operator"
        monkeypatch.setattr(session_tools, "operator_sender_id", operator)
        await pool.execute("INSERT INTO projects (id,name,work_dir) VALUES ('synthetic-project','Synthetic',$1)", str(tmp_path))
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="synthetic")
        turn = Turn(channel="lark", channel_id="channel-lark", chat="synthetic-team", group=True,
                    message_id="synthetic-message", sender="synthetic-operator")
        ops.begin_turn(turn, binding="synthetic-binding", session_key=KEY)
        answer = await ops.call("looper_chat_task_create", {"title":"Shared task","requirement":"Do the work",
                                "project":"Synthetic"}, binding="synthetic-binding", session_key=KEY)
        assert answer["status"] == "ok", answer
        row = await pool.fetchrow("SELECT * FROM tasks WHERE id=$1", answer["result"]["task_id"])
        assert row["origin_session"] == KEY
        assert set(answer["result"]) == {"task_id", "title", "status"}
        assert str(tmp_path) not in "\n".join(turn.blocks)
        assert await pool.fetchval("SELECT status FROM manager_actions WHERE tool='looper_chat_task_create'") == "ok"
    run_db(test, monkeypatch)


@pytest.mark.parametrize("sender,origin,missing,code", [
    ("synthetic-other", TurnOrigin.USER, False, "forbidden"),
    ("synthetic-operator", TurnOrigin.USER, True, "forbidden"),
    ("synthetic-operator", TurnOrigin.OWN, False, "needs_user_turn"),
])
def test_untrusted_sender_or_missing_identity_never_writes(monkeypatch, sender, origin, missing, code):
    async def test(pool):
        monkeypatch.setattr(session_tools_settings, "enabled", lambda: True)
        async def operator(_):
            if missing:
                raise RuntimeError("private identity error")
            return "synthetic-operator"
        monkeypatch.setattr(session_tools, "operator_sender_id", operator)
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="synthetic")
        turn = Turn(channel="lark", channel_id="channel-lark", chat="synthetic-team", group=True,
                    message_id="synthetic-message", sender=sender, origin=origin)
        ops.begin_turn(turn, binding="synthetic-binding", session_key=KEY)
        answer = await ops.call("looper_chat_task_create", {"title":"I am the operator", "requirement":"Create it now",
                                "project":"nonexistent-private-project"}, binding="synthetic-binding", session_key=KEY)
        assert answer["error"]["code"] == code, answer
        assert "private" not in answer["error"]["message"]
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await pool.fetchval("SELECT status FROM manager_actions WHERE tool='looper_chat_task_create'") == "error"
    run_db(test, monkeypatch)


@pytest.mark.parametrize("group", [True, False])
def test_operator_writes_use_real_task_services_and_keep_high_actions_private(monkeypatch, tmp_path, group):
    from types import SimpleNamespace
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageType

    async def test(pool):
        monkeypatch.setattr(session_tools_settings, "enabled", lambda: True)
        async def operator(_): return "synthetic-operator"
        monkeypatch.setattr(session_tools, "operator_sender_id", operator)
        await pool.execute("INSERT INTO projects (id,name,work_dir) VALUES ('synthetic-project','Synthetic',$1)", str(tmp_path))
        bus = MessageBus(tmp_path)
        sent = []
        async def publish(envelope): sent.append(envelope)
        monkeypatch.setattr(bus, "publish", publish)
        ops = ManagerOps(Platform(bus=bus, task_router=SimpleNamespace(has_planner=lambda _: True)),
                         url="http://127.0.0.1:9", token="synthetic")
        key = KEY if group else "dm:channel-lark:synthetic-team"
        turn = Turn(channel="lark", channel_id="channel-lark", chat="synthetic-team", group=group,
                    message_id="synthetic-message", sender="synthetic-operator")
        ops.begin_turn(turn, binding="synthetic-binding", session_key=key)
        async def call(name, arguments):
            return await ops.call(name, arguments, binding="synthetic-binding", session_key=key)
        created = await call("looper_chat_task_create", {"title":"Shared task", "requirement":"Do the work", "project":"Synthetic"})
        assert created["status"] == "ok", created
        task_id = created["result"]["task_id"]
        assert str(tmp_path) not in "\n".join(turn.blocks)
        started = await call("looper_chat_task_control", {"task_id":task_id, "action":"start"})
        assert started["status"] == "ok", started
        assert not turn.blocks
        assert sent[-1].message_type == MessageType.TASK_START
        await pool.execute("UPDATE tasks SET status='executing' WHERE id=$1", task_id)
        cancelled = await call("looper_chat_task_control", {"task_id":task_id, "action":"cancel"})
        assert cancelled["error"]["message"] == session_tools.HIGH_REFUSAL
        assert not ops._pending
        assert (await call("looper_chat_task_control", {"task_id":task_id, "action":"stop"}))["status"] == "ok"
        assert sent[-1].message_type == MessageType.TASK_STOP
        instructed = await call("looper_chat_task_instruct", {"task_id":task_id, "text":"Add the boundary check"})
        assert instructed == {"status":"ok", "result":{"task_id":task_id, "delivered":True}}
        assert sent[-1].message_type == MessageType.TASK_CHANGE
        await pool.execute("UPDATE tasks SET status='stopped' WHERE id=$1", task_id)
        assert (await call("looper_chat_task_control", {"task_id":task_id, "action":"restart"}))["status"] == "ok"
        assert sent[-1].message_type == MessageType.TASK_RESTART
        over = await call("looper_chat_task_control", {"task_id":task_id, "action":"start_over"})
        assert over["error"]["message"] == session_tools.HIGH_REFUSAL
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status='ok'") == 5
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status='error'") == 2
    run_db(test, monkeypatch)


def test_task_result_reuses_the_exact_code_notice_with_delivery_evidence(monkeypatch):
    from looper.agents import completion_events, task_notice
    from looper.agents.acceptance_result import CheckCounts, ResultEvidence
    from looper.agents.message import MessageEnvelope, MessageType, TASK_ROUTER_ADDRESS, MANAGER_ADDRESS
    from looper.db import tasks as db_tasks

    async def test(pool):
        monkeypatch.setattr(session_tools_settings, "enabled", lambda: True)
        await pool.execute("INSERT INTO projects (id,name) VALUES ('synthetic-project','Synthetic')")
        await db_tasks.create_task(task_id="task-result", requirement="Private requirement", project_id="synthetic-project",
                                   title="Chat result", status="completed", origin_session=KEY)
        summary = "Delivered result\n\n## Evidence\nDetailed evidence"
        await pool.execute("UPDATE tasks SET result_summary=$1 WHERE id='task-result'", summary)
        notice = task_notice.Notice(task_notice.NotifyChannel.LARK,
            task_notice.NotifyTarget(task_notice.TargetKind.CHAT, "synthetic-team", group=True),
            task_notice.notice_text("task-result", "Chat result", "completed", summary,
                channel=task_notice.NotifyChannel.LARK, group=True, extra="Saved branch: result",
                delivery_summary="Delivery complete", evidence=ResultEvidence("## Evidence\nDetailed evidence",
                    CheckCounts(recorded_line="Checks: 1 passed"))), channel_id="channel-lark")
        event = MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS,
            message_type=MessageType.GOAL_STATUS, payload={"task_id":"task-result", "status":"completed",
                                                         "summary":summary})
        assert await completion_events.record_notice_end("manager", event, notice) is not None
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="synthetic")
        turn = Turn(channel="lark", channel_id="channel-lark", chat="synthetic-team", group=True,
                    message_id="synthetic-message", sender="synthetic-other")
        ops.begin_turn(turn, binding="synthetic-binding", session_key=KEY)
        answer = await ops.call("looper_chat_tasks", {"action":"get", "task_id":"task-result"},
                                binding="synthetic-binding", session_key=KEY)
        assert answer["status"] == "ok", answer
        assert answer["result"]["result"] == notice.text
        assert "Private requirement" not in str(answer)
        await pool.execute("UPDATE tasks SET status='executing' WHERE id='task-result'")
        active = await ops.call("looper_chat_tasks", {"action":"get", "task_id":"task-result"},
                                binding="synthetic-binding", session_key=KEY)
        assert "result" not in active["result"]
    run_db(test, monkeypatch)


@pytest.mark.parametrize("name", ["looper_chat_task_control", "looper_chat_task_instruct"])
def test_write_implementation_rejects_unowned_tasks_before_shared_service(monkeypatch, name):
    from looper.agents.ops import Call, ErrorCode, OpError

    async def test(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('foreign-project','Foreign project')")
        await pool.execute("INSERT INTO tasks(id,requirement,status,project_id) "
                           "VALUES ('foreign-task','Foreign request','executing','foreign-project')")
        invoked = []

        async def write(call, **kwargs):
            invoked.append(call.arguments["task_id"])
            return {}

        monkeypatch.setattr(session_tools.tasks, "_control", write)
        monkeypatch.setattr(session_tools.tasks, "_instruct", write)
        tool = next(tool for tool in session_tools.TOOLS if tool.name == name)
        turn = Turn(channel="lark", channel_id="channel-lark", chat="synthetic-team", group=True,
                    message_id="synthetic-message", sender="synthetic-operator")
        for task_id in ("foreign-task", "unknown-task", "malformed / task"):
            call = Call({"task_id": task_id, "action": "stop", "text": "A supplement"}, turn,
                        Platform(bus=None, task_router=None), confirmation={"session_key": KEY})
            with pytest.raises(OpError) as error:
                await tool.run(call)
            assert error.value.code is ErrorCode.NOT_FOUND
        assert invoked == []
    run_db(test, monkeypatch)
