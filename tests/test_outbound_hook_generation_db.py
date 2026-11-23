"""An outbound hook trusts the MCP servers its resident process actually loaded."""

from types import SimpleNamespace

import pytest

from manager_mcp_stand_in import manager_endpoint
from looper.agents import catalog, mcp, outbound_hook
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Turn
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import mcp_tool_levels
from tests.db_support import run_with_channel_configs
from tests.fake_claude import fake_claude


@pytest.mark.parametrize("background", [True, False], ids=["deferred", "restarted"])
def test_mcp_trust_changes_only_when_the_resident_loads_the_new_library(tmp_path, monkeypatch, background):
    monkeypatch.setattr(outbound_hook, "_known", False)
    events = [{"type": "system", "subtype": "task_started", "task_id": "example-job"}] if background else []
    binary = fake_claude(tmp_path / "resident", f"EVENTS = {events!r}\n" + '''
import json, sys
for line in sys.stdin:
    print(json.dumps({'type': 'command_lifecycle', 'command_uuid': json.loads(line)['uuid'],
                      'state': 'started'}), flush=True)
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'example-session'}), flush=True)
    for event in EVENTS:
        print(json.dumps(event), flush=True)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                      'session_id': 'example-session', 'result': 'Reply.'}), flush=True)
''')

    async def check(pool):
        old = (await mcp.create({"name": "a__b", "transport": "stdio", "command": "python3"}))["item"]
        new = (await mcp.create({"name": "a", "transport": "stdio", "command": "python3"}))["item"]
        await mcp_tool_levels.put(new["item_id"], ["b__c"])
        installed = await catalog.install({"item_id": old["item_id"], "scope": "global", "roles": ["manager"]})
        await outbound_hook.set_enabled(True)
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=binary, work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path,
                                    turn_file=tmp_path / "turn.json"):
            async def ask():
                await manager._handle(MessageEnvelope.create(
                    source=channel_address("web"), target=manager.agent_address,
                    message_type=MessageType.USER_MESSAGE, payload={"text": "hello"}))

            try:
                await ask()
                pid = manager._claude.pid
                assert pid is not None and manager._process.launch.tool_hook is not None
                await catalog.uninstall(installed["install"]["install_id"])
                await catalog.install({"item_id": new["item_id"], "scope": "global", "roles": ["manager"]})
                await ask()
                assert (manager._claude.pid == pid) is background
                loaded = {item.name for item in manager._process.launch.library.items}
                assert ("a__b" in loaded) is background
                turn = Turn(channel="lark", chat="synthetic-group", message_id="synthetic-turn", group=True)
                manager.ops.begin_turn(turn, "synthetic-binding")
                manager.ops.expect("synthetic-binding", "mcp__a__b__c", {}, call_id="synthetic-call")
                answer = await manager.ops.check_outbound({"tool_name": "mcp__a__b__c", "tool_input": {},
                                                           "tool_use_id": "synthetic-call"})
                assert answer["decision"] == ("block" if background else "allow")
                assert await pool.fetchval("SELECT count(*) FROM outbound_reviews") == int(background)
            finally:
                await manager.stop()
                await bus.shutdown()

    run_with_channel_configs(check, monkeypatch)
