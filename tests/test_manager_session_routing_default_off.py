"""Session routing off (no ``manager.session_routing.enabled`` row): a group's message and the dashboard's run in
one session and one process, as before routing existed (RFC section 10).

The process's command line and its MCP entry are pinned to what ``origin/main`` gives for the same turns, with what
belongs to the machine written as placeholders: this run's directory and interpreter, the ``--settings`` value (it
lists the directory's parents) and the content-named prompt and MCP files (the MCP entry is pinned whole). Runs only
when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from looper.agents import operator_identity, session_routing
from looper.agents.ops.service import ManagerOps, Platform
from tests.db_support import DSN, run_db
from tests.lark_cli_stand_in import OPEN_ID, OTHER
from tests.test_manager_session_routing_db import GROUP_A, _lark, _published, _Rig, _web

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# Computed on origin/main with this test's turns (``_command``, ``_entry``).
COMMAND_SHA256 = "5e061eeb979d5c7fe9a14e358758921fd245a8d209a6da69c3185cb50f319234"
ENTRY = {"command": "<python>", "args": ["-m", "looper.cli", "mcp-server"],
         "env": {"LOOPER_AGENT_URL": "http://127.0.0.1:9", "LOOPER_AGENT_TOKEN": "tok",
                 "LOOPER_AGENT_TURN_FILE": "<root>/.local/manager-turn.json"}}


def _placeholders(text: str, root: Path) -> str:
    return text.replace(str(root), "<root>").replace(sys.executable, "<python>")


def _command(command: list[str], root: Path) -> str:
    parts = [_placeholders(part, root) for part in command]
    parts[parts.index("--settings") + 1] = "<settings>"
    parts = [re.sub(r"/[0-9a-f]{64}\.(md|json)$", r"/<sha>.\1", part) for part in parts]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _entry(command: list[str], root: Path) -> dict[str, Any]:
    path = Path(command[command.index("--mcp-config") + 1])
    assert path.parent == root / ".local" / "mcp"  # the same place as on origin/main
    entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["looper"]
    return json.loads(_placeholders(json.dumps(entry), root))


def test_off_one_session_one_process_and_the_same_command_line_and_mcp_entry(monkeypatch: pytest.MonkeyPatch,
                                                                              tmp_path: Path) -> None:
    async def operator(channel_id: str) -> str | None:
        return OPEN_ID

    monkeypatch.setattr(session_routing, "_routing_enabled", False)
    monkeypatch.setattr(operator_identity, "operator_sender_id", operator)
    monkeypatch.delenv("LOOPER_MCP_TRACE", raising=False)

    async def body(pool: Any) -> None:
        rig = _Rig(tmp_path)
        rig.manager.ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="tok",
                                     turn_file=tmp_path / ".local" / "manager-turn.json")
        rig.manager.ops._working_at = rig.manager.ops._clock()  # the MCP entry was probed
        await rig.start()
        try:
            await rig.reply(await _published(rig, _web("你好")))
            await rig.reply(await _published(rig, _lark("群里问一句", GROUP_A, OTHER, group=True)))
            await rig.reply(await _published(rig, _lark("私聊问一句", "oc_synthetic_dm_other", OTHER, group=False)))
        finally:
            await rig.close()
        assert len(rig.main.turns) == 3 and len(rig.main.starts) == 1 and list(rig.manager._sessions) == ["main"]
        assert await pool.fetchval("SELECT count(*) FROM agent_sessions WHERE agent_id = 'manager'") == 1
        assert await pool.fetchval("SELECT count(*) FROM manager_sessions") == 0
        (command,) = rig.main.starts
        assert _entry(command, tmp_path) == ENTRY
        assert _command(command, tmp_path) == COMMAND_SHA256, _command(command, tmp_path)

    run_db(body, monkeypatch, timeout=90)
