"""Scoped high-risk authorization and chat stops on the real Manager bus."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import card_actions, chat_settings, operator_identity, session_routing
from looper.agents.managers import ManagerFleet
from looper.agents.ops import OpError, Risk, Tool, Turn, TurnState
from looper.agents.ops.service import CONFIRM, ManagerOps, Platform, scope_for
from looper.agents.turn_context import SpeakerKind
from tests.db_support import DSN, run_db, until
from tests.test_scoped_routing_db import GROUP_A, LARK, OPERATOR, OTHER, _message, _setup

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOTE = "[Looper] Your last reply in this chat asked the user to confirm"


async def _rig(pool: Any, root: Path, monkeypatch: Any) -> tuple[Any, ManagerOps, list[Any]]:
    rig, _ = await _setup(pool, root, monkeypatch, default="legacy")
    ran: list[Any] = []

    async def run(call: Any) -> dict[str, Any]:
        ran.append(call.turn.sender)
        return {"done": True}

    async def escalate(call: Any) -> str:
        return "Delete the synthetic item?"

    tool = Tool("t_delete", "delete", {"type": "object", "properties": {}}, Risk.WRITE, run, escalate=escalate)
    scope = replace(scope_for(rig.manager._definition), revision=-1, tools={tool.name: tool, CONFIRM.name: CONFIRM})
    ops = ManagerOps(Platform(bus=rig.bus, task_router=SimpleNamespace(has_planner=lambda _: False)),
                     url="http://127.0.0.1:9", scope=scope)
    ops._working_at = ops._clock()
    rig.manager.ops = ops
    return rig, ops, ran


async def _call(rig: Any, ops: ManagerOps, sender: str, name: str = "t_delete",
                arguments: dict[str, Any] | None = None, *, explicit_session: bool = True) -> tuple[Any, Any, str]:
    message = _message(rig, f"request-{len(ops._bindings)}", sender)
    session = await rig.manager._router.target(message)
    answers, turns = [], []
    arguments = arguments or {}

    async def call(on_event: Any) -> None:
        token = rig.manager._running_token
        turns.append(token.ops_turn)
        await on_event({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "synthetic-call", "name": f"mcp__looper__{name}", "input": arguments}]}})
        answers.append(await ops.call(name, arguments, binding=token.binding, call_id="synthetic-call",
                                      **({"session_key": session.key} if explicit_session else {})))

    session.claude.on_turn = call
    await rig.bus.publish(message)
    await rig.reply(message)
    await until(lambda: not rig.manager._router.pending())
    assert len(answers) == len(turns) == 1
    return answers[0], turns[0], session.claude.turns[-1]


@pytest.mark.parametrize("name", ["t_delete", "looper_confirm"], ids=["high-risk", "confirm"])
def test_member_high_risk_and_confirm_are_operator_only(name: str, monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, ops, ran = await _rig(pool, tmp_path, monkeypatch)
        await rig.start()
        try:
            answer, turn, _ = await _call(rig, ops, OTHER, name,
                {"confirmation_id": "synthetic-confirmation"} if name == "looper_confirm" else {})
            assert turn.speaker is SpeakerKind.MEMBER and turn.principal is not None
            assert answer["error"]["code"] == "forbidden" and not ran and not ops._pending
            assert json.loads(await pool.fetchval("SELECT result FROM manager_actions")) == {"reason": "operator_only"}
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=60)


@pytest.mark.parametrize("interleave", [False, True], ids=["own-session", "other-session"])
def test_operator_confirms_without_other_session_consuming(interleave: bool, monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, ops, ran = await _rig(pool, tmp_path, monkeypatch)
        await rig.start()
        try:
            asked, turn, _ = await _call(rig, ops, OPERATOR, explicit_session=False)
            assert asked["status"] == "needs_confirmation" and not ran
            pending = ops._pending[turn.chat_key]
            if interleave:
                # Exercise the native message input without a tool call in the other session.
                member = _message(rig, "member-message", OTHER)
                shared = await rig.manager._router.target(member)
                await rig.bus.publish(member)
                await rig.reply(member)
                await until(lambda: not rig.manager._router.pending())
                assert NOTE not in shared.claude.turns[-1]
                assert ops._pending[turn.chat_key] is pending and pending.answer_turn is None
            answer, _, text = await _call(rig, ops, OPERATOR, "looper_confirm",
                                         {"confirmation_id": asked["confirmation_id"]})
            assert NOTE in text and answer == {"status": "ok", "result": {"done": True}}
            assert ran == [OPERATOR] and not ops._pending
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=60)


@pytest.mark.parametrize("mismatch", ["sender", "session", "missing-asker"])
def test_forged_confirmation_is_not_initiator(mismatch: str, monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, ops, ran = await _rig(pool, tmp_path, monkeypatch)
        await rig.start()
        try:
            asked, asker, _ = await _call(rig, ops, OPERATOR)
            pending = ops._pending[asker.chat_key]
            key = pending.session_key
            forged = replace(asker, message_id="synthetic-forged", sender=OTHER if mismatch == "sender" else OPERATOR,
                             require_match=False, state=TurnState.ANSWERING)
            if mismatch == "missing-asker":
                pending.asker = None
            if mismatch == "session":
                key = "synthetic-other-session"
            pending.answer_turn = forged.message_id
            # Forging a principal bypasses the routing guarantee: call the actual boundary directly.
            with pytest.raises(OpError, match="only the person who asked") as refused:
                await ops._confirm(asked["confirmation_id"], forged, key)
            assert refused.value.code.value == "forbidden" and not ops._pending and not ran
            # Recreate and exercise call's typed audit mapping as well.
            pending.answer_turn = None
            ops._pending[asker.chat_key] = pending
            ops.begin_turn(forged, binding="synthetic-forged", session_key=key)
            answer = await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]},
                                    binding="synthetic-forged", session_key=key)
            assert answer["error"]["code"] == "forbidden" and not ops._pending and not ran
            assert json.loads(await pool.fetchval("SELECT result FROM manager_actions ORDER BY id DESC LIMIT 1")) == {
                "reason": "not_initiator"}
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=60)


@pytest.mark.parametrize("mode", ["session-persisted", "old-card", "old-button"])
def test_restored_confirmation_session(mode: str, monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, ops, ran = await _rig(pool, tmp_path, monkeypatch)
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true')", card_actions.ENABLED_KEY)
        await rig.start()
        try:
            asked, turn, _ = await _call(rig, ops, OPERATOR)
            if mode == "old-button":
                # Native CLI cards are text-only; replay an older row from a button-capable bot channel.
                pending = ops._pending.pop(turn.chat_key)
                await pool.execute("DELETE FROM card_actions")
                turn = replace(turn, channel="lark_bot", channel_id="channel-lark_bot", cards=[], blocks=[])
                pending.asker, pending.card = turn, None
                ops._pending[turn.chat_key] = pending
                await ops._card(pending, turn)
            row = await pool.fetchrow("SELECT payload, allowed FROM card_actions")
            payload = json.loads(row["payload"])
            assert payload["session_key"] == ops._pending[turn.chat_key].session_key
            assert list(row["allowed"]) == [OPERATOR]
            if mode != "session-persisted":
                await pool.execute("UPDATE card_actions SET payload = payload - 'session_key'")
            ops._pending.clear()
            assert await ops.restore_confirmations() == 1
            if mode == "old-button":
                monkeypatch.setitem(card_actions._handlers, "confirmation", ops.press)
                token = ops._pending[turn.chat_key].card
                answer = await card_actions.press(card_actions.Press(turn.channel_id, OPERATOR,
                    card_actions.value(token, "confirm"), chat=GROUP_A))
                assert answer.later is not None
                await answer.later
                assert ran == [OPERATOR] and not ops._pending
                return
            answer, _, text = await _call(rig, ops, OPERATOR, "looper_confirm",
                                         {"confirmation_id": asked["confirmation_id"]})
            if mode == "old-card":
                assert NOTE not in text and answer["error"]["code"] == "forbidden" and not ran
                assert json.loads(await pool.fetchval("SELECT result FROM manager_actions ORDER BY id DESC LIMIT 1")) == {
                    "reason": "not_initiator"}
            else:
                assert answer["status"] == "ok" and ran == [OPERATOR]
            assert not ops._pending
            await asyncio.gather(*ops._lapsing)
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=60)


@pytest.mark.parametrize("fleet", [False, True], ids=["local", "fleet"])
@pytest.mark.parametrize("sender,kind", [(OTHER, "person"), (OPERATOR, "bot"), (OPERATOR, None), ("read-failure", "person")])
def test_member_stop_spares_the_operator(fleet: bool, sender: str, kind: str | None,
                                        monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, ops, _ = await _rig(pool, tmp_path, monkeypatch)
        monkeypatch.setattr(chat_settings, "_stop_enabled", True)
        if fleet:
            owners = ManagerFleet(platform=Platform(bus=rig.bus, task_router=None), supervisor=None,
                                  runtime_root=tmp_path, url="http://127.0.0.1:9", config=rig.manager._config, broadcaster=None)
            owners._agents[rig.manager.agent_id] = rig.manager
            rig.manager.stop_chat = owners.stop_chat
        operator = _message(rig, "operator-running", OPERATOR)
        session = await rig.manager._router.target(operator)
        session.claude.hold = True
        await rig.start()
        try:
            await rig.bus.publish(operator)
            await asyncio.wait_for(session.claude.running.wait(), 15)
            running = rig.manager.turn_for(operator.message_id)
            # Real registered queued tokens have not been prepared and must stay protected.
            queued = rig.manager._register_turn(_message(rig, "operator-queued", OPERATOR))
            member = rig.manager._register_turn(_message(rig, "member-queued", OTHER))
            shared = await rig.manager._router.target(member.envelope)
            member.ops_turn = Turn(channel="lark", chat=GROUP_A, channel_id=LARK.agent_id,
                sender=OTHER, message_id=member.message_id, principal=rig.manager._router.principal(member.envelope, shared))
            if sender == "read-failure":
                identity = operator_identity.operator_sender_id
                async def failed(*args: Any) -> None:
                    raise OSError("synthetic identity read failure")
                monkeypatch.setattr(operator_identity, "operator_sender_id", failed)
            await rig.manager._stop_from_chat(_message(rig, "/stop", sender, kind))
            assert member.stop is not None and queued.stop is None
            assert running.stop is None
            if sender == "read-failure":
                monkeypatch.setattr(operator_identity, "operator_sender_id", identity)
            await pool.execute("INSERT INTO capability_set_versions (set_id, version, items) "
                               "SELECT set_id, 2, items FROM capability_set_versions WHERE set_id = 'full' AND version = 1")
            await pool.execute("UPDATE capability_sets SET current_version = 2 WHERE id = 'full'")
            await rig.manager.scope_config_changed(LARK.agent_id, GROUP_A, stop_now=False)
            assert session.retired is not None and running.stop is None
            await rig.manager._stop_from_chat(_message(rig, "/stop", OPERATOR))
            assert queued.stop is not None
            assert running.stop is not None
        finally:
            session.claude.release.set()
            await rig.close()
    run_db(body, monkeypatch, timeout=60)


def test_member_high_risk_through_native_cli_transport(monkeypatch: Any, tmp_path: Path,
                                                      lark_cli_stand_in: Path) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark_transport import CliTransport
    from tests.lark_cli_stand_in import BOT_MENTION, BOT_OPEN_ID

    world_file = tmp_path / "world.json"
    world = json.loads(world_file.read_text())
    world["events"] = [{"type": "im.message.receive_v1", "chat_id": GROUP_A, "chat_type": "group",
        "message_id": "om_synthetic_scope", "sender_id": OTHER, "sender_type": "user", "message_type": "text",
        "content": "@HelperBot delete the synthetic item", "mentions": [BOT_MENTION]}]
    world_file.write_text(json.dumps(world))

    async def body(pool: Any) -> None:
        rig, ops, ran = await _rig(pool, tmp_path, monkeypatch)
        session = await rig.manager._router.target(_message(rig, "native-member", OTHER))
        answers = []

        async def call(on_event: Any) -> None:
            token = rig.manager._running_token
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "native-call", "name": "mcp__looper__t_delete", "input": {}}]}})
            answers.append(await ops.call("t_delete", {}, binding=token.binding, call_id="native-call"))

        session.claude.on_turn = call
        transport = CliTransport()
        adapter = LarkChannelAdapter(allowlist=SenderAllowlist(LARK.agent_id, frozenset({OTHER})), transport=transport,
            bot_open_id=BOT_OPEN_ID, runtime_root=tmp_path, channel_id=LARK.agent_id, manager_id=rig.manager.agent_id)
        adapter._bus, adapter._running = rig.bus, True
        await rig.start()
        try:
            await asyncio.wait_for(adapter._run_event_consumer(), 10)
            await until(lambda: bool(answers))
            await until(lambda: not rig.manager._router.pending())
            assert answers[0]["error"]["code"] == "forbidden" and not ran and not ops._pending
            assert json.loads(await pool.fetchval("SELECT result FROM manager_actions")) == {"reason": "operator_only"}
        finally:
            await adapter.stop()
            await rig.close()
    run_db(body, monkeypatch, timeout=60)


def test_unscoped_turns_unchanged(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, ops, ran = await _rig(pool, tmp_path, monkeypatch)
        session_routing.set_scenario_routing(rig.manager.agent_id, "per_chat")
        first = Turn(channel="lark", chat=GROUP_A, channel_id=LARK.agent_id, sender=OPERATOR, message_id="ask",
                     speaker=SpeakerKind.OPERATOR)
        ops.begin_turn(first, binding="ask", session_key="synthetic-a")
        asked = await ops.call("t_delete", {}, binding="ask", session_key="synthetic-a")
        ops.end_turn(first)
        # Existing speaker rules, with no principal: session and sender changes do not gate confirmation.
        second = replace(first, message_id="confirm", sender=OTHER)
        assert NOTE in ops.begin_turn(second, binding="confirm", session_key="synthetic-b")
        answer = await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]}, binding="confirm")
        assert answer["status"] == "ok" and ran == [OTHER]
        second.speaker = SpeakerKind.MEMBER
        answer = await ops.call("looper_confirm", {"confirmation_id": "synthetic-missing"}, binding="confirm")
        assert answer["error"]["code"] == "forbidden"
    run_db(body, monkeypatch, timeout=60)
