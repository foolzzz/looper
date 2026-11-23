"""Persona reaches the resident Manager's state, never its system contract or tools."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
from looper.agents import persona
from looper.agents.bus import MessageBus
from looper.agents.managers_api import ManagerDefinition
from looper.agents.message import MessageEnvelope, MessageType, channel_address
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


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


@pytest.mark.parametrize("kind", [SessionKind.MAIN, SessionKind.GROUP, SessionKind.DM])
def test_a_routed_session_gets_only_its_allowed_persona_fields(monkeypatch, tmp_path, kind):
    async def check(pool):
        await persona.update({"name": "管家", "address": "unused-address", "tone": "private-tone", "notes": "private-notes"})
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        route = SessionKey(kind) if kind is SessionKind.MAIN else SessionKey(kind, "channel-lark", "synthetic-chat")
        session = manager._router.session(route)
        token = manager._current.set(session)
        try:
            text = await manager._build_user_message("hello", channel="Channel: synthetic chat")
            assert text.startswith("Persona (") and "  Name: 管家" in text
            assert ("private-tone" in text) is (kind is SessionKind.MAIN)
            assert ("private-notes" in text) is (kind is SessionKind.MAIN)
            assert "unused-address" not in text
        finally:
            manager._current.reset(token)

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
