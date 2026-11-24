"""The real child execution seam records only private digests and typed events."""

import asyncio
import json
from pathlib import Path

import pytest

from looper.agents import agent_settings, memory_retro, memory_retro_role, model_call
from looper.agents.spawn import SpawnParent
from tests.db_support import DSN, run_db
from tests.fake_claude import fake_claude

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_private_retro_records_no_journal_or_response_text(monkeypatch, tmp_path):
    seen = tmp_path / "seen.json"
    output = '{"lessons":[],"note":"synthetic private retrospective response"}'
    script = f"SEEN = {str(seen)!r}\nOUTPUT = {output!r}\n" + r'''
import json, os, pathlib, sys
sys.stdin.readline()
pathlib.Path(SEEN).write_text(json.dumps({"argv": sys.argv, "cwd": os.getcwd(), "files": os.listdir(".")}))
model = sys.argv[sys.argv.index("--model") + 1]
print(json.dumps({"type": "system", "subtype": "init", "session_id": "synthetic", "model": model,
                  "tools": [], "mcp_servers": []}), flush=True)
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                  {"type": "text", "text": OUTPUT}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": OUTPUT,
                  "session_id": "synthetic", "total_cost_usd": .004,
                  "modelUsage": {model: {"costUSD": .004}}}), flush=True)
'''
    cli = fake_claude(tmp_path / "claude", script)

    async def check(pool):
        await agent_settings.update_role("memory_retro", {"profile_id": "agent-claude-code-sonnet-5-5"})
        resolved = await agent_settings.load_agent_settings(agent_settings.AgentType.MEMORY_RETRO)
        value = memory_retro.RetroInput("synthetic-task", "synthetic-project",
            "synthetic private project journal", {"J1": 1}, {})
        done = await memory_retro_role.run(value, parent=SpawnParent(None), runtime_root=tmp_path / "runtime",
            claude_bin=cli, agent_settings=resolved, role_config={})
        assert isinstance(done, model_call.CallOk), done
        launch = json.loads(seen.read_text())
        assert launch["files"] == [] and not Path(launch["cwd"]).exists()
        argv = launch["argv"]
        assert argv[argv.index("--tools") + 1] == "" and "--plugin-dir" not in argv
        row = dict(await pool.fetchrow("SELECT * FROM llm_records WHERE id=$1", done.record_id))
        assert row["run_kind"] == "memory_retro" and row["output_text"] is None
        assert json.loads(row["input_text"])["chars"] == len(value.prompt)
        events = await pool.fetch("SELECT event FROM llm_record_events WHERE record_id=$1", done.record_id)
        stored = json.dumps([row, [dict(event) for event in events]], default=str)
        assert value.prompt not in stored and "synthetic private retrospective response" not in stored

    run_db(check, monkeypatch)
