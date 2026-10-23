"""The dashboard chat routed to the Manager the user picked, on a real PostgreSQL: targets, send (the thread's owner is
claimed and kept), stop, the running turn, a Manager's session and each Manager's conversation list; a deleted
Manager's threads stay out of every list.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.test_managers_schema_db import _manager
from tests.test_usage_api_db import _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


class _Stub:
    def __init__(self, agent_id: str, name: str, tokens: tuple[str, ...] = ()) -> None:
        self.agent_id = agent_id
        self.name = name
        self.tokens = tokens
        self.stopped: list[str] = []
        self.turn_asked: list[tuple[str, str]] = []

    async def stop_turn(self, message_id: str, *, via: str) -> Any:
        self.stopped.append(message_id)
        return SimpleNamespace(value="stopping" if message_id in self.tokens else "not_running")

    def turn_for(self, message_id: str) -> Any:
        if message_id not in self.tokens:
            return None
        return SimpleNamespace(method=None, stop=None, phase=SimpleNamespace(value="running"))

    def current_turn_for(self, channel: str, chat: str) -> Any:
        self.turn_asked.append((channel, chat))
        return None

    async def session_view(self) -> dict[str, Any]:
        return {"manager": self.agent_id}

    async def select_session_profile(self, profile_id: Any, *, via: str, origin: Any) -> Any:
        return SimpleNamespace(effect=SimpleNamespace(value="applied"), text="ok")


class _Fleet:
    def __init__(self, *agents: _Stub) -> None:
        self._agents = {a.agent_id: a for a in agents}

    def get(self, manager_id: str) -> _Stub | None:
        return self._agents.get(manager_id)

    @property
    def default(self) -> _Stub:
        return self._agents["manager"]

    def agents(self) -> list[_Stub]:
        return list(self._agents.values())

    def turn_for(self, message_id: str) -> Any:
        for agent in self._agents.values():
            token = agent.turn_for(message_id)
            if token is not None:
                return agent, token
        return None


class _Web:
    def __init__(self) -> None:
        self.targets: list[Any] = []

    async def handle_user_message(self, **kwargs: Any) -> dict[str, Any]:
        self.targets.append(kwargs["target"])
        return {"ok": True}


async def _call(fleet: _Fleet, web: _Web, method: str, path: str, body: Any = None,
                query: dict[str, str] | None = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=web, supervisor=SimpleNamespace(), managers=fleet,
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    writer = _Writer()
    raw = b"" if body is None else json.dumps(body).encode()
    await server._route_api(method, path, query or {}, raw, writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


async def _setup(pool: Any) -> tuple[_Fleet, _Web, _Stub, _Stub]:
    await _manager(pool, "review", name="Code review")
    default, review = _Stub("manager", "Manager"), _Stub("manager-review", "Code review", ("m-review",))
    return _Fleet(default, review), _Web(), default, review


async def _say(pool: Any, session_id: str, message_id: str = "m1") -> None:
    from looper.db import chat

    await chat.save_message(session_id=session_id, message_id=message_id, role="user", content="hi", pool=pool)


def _send(session_id: str, manager: str | None, text: str = "hello") -> dict[str, Any]:
    target = {"kind": "manager"} if manager is None else {"kind": "manager", "id": manager}
    return {"text": text, "session_id": session_id, "message_id": "m-" + session_id, "target": target}


def test_targets_list_the_running_managers_with_names(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        fleet, web, _, _ = await _setup(pool)
        status, answer = await _call(fleet, web, "GET", "/api/chat/targets")
        managers = [t for t in answer["targets"] if t["kind"] == "manager"]
        assert status == 200 and managers == [
            {"kind": "manager", "id": "manager", "name": "Manager", "can_send": True},
            {"kind": "manager", "id": "manager-review", "name": "Code review", "can_send": True}]

    run_db(test, monkeypatch)


def test_a_thread_belongs_to_the_manager_that_first_took_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        fleet, web, _, _ = await _setup(pool)
        status, _ = await _call(fleet, web, "POST", "/api/chat/send", _send("s-new", "manager-review"))
        assert status == 200 and web.targets[-1].agent_id == "manager-review"
        assert await chat.thread_owner("s-new") == "manager-review"

        status, answer = await _call(fleet, web, "POST", "/api/chat/send", _send("s-new", None))
        assert status == 409 and answer["manager_id"] == "manager-review"
        assert "belongs to Manager Code review" in answer["error"]
        assert len(web.targets) == 1
        assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id = 's-new'") == 1

        await _say(pool, "s-old")  # a thread from before: no row, the default Manager's
        status, answer = await _call(fleet, web, "POST", "/api/chat/send", _send("s-old", "manager-review"))
        assert status == 409 and answer["manager_id"] == "manager"
        status, _ = await _call(fleet, web, "POST", "/api/chat/send", _send("s-old", None))
        assert status == 200 and web.targets[-1].agent_id == "manager"

    run_db(test, monkeypatch)


def test_an_unknown_or_disabled_manager_takes_no_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        fleet, web, _, _ = await _setup(pool)
        await _manager(pool, "off", name="Off", enabled=False)
        status, answer = await _call(fleet, web, "POST", "/api/chat/send", _send("s1", "manager-nope"))
        assert status == 400 and answer["error"] == "no Manager 'manager-nope'"
        status, answer = await _call(fleet, web, "POST", "/api/chat/send", _send("s1", "manager-off"))
        assert status == 409 and answer["error"] == "Manager 'manager-off' is disabled"
        assert web.targets == []

    run_db(test, monkeypatch)


def test_stop_finds_the_manager_whose_turn_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents import chat_settings
        monkeypatch.setattr(chat_settings, "stop_enabled", lambda: True)
        fleet, web, default, review = await _setup(pool)
        status, answer = await _call(fleet, web, "POST", "/api/chat/stop", {"message_id": "m-review"})
        assert status == 200 and answer["state"] == "stopping"
        assert review.stopped == ["m-review"] and default.stopped == []
        status, answer = await _call(fleet, web, "POST", "/api/chat/stop", {"message_id": "m-none"})
        assert status == 409 and answer["state"] == "not_running" and default.stopped == ["m-none"]

    run_db(test, monkeypatch)


def test_the_running_turn_is_asked_of_the_threads_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        fleet, web, default, review = await _setup(pool)
        await chat.claim_thread("s-r", "manager-review")
        assert await _call(fleet, web, "GET", "/api/chat/turn", query={"session_id": "s-r"}) == (200, {"turn": None})
        assert review.turn_asked == [("web", "s-r")] and default.turn_asked == []
        await _call(fleet, web, "GET", "/api/chat/turn", query={"session_id": "s-other"})
        assert default.turn_asked == [("web", "s-other")]

    run_db(test, monkeypatch)


def test_a_managers_session_is_the_one_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        fleet, web, _, _ = await _setup(pool)
        query = {"manager": "manager-review"}
        assert await _call(fleet, web, "GET", "/api/manager/session", query=query) == (
            200, {"manager": "manager-review"})
        assert (await _call(fleet, web, "GET", "/api/manager/session"))[1] == {"manager": "manager"}
        status, answer = await _call(fleet, web, "GET", "/api/manager/session", query={"manager": "manager-nope"})
        assert status == 404 and answer == {"error": "no running Manager 'manager-nope'"}
        status, answer = await _call(fleet, web, "PUT", "/api/manager/session/profile",
                                     {"manager": "manager-nope", "profile_id": None})
        assert status == 404
        status, answer = await _call(fleet, web, "PUT", "/api/manager/session/profile",
                                     {"manager": "manager-review", "profile_id": None})
        assert status == 200 and answer["manager"] == "manager-review"

    run_db(test, monkeypatch)


def test_each_manager_lists_its_own_conversations(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        fleet, web, _, _ = await _setup(pool)
        await _say(pool, "s-old")
        await _say(pool, "s-def")
        await chat.claim_thread("s-def", "manager")
        await _say(pool, "s-rev")
        await chat.claim_thread("s-rev", "manager-review")

        async def ids(query: dict[str, str]) -> list[str]:
            status, answer = await _call(fleet, web, "GET", "/api/chat/sessions", query=query)
            assert status == 200, answer
            return sorted(s["session_id"] for s in answer["sessions"])

        assert await ids({}) == ["s-def", "s-old"]
        assert await ids({"manager": "manager-review"}) == ["s-rev"]
        assert await ids({"manager": "manager-review", "session_id": "s-rev"}) == ["s-rev"]
        status, answer = await _call(fleet, web, "GET", "/api/chat/sessions", query={"session_id": "s-rev"})
        assert status == 409 and answer["manager_id"] == "manager-review"
        status, answer = await _call(fleet, web, "GET", "/api/chat/sessions", query={"manager": "manager-nope"})
        assert status == 404 and answer == {"error": "no Manager 'manager-nope'"}

    run_db(test, monkeypatch)


def test_another_managers_thread_is_named_never_listed_or_read_as_a_new_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        fleet, web, _, _ = await _setup(pool)
        await _say(pool, "s-old")  # messages, no row: the default Manager's
        await _say(pool, "s-def")
        await chat.claim_thread("s-def", "manager")
        await chat.claim_thread("s-claimed", "manager")  # claimed by a send whose message is not saved yet

        async def get(path: str, query: dict[str, str]) -> tuple[int, Any]:
            return await _call(fleet, web, "GET", path, query=query)

        for session_id in ("s-old", "s-def", "s-claimed"):
            status, answer = await get("/api/chat/sessions", {"manager": "manager-review", "session_id": session_id})
            assert status == 409, (session_id, answer)
            assert answer == {"error": "this conversation belongs to Manager Manager: open it there or start a new "
                                       "one", "manager_id": "manager"}
            status, answer = await get("/api/chat/messages", {"manager": "manager-review", "session_id": session_id})
            assert status == 409 and answer["manager_id"] == "manager" and "messages" not in answer
        # A new id is a new thread: the empty list, no messages.
        assert await get("/api/chat/sessions", {"manager": "manager-review", "session_id": "s-new"}) == (
            200, {"sessions": []})
        assert await get("/api/chat/messages", {"manager": "manager-review", "session_id": "s-new"}) == (
            200, {"messages": []})
        # No `manager` is the default Manager: a scenario Manager's thread is not read through it, by messages or events.
        await _say(pool, "s-rev")
        await chat.claim_thread("s-rev", "manager-review")
        for path, key in (("/api/chat/messages", "session_id"), ("/api/chat/events", "session")):
            status, answer = await get(path, {key: "s-rev"})
            assert status == 409 and answer["manager_id"] == "manager-review" and "messages" not in answer
        status, answer = await get("/api/chat/events", {"session": "s-old", "manager": "manager-review"})
        assert status == 409 and answer["manager_id"] == "manager"
        # Its owner reads it.
        status, answer = await get("/api/chat/sessions", {"session_id": "s-def"})
        assert status == 200 and [s["session_id"] for s in answer["sessions"]] == ["s-def"]
        status, answer = await get("/api/chat/messages", {"manager": "manager", "session_id": "s-def"})
        assert status == 200 and len(answer["messages"]) == 1

    run_db(test, monkeypatch)


def test_a_deleted_managers_threads_are_in_no_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat, managers

    async def test(pool: Any) -> None:
        fleet, web, _, _ = await _setup(pool)
        await chat.claim_thread("s-rev", "manager-review")
        await _say(pool, "s-rev")
        assert await managers.delete_manager("manager-review") == "deleted"
        fleet._agents.pop("manager-review")

        status, answer = await _call(fleet, web, "GET", "/api/chat/sessions")
        assert status == 200 and answer["sessions"] == []
        status, _ = await _call(fleet, web, "GET", "/api/chat/sessions", query={"manager": "manager-review"})
        assert status == 404
        status, answer = await _call(fleet, web, "POST", "/api/chat/send", _send("s-rev", None))
        assert status == 409 and "deleted Manager" in answer["error"]
        assert web.targets == []

    run_db(test, monkeypatch)
