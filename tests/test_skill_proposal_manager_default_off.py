"""The real Manager preparation keeps its fixed-main prompt, command and signature with proposals off."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import claude_cli
from looper.agents.agent_settings import resolve
from looper.agents.library import EMPTY_LIBRARY
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Turn
from looper.agents.ops.skill_proposals import TOOL
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.tiers import manager as module

BASELINE = Path(__file__).parent / "fixtures" / "skill_proposals_manager_off.json"


async def prepare(root: Path, monkeypatch: pytest.MonkeyPatch, listing: list[dict[str, Any]], *,
                  method: Any = None) -> Any:
    manager = module.ManagerAgent(config=module.ManagerConfig(work_dir=root), runtime_root=root)
    manager._session_loaded = True
    settings = replace(resolve("manager", {}, None), profile=builtin_resolved_profile())
    for name, result in (("_load_db_config", None), ("_load_agent_settings", settings), ("_read_templates", None),
                         ("_library_with_server", EMPTY_LIBRARY), ("_build_user_message", "hello")):
        monkeypatch.setattr(manager, name, AsyncMock(return_value=result))
    monkeypatch.setattr(module, "load_installed", AsyncMock(return_value=EMPTY_LIBRARY))
    monkeypatch.setattr(module.outbound_hook, "enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(claude_cli, "_outside_memory", lambda *args, **kwargs: ["/synthetic/CLAUDE.md"])

    async def attach(launch: Any, **kwargs: Any) -> Any:
        return launch

    monkeypatch.setattr(module.llmrec, "attach", attach)
    manager.ops = SimpleNamespace(current_listing=AsyncMock(return_value=listing))
    envelope = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
        message_type=MessageType.USER_MESSAGE, payload={"text": "hello", "channel_context": {"session_id": "synthetic"}})
    ask = module.Ask("hello", turn=Turn("web", "synthetic", message_id=envelope.message_id))
    return await (method or module.ManagerAgent._prepare_turn)(manager, manager._detached_token(envelope),
                                                              envelope, ask=ask)


def fingerprint(prepared: Any, root: Path) -> dict[str, str]:
    command = list(prepared.signature[0])
    command[0] = "<claude>"
    index = command.index("--settings") + 1
    settings = command[index]
    command[index] = json.loads(settings) if settings.startswith("{") else json.loads(Path(settings).read_text())
    assert prepared.signature[1] == hashlib.sha256(prepared.system_prompt.text.encode()).hexdigest()
    signature = [command, prepared.system_prompt.text, *prepared.signature[2:]]

    def digest(value: Any) -> str:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False).replace(str(root), "<root>")
        text = re.sub(r"/[0-9a-f]{64}\.(md|json)", r"/<sha>.\1", text)
        return hashlib.sha256(text.encode()).hexdigest()

    return {key: digest(value) for key, value in {"system": prepared.system_prompt.text,
            "command": command, "signature": signature}.items()}


def test_off_preparation_matches_fixed_main_and_on_restarts_for_the_listing(monkeypatch, tmp_path):
    async def check() -> None:
        baseline = json.loads(BASELINE.read_text())
        off = await prepare(tmp_path, monkeypatch, baseline["listing"])
        assert fingerprint(off, tmp_path) == baseline["prepared"]
        on = await prepare(tmp_path, monkeypatch, [*baseline["listing"], TOOL.listing()])
        assert on.system_prompt.text == off.system_prompt.text
        assert on.signature[:-1] == off.signature
        assert on.signature[-1] == hashlib.sha256(json.dumps(
            [*baseline["listing"], TOOL.listing()], sort_keys=True).encode()).hexdigest()

    asyncio.run(check())
