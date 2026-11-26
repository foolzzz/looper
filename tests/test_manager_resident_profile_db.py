"""Private resident profile data never appears in a shared turn or its stable-state removals."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from manager_mcp_stand_in import manager_endpoint
from looper.agents import operator_identity, operator_profile, persona, session_routing
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.lark_transport import CliTransport
from looper.agents.message import MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.tiers.state_view import WholeReason
from tests.db_support import DSN, run_db
from tests.lark_cli_stand_in import BOT_MENTION, BOT_OPEN_ID, OTHER
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
@pytest.mark.parametrize("entry,with_persona,profile", [
    ("group", True, False), ("group", True, True), ("api", True, True), ("api", False, True)])
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_isolated_turn_restores_operator_whole_state(
        monkeypatch, tmp_path, lark_cli_stand_in, stable, entry, with_persona, profile, changed, restart):
    async def check(pool):
        vault = tmp_path / "vault"
        vault.mkdir()
        profile_note = vault / "关于我.md"
        profile_note.write_text("## Name\nSynthetic private name\n", encoding="utf-8")
        await pool.executemany("INSERT INTO system_settings (key,value) VALUES ($1,$2)", [
            ("knowledge_vault", json.dumps({"path": str(vault)})),
            ("manager.profile.enabled", json.dumps(profile)), ("manager.stable_context", json.dumps(stable))])
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
                assert ("Name: Synthetic private name" in resident.turns[-1]) is profile
                first = resident.turns[-1]
                await manager._handle(own)  # no intervening audience: preserve the normal unchanged bytes
                if stable:
                    rev = first.split("\n", 1)[0].removeprefix("Looper state (rev ").removesuffix("):")
                    assert resident.turns[-1] == ("Looper state: unchanged since rev " + rev
                                                 + first[first.index("\n\nChannel:"):])
                else:
                    assert resident.turns[-1] == first
                shown = manager._state_shown
                if changed:
                    profile_note.write_text("## Name\nSynthetic updated name\n", encoding="utf-8")
                if entry == "api":
                    outsider = message("web", {"session_id": "synthetic-client-chat",
                        "client": {"id": "synthetic-client", "name": "Test client"}})
                else:
                    # Exercise native CLI intake as well as the Manager's audience boundary.
                    world_file = tmp_path / "world.json"
                    world = json.loads(world_file.read_text())
                    world["events"] = [{"type": "im.message.receive_v1", "chat_id": "oc_synthetic_group",
                        "chat_type": "group", "message_id": "om_synthetic_group", "message_type": "text",
                        "sender_id": OTHER, "sender_type": "user", "content": "@HelperBot hello",
                        "mentions": [BOT_MENTION]}]
                    world_file.write_text(json.dumps(world))
                    adapter = LarkChannelAdapter(runtime_root=tmp_path, transport=CliTransport(), bot_open_id=BOT_OPEN_ID,
                        allowlist=SenderAllowlist("channel-lark", frozenset({OTHER})))
                    adapter._bus, adapter._running = bus, True
                    await asyncio.wait_for(adapter._run_event_consumer(), timeout=10)
                    await asyncio.wait_for(asyncio.gather(*adapter._reactions._pending), timeout=10)
                    outsider = bus.get_queue(manager.agent_id).get_nowait()
                    assert outsider.payload["channel_context"]["chat_type"] == "group"
                for _ in range(2):  # check every isolated block, including the first
                    await manager._handle(outsider)
                    request = resident.turns[-1] + manager._process.system_prompt.text
                    assert all(text not in request for text in (
                        operator_profile.HEADING, "Synthetic private name", "Synthetic updated name"))
                    assert ("  Notes: persona-notes" in resident.turns[-1]) is (with_persona and entry == "api")
                    assert manager._state_shown is shown
                    assert manager._state_pending_whole is (WholeReason.OTHER_AUDIENCE if stable else None)
                    if stable:
                        assert shown is not None
                        assert resident.turns[-1].startswith("Looper state (rev ")
                if restart:
                    await manager.stop()
                    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
                    manager._bus, manager._claude = bus, resident
                await manager._handle(own)
                recovered = resident.turns[-1]
                if stable:
                    assert recovered.startswith("Looper state (rev ")
                    assert manager._state_shown.lines == tuple(recovered.split("\n\nChannel:")[0].split("\n")[1:] + [""])
                    if not restart:
                        reason = await pool.fetchval("SELECT metadata->'looper_state'->>'reason' FROM llm_records "
                                                     "WHERE agent_id='manager' ORDER BY id DESC LIMIT 1")
                        assert reason == WholeReason.OTHER_AUDIENCE.value
                assert ("  Notes: persona-notes" in recovered) is with_persona
                assert ("Name: Synthetic " + ("updated" if changed else "private") + " name" in recovered) is profile
                await manager._handle(own)
                if stable:
                    assert resident.turns[-1].startswith("Looper state: unchanged since rev ")
                else:
                    assert resident.turns[-1] == recovered
        finally:
            await manager.stop()
            await bus.shutdown()
    run_db(check, monkeypatch, timeout=40)
