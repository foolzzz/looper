"""Private resident profile data never appears in a shared turn or its stable-state removals."""

import json
from types import SimpleNamespace

import pytest

from manager_mcp_stand_in import manager_endpoint
from looper.agents import operator_identity, operator_profile, persona, session_routing
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import DSN, run_db
from tests.test_manager_session_routing_db import _Resident

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("entry", ["other-dm", "group", "api"])
@pytest.mark.parametrize("clear_switch", [False, True])
def test_private_profile_does_not_leak_via_stable_removal(monkeypatch, tmp_path, entry, clear_switch):
    async def check(pool):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "关于我.md").write_text("## Name\nSynthetic private name\n", encoding="utf-8")
        await pool.executemany("INSERT INTO system_settings (key,value) VALUES ($1,$2)", [
            ("knowledge_vault", json.dumps({"path": str(vault)})),
            ("manager.profile.enabled", "true"), ("manager.stable_context", "true")])
        monkeypatch.setattr(session_routing, "_routing_enabled", False)
        async def identity(_):
            return "synthetic-operator"
        monkeypatch.setattr(operator_identity, "operator_sender_id", identity)
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        resident = _Resident("profile-session")
        manager._claude = resident
        def message(channel, context):
            return MessageEnvelope.create(source=channel_address(channel), target=manager.agent_address,
                message_type=MessageType.USER_MESSAGE, payload={"text": "hello", "channel_context": context})
        try:
            async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path):
                await manager._handle(message("web", {"session_id": "profile-chat"}))
                assert "Name: Synthetic private name" in resident.turns[-1]
                if clear_switch:
                    await operator_profile.update_enabled(False)
                await manager._handle(message("web", {"session_id": "synthetic-client-chat",
                    "client": {"id": "synthetic-client", "name": "Test client"}}) if entry == "api" else
                    message("lark", {"chat_id": "synthetic-other",
                        "chat_type": "group" if entry == "group" else "p2p", "sender_id": "other"}))
                assert "Synthetic private name" not in resident.turns[-1]
                assert operator_profile.HEADING not in resident.turns[-1]
                assert resident.turns[-1].startswith("Looper state (rev ")
        finally:
            await manager.stop()
            await bus.shutdown()
    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("stable", [False, True])
@pytest.mark.parametrize("with_persona", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_api_client_omits_profile_and_preserves_operator_state(
        monkeypatch, tmp_path, stable, with_persona, changed):
    async def check(pool):
        vault = tmp_path / "vault"
        vault.mkdir()
        profile_note = vault / "关于我.md"
        profile_note.write_text("## Name\nSynthetic private name\n", encoding="utf-8")
        await pool.executemany("INSERT INTO system_settings (key,value) VALUES ($1,$2)", [
            ("knowledge_vault", json.dumps({"path": str(vault)})),
            ("manager.profile.enabled", "true"), ("manager.stable_context", json.dumps(stable))])
        # Make a removal diff smaller than the whole state, so this also exercises that leak path.
        await pool.executemany("INSERT INTO projects (id,name) VALUES ($1,$2)",
                              [(f"p-{i}", f"project-{i}") for i in range(30)])
        if with_persona:
            await persona.update({"name": "Assistant", "tone": "concise", "notes": "persona-notes"})
        monkeypatch.setattr(session_routing, "_routing_enabled", False)
        async def identity(_):
            return "synthetic-operator"
        monkeypatch.setattr(operator_identity, "operator_sender_id", identity)
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        resident = _Resident("profile-session")
        manager._claude = resident
        def message(channel, context):
            return MessageEnvelope.create(source=channel_address(channel), target=manager.agent_address,
                message_type=MessageType.USER_MESSAGE, payload={"text": "hello", "channel_context": context})
        own = message("lark", {"chat_id": "synthetic-dm", "chat_type": "p2p",
                               "sender_id": "synthetic-operator"})
        try:
            async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path):
                await manager._handle(own)
                assert "Name: Synthetic private name" in resident.turns[-1]
                shown = manager._state_shown
                if changed:
                    profile_note.write_text("## Name\nSynthetic updated name\n", encoding="utf-8")
                await manager._handle(message("web", {"session_id": "synthetic-client-chat",
                    "client": {"id": "synthetic-client", "name": "Test client"}}))
                request = resident.turns[-1] + manager._process.system_prompt.text
                assert all(text not in request for text in (
                    operator_profile.HEADING, "Synthetic private name", "Synthetic updated name"))
                if with_persona:
                    assert "  Name: Assistant\n  Tone: concise\n  Notes: persona-notes" in resident.turns[-1]
                assert manager._state_shown is shown
                if stable:
                    assert shown is not None
                    assert resident.turns[-1].startswith("Looper state (rev ")
                await manager._handle(own)
                if stable:
                    assert resident.turns[-1].startswith(
                        "Looper state: changes" if changed else "Looper state: unchanged")
                else:
                    assert "Name: Synthetic " + ("updated" if changed else "private") + " name" in resident.turns[-1]
        finally:
            await manager.stop()
            await bus.shutdown()
    run_db(check, monkeypatch, timeout=40)
