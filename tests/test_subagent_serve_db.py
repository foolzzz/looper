"""The disposable serve lifecycle starts queued explorers and delivers only once across restart."""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper.db import agent_spawns, llm_records, projects
from tests.db_support import DSN, fresh_database_without_recording
from tests.runner_support import require_disposable_database

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_serve_resumes_queued_explorer_and_restart_does_not_redeliver(monkeypatch):
    require_disposable_database(str(DSN), served=False)
    monkeypatch.setattr(runner, "_SERVE", "import sys\nfrom looper.cli import main\n"
        'sys.exit(main(["serve","--host","127.0.0.1","--port",sys.argv[1],"--runtime-root",sys.argv[2]]))\n')
    with tempfile.TemporaryDirectory(prefix="explorer.", dir="/tmp") as directory:
        root = Path(directory)
        runtime, home, bin_dir = (root / name for name in ("runtime", "home", "bin"))
        home.mkdir()
        bin_dir.mkdir()
        source = root / "source"
        source.mkdir()
        (source / "source.py").write_text("local evidence")
        script = f"#!{sys.executable}\n" + r'''
import json, sys
if '--version' in sys.argv:
    print('2.1.329 (test runtime)'); sys.exit(0)
if sys.argv[1:] == ['auth', '--help']:
    print('Usage: claude auth status'); sys.exit(0)
if sys.argv[1:3] == ['auth', 'status']:
    print('{"loggedIn":true}'); sys.exit(0)
if '--output-format' not in sys.argv:
    sys.exit(76)
json.loads(sys.stdin.readline())
text = json.dumps({'summary':'local evidence','findings':[{'claim':'read source',
    'evidence':{'path':'source.py'}}], 'open_questions':[], 'confidence':'high'})
print(json.dumps({'type':'system','subtype':'init','session_id':'standin',
    'tools':['Read','Glob','Grep'],'mcp_servers':[]}), flush=True)
print(json.dumps({'type':'result','subtype':'success','is_error':False,
    'result':text,'total_cost_usd':.02}), flush=True)
'''
        for name in ("claude", "codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "lark-cli"):
            executable = bin_dir / name
            executable.write_text(script)
            executable.chmod(0o755)
        environment = {"CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
            "GROK_HOME": str(home / ".grok"), "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"), "LOOPER_TELEGRAM_BOT_TOKEN": ""}
        with fresh_database_without_recording(str(DSN)) as (url, _):
            async def seed():
                conn = await asyncpg.connect(url)
                try:
                    project = await projects.create_project(name="Explorer serve", work_dir=str(source), pool=conn)
                    await conn.execute("UPDATE projects SET subagents='on' WHERE id=$1", project["id"])
                    await conn.execute("INSERT INTO tasks(id,project_id,requirement,status) "
                        "VALUES('serve-task',$1,'Read source','running')", project["id"])
                    await conn.execute("INSERT INTO task_steps(id,task_id,step_index,step_key,instruction,status,worker_id) "
                        "VALUES('serve-step','serve-task',0,'s1','Read source','dispatched','worker-serve')")
                    parent = await llm_records.start_record(agent_id="worker-serve", task_id="serve-task", pool=conn)
                    return await agent_spawns.create(kind="explorer", task_id="serve-task", step_id="serve-step",
                        parent_record_id=parent, brief={"goal":"Read source", "deliverable":"Evidence", "scope":["."]},
                        inputs={"work_dir":str(source)}, budget_usd=.5, state=agent_spawns.SpawnState.QUEUED,
                        idempotency_key="once", spawner="worker@s1", profile_id="agent-claude-code-sonnet-5-5",
                        delivery="room", pool=conn)
                finally:
                    await conn.close()

            row = None
            async def readback():
                conn = await asyncpg.connect(url)
                try:
                    spawn = await agent_spawns.get(row["id"], pool=conn)
                    records = await conn.fetch("SELECT id FROM llm_records WHERE run_kind='explorer'")
                    messages = await conn.fetch("SELECT kind FROM room_messages WHERE kind='spawn_result'")
                    return spawn, records, messages
                finally:
                    await conn.close()

            for iteration in range(2):
                with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                        notes_root=home / "notes", env=environment, startup_timeout=45,
                        log_path=root / f"serve-{iteration}.log") as api:
                    assert api.post("/api/v1/system/status", {})["status"] == "ok"
                    if row is None:
                        row = asyncio.run(seed())
                    deadline = time.monotonic() + 20
                    while True:
                        spawn, records, messages = asyncio.run(readback())
                        if spawn["delivered_at"] is not None:
                            break
                        assert time.monotonic() < deadline, (spawn["state"], spawn["detail"])
                        time.sleep(.05)
                    assert spawn["state"] == "answered" and spawn["result"]["verified"] is True
                    assert len(records) == len(messages) == 1
