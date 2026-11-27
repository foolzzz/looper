"""Pinned origin/main wire contract: a second input is another turn when the switch is off."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys

import pytest

from looper.agents import chat_settings, session_routing
from dataclasses import replace
from looper.agents.library import library_sha256
from looper.agents.ops.service import ManagerOps, Platform
from tests.db_support import DSN, run_db
from tests.manager_surface_support import assert_main_surfaces
from tests.test_manager_session_routing_db import _Rig, _web

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# Captured before the Manager wiring change from origin/main with this test's deterministic state. The system
# prompt and the tools are checked per section and per tool against tests/fixtures/manager_surfaces_off.json.
BASELINE = {
    "command": "5e061eeb979d5c7fe9a14e358758921fd245a8d209a6da69c3185cb50f319234",
    "prompt": "02bb63fba8b06401b47768209b31db99af74b73feb44eaecdcb8d7659874d407",
    "signature": "990c9350f5706e2d149cabea75d5c98f24e3abf53cf7a030ae564d7758495203",  # the MCP entry gained -I on purpose
}


def test_disabled_wire_contract_matches_main(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_mid_turn_input_enabled", False)
    monkeypatch.setattr(session_routing, "_routing_enabled", False)
    monkeypatch.delenv("LOOPER_MCP_TRACE", raising=False)
    async def body(pool):
        rig = _Rig(tmp_path)
        rig.main.hold = True
        rig.manager.ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="tok",
                                    turn_file=tmp_path / ".local" / "manager-turn.json")
        rig.manager.ops._working_at = rig.manager.ops._clock()
        async def sections(*args, **kwargs): return ["Synthetic Looper state"]
        async def brief(*args, **kwargs): return ""
        monkeypatch.setattr(rig.manager, "_sections", sections)
        monkeypatch.setattr(rig.manager, "_resume_brief", brief)
        prepared = []
        prepare = rig.manager._prepare_turn
        async def capture(*args, **kwargs):
            result = await prepare(*args, **kwargs)
            prepared.append(result)
            return result
        monkeypatch.setattr(rig.manager, "_prepare_turn", capture)
        await rig.start()
        try:
            first, second = _web("first"), _web("second")
            await rig.bus.publish(first)
            await asyncio.wait_for(rig.main.running.wait(), 15)
            process = rig.manager._process
            tools = await rig.manager.ops.current_listing(prepared[0].turn)
            await rig.bus.publish(second)
            await asyncio.sleep(0.1)
            assert len(rig.main.turns) == 1
            rig.main.release.set()
            await rig.reply(second)
            assert len(rig.main.turns) == 2
            assert not rig.manager._mid_turn._turns and not rig.manager._mid_turn._parents
            assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE agent_id='manager'") == 2
            def normalize(value):
                if isinstance(value, str):
                    value = value.replace(str(tmp_path), "<root>").replace(sys.executable, "<python>")
                    return re.sub(r"/[0-9a-f]{64}\.(md|json)", r"/<sha>.\1", value)
                if isinstance(value, dict):
                    return {key: normalize(part) for key, part in value.items()}
                if isinstance(value, (list, tuple)):
                    parts = [normalize(part) for part in value]
                    if "--settings" in parts:
                        parts[parts.index("--settings") + 1] = "<settings>"
                    return parts
                return value
            # Re-hash the MCP entry after normalizing the temporary binding-file path.
            entry = rig.manager.ops.server_config()
            item_sha = hashlib.sha256(json.dumps(normalize(entry), sort_keys=True).encode()).hexdigest()
            normalized_library = library_sha256([replace(item, sha256=item_sha) for item in process.launch.library.items])
            signature = list(process.signature)
            assert signature[1] == process.system_prompt.sha256
            assert signature[3] == process.launch.library.sha256
            signature[1] = "<system prompt, checked per section>"
            signature[3] = normalized_library
            def digest(value):
                return hashlib.sha256(json.dumps(normalize(value), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            observed = {"prompt": digest(rig.main.turns), "command": digest(process.command),
                        "signature": digest(signature)}
            assert observed == BASELINE, observed
            assert_main_surfaces(tools, process.system_prompt.text, tmp_path)
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=45)
