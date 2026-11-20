"""The default Manager's ``looper_leads_search`` (message-leads-intents RFC, section 5): only the default Manager has
it, only while leads are on, only in the user's own turns; its answer, its line after the reply, its audit trail, and
the group backstop.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents.managers_api import ManagerDefinition, ManagerTools, TaskPolicy, TaskScope
from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from looper.agents.ops import Call, ErrorCode, OpError, Turn
from looper.agents.ops import leads as ops_leads
from looper.agents.ops.service import ManagerOps, Platform, scope_for
from tests import leads_support as sup
from tests import subscription_support as stream
from tests.db_support import DSN, run_with_channel_configs
from tests.test_manager_tools_db import _converse

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

UTC = timezone.utc
OP = "ou_operatorsynthetic"
TOOL = ops_leads.LEADS_TOOL
SUMMARY = "Project Aurora launch moves to December {day} after the security review"


@pytest.fixture(autouse=True)
def _nothing_seen():
    ops_leads._SEEN.clear()
    yield
    ops_leads._SEEN.clear()


async def _leads(pool: Any, on: bool = True) -> None:
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', $1) "
                       "ON CONFLICT (key) DO UPDATE SET value = $1", "true" if on else "false")


async def _seed(pool: Any, count: int = 7) -> list[int]:
    """``count`` leads about Project Aurora, newest first, and the operator as the Lark bot's operator."""
    await stream.source(pool)
    await stream.person(pool, "ou_alice", "Alice Example")
    await pool.execute("UPDATE channel_configs SET config = $1 WHERE channel_id = 'channel-lark'",
                       json.dumps({"allowed_open_ids": [OP, "ou_someoneelse"], "operator_sender_id": OP}))
    now = datetime.now(UTC)
    ids = []
    for day in range(count):
        seq = await stream.add(pool, f"aurora {day}", chat_id="oc_release", chat_name="Release room",
                               sender="ou_alice", sent_at=now - timedelta(hours=day + 1))
        ids.append(await sup.lead(pool, [seq], summary=SUMMARY.format(day=day + 3), detail="a detail kept back"))
    await _leads(pool)
    return ids


def _ops() -> ManagerOps:
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok")
    ops._working_at = ops._clock()
    return ops


def _web(message_id: str = "m-1", **fields: Any) -> Turn:
    return Turn(channel="web", chat="chat-1", message_id=message_id, **fields)


async def _search(ops: ManagerOps, turn: Turn, **arguments: Any) -> dict[str, Any]:
    ops.begin_turn(turn)
    try:
        return await ops.call(TOOL, {"query": "aurora", **arguments})
    finally:
        ops.end_turn(turn)


async def _trail(pool: Any) -> list[Any]:
    return await pool.fetch("SELECT tool, status, error_code, result FROM manager_actions ORDER BY id")


def test_only_the_default_manager_lists_it_and_only_while_leads_are_on(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        ops = _ops()
        assert TOOL not in [tool["name"] for tool in await ops.current_listing()]
        await _seed(pool)
        assert [tool["name"] for tool in await ops.current_listing()][-1] == TOOL
        scenario = ManagerDefinition(
            id="manager-desk", name="Desk", description="", system_prompt="p", profile_id=None,
            tools=ManagerTools.ALL, task_scope=TaskScope.ALL, task_policy=TaskPolicy.FREE, enabled=True,
            builtin=False, revision=-1)
        desk = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                          scope=scope_for(scenario))
        assert TOOL not in [tool["name"] for tool in await desk.current_listing()]
        answer = await _search(desk, _web())
        assert answer["status"] == "error" and answer["error"]["code"] == "forbidden"

    run_with_channel_configs(test, monkeypatch)


def test_a_dashboard_search_answers_at_most_five_leads_without_ids_and_audits_only_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        ids = await _seed(pool)
        turn = _web()
        answer = await _search(_ops(), turn)
        assert answer["status"] == "ok", answer
        result = answer["result"]
        assert set(result) == {"notice", "leads", "more"} and result["notice"] == ops_leads.TOOL_NOTICE
        assert len(result["leads"]) == 5 and result["more"] == 2
        for lead in result["leads"]:
            assert set(lead) == {"kind", "summary", "confidence", "state", "valid_until", "sources"}
            assert [set(source) for source in lead["sources"]] == [{"chat", "sender", "sent_at", "link"}]
            assert lead["sources"][0]["chat"] == "Release room" and lead["sources"][0]["sender"] == "Alice Example"
        text = json.dumps(result, ensure_ascii=False)
        assert "lead-" not in text and "a detail kept back" not in text and "ou_alice" not in text
        assert turn.blocks == ["（查了飞书消息线索：「aurora」，5 条）"]
        ((tool, status, error, stored),) = await _trail(pool)
        assert (tool, status, error) == (TOOL, "ok", None)
        assert json.loads(stored) == {"lead_ids": [f"lead-{i}" for i in ids[:5]], "count": 5}

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("turn", [
    Turn(channel="lark", chat="oc_group", message_id="m-1", sender=OP, group=True, channel_id="channel-lark"),
    Turn(channel="lark", chat="ou_someoneelse", message_id="m-1", sender="ou_someoneelse", channel_id="channel-lark"),
    Turn(channel="web", chat="chat-1", message_id="m-1", client_id="client-1", client_name="script"),
    Turn(channel="web", chat="chat-1"),  # Claude's own turn (a completion turn too)
    Turn(channel="telegram", chat="12345", message_id="m-1", sender="12345", channel_id="channel-telegram"),
], ids=["group", "another-sender", "api-client", "autonomous", "no-operator-id"])
def test_every_other_turn_is_refused_and_recorded(monkeypatch: pytest.MonkeyPatch, turn: Turn) -> None:
    async def test(pool: Any) -> None:
        await _seed(pool)
        await pool.execute("UPDATE channel_configs SET config = $1 WHERE channel_id = 'channel-telegram'",
                           json.dumps({"allowed_user_ids": ["12345"]}))
        answer = await _search(_ops(), turn)
        assert answer["status"] == "error" and answer["error"] == {
            "code": "forbidden",
            "message": "message leads are searched only in the user's own direct chat or on the dashboard"}
        assert turn.blocks == []
        ((tool, status, error, _),) = await _trail(pool)
        assert (tool, status, error) == (TOOL, "error", "forbidden")
        assert ops_leads._SEEN == {}

    run_with_channel_configs(test, monkeypatch)


def test_the_operators_own_direct_chat_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _seed(pool)
        turn = Turn(channel="lark", chat=OP, message_id="m-1", sender=OP, channel_id="channel-lark")
        answer = await _search(_ops(), turn, limit=2)
        assert answer["status"] == "ok" and len(answer["result"]["leads"]) == 2, answer
        assert turn.blocks == ["（查了飞书消息线索：「aurora」，2 条）"]

    run_with_channel_configs(test, monkeypatch)


def test_turning_leads_off_takes_the_tool_away(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _seed(pool)
        ops = _ops()
        assert (await _search(ops, _web()))["status"] == "ok"
        await _leads(pool, on=False)
        answer = await _search(ops, _web(message_id="m-2"))
        assert answer["error"] == {"code": "forbidden", "message": f"this Manager no longer has {TOOL}"}
        assert TOOL not in [tool["name"] for tool in await ops.current_listing()]
        # Off between the scope and the run (a call already admitted): the run itself refuses.
        with pytest.raises(OpError) as refused:
            await ops_leads.TOOL.run(Call({"query": "aurora"}, _web(message_id="m-3"), ops.platform))
        assert refused.value.code is ErrorCode.CONFLICT and str(refused.value) == "message leads are turned off"

    run_with_channel_configs(test, monkeypatch)


def _envelope(*, group: bool) -> MessageEnvelope:
    chat = "oc_group" if group else OP
    return MessageEnvelope.create(
        source=channel_address("lark", "channel-lark"), target=manager_address("manager"),
        message_type=MessageType.USER_MESSAGE,
        payload={"text": "q", "channel_context": {"chat_id": chat, "chat_type": "group" if group else "p2p",
                                                  "sender_id": OP}})


def test_a_group_reply_that_quotes_a_searched_lead_is_not_sent(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    async def test(pool: Any) -> None:
        await _seed(pool, count=1)
        assert (await _search(_ops(), _web()))["status"] == "ok"
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        quoted = "好的：project  AURORA, launch moves to december-3 after the security review。"
        shares_15 = "我们讨论过 Project Aurora la 之类的话题，暂时没有结论。"
        assert manager._group_safe(_envelope(group=True), quoted) == ops_leads.WITHHELD_REPLY
        assert manager._group_safe(_envelope(group=True), shares_15) == shares_15
        assert manager._group_safe(_envelope(group=False), quoted) == quoted
        assert manager.leads_withheld == 1
        # A day later, or after /new, the session's searches are forgotten.
        assert not ops_leads.quotes_a_lead("manager", quoted, now=ops_leads._SEEN["manager"][0][0] + 86401)
        ops_leads.forget("manager")
        assert manager._group_safe(_envelope(group=True), quoted) == quoted

    run_with_channel_configs(test, monkeypatch)


def test_the_manager_searches_over_mcp_and_its_reply_says_what_it_searched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _seed(pool, count=2)
        chat = await _converse(tmp_path, [[(TOOL, {"query": "aurora"})]], ["Aurora 的上线时间定了吗？"])
        assert chat.tools[-1] == TOOL
        (reply,) = chat.replies
        assert reply.endswith("\n\n（查了飞书消息线索：「aurora」，2 条）")
        (answer,) = json.loads(reply.split("\n\n")[0])
        assert answer["status"] == "ok" and len(answer["result"]["leads"]) == 2

    run_with_channel_configs(test, monkeypatch)
