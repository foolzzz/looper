"""Channel history stays with its pinned Manager on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from looper.agents import api_v1
from looper.agents.bus import MessageBus
from looper.agents.channels.base import DeliveryReceipt
from looper.agents.channels.chat_history import CHAT_RECORD, record_reply
from looper.agents.channels.web import WebChannelAdapter
from looper.agents.http_server import AgentHTTPServer
from looper.agents.managers_api import ManagerDefinition
from looper.agents.message import AgentAddress, MessageEnvelope, MessageType, channel_address, manager_address
from looper.agents.ops.clients import ClientOps
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig, _session_key
from looper.db import api_clients, chat, managers
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
REVIEW = "manager-review"


@asynccontextmanager
async def _agents(pool: Any, root: Path):
    async with pool.acquire() as conn:
        row = await managers.insert_manager({"id": REVIEW, "name": "Review", "system_prompt": "Review tasks."},
                                            conn=conn)
    default = ManagerAgent(config=ManagerConfig(work_dir=root), runtime_root=root)
    review = ManagerAgent(config=ManagerConfig(work_dir=root), runtime_root=root,
                          definition=ManagerDefinition.from_row(row))
    bus = MessageBus(runtime_root=root)
    default._bus = review._bus = bus
    try:
        yield {"manager": default, REVIEW: review}, bus
    finally:
        await default.stop()
        await review.stop()
        await bus.shutdown()


def _user(kind: str, chat_id: str, text: str, *, manager: str = "manager", pinned: str | None = None,
          chat_type: str = "p2p", channel_id: str | None = None) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=channel_address(kind, channel_id), target=manager_address(manager),
        message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"chat_id": chat_id, "chat_type": chat_type}},
        metadata={"channel_thread_id": pinned} if pinned else {})


def _server(agents: dict[str, ManagerAgent], web: Any = None) -> AgentHTTPServer:
    fleet = SimpleNamespace(get=agents.get, default=agents["manager"], agents=lambda: list(agents.values()))
    return AgentHTTPServer(web_adapter=web or SimpleNamespace(), supervisor=SimpleNamespace(), managers=fleet,
                           task_router=SimpleNamespace(), client_ops=ClientOps(SimpleNamespace()))


async def _call(server: AgentHTTPServer, domain: str, action: str, body: dict[str, Any],
                principal: api_v1.Principal = api_v1.OPERATOR) -> tuple[int, dict[str, Any]]:
    path = f"/api/v1/{domain}/{action}"
    operation = api_v1.find("POST", path)
    status, answer = await server._answer_v1("POST", path, operation, {}, json.dumps(body).encode(), principal)
    if answer["status"] == "ok":
        api_v1.validate_arguments(operation.result, answer["result"])
    return int(status.split()[0]), answer


async def _replies(bus: MessageBus, env: MessageEnvelope) -> list[MessageEnvelope]:
    return [reply for reply in await bus.drain_queue(env.source.agent_id)
            if reply.message_type is MessageType.AGENT_REPLY]


async def _answer(_self: ManagerAgent, _token: Any, prepared: Any) -> str:
    return f"answer {prepared.message}"


@pytest.mark.parametrize(("kind", "chat_id", "chat_type", "stored"), [
    ("telegram", "42", "p2p", True),
    ("telegram", "-10042", "group", False),
    ("lark", "oc_private", "p2p", True),
    ("lark", "oc_team", "group", True),
    ("lark_bot", "oc_bot_team", "group", True),
])
def test_rebinding_and_late_answers_keep_two_stable_owned_histories(monkeypatch, tmp_path, kind, chat_id,
                                                                    chat_type, stored):
    monkeypatch.setattr(ManagerAgent, "_run_prepared", _answer)

    async def run(pool):
        async with _agents(pool, tmp_path) as (agents, bus):
            default, review = agents["manager"], agents[REVIEW]
            base, bound = f"{kind}:{chat_id}", f"{kind}:{chat_id}#{REVIEW}"
            first = _user(kind, chat_id, "first owner", chat_type=chat_type)
            other = _user(kind, chat_id, "second owner", manager=REVIEW, pinned=bound, chat_type=chat_type)
            back = _user(kind, chat_id, "first owner again", chat_type=chat_type)
            await default._handle(first)
            late = (await _replies(bus, first))[0]
            await review._handle(other)
            second = (await _replies(bus, other))[0]
            # The old answer arrives only after the other Manager has started answering this same platform chat.
            for reply in (second, late):
                await record_reply(kind, reply, DeliveryReceipt(channel=kind, success=True))
            await default._handle(back)
            for reply in await _replies(bus, back):
                await record_reply(kind, reply, DeliveryReceipt(channel=kind, success=True))
            server = _server(agents)
            for owner, sid, expected in (("manager", base, ["first owner", "answer first owner",
                                                          "first owner again", "answer first owner again"]),
                                         (REVIEW, bound, ["second owner", "answer second owner"])):
                status, listing = await _call(server, "operator_chat", "sessions", {"manager": owner})
                assert status == 200
                assert [(s["session_id"], s["channel"]) for s in listing["result"]["sessions"]] == (
                    [(sid, kind)] if stored else [])
                status, history = await _call(server, "operator_chat", "messages",
                                               {"manager": owner, "session_id": sid})
                assert status == 200
                assert [m["content"] for m in history["result"]["messages"]] == (expected if stored else [])
                recent = await chat.recent_conversations(sessions=10, messages=10, manager_id=owner)
                assert [c["session_id"] for c in recent] == ([sid] if stored else [])
                if stored:
                    status, foreign = await _call(server, "operator_chat", "messages", {
                        "manager": REVIEW if owner == "manager" else "manager", "session_id": sid})
                    assert status == 409 and foreign["error"]["details"]["manager_id"] == owner
            assert (CHAT_RECORD in late.metadata) is stored
            assert (CHAT_RECORD in second.metadata) is stored
            brief = await review._resume_brief(other, "second owner")
            assert ("answer second owner" in brief) is stored
            assert "first owner" not in brief

    run_db(run, monkeypatch, timeout=30)


def test_failed_and_autonomous_replies_keep_the_original_suffix_and_resume_exclusion(monkeypatch, tmp_path):
    monkeypatch.setattr(ManagerAgent, "_run_prepared", _answer)

    async def run(pool):
        async with _agents(pool, tmp_path) as (agents, bus):
            agent = agents[REVIEW]
            sid = f"telegram:42#{REVIEW}"
            original = _user("telegram", "42", "bound question", manager=REVIEW, pinned=sid)
            await agent._handle(original)
            reply = (await _replies(bus, original))[0]
            await record_reply("telegram", reply, DeliveryReceipt(channel="telegram", success=False,
                                                                  error="synthetic delivery failure"))
            await agent._send_own_reply(original, "autonomous answer")
            own = (await _replies(bus, original))[0]
            await record_reply("telegram", own, DeliveryReceipt(channel="telegram", success=True))
            current = _user("telegram", "42", "current question", manager=REVIEW, pinned=sid)
            await agent._persist_chat(current, "user", "current question", current.message_id)
            brief = await agent._resume_brief(current, "current question")
            assert "assistant (not delivered): answer bound question" in brief
            assert "autonomous answer" in brief and "current question" not in brief
            assert [reply.metadata[CHAT_RECORD]["session_id"], own.metadata[CHAT_RECORD]["session_id"]] == [sid, sid]
            messages = await chat.list_messages(sid)
            assert [m["content"] for m in messages] == ["bound question", "answer bound question",
                                                         "autonomous answer", "current question"]
            assert messages[1]["delivery_failure"] == "send_failed"
            assert await chat.list_messages("telegram:42") == []

    run_db(run, monkeypatch, timeout=30)


def test_explicit_binding_to_the_bot_owner_claims_its_unsuffixed_thread(monkeypatch, tmp_path):
    async def run(pool):
        async with _agents(pool, tmp_path) as (agents, bus):
            env = _user("lark", "oc_own", "own binding", manager=REVIEW, channel_id="channel-lark-7")
            assert _session_key(env) == "channel-lark-7:oc_own"
            await agents[REVIEW]._persist_chat(env, "user", "own binding", env.message_id)
            await agents[REVIEW]._send_reply(env, "own answer")
            await record_reply("lark", (await _replies(bus, env))[0], DeliveryReceipt(channel="lark", success=True))
            assert await chat.thread_owner("channel-lark-7:oc_own") == REVIEW
            assert [m["content"] for m in await chat.list_messages("channel-lark-7:oc_own")] == [
                "own binding", "own answer"]

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("failed", [False, True], ids=["delivered", "failed-delivery"])
def test_conflicting_owner_rejects_user_normal_and_autonomous_answers(monkeypatch, tmp_path, failed):
    async def run(pool):
        async with _agents(pool, tmp_path) as (agents, bus):
            # The suffix names the destination; the existing database owner still wins every write check.
            sid = "telegram:42#manager"
            await chat.claim_thread(sid, REVIEW)
            await chat.save_message(session_id=sid, message_id="existing", role="user", content="foreign history",
                                    channel="telegram")
            env = _user("telegram", "42", "rejected user", pinned=sid)
            await agents["manager"]._persist_chat(env, "user", "rejected user", env.message_id)
            await agents["manager"]._send_reply(env, "rejected answer")
            await agents["manager"]._send_own_reply(env, "rejected autonomous answer")
            for reply in await _replies(bus, env):
                await record_reply("telegram", reply, DeliveryReceipt(channel="telegram", success=not failed,
                                                                       error="synthetic failure" if failed else None))
            assert await chat.thread_owner(sid) == REVIEW
            assert [m["content"] for m in await chat.list_messages(sid)] == ["foreign history"]

    run_db(run, monkeypatch, timeout=30)


def test_legacy_unclaimed_content_cannot_be_claimed_by_another_manager(monkeypatch, tmp_path):
    async def run(pool):
        async with _agents(pool, tmp_path):
            await chat.save_message(session_id="lark:oc_old", message_id="old", role="user", content="legacy",
                                    channel="lark")
            with pytest.raises(ValueError):
                await chat.save_channel_message(session_id="lark:oc_old", message_id="foreign", role="user",
                                                content="must not appear", channel="lark", manager_id=REVIEW)
            await chat.save_channel_message(session_id="lark:oc_old", message_id="default", role="assistant",
                                            content="default answer", channel="lark", manager_id="manager")
            assert await chat.thread_owner("lark:oc_old") in (None, "manager")
            assert [m["content"] for m in await chat.list_messages("lark:oc_old")] == ["legacy", "default answer"]
            assert await chat.recent_conversations(sessions=10, messages=10, manager_id=REVIEW) == []

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("source", [channel_address("telegram"), AgentAddress("planner", REVIEW)])
def test_reply_records_require_a_manager_source(monkeypatch, tmp_path, source):
    async def run(pool):
        async with _agents(pool, tmp_path):
            sid = f"telegram:42#{REVIEW}"
            metadata = {CHAT_RECORD: {"session_id": sid, "message_id": "answer"}}
            for sender, text in ((source, "untrusted answer"), (manager_address(REVIEW), "trusted answer")):
                reply = MessageEnvelope.create(source=sender, target=channel_address("telegram"),
                                               message_type=MessageType.AGENT_REPLY, payload={"text": text},
                                               metadata=metadata)
                await record_reply("telegram", reply, DeliveryReceipt(channel="telegram", success=True))
                if sender == source:
                    assert await chat.thread_owner(sid) is None
                    assert await chat.list_messages(sid) == []
            assert await chat.thread_owner(sid) == REVIEW
            assert [m["content"] for m in await chat.list_messages(sid)] == ["trusted answer"]

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("table", ["chat_threads", "chat_messages"], ids=["claim-failure", "write-failure"])
def test_claim_and_message_write_roll_back_together(monkeypatch, table):
    async def run(pool):
        await pool.execute("CREATE FUNCTION reject_history() RETURNS trigger LANGUAGE plpgsql AS $$ "
                           "BEGIN RAISE EXCEPTION 'synthetic write failure'; END $$")
        await pool.execute(f"CREATE TRIGGER reject_history BEFORE INSERT ON {table} "
                           "FOR EACH ROW EXECUTE FUNCTION reject_history()")
        with pytest.raises(asyncpg.RaiseError, match="synthetic write failure"):
            await chat.save_channel_message(session_id="telegram:42#manager", message_id="failed", role="user",
                                            content="must roll back", channel="telegram", manager_id="manager")
        assert await chat.thread_owner("telegram:42#manager") is None
        assert await chat.list_messages("telegram:42#manager") == []
        await pool.execute(f"DROP TRIGGER reject_history ON {table}")
        await chat.save_channel_message(session_id="telegram:42#manager", message_id="positive", role="user",
                                        content="saved", channel="telegram", manager_id="manager")
        assert [m["content"] for m in await chat.list_messages("telegram:42#manager")] == ["saved"]

    run_db(run, monkeypatch, timeout=30)


def test_concurrent_conflicting_writers_cannot_change_the_first_owner(monkeypatch, tmp_path):
    async def run(pool):
        async with _agents(pool, tmp_path):
            async def write(owner):
                try:
                    await chat.save_channel_message(session_id="telegram:42", message_id=owner, role="user",
                                                    content=owner, channel="telegram", manager_id=owner)
                except ValueError:
                    return False
                return True
            results = await asyncio.gather(write("manager"), write(REVIEW))
            assert sorted(results) == [False, True]
            owner = await chat.thread_owner("telegram:42")
            assert [m["content"] for m in await chat.list_messages("telegram:42")] == [owner]
            assert await chat.claim_thread("telegram:42", REVIEW if owner == "manager" else "manager") == owner

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("client_owned", [False, True], ids=["dashboard", "client"])
def test_web_and_client_messages_keep_the_supplied_session(monkeypatch, tmp_path, client_owned):
    async def run(pool):
        async with _agents(pool, tmp_path) as (agents, bus):
            web = WebChannelAdapter()
            web._bus = bus
            client = None
            principal = api_v1.OPERATOR
            if client_owned:
                view, _ = await api_clients.create("Synthetic client")
                client = {"id": view["id"], "name": view["name"]}
                principal = api_v1.Principal("client", view["id"], view["name"])
            sid = "client-supplied" if client_owned else "dashboard-supplied"
            await chat.claim_thread(sid, "manager", client_id=principal.client_id)
            await chat.save_message(session_id=sid, message_id="question", role="user", content="web question")
            await web.handle_user_message(text="web question", session_id=sid, message_id="question", client=client)
            env = (await bus.drain_queue("manager"))[0]
            assert _session_key(env) == sid
            await agents["manager"]._persist_chat(env, "user", "must not double write", env.message_id)
            await agents["manager"]._send_reply(env, "web answer")
            reply = (await _replies(bus, env))[0]
            assert CHAT_RECORD not in reply.metadata
            assert (await web.deliver(reply)).success
            body = {"conversation_id": sid} if client_owned else {"session_id": sid}
            status, answer = await _call(_server(agents, web), "chat" if client_owned else "operator_chat",
                                         "messages", body, principal)
            assert status == 200
            assert [m["content"] for m in answer["result"]["messages"]] == ["web question", "web answer"]

    run_db(run, monkeypatch, timeout=30)
