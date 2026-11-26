"""A task conversation: its thread, streamed answer, stop, fixed commands, and the unchanged Manager path."""

import asyncio
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
from tests.test_operator_auth import TOKEN, _request

PREFIX = "/api/v1/operator_chat/"
# Hash of /operator_chat/* paths plus the component schemas those paths $ref, excluding ErrorCode, after stripping.
# Recompute by hashing that same selection from main's document with nothing stripped; the two hashes must match.
PRE_CARD_DOCUMENT = "e16f2f654a840fefeb74946ab3c816dfed7070f7fdff84431df1bbebdb1339fa"
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
        self.entered = None
        self.release = None

    async def submit(self, task_id, text, *, message_id, sink, history=()):
        self.calls.append({"task_id": task_id, "text": text, "message_id": message_id, "history": list(history)})
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
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


async def make_task(pool, tmp_path, *, status="draft", manager_id="manager"):
    project = await projects.create_project(name=f"conversation-{uuid.uuid4().hex[:8]}", work_dir=str(tmp_path),
                                            pool=pool)
    task_id = f"task-{project['id'].removeprefix('proj-')}"
    await tasks.create_task(task_id=task_id, requirement="draft work", project_id=project["id"],
                            status=status, work_dir=str(tmp_path), manager_id=manager_id, pool=pool)
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
    assert "agent.tool" not in thread and "task.event" in thread
    thread.pop("task.event")
    history = document["paths"]["/operator_chat/messages"]["post"]
    request = history["requestBody"]["content"]["application/json"]["schema"]["properties"]
    for key in ("before", "limit"):
        request.pop(key)
    result = history["responses"]["200"]["content"]["application/json"]["schema"]["allOf"][1]["properties"]["result"]
    for key in ("has_more", "next_before", "turn_state"):
        result["properties"].pop(key)
    result["required"] = ["messages"]
    result["additionalProperties"] = True
    codes = document["components"]["schemas"]["ErrorCode"]["enum"]
    for code in ("invalid_cursor", "cursor_session_mismatch"):
        assert code in codes
        codes.remove(code)
    return document


def _manager_surface(document):
    """Operator-chat paths and the component schemas they reference. ErrorCode stays out of the hash."""
    paths = {path: value for path, value in document["paths"].items() if path.startswith("/operator_chat/")}
    assert paths
    schemas = {}

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.removeprefix("#/components/schemas/")
                if "/" not in name and name != "ErrorCode" and name not in schemas:
                    schemas[name] = document["components"]["schemas"][name]
                    walk(schemas[name])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(paths)
    return {"paths": paths, "components": {"schemas": schemas}}


def test_manager_path_unchanged(monkeypatch, tmp_path):
    send = api_v1.find("POST", PREFIX + "send")
    assert send.description.startswith("Send a dashboard chat message.")
    surface = json.dumps(_manager_surface(_stripped_document()), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    assert hashlib.sha256(surface.encode()).hexdigest() == PRE_CARD_DOCUMENT
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


INDENTED = "  keep indentation\n    nested\n"
STARTING_REPLY = "已存储，启动中"
ORCHESTRATED_REPLY = "这句话已留下，没有交给任务管家。编排中的纠正还不能从这里送出。"
MESSAGE_CONFLICT = "this message_id was already sent with different text"


def test_task_text_kept_verbatim(monkeypatch, tmp_path):
    """A task target stores and delivers the original text; Manager and Planner targets stay trimmed."""
    pool = Pool()
    use_pool(monkeypatch, pool)
    recorded = []

    async def supplement(task_id, text, **kwargs):
        recorded.append(text)
        return await kwargs["wake"](1)

    monkeypatch.setattr("looper.agents.http_server.projects.add_task_supplement", supplement)

    async def check(conn):
        settings.set_enabled(True)
        task_id = await make_task(conn, tmp_path)
        async with opened(tmp_path) as server:
            server._task_router.has_planner = lambda wanted: wanted == task_id
            status, answer = await send(server, task_id, INDENTED, "raw")
            assert status == 200, answer
            assert pool.calls[0]["text"] == INDENTED
            stored = await conn.fetchval("SELECT content FROM chat_messages WHERE message_id = 'raw'")
            assert stored == INDENTED
            status, answer = await send(server, task_id, "  /done  \n", "done-raw")
            assert status == 200, answer
            done = await reply(server, answer["result"]["correlation_id"])
            assert final_text(done)["text"] == "任务已完成"
            assert [call["text"] for call in pool.calls] == [INDENTED]
            assert await conn.fetchval("SELECT content FROM chat_messages WHERE message_id = 'done-raw'") == "  /done  \n"
            status, answer = await send(server, task_id, "  \n\t  ", "blank")
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert await conn.fetchval("SELECT count(*) FROM chat_messages WHERE message_id = 'blank'") == 0
            status, _, answer = await post(server, "send", {"text": "  hello manager \n", "message_id": "mgr",
                                                           "session_id": "manager-thread"})
            assert status == 200, answer
            assert await conn.fetchval("SELECT content FROM chat_messages WHERE message_id = 'mgr'") == "hello manager"
            status, _, answer = await post(server, "send", {
                "text": "  hello planner \n", "message_id": "pln", "session_id": "planner-thread",
                "target": {"kind": "planner", "task_id": task_id}})
            assert status == 200, answer
            assert recorded == ["hello planner"]
            assert await conn.fetchval("SELECT content FROM chat_messages WHERE message_id = 'pln'") == "hello planner"
    run_db(check, monkeypatch, timeout=60)


def test_task_message_retry_claims_once(monkeypatch, tmp_path):
    """One message id runs one turn. The same retry reuses it; a different body conflicts."""
    import asyncio

    pool = Pool()
    pool.entered = asyncio.Event()
    pool.release = asyncio.Event()
    use_pool(monkeypatch, pool)

    async def check(conn):
        settings.set_enabled(True)
        task_id = await make_task(conn, tmp_path)
        async with opened(tmp_path) as server:
            text = "  retry me\n    again\n"

            async def both():
                return await asyncio.gather(send(server, task_id, text, "same"),
                                            send(server, task_id, text, "same"))

            running = asyncio.create_task(both())
            await pool.entered.wait()
            for _ in range(50):
                if len(pool.calls) > 1 or running.done():
                    break
                await asyncio.sleep(0.01)
            observed = len(pool.calls)
            pool.release.set()
            first, second = await running
            assert observed == 1
            assert pool.calls[0]["text"] == text
            assert first[0] == 200 and second[0] == 200, (first, second)
            assert first[1]["result"]["correlation_id"] == second[1]["result"]["correlation_id"]
            assert len(pool.calls) == 1
            assert await conn.fetchval("SELECT content FROM chat_messages WHERE message_id = 'same'") == text
            status, answer = await send(server, task_id, "different body", "same")
            assert status == 409, answer
            assert answer["error"]["code"] == "conflict"
            assert answer["error"]["message"] == MESSAGE_CONFLICT
            assert await conn.fetchval("SELECT content FROM chat_messages WHERE message_id = 'same'") == text
            assert len(pool.calls) == 1
            left, right = await asyncio.gather(send(server, task_id, "alpha-body", "race"),
                                               send(server, task_id, "beta-body", "race"))
            statuses = sorted((left[0], right[0]))
            assert statuses == [200, 409], (left, right)
            conflict = left if left[0] == 409 else right
            assert conflict[1]["error"]["code"] == "conflict"
            assert conflict[1]["error"]["message"] == MESSAGE_CONFLICT
            assert len(pool.calls) == 2
            stored = await conn.fetchval("SELECT content FROM chat_messages WHERE message_id = 'race'")
            assert stored in ("alpha-body", "beta-body")
            assert pool.calls[1]["text"] == stored
    run_db(check, monkeypatch, timeout=60)


def test_task_message_routes_by_status(monkeypatch, tmp_path):
    """pending and assigned are stored with a starting reply. Other orchestrated statuses are not sent to the task Manager."""
    pool = Pool()
    use_pool(monkeypatch, pool)

    async def check(conn):
        settings.set_enabled(True)
        async with opened(tmp_path) as server:
            for status_name in ("pending", "assigned"):
                task_id = await make_task(conn, tmp_path, status=status_name)
                before = len(pool.calls)
                status, answer = await send(server, task_id, INDENTED, "s-" + status_name)
                assert status == 200, answer
                events = await reply(server, answer["result"]["correlation_id"])
                assert final_text(events)["text"] == STARTING_REPLY
                assert final_text(events)["outcome"] == "starting"
                assert {"kind": "starting", "status": status_name} in [payload for kind, payload in events
                                                                       if kind == "task.event"]
                assert len(pool.calls) == before
                assert await conn.fetchval(
                    "SELECT content FROM chat_messages WHERE message_id = $1", "s-" + status_name) == INDENTED
            task_id = await make_task(conn, tmp_path, status="executing")
            before = len(pool.calls)
            status, answer = await send(server, task_id, INDENTED, "s-executing")
            assert status == 200, answer
            events = await reply(server, answer["result"]["correlation_id"])
            assert final_text(events)["text"] == ORCHESTRATED_REPLY
            assert final_text(events)["outcome"] == "refused"
            assert {"kind": "refused", "reason": "orchestrated", "status": "executing"} in [
                payload for kind, payload in events if kind == "task.event"]
            assert len(pool.calls) == before
            assert await conn.fetchval(
                "SELECT content FROM chat_messages WHERE message_id = 's-executing'") == INDENTED
            for status_name in ("running", "waiting_user"):
                task_id = await make_task(conn, tmp_path, status=status_name)
                before = len(pool.calls)
                status, answer = await send(server, task_id, "hello " + status_name, "s-" + status_name)
                assert status == 200, answer
                events = await reply(server, answer["result"]["correlation_id"])
                assert final_text(events)["outcome"] == "refused"
                assert {"kind": "refused", "reason": "orchestrated", "status": status_name} in [
                    payload for kind, payload in events if kind == "task.event"]
                assert len(pool.calls) == before
            draft_id = await make_task(conn, tmp_path, status="draft")
            status, answer = await send(server, draft_id, INDENTED, "s-draft")
            assert status == 200, answer
            assert pool.calls[-1]["task_id"] == draft_id
            assert pool.calls[-1]["text"] == INDENTED
    run_db(check, monkeypatch, timeout=60)


def test_fixed_commands_never_submit_or_enter_history(monkeypatch, tmp_path):
    """Each fixed command is matched on the trimmed message and never becomes a turn or model history."""
    from looper.agents.task_commands import TaskCommand

    pool = Pool()
    use_pool(monkeypatch, pool)

    async def check(conn):
        settings.set_enabled(True)
        task_id = await make_task(conn, tmp_path)
        async with opened(tmp_path) as server:
            for command in TaskCommand:
                status, answer = await send(server, task_id, f"\n  {command.value.upper()}  ",
                                            "cmd-" + command.value[1:])
                assert status == 200, answer
                events = await reply(server, answer["result"]["correlation_id"])
                assert final_text(events)["outcome"] == "command"
                assert pool.calls == []
            status, answer = await send(server, task_id, "please continue", "after-commands")
            assert status == 200, answer
            assert [call["text"] for call in pool.calls] == ["please continue"]
            blob = json.dumps(pool.calls[0]["history"], ensure_ascii=False)
            for command in TaskCommand:
                assert command.value not in blob
                assert f"\n  {command.value.upper()}  " not in blob
            for phrase in ("现在没有在进行的回合", "任务已完成", "这个命令要等"):
                assert phrase not in blob
            assert conn is not None
    run_db(check, monkeypatch, timeout=60)


def test_command_rows_keep_a_typed_kind_and_leave_model_history(monkeypatch, tmp_path):
    """Model history follows the stored kind. A row whose text is /done stays when its kind is message."""
    from looper.agents.task_commands import TaskCommand
    from looper.agents.tiers.task_manager import resume_message

    pool = Pool()
    use_pool(monkeypatch, pool)

    async def check(conn):
        settings.set_enabled(True)
        task_id = await make_task(conn, tmp_path)
        thread = task_thread(task_id)
        async with opened(tmp_path) as server:
            status, answer = await send(server, task_id, "\n  /DONE  ", "cmd-done")
            assert status == 200, answer
            events = await reply(server, answer["result"]["correlation_id"])
            assert final_text(events)["outcome"] == "command"
            assert await conn.fetchval(
                "SELECT entry_kind FROM chat_messages WHERE session_id = $1 AND message_id = 'cmd-done'",
                thread) == "command"
            assert await conn.fetchval(
                "SELECT entry_kind FROM chat_messages WHERE session_id = $1 AND message_id = 'cmd-done:reply'",
                thread) == "command_reply"
            await conn.execute(
                """
                INSERT INTO chat_messages (session_id, message_id, role, content, channel, created_at, entry_kind)
                VALUES ($1, 'typed-message', 'user', '/done', 'web', now(), 'message'),
                       ($1, 'typed-command', 'user', 'not a command word', 'web', now(), 'command'),
                       ($1, 'typed-between', 'user', 'between', 'web', now(), 'message'),
                       ($1, 'typed-reply', 'assistant', 'stored as the code reply', 'web', now(), 'command_reply')
                """,
                thread)
            status, answer = await send(server, task_id, "please continue", "after-typed")
            assert status == 200, answer
            history = pool.calls[0]["history"]
            contents = [item["content"] for item in history]
            assert "/done" in contents
            assert "between" in contents
            assert "please continue" in contents
            assert "not a command word" not in contents
            assert "stored as the code reply" not in contents
            assert all(item.get("entry_kind") != "command" for item in history)
            assert all(item.get("entry_kind") != "command_reply" for item in history)
            for command in TaskCommand:
                assert command.value.upper() not in json.dumps(history, ensure_ascii=False)
            resumed = resume_message("next", history)
            assert "/done" in resumed and "entry_kind" not in resumed
            assert "not a command word" not in resumed
            assert "stored as the code reply" not in resumed
    run_db(check, monkeypatch, timeout=60)


def test_task_thread_is_claimed_by_the_tasks_manager(monkeypatch, tmp_path):
    """The task thread is owned by that task's Manager on first use, and a second send keeps the owner."""
    from tests.test_managers_schema_db import _manager

    pool = Pool()
    use_pool(monkeypatch, pool)

    async def headers_of(server, body):
        reader, writer = await asyncio.open_connection("127.0.0.1", server._port)
        try:
            writer.write(_request(server._port, "POST " + PREFIX + "thread_events", _bearer(TOKEN),
                                  json.dumps(body)).encode())
            await writer.drain()
            return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        finally:
            writer.close()
            await writer.wait_closed()

    async def check(conn):
        settings.set_enabled(True)
        manager = await _manager(conn, "review")
        task_id = await make_task(conn, tmp_path, manager_id=manager["id"])
        thread = task_thread(task_id)
        async with opened(tmp_path) as server:
            status, answer = await send(server, task_id, "hello manager", "owned")
            assert status == 200, answer
            assert await chat.thread_owner(thread) == manager["id"]
            status, answer = await send(server, task_id, "again", "owned-2")
            assert status == 200, answer
            assert await chat.thread_owner(thread) == manager["id"]
            assert await conn.fetchval("SELECT count(*) FROM chat_threads WHERE session_id = $1", thread) == 1
            opened_headers = await headers_of(server, {"session_id": thread, "manager": manager["id"]})
            assert b"200 OK" in opened_headers
            status, _, body = await post(server, "thread_events", {"session_id": thread, "manager": "manager"})
            assert status == 409, body
            assert body["error"]["details"]["manager_id"] == manager["id"]
            status, _, messages = await post(server, "messages", {"session_id": thread, "manager": manager["id"]})
            assert status == 200, messages
            assert any(row["message_id"] == "owned" for row in messages["result"]["messages"])
    run_db(check, monkeypatch, timeout=60)
