"""Persona reaches the resident Manager's state, never its system contract or tools."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
from looper.agents import chat_settings, manager_persona_context, operator_identity, persona, session_routing
from looper.agents.bus import MessageBus
from looper.agents.managers_api import ManagerDefinition
from looper.agents.message import MessageEnvelope, MessageType, channel_address
from looper.agents.session_routing import SessionKind
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import DSN, run_db, v1_api
from tests.test_manager_session_routing_db import _Resident

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("recovery", ["claude_start", "claude_stop", "switch_session", "empty_reply"])
@pytest.mark.parametrize("stable", [False, True])
@pytest.mark.parametrize("group", [True, False], ids=["group", "other-dm"])
def test_shared_main_recovery_keeps_the_turn_persona_scope(monkeypatch, tmp_path, recovery, stable, group):
    async def check(pool):
        monkeypatch.setattr(session_routing, "_routing_enabled", False)
        monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", recovery == "empty_reply")
        if stable:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('manager.stable_context','true')")
        await persona.update({"name": "管家", "tone": "private-tone", "notes": "private-notes"})
        await pool.executemany("INSERT INTO projects (id,name) VALUES ($1,$2)",
                              [(f"p-{i}", f"project-{i}") for i in range(30)])
        async def operator(_):
            return "operator-test"
        monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        resident = _Resident("sess-main")
        manager._claude = resident
        own = MessageEnvelope.create(source=channel_address("web"), target=manager.agent_address,
            message_type=MessageType.USER_MESSAGE,
            payload={"text": "hello", "channel_context": {"session_id": "persona-chat"}})
        outsider = MessageEnvelope.create(source=channel_address("lark"), target=manager.agent_address,
            message_type=MessageType.USER_MESSAGE, payload={"text": "hello", "channel_context": {
                "chat_id": "synthetic-group" if group else "synthetic-dm",
                "chat_type": "group" if group else "p2p", "sender_id": "other-test"}})
        try:
            async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path):
                await manager._handle(own)
                assert "  Notes: private-notes" in resident.turns[-1]
                shown = manager._state_shown
                scopes = []
                start_turn = manager._start_turn
                async def observe_scope(token, prepared):
                    scopes.append(prepared.run.persona_scope)
                    await start_turn(token, prepared)
                monkeypatch.setattr(manager, "_start_turn", observe_scope)
                attempts = 0
                if recovery == "empty_reply":
                    run_turn = resident.run_turn
                    async def empty_then_reply(*args, **kwargs):
                        nonlocal attempts
                        attempts += 1
                        turn = await run_turn(*args, **kwargs)
                        if attempts < 3:
                            turn.result_event["result"] = ""
                        return turn
                    monkeypatch.setattr(resident, "run_turn", empty_then_reply)
                else:
                    if recovery == "claude_start":
                        await resident.stop()
                        owner, method = resident, "start"
                    elif recovery == "claude_stop":
                        monkeypatch.setattr(resident, "stale", lambda _: resident.started)
                        owner, method = manager, "_stop_replaced_session"
                    else:
                        manager._sessions["main"].session_runtime = "omp"
                        manager._process = None  # no running predecessor needs a handoff
                        owner, method = manager, "_switch_session"
                    operation = getattr(owner, method)
                    async def fail_once(*args, **kwargs):
                        nonlocal attempts
                        attempts += 1
                        if attempts == 1:
                            raise TimeoutError("synthetic startup failure")
                        return await operation(*args, **kwargs)
                    monkeypatch.setattr(owner, method, fail_once)
                await manager._handle(outsider)
                queue = bus.get_queue(outsider.source.agent_id)
                sent = [queue.get_nowait() for _ in range(queue.qsize())]
                replies = [item for item in sent if item.message_type is MessageType.AGENT_REPLY]
                assert len(replies) == 1 and "error" not in replies[0].payload
                assert attempts == (3 if recovery == "empty_reply" else 2)
                assert len(resident.turns) == (4 if recovery == "empty_reply" else 2)
                assert all(text not in "".join(resident.turns[1:])
                           for text in ("Persona", "管家", "private-tone", "private-notes"))
                assert "Projects:" in resident.turns[1]
                assert manager._state_shown is shown
                if stable:
                    assert shown is not None
                    assert any(line.startswith("Looper state (rev ") for line in resident.turns[1].splitlines())
                assert scopes == [manager_persona_context.Scope.OMIT] * (3 if recovery == "empty_reply" else 2)
        finally:
            await manager.stop()
            await bus.shutdown()

    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("stable,cleared", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("channel", ["lark", "telegram"])
@pytest.mark.parametrize("group", [False, True])
def test_shared_main_hides_persona_from_other_chats(monkeypatch, tmp_path, stable, cleared, channel, group):
    async def check(pool):
        monkeypatch.setattr(session_routing, "_routing_enabled", False)
        async def operator(_):
            return "operator-test"
        monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
        if stable:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('manager.stable_context','true')")
        await persona.update({"name": "管家", "tone": "private-tone", "notes": "private-notes"})
        # Enough ordinary state to make a persona-removal diff small: it must still never be sent.
        await pool.executemany("INSERT INTO projects (id,name) VALUES ($1,$2)",
                              [(f"p-{i}", f"project-{i}") for i in range(30)])
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(
            claude_bin=mcp_manager_claude(tmp_path, [[], [], []]), work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        def turns():
            return [json.loads(line) for line in (tmp_path / "turns.jsonl").read_text().splitlines()]
        def message(source, context):
            return MessageEnvelope.create(source=channel_address(source), target=manager.agent_address,
                message_type=MessageType.USER_MESSAGE, payload={"text": "hello", "channel_context": context})
        own = message("web", {"session_id": "persona-chat"})
        try:
            async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path):
                await manager._handle(own)
                assert "  Name: 管家\n  Tone: private-tone\n  Notes: private-notes" in turns()[-1]
                shown = manager._state_shown
                if cleared:
                    await persona.update(dict.fromkeys(("name", "tone", "notes")))
                await manager._handle(message(channel, {"chat_id": "-12345" if group else "12345",
                    "chat_type": "group" if group else "p2p", "sender_id": "other-test"}))
                outsider = turns()[-1]
                assert all(text not in outsider for text in ("Persona", "管家", "private-tone", "private-notes"))
                assert "Projects:" in outsider
                assert manager._state_shown is shown
                if stable:
                    assert outsider.startswith("Looper state (rev ")
                    assert shown is not None
                await manager._handle(own)
                if stable:
                    assert turns()[-1].startswith("Looper state: changes" if cleared else "Looper state: unchanged")
                else:
                    assert "  Tone: private-tone\n  Notes: private-notes" in turns()[-1]
        finally:
            await manager.stop()
            await bus.shutdown()

    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("stable", [False, True])
def test_operator_edits_reach_the_next_turn_and_clear_without_restarting(monkeypatch, tmp_path, stable):
    async def check(pool):
        await pool.execute("INSERT INTO projects (id,name) VALUES ('persona-alpha','alpha'), ('persona-beta','beta')")
        if stable:
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ('manager.stable_context', 'true')")
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(
            claude_bin=mcp_manager_claude(tmp_path, [[], [], [], []]), work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        try:
            async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path), \
                    v1_api(tmp_path, pool) as call:
                async def save(body):
                    status, response = await call("manager_persona.update", body)
                    assert status == 200, response

                await save({"name": "管家", "address": "unused-address", "tone": "简洁、直接", "notes": "x" * 400})
                signatures, tools, pids = [], [], []
                for index in range(4):
                    if index == 2:
                        await save({"name": "小助"})
                    if index == 3:
                        await save({"name": None, "address": None, "tone": None, "notes": None})
                    await manager._handle(MessageEnvelope.create(
                        source=channel_address("web"), target=manager.agent_address,
                        message_type=MessageType.USER_MESSAGE,
                        payload={"text": f"hello {index}", "channel_context": {"session_id": "persona-chat"}}))
                    signatures.append(manager._process.signature)
                    pids.append(manager._claude.pid)
                    tools.append(await manager.ops.current_listing(None))
                assert all(signature == signatures[0] for signature in signatures)
                assert pids[0] is not None and len(set(pids)) == 1
                assert all(listed == tools[0] for listed in tools)
                assert "Persona" not in manager._process.system_prompt.text
                assert await pool.fetchval("SELECT value FROM system_settings WHERE key='manager.persona'") is None
        finally:
            await manager.stop()
            await bus.shutdown()
        turns = [json.loads(line) for line in (tmp_path / "turns.jsonl").read_text().splitlines()]
        first, unchanged, renamed, cleared = turns
        assert first.index("Persona (") < first.index("Projects:") < first.index("Channel:")
        assert "  Name: 管家\n  Tone: 简洁、直接\n  Notes: " in first
        assert "unused-address" not in "".join(turns) and "Address the operator as" not in "".join(turns)
        assert "Name: 小助" in renamed
        if stable:
            assert first.startswith("Looper state (rev ")
            assert unchanged.startswith("Looper state: unchanged") and "Persona" not in unchanged
            assert renamed.startswith("Looper state: changes")
            assert "+  Name: 小助" in renamed and "-  Name: 管家" in renamed
            assert cleared.startswith("Looper state (rev ") and "Persona" not in cleared
        else:
            assert all(turn.startswith("Persona (") for turn in turns[:3])
            assert "Persona" not in cleared and cleared.startswith("Projects:")

    run_db(check, monkeypatch, timeout=60)


@pytest.mark.parametrize("entry,kind", [
    ("web", SessionKind.MAIN), ("api", SessionKind.MAIN),
    ("lark", SessionKind.MAIN), ("telegram", SessionKind.MAIN),
    ("group", SessionKind.GROUP), ("dm", SessionKind.DM),
])
def test_a_routed_session_gets_only_its_allowed_persona_fields(monkeypatch, tmp_path, entry, kind):
    async def check(pool):
        await persona.update({"name": "管家", "address": "unused-address", "tone": "private-tone", "notes": "private-notes"})
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        async def operator(_):
            return "operator-test"
        monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
        channel = entry if entry in ("lark", "telegram") else "web" if entry in ("web", "api") else "lark"
        context = {"chat_id": "12345", "chat_type": "group" if entry == "group" else "p2p",
                   "sender_id": "other-test" if entry == "dm" else "operator-test"}
        payload = {"text": "hello", "channel_context": context}
        if entry == "api":
            context["client"] = {"id": "synthetic-client", "name": "Test client"}
        envelope = MessageEnvelope.create(source=channel_address(channel), target=manager.agent_address,
                                          message_type=MessageType.USER_MESSAGE, payload=payload)
        route = await manager._router._user_key(envelope)
        assert route.kind is kind
        session = manager._router.session(route)
        token = manager._current.set(session)
        try:
            text = await manager._build_user_message("hello", channel="Channel: synthetic chat", envelope=envelope)
            assert text.startswith("Persona (") and "  Name: 管家" in text
            assert ("private-tone" in text) is (kind is SessionKind.MAIN)
            assert ("private-notes" in text) is (kind is SessionKind.MAIN)
            assert "unused-address" not in text
            if kind is not SessionKind.MAIN:
                assert "tone" not in text.splitlines()[0]
        finally:
            manager._current.reset(token)

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("unavailable", [False, True])
def test_unverified_direct_sender_gets_no_persona(monkeypatch, tmp_path, caplog, unavailable):
    async def check(pool):
        async def operator(_):
            if unavailable:
                raise RuntimeError("synthetic identity outage")
            return None
        monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
        await persona.update({"name": "管家", "tone": "private-tone", "notes": "private-notes"})
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        envelope = MessageEnvelope.create(source=channel_address("lark"), target=manager.agent_address,
            message_type=MessageType.USER_MESSAGE, payload={"text": "hello", "channel_context": {
                "chat_id": "synthetic-dm", "chat_type": "p2p", "sender_id": "other-test"}})
        text = await manager._build_user_message("hello", envelope=envelope)
        assert "Projects:" in text and "Persona" not in text
        assert "private-tone" not in text and "private-notes" not in text
        if unavailable:
            assert "could not read persona audience identity" in caplog.text

    run_db(check, monkeypatch, timeout=30)


def test_a_scenario_manager_ignores_the_default_persona(monkeypatch, tmp_path):
    from looper.db import managers

    async def check(pool):
        row = await managers.insert_manager({"id": "manager-scenario", "name": "Scenario",
                                            "system_prompt": "Review carefully.", "task_scope": "all"}, conn=pool)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path,
                               definition=ManagerDefinition.from_row(row))
        before = await manager._sections(None)
        await persona.update({"name": "管家", "tone": "private-tone", "notes": "private-notes"})
        assert await manager._sections(None) == before

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("stored", ['{', '{"name":"' + 'x' * 21 + '"}'])
def test_unreadable_or_oversize_storage_has_no_model_section(monkeypatch, tmp_path, caplog, stored):
    async def check(pool):
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        before = await manager._sections(None)
        await pool.execute("INSERT INTO system_settings (key,value) VALUES ('manager.persona',$1)", stored)
        await persona.load()
        assert await manager._sections(None) == before
        assert "manager.persona is unreadable" in caplog.text

    run_db(check, monkeypatch, timeout=30)
