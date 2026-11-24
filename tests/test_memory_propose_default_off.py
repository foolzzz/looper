"""Real Manager preparation preserves frozen surfaces whenever memory proposals are unavailable."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper.agents import claude_cli, profile_registry
from looper.agents.bus import MessageBus
from looper.agents.claude_cli import ResidentTurn
from looper.agents.managers_api import ManagerDefinition
from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from looper.agents.ops import memory_propose
from looper.agents.ops.service import ManagerOps, Platform, scope_for
from looper.agents.run_usage import Usage
from looper.agents.serve import seed_channel_configs
from looper.agents.tiers.manager import DEFAULT_SYSTEM_PROMPT, ManagerAgent, ManagerConfig
from looper.db import managers
from tests.db_support import DSN, run_db
from tests.manager_surface_support import BASELINE, assert_main_surfaces, normalized, sections
from tests.memory_propose_support import setup

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


class _Resident:
    """Only the runtime is a stand-in: preparation, settings, prompts and listings are real."""

    pid = None
    shares_bridge = True
    can_interrupt = True

    def __init__(self):
        self.started, self.command, self.totals = False, [], Usage()

    def stale(self, signature):
        return False

    async def start(self, command, **kwargs):
        self.started, self.command = True, command

    async def stop(self, **kwargs):
        self.started = False

    async def try_stop_if_idle(self):
        self.started = False
        return True

    async def run_turn(self, text, on_event, **kwargs):
        await on_event({"type": "system", "subtype": "init", "session_id": "sess-main"})
        return ResidentTurn({"type": "result", "subtype": "success", "is_error": False,
                             "result": "Synthetic reply", "session_id": "sess-main"})


def _envelope(case, owner="manager"):
    channel, context = "web", {"session_id": "persona-chat"}
    if case in {"group", "other-sender"}:
        channel = "lark"
        context = {"chat_id": "oc_synthetic_group" if case == "group" else "ou_synthetic_other",
                   "chat_type": "group" if case == "group" else "p2p",
                   "sender_id": "ou_synthetic_operator" if case == "group" else "ou_synthetic_other",
                   "message_id": "om_synthetic_turn"}
    elif case == "api-client":
        context["client"] = {"id": "client-synthetic", "name": "Synthetic client"}
    return MessageEnvelope.create(
        source=channel_address(channel), target=manager_address(owner), message_type=MessageType.USER_MESSAGE,
        payload={"text": "hello", "channel_context": context})


async def _observe(root, monkeypatch, *, case="default", definition=None):
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
    monkeypatch.setattr(claude_cli, "_outside_memory", lambda *args, **kwargs: ["/synthetic/CLAUDE.md"])
    monkeypatch.delenv("LOOPER_MCP_TRACE", raising=False)
    bus = MessageBus(runtime_root=root)
    manager = ManagerAgent(config=ManagerConfig(claude_bin="claude", work_dir=root), runtime_root=root,
                           definition=definition)
    manager._bus, manager._claude = bus, _Resident()
    manager.ops = ManagerOps(Platform(bus=bus, task_router=SimpleNamespace(has_planner=lambda _: False)),
                             url="http://127.0.0.1:9", token="tok", scope=scope_for(definition) if definition else None,
                             turn_file=root / ".local" / "manager-turn.json")
    manager.ops._working_at = manager.ops._clock()
    prepared = []
    prepare = manager._prepare_turn

    async def capture(*args, **kwargs):
        result = await prepare(*args, **kwargs)
        prepared.append(result)
        return result

    monkeypatch.setattr(manager, "_prepare_turn", capture)
    try:
        await manager._handle(_envelope(case, manager.agent_id))
        item, = prepared
        assert item.signature is not None
        tools = await manager.ops.current_listing(item.turn)
        command = list(item.signature[0])
        command[0] = "<claude>"
        index = command.index("--settings") + 1
        raw = command[index]
        if raw.startswith("{"):
            command[index] = json.loads(raw)
        else:
            settings_file = Path(raw)
            content = settings_file.read_text()
            assert settings_file.stem == hashlib.sha256(content.encode()).hexdigest()
            command[index] = json.loads(content)
        assert item.signature[1] == hashlib.sha256(item.system_prompt.text.encode()).hexdigest()
        config = json.loads(Path(item.launch.library.mcp_config).read_text())
        library_item, = item.launch.library.items
        assert item.signature[3] == item.launch.library.sha256
        assert library_item.sha256 == hashlib.sha256(
            json.dumps(config["mcpServers"]["looper"], sort_keys=True).encode()).hexdigest()
        signature = [command, "<system prompt, checked per section>", item.signature[2], config,
                     *item.signature[4:]]
        return {"tools": tools, "system": item.system_prompt.text, "signature": signature,
                "command": command, "turn": item.turn_text}
    finally:
        await manager.stop()
        await bus.shutdown()


def _assert_unavailable(view, root, *, scoped=False):
    if not scoped:
        assert_main_surfaces(view["tools"], view["system"], root)
    assert memory_propose.TOOL_NAME not in [tool["name"] for tool in view["tools"]]
    assert "## Memory proposals" not in view["system"]
    assert len(view["signature"]) == (7 if scoped else 6)
    if scoped:
        assert view["signature"][-1] == hashlib.sha256(json.dumps(view["tools"], sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("case", ["unset", "disabled", "source-disabled", "no-target", "scenario",
                                  "group", "other-sender", "api-client"])
def test_off_keeps_the_frozen_tools_prompt_command_and_signature(monkeypatch, tmp_path, case):
    async def check(pool):
        definition = None
        if case == "scenario":
            row = await managers.insert_manager({"id": "manager-synthetic", "name": "Synthetic",
                                                 "system_prompt": DEFAULT_SYSTEM_PROMPT, "task_scope": "all"},
                                                conn=pool)
            definition = ManagerDefinition.from_row(row)
        if case in {"group", "other-sender"}:
            await seed_channel_configs(pool)
            await pool.execute("UPDATE channel_configs SET config=$1 WHERE channel_id='channel-lark'", json.dumps({
                "operator_sender_id": "ou_synthetic_operator",
                "allowed_open_ids": ["ou_synthetic_operator", "ou_synthetic_other"]}))
        root = tmp_path / "runtime"
        baseline = await _observe(root, monkeypatch, case=case, definition=definition)
        _assert_unavailable(baseline, root, scoped=case == "scenario")
        if case != "unset":
            await setup(pool, tmp_path)
            raw = json.loads(await pool.fetchval("SELECT value FROM system_settings WHERE key='memory_write'"))
            if case == "disabled":
                raw["enabled"] = False
            elif case == "source-disabled":
                raw["sources"]["manager"] = False
            elif case == "no-target":
                raw["targets"] = {}
            await pool.execute("UPDATE system_settings SET value=$1 WHERE key='memory_write'", json.dumps(raw))
        current = await _observe(root, monkeypatch, case=case, definition=definition)
        _assert_unavailable(current, root, scoped=case == "scenario")
        # Compare complete surfaces, including native command and the unchanged listing-hash decision.
        assert normalized(current, root) == normalized(baseline, root)

    run_db(check, monkeypatch, timeout=60)


def test_on_adds_exactly_one_tool_and_one_section_after_the_frozen_ones(monkeypatch, tmp_path):
    async def check(pool):
        await setup(pool, tmp_path)
        view = await _observe(tmp_path, monkeypatch)
        tools, system = view["tools"], view["system"]
        assert_main_surfaces(tools, system, tmp_path)
        assert [tool["name"] for tool in tools] == [*BASELINE["tools"]["order"], memory_propose.TOOL_NAME]
        assert len(sections(system)) == len(BASELINE["system"]["default"]["order"]) + 1
        assert sections(system)[-1] == ("## Memory proposals (looper_memory_propose)", memory_propose.MEMORY_CONTRACT)
        assert len(view["signature"]) == 7
        assert view["signature"][-1] == hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()

    run_db(check, monkeypatch, timeout=40)
