"""Resident restart reasons contain only component names and short hashes."""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MessageType
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig, default_definition
from looper.db import agent_setting_layers, managers, role_configs
from library_fixtures import ALPHA_SKILL, DEMO_PLUGIN
from tests.test_manager_new_session import _web
from tests.test_manager_reply import _fake_claude, _resident


@pytest.mark.parametrize(("change", "expected"), [
    ("env", {"env"}),
    ("prompt", {"launch_args", "system_prompt"}),
    ("tools", {"tools"}),
    ("library", {"launch_args", "library"}),
    ("unchanged", set()),
    ("exited", set()),
])
def test_signature_restart_logs_only_changed_components(
    agent_db, enabled_library, tmp_path, monkeypatch, caplog, change, expected,
):
    prompt_sentinel, env_sentinel = "PRIVATE_PROMPT_SENTINEL", "PRIVATE_ENV_SENTINEL"
    config = {"system_prompt": prompt_sentinel}
    layers = [{"scope": "system", "env": [{"key": "LOOPER_TEST_VALUE", "value": env_sentinel}]}]
    monkeypatch.setattr(role_configs, "get_config", AsyncMock(side_effect=lambda _: dict(config)))
    monkeypatch.setattr(agent_setting_layers, "list_layers", AsyncMock(side_effect=lambda *a, **k: layers))
    caplog.set_level(logging.INFO, logger="looper.agents.tiers.manager")
    if change == "library":
        enabled_library(DEMO_PLUGIN, ALPHA_SKILL)

    async def exercise():
        bus = MessageBus(runtime_root=tmp_path)
        definition = (replace(default_definition(), id="scenario-manager", system_prompt=prompt_sentinel)
                      if change == "tools" else default_definition())
        manager = ManagerAgent(config=ManagerConfig(claude_bin=_fake_claude(tmp_path, _resident()), work_dir=tmp_path),
                               runtime_root=tmp_path, definition=definition)
        manager._bus = bus
        tools = [{"name": "test_tool", "description": "first", "inputSchema": {"type": "object"}}]
        if change == "tools":
            monkeypatch.setattr(managers, "get_manager", AsyncMock(return_value=asdict(definition)))
            router = SimpleNamespace(has_planner=lambda _: False)
            manager.ops = ManagerOps(Platform(bus=None, task_router=router), url="http://127.0.0.1:9", token="test-token")
            manager.ops._working_at = manager.ops._clock()
            monkeypatch.setattr(manager.ops, "current_listing", AsyncMock(side_effect=lambda *a, **k: tools))
        pids, signatures = [], []
        try:
            for index in range(3):
                if index == 1:
                    if change == "env":
                        layers[0]["env"][0]["value"] += "_changed"
                    elif change == "prompt":
                        config["system_prompt"] += "_changed"
                    elif change == "tools":
                        tools[0]["description"] = "second"
                    elif change == "library":
                        enabled_library(DEMO_PLUGIN)
                    elif change == "exited":
                        process = manager._claude._process
                        process.terminate()
                        await process.wait()
                        assert manager._claude.stale(manager._process.signature)
                await manager._handle(_web(f"turn {index}"))
                pids.append(manager._claude.pid)
                signatures.append(manager._process.signature)
            queue = bus.get_queue(_web("").source.agent_id)
            replies = []
            while not queue.empty():
                envelope = queue.get_nowait()
                if envelope.message_type is MessageType.AGENT_REPLY:
                    replies.append(envelope.payload)
            assert len(replies) == 3 and all("error" not in reply for reply in replies)
            assert (pids[0] != pids[1]) is bool(expected or change == "exited")
            assert pids[1] == pids[2]
        finally:
            await manager.stop()
            await bus.shutdown()
        return manager.agent_id, signatures

    manager_id, signatures = asyncio.run(exercise())
    records = [record for record in caplog.records if "signature changed:" in record.getMessage()]
    assert len(records) == bool(expected)
    assert prompt_sentinel not in caplog.text and env_sentinel not in caplog.text
    assert str(tmp_path) not in "\n".join(record.getMessage() for record in records)
    assert "test-token" not in caplog.text
    if expected:
        (record,) = records
        assert record.levelno == logging.INFO
        prefix, changes = record.getMessage().split("signature changed: ", 1)
        assert manager_id in prefix and "session=main" in prefix
        pairs = [re.fullmatch(r"([a-z_]+)=([0-9a-f]{8})->([0-9a-f]{8})", part) for part in changes.split(", ")]
        assert all(pairs) and len(pairs) == len(expected) and {pair[1] for pair in pairs} == expected
        assert all(pair[2] != pair[3] for pair in pairs)
        indexes = {"launch_args": 0, "system_prompt": 1, "env": 2, "library": 3, "tools": 6}
        for pair in pairs:
            assert list(pair.group(2, 3)) == [
                hashlib.sha256(json.dumps(signature[indexes[pair[1]]]).encode()).hexdigest()[:8]
                for signature in signatures[:2]]
