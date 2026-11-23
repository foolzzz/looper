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
BASELINE = {False: {'sections': '1db4fa7cabcd2bd37cbfc5da7110f1baee4f19644ade5c15e852ad21ba7d948d',
         'turn': '92d3f8208e8edc7e68edfd6aae514b9aab2c0d575d36a1fc00e047f329336211',
         'command': 'e8f4c7aa9c0df509ed6ec960fcb698d7e476c3edbb278821e7601c2034a08740',
         'signature': '5f76b151eb503d212e137ffa5c78c04cb6b5a885a78a5b415327f184c943ff13'},
 True: {'sections': '1db4fa7cabcd2bd37cbfc5da7110f1baee4f19644ade5c15e852ad21ba7d948d',
        'turn': '45d307a205c1b2e5f448e6147f0db2bc033ac11c9bf1ade140bb7cbdb2e64462',
        'command': 'e8f4c7aa9c0df509ed6ec960fcb698d7e476c3edbb278821e7601c2034a08740',
        'signature': '5f76b151eb503d212e137ffa5c78c04cb6b5a885a78a5b415327f184c943ff13'}}


async def observe(root, monkeypatch):
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
        await manager._handle(MessageEnvelope.create(
            source=channel_address("web"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "hello", "channel_context": {"session_id": "persona-chat"}}))
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
