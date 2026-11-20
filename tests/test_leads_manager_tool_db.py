"""The default Manager's ``looper_leads_search`` (message-leads-intents RFC, section 5): only the default Manager has
it, only while leads are on, only in the user's own turns; its answer, its line after the reply, its audit trail, and
the group backstop.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
import time
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
    ops_leads._WITHHELD.clear()
    yield
    ops_leads._SEEN.clear()
    ops_leads._WITHHELD.clear()


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
    """A call in ``turn``, carrying its binding as the MCP bridge does (a turn Claude runs on its own has none)."""
    binding = None if turn.autonomous else f"b-{turn.message_id}-{id(turn)}"
    ops.begin_turn(turn, binding=binding)
    try:
        return await ops.call(TOOL, {"query": "aurora", **arguments}, binding=binding)
    finally:
        ops.end_turn(turn)


def _names(listing: list[dict[str, Any]]) -> list[str]:
    return [tool["name"] for tool in listing]


async def _trail(pool: Any) -> list[Any]:
    return await pool.fetch("SELECT tool, status, error_code, result FROM manager_actions ORDER BY id")


def test_only_the_default_manager_lists_it_and_only_while_leads_are_on(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        ops = _ops()
        assert TOOL not in _names(await ops.current_listing(_web()))
        await _seed(pool)
        assert _names(await ops.current_listing(_web()))[-1] == TOOL
        scenario = ManagerDefinition(
            id="manager-desk", name="Desk", description="", system_prompt="p", profile_id=None,
            tools=ManagerTools.ALL, task_scope=TaskScope.ALL, task_policy=TaskPolicy.FREE, enabled=True,
            builtin=False, revision=-1)
        desk = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                          scope=scope_for(scenario))
        assert TOOL not in _names(await desk.current_listing(_web()))
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


def _refused_turns() -> list[Turn]:
    return [
        Turn(channel="lark", chat="oc_group", message_id="m-1", sender=OP, group=True, channel_id="channel-lark"),
        Turn(channel="lark", chat="ou_someoneelse", message_id="m-1", sender="ou_someoneelse",
             channel_id="channel-lark"),
        Turn(channel="web", chat="chat-1", message_id="m-1", client_id="client-1", client_name="script"),
        Turn(channel="web", chat="chat-1"),  # Claude's own turn (a completion turn too)
        Turn(channel="telegram", chat="12345", message_id="m-1", sender="12345", channel_id="channel-telegram"),
    ]


REFUSED_IDS = ["group", "another-sender", "api-client", "autonomous", "no-operator-id"]


async def _seed_with_telegram(pool: Any) -> None:
    await _seed(pool)
    await pool.execute("UPDATE channel_configs SET config = $1 WHERE channel_id = 'channel-telegram'",
                       json.dumps({"allowed_user_ids": ["12345"]}))


@pytest.mark.parametrize("turn", _refused_turns(), ids=REFUSED_IDS)
def test_every_other_turn_is_refused_and_recorded(monkeypatch: pytest.MonkeyPatch, turn: Turn) -> None:
    async def test(pool: Any) -> None:
        await _seed_with_telegram(pool)
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
        assert TOOL not in _names(await ops.current_listing(_web()))
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
        assert not ops_leads.quotes_a_lead("manager", quoted, now=time.monotonic() + ops_leads.SEEN_SECONDS + 1)
        ops_leads.forget("manager")
        assert manager._group_safe(_envelope(group=True), quoted) == quoted

    run_with_channel_configs(test, monkeypatch)


def test_the_manager_searches_over_mcp_and_its_reply_says_what_it_searched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _seed(pool, count=2)
        chat = await _converse(tmp_path, [[(TOOL, {"query": "aurora"})]], ["Aurora 的上线时间定了吗？"], bound=True)
        assert chat.tools[-1] == TOOL
        (reply,) = chat.replies
        assert reply.endswith("\n\n（查了飞书消息线索：「aurora」，2 条）")
        (answer,) = json.loads(reply.split("\n\n")[0])
        assert answer["status"] == "ok" and len(answer["result"]["leads"]) == 2

    run_with_channel_configs(test, monkeypatch)



@pytest.mark.parametrize("turn", _refused_turns(), ids=REFUSED_IDS)
def test_the_tool_is_offered_only_in_the_users_own_turns(monkeypatch: pytest.MonkeyPatch, turn: Turn) -> None:
    async def test(pool: Any) -> None:
        await _seed_with_telegram(pool)
        ops = _ops()
        assert TOOL not in _names(await ops.current_listing(turn))
        dm = Turn(channel="lark", chat=OP, message_id="m-2", sender=OP, channel_id="channel-lark")
        assert TOOL in _names(await ops.current_listing(_web())) and TOOL in _names(await ops.current_listing(dm))
        # No turn: none. MCP's tools/list (as the endpoint asks): the running turn's.
        assert TOOL not in _names(await ops.current_listing())
        assert TOOL not in _names(await ops.current_listing())
        ops.begin_turn(turn, binding=None if turn.autonomous else "b-other")
        assert TOOL not in _names(await ops.current_listing())
        ops.end_turn(turn)
        ops.begin_turn(_web(), binding="b-web")
        assert TOOL in _names(await ops.current_listing())

    run_with_channel_configs(test, monkeypatch)


def test_an_operator_tool_runs_only_in_the_turn_its_call_is_bound_to(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import chat_settings

    async def test(pool: Any) -> None:
        await _seed(pool)
        assert not chat_settings.stop_enabled()  # the default: other tools run in the turn running when they come
        ops = _ops()
        group = Turn(channel="lark", chat="oc_group", message_id="m-g", sender=OP, group=True,
                     channel_id="channel-lark")
        ops.begin_turn(group, binding="b-group")
        ops.end_turn(group)
        web = _web()
        ops.begin_turn(web, binding="b-web")
        for binding in ("b-group", None, "b-unknown"):  # a late group call, none, one Looper never gave
            answer = await ops.call(TOOL, {"query": "aurora"}, binding=binding)
            assert answer["status"] == "error" and answer["error"]["code"] == "forbidden", (binding, answer)
        assert web.blocks == [] and ops_leads._SEEN == {}
        assert (await ops.call(TOOL, {"query": "aurora"}, binding="b-web"))["status"] == "ok"
        ops.end_turn(web)
        ended = await ops.call(TOOL, {"query": "aurora"}, binding="b-web")
        assert ended["status"] == "error" and ended["error"]["code"] == "expired", ended

    run_with_channel_configs(test, monkeypatch)


def test_a_group_turn_after_the_users_own_restarts_the_process_without_the_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The resident process reads its tools once, when it starts: the turn's own tools are in its signature, so a
    group turn after the user's own (and the user's own after it) starts it again, resuming the session."""
    from types import SimpleNamespace

    from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
    from looper.agents.bus import MessageBus
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    prepared: list[Any] = []
    prepare = ManagerAgent._prepare_turn

    async def keep(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await prepare(self, *args, **kwargs)
        if result.signature is not None:
            prepared.append(result)
        return result

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", keep)
    web = channel_address("web")
    lark = channel_address("lark", "channel-lark")

    def message(index: int, source: Any, context: dict[str, Any], origin: str) -> MessageEnvelope:
        return MessageEnvelope.create(source=source, target=manager_address("manager"),
                                      message_type=MessageType.USER_MESSAGE,
                                      payload={"text": f"hello {index}", "channel_context": context},
                                      correlation_id=f"corr-{index}", metadata={"origin_channel_id": origin})

    async def test(pool: Any) -> None:
        await _seed(pool, count=1)
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=mcp_manager_claude(tmp_path, [[], [], []]),
                                                    work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda task_id: False), tmp_path,
                                    turn_file=tmp_path / "turn.json"):
            for index, (source, context, origin) in enumerate([
                (web, {"session_id": "chat-1"}, "channel-web"),
                (lark, {"chat_id": "oc_group", "chat_type": "group", "sender_id": OP, "message_id": "om_g1"},
                 "channel-lark"),
                (web, {"session_id": "chat-1"}, "channel-web"),
            ]):
                await manager._handle(message(index, source, context, origin))
            await manager.stop()
        await bus.shutdown()
        own, group, again = prepared
        assert own.signature != group.signature and again.signature == own.signature
        assert len(group.signature) == len(own.signature) - 1  # the listing's hash: only a turn with the tool has it

    run_with_channel_configs(test, monkeypatch)
