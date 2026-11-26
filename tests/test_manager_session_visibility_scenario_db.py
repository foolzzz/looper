"""RFC chat scopes step 0 leaves scenario sessions at the per-chat routing baseline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from looper.agents import session_routing
from looper.agents.library import ItemKind, ScannedItem, SnapshotFile
from looper.agents.ops.service import ManagerOps, Platform, scope_for
from looper.agents.tiers.manager import ManagerAgent
from looper.db import agent_sessions, library, manager_installs, manager_sessions
from tests.db_support import DSN, run_db
from tests.test_manager_session_routing_db import GROUP_A, OTHER, _Resident, _lark, _published
from tests.test_manager_session_routing_default_off import _command
from tests.test_scenario_session_routing_db import MANAGER, _scenario, _to

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
FIXTURE = Path(__file__).parent / "fixtures" / "manager_session_visibility_scenario.json"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


async def _install_skills(pool: Any) -> None:
    # Exercise both inherited and Manager-specific installs through the real loader.
    for name in ("synthetic-global", "synthetic-scenario"):
        content = f"---\nname: {name}\ndescription: Synthetic skill\n---\nUse synthetic data.\n"
        item = ScannedItem(ItemKind.SKILL, name, "synthetic", "", (
            SnapshotFile("SKILL.md", content, hashlib.sha256(content.encode()).hexdigest(), False),))
        async with pool.acquire() as conn:
            row = await library.insert_item(conn, item.row() | {"description": "Synthetic skill"}, origin="ui")
            if name == "synthetic-global":
                await library.insert_install(conn, item_id=row["id"], scope="global", project_id=None,
                                             roles=["manager"], enabled=True)
            else:
                await manager_installs.put_install(MANAGER, row["id"], conn=conn)


@pytest.mark.parametrize("mode", ["off", "per_chat"])
@pytest.mark.parametrize("default_routing", [False, True])
def test_scenario_group_keeps_main_launch_library_prompt_and_tool_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, default_routing: bool,
) -> None:
    async def body(pool: Any) -> None:
        rig = await _scenario(pool, tmp_path, mode)
        monkeypatch.setattr(session_routing, "_routing_enabled", default_routing)
        await pool.execute(
            "INSERT INTO agent_profiles (id, name, runtime_id, model_id, custom_prompt) "
            "SELECT 'synthetic-profile', 'Synthetic profile', runtime_id, model_id, 'Synthetic profile contract.' "
            "FROM agent_profiles WHERE builtin ORDER BY id LIMIT 1")
        await agent_sessions.select_profile(MANAGER, "synthetic-profile")
        await _install_skills(pool)
        ops = ManagerOps(Platform(bus=None, task_router=None, manager=rig.manager),
                         url="http://127.0.0.1:9", token="synthetic", scope=scope_for(rig.manager._definition))
        ops._session_tokens[f"group:channel-lark:{GROUP_A}"] = "synthetic-group-token"
        ops._working_at = ops._clock()
        rig.manager.ops = ops
        captures = []
        prepare = ManagerAgent._prepare_turn

        async def capture(self: Any, *args: Any, **kwargs: Any) -> Any:
            prepared = await prepare(self, *args, **kwargs)
            if prepared.signature is not None:
                captures.append((prepared, await ops.current_listing(prepared.turn),
                                 await self._sections(prepared.envelope),
                                 await ops.call("looper_projects", {"action": "list"}, binding=args[0].binding,
                                                session_key=self._session_now().key)))
            return prepared

        monkeypatch.setattr(ManagerAgent, "_prepare_turn", capture)
        await rig.start()
        resident = rig.group(GROUP_A, _Resident("synthetic-scenario-group")) if mode == "per_chat" else rig.main
        try:
            await rig.reply(await _published(rig, _to(rig, _lark("Synthetic question", GROUP_A, OTHER, group=True))))
            assert len(captures) == len(resident.turns) == 1
            prepared, tools, sections, project_read = captures[0]
            assert project_read["status"] == "ok", project_read
            launch = prepared.launch
            assert launch.access is None
            assert launch.profile.id == "synthetic-profile"
            assert {item.name for item in launch.library.items} == {"synthetic-global", "synthetic-scenario", "looper"}
            assert "Use the synthetic scenario contract." in prepared.system_prompt.text
            assert "Synthetic profile contract." in prepared.system_prompt.text
            assert tools and any(tool["name"] == "looper_task_create" for tool in tools)

            def normalized(value: Any) -> Any:
                text = json.dumps(value, ensure_ascii=False).replace(str(tmp_path), "<root>")
                text = text.replace(sys.executable, "<python>")
                return json.loads(re.sub(r"/[0-9a-f]{64}\.(md|json)", r"/<sha>.\1", text))

            actual = {
                "launch_args": normalized(launch.args()),
                "resident_command": _command(list(prepared.signature[0]), tmp_path),
                # The built-in MCP hash contains the interpreter path; freeze its payload below instead.
                "library": {"items": [item.view() for item in launch.library.items if item.name != "looper"],
                            "plugin_dirs": normalized(launch.library.plugin_dirs),
                            "manager_tools": list(launch.library.manager_tools or ()), "error": launch.library.error},
                "mcp": normalized(json.loads(Path(launch.library.mcp_config).read_text())),
                "prompt": prepared.system_prompt.sha256,
                "tool_order": [tool["name"] for tool in tools],
                "tools": {tool["name"]: _digest(tool) for tool in tools},
                "sections": _digest(sections),
                "input": _digest(resident.turns[0]),
            }
            expected = json.loads(FIXTURE.read_text())["cases"][mode]
            assert actual == expected
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=90)


def test_scenario_legacy_history_and_handoff_are_not_subject_to_the_default_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def body(pool: Any) -> None:
        rig = await _scenario(pool, tmp_path, "per_chat")
        session = rig.manager._router.session(session_routing.SessionKey(
            session_routing.SessionKind.GROUP, "channel-lark", GROUP_A))
        await manager_sessions.save({
            "manager_id": MANAGER, "session_key": session.key, "kind": "group", "channel_id": "channel-lark",
            "chat": GROUP_A, "session_id": "synthetic-legacy-session", "session_totals": {},
            "runtime_type": "claude-code", "profile_id": None, "provider_id": None,
            "clean_start": False, "pending_handoff": "Synthetic scenario handoff",
        })
        await rig.manager._load_session_state(session)
        assert session.session_id == "synthetic-legacy-session"
        assert session.pending_handoff == "Synthetic scenario handoff"
        await rig.manager._write_session_state(session=session)
        stored = await manager_sessions.get(MANAGER, session.key)
        assert stored["session_id"] == session.session_id
        assert stored["pending_handoff"] == session.pending_handoff
        assert stored["context_policy_version"] == 0

    run_db(body, monkeypatch, timeout=30)


def test_per_chat_group_through_native_lark_cli_keeps_scenario_capabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lark_cli_stand_in: Path,
) -> None:
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.chat_routing import load_snapshot
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.lark_transport import CliTransport
    from tests.lark_cli_stand_in import BOT_MENTION, BOT_OPEN_ID
    from tests.test_manager_session_routing_db import LARK

    world_file = tmp_path / "world.json"
    world = json.loads(world_file.read_text())
    world["events"] = [{
        "type": "im.message.receive_v1", "chat_id": GROUP_A, "chat_type": "group",
        "message_id": "om_synthetic_scenario", "message_type": "text", "sender_id": OTHER,
        "sender_type": "user", "content": "@HelperBot Synthetic question", "mentions": [BOT_MENTION],
    }]
    world_file.write_text(json.dumps(world))

    async def body(pool: Any) -> None:
        rig = await _scenario(pool, tmp_path, "per_chat")
        await _install_skills(pool)
        ops = ManagerOps(Platform(bus=None, task_router=None, manager=rig.manager),
                         url="http://127.0.0.1:9", token="synthetic", scope=scope_for(rig.manager._definition))
        ops._working_at = ops._clock()
        rig.manager.ops = ops
        resident = rig.group(GROUP_A, _Resident("synthetic-native-group"))
        snapshot = await load_snapshot(LARK.agent_id)
        transport = CliTransport()
        adapter = LarkChannelAdapter(
            runtime_root=tmp_path, transport=transport, bot_open_id=BOT_OPEN_ID,
            allowlist=SenderAllowlist(LARK.agent_id, frozenset({OTHER})),
            resolve_chat=lambda channel, chat: snapshot.resolve(chat), manager_running=lambda _: True)
        await rig.start()
        adapter._bus = rig.bus
        stream = transport.events()
        incoming = []
        publish = rig.bus.publish

        async def capture(message: Any) -> None:
            if message.source == LARK and message.target == rig.manager.agent_address:
                incoming.append(message)
            await publish(message)

        monkeypatch.setattr(rig.bus, "publish", capture)
        try:
            event = await asyncio.wait_for(anext(stream), 5)
            await adapter._handle_lark_event(event)
            (message,) = incoming
            await rig.reply(message)
            session = rig.session(GROUP_A)
            assert resident.turns and not rig.main.turns
            assert session.process.launch.access is None
            assert {item.name for item in session.process.launch.library.items} == {
                "synthetic-global", "synthetic-scenario", "looper"}
            assert "Use the synthetic scenario contract." in session.process.system_prompt.text
            assert "looper_task_create" in {tool["name"] for tool in await ops.current_listing(session_key=session.key)}
            await adapter.deliver(rig.replies[message.message_id][-1])
            calls = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
            assert any("+messages-reply" in call for call in calls)
        finally:
            await stream.aclose()
            await adapter.stop()
            await rig.close()

    run_db(body, monkeypatch, timeout=60)
