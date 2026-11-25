"""A task conversation: its thread, streamed answer, stop, fixed commands, and the unchanged Manager path."""

import copy
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from looper.agents import api_v1, chat_settings, task_conversations_settings as settings
from looper.agents.bus import MessageBus
from looper.agents.channels.web import WebChannelAdapter
from looper.agents.message import MANAGER_ADDRESS, MessageType, channel_address
from looper.agents.ops.clients import ClientOps
from looper.agents.tiers.manager import StopResult
from looper.agents.tiers.task import TaskRouter, _WorkspaceFinish
from looper.agents.tiers import task_manager as task_managers
from looper.agents.tiers.task_manager import RefuseReason, Submitted, ToolRecord
from looper.db import chat, projects, tasks
from looper.db.chat import PLANNER_THREAD_PREFIX, task_thread
from looper.db import task_chat
from tests.db_support import run_db
from tests.test_api_v1_auth import _bearer, _send, _send_raw, _serving
from tests.test_operator_auth import TOKEN

PREFIX = "/api/v1/operator_chat/"
# Canonical OpenAPI document on this branch before the task target and the two stream events.
PRE_CARD_DOCUMENT = "18973c35d8b7af331ff6ca9f7a1854b9df4a97a06fea12400dd73709fe5ebb8d"
LATER = "这个命令要等「交给后台」上线后才能用"


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(settings, "_enabled", False)
    monkeypatch.setattr(chat_settings, "_stop_enabled", False)


class Pool:
    """Records submit and stop, and can drive the sink the server hands it."""

    def __init__(self):
        self.calls = []
        self.stopped = []
        self.stopped_messages = []
        self.running = {}
        self.drive = None
        self.refuse = False

    async def submit(self, task_id, text, *, message_id, sink, history=()):
        self.calls.append({"task_id": task_id, "text": text, "message_id": message_id, "history": list(history)})
        self.running[task_id] = message_id
        if self.drive is not None:
            await self.drive(sink)
            self.running.pop(task_id, None)
        if self.refuse:
            await sink.event("refused", {"reason": "work_mode_off"})
            await sink.final("no", "refused")
            self.running.pop(task_id, None)
            return Submitted("refused", RefuseReason.WORK_MODE_OFF)
        return Submitted("started")

    def in_turn(self, task_id):
        return task_id in self.running

    async def stop_current(self, task_id):
        if task_id not in self.running:
            return False
        self.stopped.append(task_id)
        self.running.pop(task_id, None)
        return True

    async def stop_by_message(self, message_id):
        for task_id, running in list(self.running.items()):
            if running == message_id:
                self.stopped_messages.append(message_id)
                self.running.pop(task_id, None)
                return True
        return False


class Manager:
    def __init__(self):
        self.asked = []

    def turn_for(self, message_id):
        return None

    async def stop_turn(self, message_id, via="web"):
        self.asked.append((message_id, via))
        return StopResult.STOPPING


def target(task_id):
    return {"kind": "task", "task_id": task_id}


async def make_task(pool, tmp_path, *, status="draft"):
    project = await projects.create_project(name=f"conversation-{uuid.uuid4().hex[:8]}", work_dir=str(tmp_path),
                                            pool=pool)
    task_id = f"task-{project['id'].removeprefix('proj-')}"
    await tasks.create_task(task_id=task_id, requirement="draft work", project_id=project["id"],
                            status=status, work_dir=str(tmp_path), pool=pool)
    return task_id


async def post(server, action, body):
    return await _send(server, "POST " + PREFIX + action, _bearer(TOKEN), json.dumps(body))


def parse_events(body):
    return [(block.splitlines()[0][7:], json.loads(block.splitlines()[1][6:]))
            for block in body.strip().split("\n\n") if block.strip()]


async def reply(server, correlation_id):
    status, _, raw = await _send_raw(server, "POST " + PREFIX + "reply_events", _bearer(TOKEN),
                                     json.dumps({"correlation_id": correlation_id}))
    assert status == 200, raw[:400]
    return [event for event in parse_events(raw) if event[0] != "heartbeat"]


async def send(server, task_id, text, message_id, **extra):
    status, _, answer = await post(server, "send", {"text": text, "message_id": message_id,
                                                    "target": target(task_id), **extra})
    return status, answer


def final_text(events):
    finals = [payload for kind, payload in events if kind == "answer.final"]
    assert finals, events
    return finals[-1]


def command_event(events, command):
    found = [payload for kind, payload in events if kind == "task.event"]
    assert {"kind": "command", "command": command} in found
    return found


@asynccontextmanager
async def opened(tmp_path):
    bus = MessageBus(runtime_root=tmp_path)
    adapter = WebChannelAdapter()
    await adapter.start(bus)
    try:
        # A real socket: an in-process reader is already at EOF, and that cancels a reply stream
        # before the task Manager's final answer is written.
        async with _serving(tmp_path, listen=True) as server:
            server._web_adapter = adapter
            server._task_router = TaskRouter(runtime_root=tmp_path)
            server._client_ops = ClientOps(SimpleNamespace())
            yield server
    finally:
        await adapter.stop()
        executor = getattr(bus, "_journal_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


def use_pool(monkeypatch, standin):
    monkeypatch.setattr(task_managers, "current_pool", lambda: standin)


def test_send_to_task(monkeypatch, tmp_path):
    pool = Pool()

    async def drive(sink):
        await sink.delta("hello")
        await sink.tool(ToolRecord("tool-1", "read", "{}", "", False))
        await sink.tool(ToolRecord("tool-1", "read", "{}", "done", False))
        await sink.event("turn_summary", {"files": []})
        await sink.final("the answer", "answered")

    pool.drive = drive
    use_pool(monkeypatch, pool)

    async def check(conn):
        settings.set_enabled(True)
        task_id = await make_task(conn, tmp_path)
        async with opened(tmp_path) as server:
            status, answer = await send(server, task_id, "please look", "m1")
            assert status == 200, answer
            correlation_id = answer["result"]["correlation_id"]
            assert correlation_id
            assert pool.calls == [{
                "task_id": task_id, "text": "please look", "message_id": "m1",
                "history": pool.calls[0]["history"]}]
            assert pool.calls[0]["history"][-1]["role"] == "user"
            assert pool.calls[0]["history"][-1]["content"] == "please look"
            json.dumps(pool.calls[0]["history"])
            events = await reply(server, correlation_id)
            assert [kind for kind, _ in events] == ["answer.delta", "agent.tool", "agent.tool", "task.event",
                                                    "answer.final"]
            assert events[0][1] == {"text": "hello"}
            assert events[1][1] == {"tool_use_id": "tool-1", "name": "read", "args": "{}", "result": "",
                                    "is_error": False}
            assert events[2][1]["result"] == "done" and events[2][1]["is_error"] is False
            assert events[3][1] == {"kind": "turn_summary", "files": []}
            assert events[4][1]["text"] == "the answer" and events[4][1]["outcome"] == "answered"
            status, _, messages = await post(server, "messages", {"session_id": task_thread(task_id),
                                                                  "manager": "other"})
            assert status == 200, messages
            rows = messages["result"]["messages"]
            assert [(row["role"], row["message_id"]) for row in rows] == [
                ("user", "m1"), ("tool", "m1:tool:tool-1"), ("event", "m1:event:1"), ("assistant", "m1:reply")]
            assert json.loads(rows[1]["content"])["result"] == "done"
            assert json.loads(rows[2]["content"]) == {"kind": "turn_summary", "files": []}
            assert rows[3]["content"] == "the answer"
            stored = await conn.fetchrow("SELECT channel FROM chat_messages WHERE message_id = 'm1'")
            assert stored["channel"] == "web"
            assert await conn.fetchval("SELECT count(*) FROM chat_messages WHERE message_id LIKE 'm1:tool:%'") == 1
            pool.drive = None
            pool.refuse = True
            status, answer = await send(server, task_id, "nope", "m-refused")
            assert status == 200 and answer["result"]["correlation_id"]
            assert pool.calls[-1]["text"] == "nope"
            refused = await reply(server, answer["result"]["correlation_id"])
            assert final_text(refused)["outcome"] == "refused"
    run_db(check, monkeypatch, timeout=60)


def test_send_task_refusals(monkeypatch, tmp_path):
    pool = Pool()
    use_pool(monkeypatch, pool)

    async def check(conn):
        task_id = await make_task(conn, tmp_path)
        async with opened(tmp_path) as server:
            async def refused(body, code, message=None):
                status, _, answer = await post(server, "send", body)
                assert status == (400 if code == "invalid" else 409), answer
                assert answer["error"]["code"] == code
                if message is not None:
                    assert answer["error"]["message"] == message
                assert await conn.fetchval("SELECT count(*) FROM chat_messages") == 0
                assert pool.calls == []

            await refused({"text": "hi", "message_id": "m", "target": target(task_id)}, "conflict",
                          "work mode is off (tasks.conversations.enabled)")
            settings.set_enabled(True)
            await refused({"text": "hi", "message_id": "m", "target": target("missing-task")}, "invalid",
                          "no task missing-task")
            await refused({"text": "hi", "message_id": "m", "target": target(task_id), "session_id": "other"},
                          "invalid", "a task's conversation is its own thread")
            await refused({"text": "hi", "message_id": "m", "target": target(task_id),
                           "attachments": [{"filename": "note.txt", "content_base64": "eA=="}]}, "invalid",
                          "attachments go only to a Manager")
            await refused({"text": "hi", "message_id": "m", "target": target(task_id),
                           "new_session_profile_id": "chosen"}, "invalid",
                          "new_session_profile_id goes only with /new to the Manager")
            await refused({"text": "hi", "target": target(task_id)}, "invalid")
            use_pool(monkeypatch, None)
            status, _, answer = await post(server, "send", {"text": "hi", "message_id": "m",
                                                           "target": target(task_id)})
            assert status == 503 and answer["error"]["code"] == "unavailable"
            assert await conn.fetchval("SELECT count(*) FROM chat_messages") == 0
    run_db(check, monkeypatch)


def test_commands_never_reach_pool(monkeypatch, tmp_path):
    pool = Pool()
    use_pool(monkeypatch, pool)

    async def check(conn):
        settings.set_enabled(True)
        task_id = await make_task(conn, tmp_path)
        orchestrated = await make_task(conn, tmp_path, status="executing")
        warned = await make_task(conn, tmp_path)
        async with opened(tmp_path) as server:
            status, answer = await send(server, task_id, "/done", "done-1")
            assert status == 200, answer
            events = await reply(server, answer["result"]["correlation_id"])
            assert final_text(events)["text"] == "任务已完成"
            assert final_text(events)["outcome"] == "command"
            command_event(events, "/done")
            assert pool.calls == []
            assert await conn.fetchval("SELECT status FROM tasks WHERE id = $1", task_id) == "completed"
            status, answer = await send(server, task_id, " /DONE ", "done-2")
            assert status == 200, answer
            again = await reply(server, answer["result"]["correlation_id"])
            assert final_text(again)["text"] == "任务已经是完成状态"
            assert pool.calls == []
            status, answer = await send(server, orchestrated, "/done", "done-live")
            assert status == 200, answer
            blocked = await reply(server, answer["result"]["correlation_id"])
            assert final_text(blocked)["text"] == (
                f"the background is working on task {orchestrated}: stop it or wait for it to end first")
            command_event(blocked, "/done")
            assert pool.calls == []
            assert await conn.fetchval("SELECT status FROM tasks WHERE id = $1", orchestrated) == "executing"

            async def finish(self, task_id, workspace_mode, *, pool=None):
                return _WorkspaceFinish(branch_line="branch line that stays out", warning="kept the worktree")

            monkeypatch.setattr(TaskRouter, "_finish_task_workspace", finish)
            status, answer = await send(server, warned, "/done", "done-warn")
            assert status == 200, answer
            warning = await reply(server, answer["result"]["correlation_id"])
            assert final_text(warning)["text"] == "任务已完成\nkept the worktree"
            assert "branch line" not in final_text(warning)["text"]
            assert pool.calls == []
            status, answer = await send(server, task_id, "/background", "later")
            assert status == 200, answer
            later = await reply(server, answer["result"]["correlation_id"])
            assert final_text(later)["text"] == LATER
            command_event(later, "/background")
            assert pool.calls == []
            for word in ("/pause", "/takeover", "/start"):
                status, answer = await send(server, task_id, word, "later-" + word[1:])
                assert status == 200, answer
                events = await reply(server, answer["result"]["correlation_id"])
                assert final_text(events)["text"] == LATER
            assert pool.calls == []
            status, answer = await send(server, task_id, "work", "running")
            assert status == 200, answer
            assert [call["text"] for call in pool.calls] == ["work"]
            assert task_id in pool.running
            status, answer = await send(server, task_id, "/STOP", "stop-1")
            assert status == 200, answer
            stopped = await reply(server, answer["result"]["correlation_id"])
            assert final_text(stopped)["text"] == "已停止这一轮"
            command_event(stopped, "/stop")
            assert pool.stopped == [task_id]
            assert [call["text"] for call in pool.calls] == ["work"]
            status, answer = await send(server, task_id, "/stop", "stop-2")
            assert status == 200, answer
            idle = await reply(server, answer["result"]["correlation_id"])
            assert final_text(idle)["text"] == "现在没有在进行的回合"
            assert pool.calls == [{"task_id": task_id, "text": "work", "message_id": "running",
                                   "history": pool.calls[0]["history"]}]
    run_db(check, monkeypatch, timeout=60)


def test_stop_entry(monkeypatch, tmp_path):
    pool = Pool()
    use_pool(monkeypatch, pool)
    manager = Manager()

    async def check(conn):
        settings.set_enabled(True)
        chat_settings.set_stop_enabled(False)
        task_id = await make_task(conn, tmp_path)
        async with opened(tmp_path) as server:
            server._manager = manager
            server._managers = None
            status, answer = await send(server, task_id, "work", "task-message")
            assert status == 200, answer
            assert pool.running[task_id] == "task-message"
            status, _, body = await post(server, "stop", {"message_id": "task-message"})
            assert status == 200, body
            assert body["result"] == {"state": "stopping", "phase": None, "method": None}
            assert pool.stopped_messages == ["task-message"]
            assert manager.asked == []
            chat_settings.set_stop_enabled(True)
            status, _, body = await post(server, "stop", {"message_id": "manager-message"})
            assert status == 200, body
            assert body["result"]["state"] == "stopping"
            assert manager.asked == [("manager-message", "web")]
            assert pool.stopped_messages == ["task-message"]
    run_db(check, monkeypatch)


def test_task_threads_hidden(monkeypatch, tmp_path):
    async def check(conn):
        await chat.save_message(session_id="task-chat:hidden", message_id="u", role="user", content="task",
                                channel="web")
        await chat.save_message(session_id="manager-thread", message_id="u", role="user", content="manager")
        await chat.save_message(session_id=f"{PLANNER_THREAD_PREFIX}hidden", message_id="u", role="user",
                                content="planner")
        sessions = {row["session_id"] for row in await chat.list_sessions()}
        recent = {row["session_id"] for row in await chat.recent_conversations(sessions=10, messages=5)}
        assert "manager-thread" in sessions and "manager-thread" in recent
        assert "task-chat:hidden" not in sessions and "task-chat:hidden" not in recent
        assert f"{PLANNER_THREAD_PREFIX}hidden" not in sessions
        assert f"{PLANNER_THREAD_PREFIX}hidden" not in recent
        assert conn is not None
    run_db(check, monkeypatch)


def test_interrupted_state(monkeypatch, tmp_path):
    pool = Pool()
    use_pool(monkeypatch, pool)

    async def check(conn):
        task_id = await make_task(conn, tmp_path)
        thread = task_thread(task_id)
        assert await task_chat.turn_state(task_id) == "idle"
        await chat.save_message(session_id=thread, message_id="u", role="user", content="hi", channel="web")
        assert await task_chat.turn_state(task_id) == "interrupted"
        pool.running[task_id] = "u"
        assert await task_chat.turn_state(task_id) == "running"
        pool.running.pop(task_id)
        await chat.save_message(session_id=thread, message_id="u:reply", role="assistant", content="ok")
        assert await task_chat.turn_state(task_id) == "idle"
        await chat.save_message(session_id=thread, message_id="u2", role="user", content="again")
        await chat.save_message(session_id=thread, message_id="u2:tool:1", role="tool", content="{}")
        assert await task_chat.turn_state(task_id) == "interrupted"
    run_db(check, monkeypatch)


def _stripped_document():
    document = copy.deepcopy(api_v1.document())
    send = document["paths"]["/operator_chat/send"]["post"]
    branches = send["requestBody"]["content"]["application/json"]["schema"]["properties"]["target"]["oneOf"]
    kept = [branch for branch in branches if branch["properties"]["kind"].get("const") != "task"]
    assert len(kept) == len(branches) - 1
    send["requestBody"]["content"]["application/json"]["schema"]["properties"]["target"]["oneOf"] = kept
    events = document["paths"]["/operator_chat/reply_events"]["post"]["responses"]["200"]["content"][
        "text/event-stream"]["schema"]["x-looper-events"]
    assert "agent.tool" in events and "task.event" in events
    events.pop("agent.tool")
    events.pop("task.event")
    schemas = document["components"]["schemas"]
    assert "OperatorChatAgentTool" in schemas and "OperatorChatTaskEvent" in schemas
    schemas.pop("OperatorChatAgentTool")
    schemas.pop("OperatorChatTaskEvent")
    thread = document["paths"]["/operator_chat/thread_events"]["post"]["responses"]["200"]["content"][
        "text/event-stream"]["schema"]["x-looper-events"]
    assert "agent.tool" not in thread and "task.event" not in thread
    return document


def test_manager_path_unchanged(monkeypatch, tmp_path):
    send = api_v1.find("POST", PREFIX + "send")
    assert send.description.startswith("Send a dashboard chat message.")
    stripped = json.dumps(_stripped_document(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(stripped.encode()).hexdigest() == PRE_CARD_DOCUMENT
    live = api_v1.document()
    kinds = live["paths"]["/operator_chat/send"]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "properties"]["target"]["oneOf"]
    assert any(branch["properties"]["kind"].get("const") == "task" for branch in kinds)
    assert live is api_v1.document()
    published = []

    async def publish(envelope):
        published.append(envelope)

    seen = []
    original = chat.save_message

    async def spy(**kwargs):
        seen.append(kwargs)
        await original(**kwargs)

    monkeypatch.setattr(chat, "save_message", spy)

    async def check(conn):
        assert not settings.enabled()
        async with _serving(tmp_path) as server:
            adapter = WebChannelAdapter()
            adapter._bus = SimpleNamespace(publish=publish, runtime_root=tmp_path)
            server._web_adapter = adapter
            server._task_router = TaskRouter(runtime_root=tmp_path)
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await post(server, "send", {"text": "hello from the manager",
                                                           "session_id": "thread-m", "message_id": "m-1"})
            assert status == 200, answer
            (envelope,) = published
            assert envelope.message_type is MessageType.USER_MESSAGE
            assert envelope.source == channel_address("web", "channel-web")
            assert envelope.target == MANAGER_ADDRESS
            assert envelope.payload == {
                "text": "hello from the manager", "language": "zh", "attachments": [],
                "channel_context": {"session_id": "thread-m", "goal_id": None, "client_message_id": "m-1"}}
            assert envelope.metadata == {"origin_channel_id": "channel-web"}
            assert seen == [{"session_id": "thread-m", "message_id": "m-1", "role": "user",
                             "content": "hello from the manager"}]
            stored = await conn.fetchrow("SELECT channel, content FROM chat_messages WHERE message_id = 'm-1'")
            assert stored["channel"] == "web" and stored["content"] == "hello from the manager"
    run_db(check, monkeypatch)
