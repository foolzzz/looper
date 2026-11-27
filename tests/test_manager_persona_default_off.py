"""Fixed main fingerprints of the real Manager preparation path and the Planner prompt."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper.agents import claude_cli, persona, profile_registry
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import DSN, run_db
from tests.manager_surface_support import assert_main_surfaces
from tests.prompt_sections import assert_frozen, planner_prompt
from tests.test_manager_session_routing_db import _Resident


# Captured from origin/main (51a8d2f7c) with the same synthetic database and inputs.
# Normalize temporary paths and check content-addressed hashes against their sources first. The system prompt and
# the tools are checked per section and per tool against tests/fixtures/manager_surfaces_off.json.
# The signature hashes changed on purpose (the MCP entry gained -I); sections, turn and command are unchanged.
BASELINE = {False: {'sections': '1db4fa7cabcd2bd37cbfc5da7110f1baee4f19644ade5c15e852ad21ba7d948d',
         'turn': '92d3f8208e8edc7e68edfd6aae514b9aab2c0d575d36a1fc00e047f329336211',
         'command': 'e8f4c7aa9c0df509ed6ec960fcb698d7e476c3edbb278821e7601c2034a08740',
         'signature': '3291348a51946f4452605f5194bb1ad8a02f3675a3f9af44d2987bc469626ada'},
 True: {'sections': '1db4fa7cabcd2bd37cbfc5da7110f1baee4f19644ade5c15e852ad21ba7d948d',
        'turn': '45d307a205c1b2e5f448e6147f0db2bc033ac11c9bf1ade140bb7cbdb2e64462',
        'command': 'e8f4c7aa9c0df509ed6ec960fcb698d7e476c3edbb278821e7601c2034a08740',
        'signature': '3291348a51946f4452605f5194bb1ad8a02f3675a3f9af44d2987bc469626ada'}}

# Turn hashes captured on 8a69cf7ca before changing the audience guard (stable off, on).
AUDIENCE_TURNS = {
    "api": ("0bd9831de9506288df0df68a7c7bf6f5699c195afa7726a0e3e97f1782f5bb13",
            "edbea9634c8f2776afaa3b24bb6a4591865cc43530b800a3e9d058eb9250f0fd"),
    "lark": ("27e5216bfe7451e13ca442ab2f9bedbd2fec6841650a345bdfa4147e070e6e43",
             "0a71d08e7e54d369b20e5bb17895e2f0bf6204fa4ba1f077a6d9fbd909e0b326"),
    "telegram": ("b2c90ba295974a125e9b7e0c209c2997bf39c167765176df9d6fa62b51253bec",
                 "e5a8cacb3b8f6b6a2f1889b4c286ee8bc42e0b68004daff9f575b8ca9214ae1c"),
    "group": ("08d97464220c942acbf47a8fd30e2ddadb7d786afc9e7e26b1ff8b35fa2d02a8",
              "67166cc2b402b334cbc9108f3042ec947083c1dfa3a64b22fc6bef7dfd692b43"),
}
AUDIENCE_TURNS["other-dm"] = AUDIENCE_TURNS["lark"]


async def observe(root, monkeypatch, *, entry="web"):
    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
    monkeypatch.setattr(claude_cli, "_outside_memory", lambda *args, **kwargs: ["/synthetic/CLAUDE.md"])
    bus = MessageBus(runtime_root=root)
    manager = ManagerAgent(config=ManagerConfig(claude_bin="claude", work_dir=root), runtime_root=root)
    manager._bus = bus
    manager._claude = _Resident("sess-main")
    manager.ops = ManagerOps(Platform(bus=bus, task_router=SimpleNamespace(has_planner=lambda _: False)),
                             url="http://127.0.0.1:9", token="tok",
                             turn_file=root / ".local" / "manager-turn.json")
    manager.ops._working_at = manager.ops._clock()
    prepared = []
    prepare = manager._prepare_turn

    async def capture(*args, **kwargs):
        result = await prepare(*args, **kwargs)
        prepared.append(result)
        return result

    monkeypatch.setattr(manager, "_prepare_turn", capture)
    monkeypatch.delenv("LOOPER_MCP_TRACE", raising=False)
    try:
        context = {"session_id": "persona-chat"} if entry in ("web", "api") else {
            "chat_id": "-12345" if entry == "group" else "12345",
            "chat_type": "group" if entry == "group" else "p2p",
            "sender_id": "synthetic-operator" if entry in ("lark", "telegram") else "synthetic-other"}
        if entry == "api":
            context["client"] = {"id": "synthetic-client", "name": "Test client"}
        await manager._handle(MessageEnvelope.create(
            source=channel_address(entry if entry in ("lark", "telegram") else
                                   "lark" if entry in ("group", "other-dm") else "web"),
            target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "hello", "channel_context": context}))
        item, = prepared
        tools = await manager.ops.current_listing(item.turn)
        command = list(item.signature[0])
        command[0] = "<claude>"
        index = command.index("--settings") + 1
        raw_settings = command[index]
        if raw_settings.startswith("{"):
            command[index] = json.loads(raw_settings)
        else:
            settings_file = Path(raw_settings)
            content = settings_file.read_text()
            assert settings_file.stem == hashlib.sha256(content.encode()).hexdigest()
            command[index] = json.loads(content)
        signature = [command, *item.signature[1:]]
        assert signature[1] == hashlib.sha256(item.system_prompt.text.encode()).hexdigest()
        config = json.loads(Path(item.launch.library.mcp_config).read_text())
        library_item, = item.launch.library.items
        assert signature[3] == item.launch.library.sha256
        assert library_item.sha256 == hashlib.sha256(
            json.dumps(config["mcpServers"]["looper"], sort_keys=True).encode()).hexdigest()
        signature[1], signature[3] = "<system prompt, checked per section>", config

        def digest(value):
            blob = json.dumps(value, sort_keys=True, ensure_ascii=False)
            blob = blob.replace(str(root), "<root>").replace(sys.executable, "<python>")
            blob = re.sub(r"/[0-9a-f]{64}\.(md|json)", r"/<sha>.\1", blob)
            return hashlib.sha256(blob.encode()).hexdigest()

        return {key: digest(value) for key, value in {
            "sections": await manager._sections(None), "turn": item.turn_text, "command": command,
            "signature": signature,
        }.items()}, tools, item.system_prompt.text
    finally:
        await manager.stop()
        await bus.shutdown()


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("stored", [None, "{}", '{"address":"unused-address"}'])
@pytest.mark.parametrize("stable", [False, True])
def test_disabled_or_unset_persona_matches_main(monkeypatch, tmp_path, stored, stable):
    async def check(pool):
        if stored is not None:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('manager.persona',$1)", stored)
        await persona.load()
        if stable:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('manager.stable_context','true')")
        digests, tools, system = await observe(tmp_path, monkeypatch)
        assert digests == BASELINE[stable]
        assert_main_surfaces(tools, system, tmp_path, variant="stable_context" if stable else "default")

    run_db(check, monkeypatch, timeout=40)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("entry", ["api", "lark", "telegram", "group", "other-dm"])
@pytest.mark.parametrize("stable", [False, True])
def test_profile_off_audiences_match_main_bytes_without_identity_reads(monkeypatch, tmp_path, entry, stable):
    from unittest.mock import AsyncMock
    from looper.agents import operator_identity, session_routing

    async def check(pool):
        monkeypatch.setattr(session_routing, "_routing_enabled", False)
        read = AsyncMock(side_effect=RuntimeError("identity must not be read"))
        monkeypatch.setattr(operator_identity, "operator_sender_id", read)
        if stable:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('manager.stable_context','true')")
        digests, tools, system = await observe(tmp_path, monkeypatch, entry=entry)
        assert digests == {**BASELINE[stable], "turn": AUDIENCE_TURNS[entry][stable]}
        assert_main_surfaces(tools, system, tmp_path, variant="stable_context" if stable else "default")
        read.assert_not_awaited()

    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("value", [persona.Persona(), persona.Persona(name="管家", tone="简洁、直接", notes="private-notes")])
def test_the_planner_prompt_keeps_its_main_hash_with_or_without_persona(value):
    previous = persona.current()
    try:
        persona.set_current(value)
        text = planner_prompt().text
        assert_frozen("planner_prompt", text)
        # Sections may be added by other features; the persona's own words never reach the Planner.
        assert not any(word and word in text for word in (value.name, value.tone, value.notes))
    finally:
        persona.set_current(previous)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("stored", [None, "false"])
@pytest.mark.parametrize("stable", [False, True])
def test_resident_profile_off_matches_fixed_main_baseline(monkeypatch, tmp_path, stored, stable):
    from unittest.mock import AsyncMock
    from looper.agents import operator_profile

    async def check(pool):
        root = tmp_path / "vault"
        root.mkdir()
        (root / "关于我.md").write_text("## Name\nSynthetic operator\n", encoding="utf-8")
        await pool.execute("INSERT INTO system_settings (key,value) VALUES ('knowledge_vault',$1)",
                           json.dumps({"path": str(root), "profile_source": "关于我.md"}))
        if stored is not None:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('manager.profile.enabled',$1)", stored)
        if stable:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('manager.stable_context','true')")
        read = AsyncMock(side_effect=AssertionError("disabled profile was read"))
        monkeypatch.setattr(operator_profile, "resident_text", read)
        digests, tools, system = await observe(tmp_path, monkeypatch)
        assert digests == BASELINE[stable]
        assert_main_surfaces(tools, system, tmp_path, variant="stable_context" if stable else "default")
        read.assert_not_awaited()

    run_db(check, monkeypatch, timeout=40)
