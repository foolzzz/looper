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
from looper.agents.agent_settings import Harness, Layer
from looper.agents.bus import MessageBus
from looper.agents.knowledge import EMPTY_KNOWLEDGE
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.tiers.planner import PlannerAgent, PlannerSettings, TaskPlan
from tests.db_support import DSN, run_db
from tests.test_manager_session_routing_db import _Resident


# Captured from origin/main (51a8d2f7c) with the same synthetic database and inputs.
# Normalize temporary paths and check content-addressed hashes against their sources first.
BASELINE = {False: {'sections': '1db4fa7cabcd2bd37cbfc5da7110f1baee4f19644ade5c15e852ad21ba7d948d',
         'turn': '92d3f8208e8edc7e68edfd6aae514b9aab2c0d575d36a1fc00e047f329336211',
         'system': '7d14661d728105ddd57740dc865754ac93a3a509613d185b65ec5a9adea55ebf',
         'tools': 'de8b33d0d94ea993f875cf3c2e54c07afe471aece5825a4e845610404da22ddc',
         'command': 'e8f4c7aa9c0df509ed6ec960fcb698d7e476c3edbb278821e7601c2034a08740',
         'signature': '1396add8832134a87b315c2398875f8dc5d02d687dc2f31a80af00eb92d7cdd4'},
 True: {'sections': '1db4fa7cabcd2bd37cbfc5da7110f1baee4f19644ade5c15e852ad21ba7d948d',
        'turn': '45d307a205c1b2e5f448e6147f0db2bc033ac11c9bf1ade140bb7cbdb2e64462',
        'system': '9be99c242f3fa3240f1222686b1001a889941b8123f61cd6684a7559feec6c05',
        'tools': 'de8b33d0d94ea993f875cf3c2e54c07afe471aece5825a4e845610404da22ddc',
        'command': 'e8f4c7aa9c0df509ed6ec960fcb698d7e476c3edbb278821e7601c2034a08740',
        'signature': '4c884da72244a15b83eef844f6055b5671c038893347e7ffbd291d953923b522'}}


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
        signature[1], signature[3] = item.system_prompt.text, config

        def digest(value):
            blob = json.dumps(value, sort_keys=True, ensure_ascii=False)
            blob = blob.replace(str(root), "<root>").replace(sys.executable, "<python>")
            blob = re.sub(r"/[0-9a-f]{64}\.(md|json)", r"/<sha>.\1", blob)
            return hashlib.sha256(blob.encode()).hexdigest()

        return {key: digest(value) for key, value in {
            "sections": await manager._sections(None), "turn": item.turn_text,
            "system": item.system_prompt.text, "tools": tools, "command": command, "signature": signature,
        }.items()}
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
        assert await observe(tmp_path, monkeypatch) == BASELINE[stable]

    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("value", [persona.Persona(), persona.Persona(name="管家", tone="简洁、直接", notes="private-notes")])
def test_the_planner_prompt_keeps_its_main_hash_with_or_without_persona(value):
    previous = persona.current()
    try:
        persona.set_current(value)
        planner = PlannerAgent(task_id="task-baseline", runtime_root=Path("."))
        prompt = planner._system_prompt(
            TaskPlan("p", "g", "Build the project", ""), PlannerSettings("Plan carefully.", 60, 0, 0),
            EMPTY_KNOWLEDGE, Harness("Work carefully.", Layer.BUILTIN))
        assert prompt.sha256 == "9bda3e3a4ac7dd6455a3c527df7be9e10c8c0e8e71a5a40ff34000806c73e858"
    finally:
        persona.set_current(previous)
