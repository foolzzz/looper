"""The disposable serve lifecycle starts queued explorers and delivers only once across restart."""
import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper.db import agent_spawns, llm_records, projects
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


@pytest.mark.parametrize("subagents", ["off", "on"])
def test_serve_resumes_queued_explorer_and_restart_does_not_redeliver(monkeypatch, subagents):
    # This task's disposable server and CI; the older runner helper names a
    # different local server and assumes a fixed password.
    target = urlsplit(str(DSN))
    if (target.scheme != "postgresql" or target.hostname not in ("127.0.0.1", "localhost")
            or target.port not in (54331, 5432) or target.username != "postgres"
            or unquote(target.path.lstrip("/")) in ("looper", "loopx") or target.query or target.fragment):
        raise ValueError("requires the dedicated disposable PostgreSQL server")
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
            async def configure():
                conn = await asyncpg.connect(url)
                try:
                    project = await projects.create_project(name="Explorer serve", work_dir=str(source), pool=conn)
                    await conn.execute("UPDATE projects SET subagents=$2 WHERE id=$1", project["id"], subagents)
                    return project
                finally:
                    await conn.close()

            project = asyncio.run(configure())

            async def seed():
                conn = await asyncpg.connect(url)
                try:
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
                    spawn = await agent_spawns.get(row["id"], pool=conn) if row is not None else None
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
                    if subagents == "off":
                        assert asyncio.run(readback()) == (None, [], [])
                        continue
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


def test_worker_startup_scan_leaves_delegated_explorer_keepers_for_the_spawn_service(tmp_path, monkeypatch):
    from looper.agents import keeper, kept_dirs
    from looper.db import plans

    directory = tmp_path / keeper.RUNS_DIR / "explorer-owned"
    directory.mkdir(parents=True)
    (directory / keeper.STATE).write_text("{}")
    no_worker_scan = Mock(side_effect=AssertionError("explorer has its own recovery owner"))
    monkeypatch.setattr(kept_dirs.KeeperRecord, "read", no_worker_scan)
    monkeypatch.setattr(kept_dirs, "live", lambda records: {})
    monkeypatch.setattr(plans, "run_holds", AsyncMock(return_value={}))
    scan = asyncio.run(kept_dirs.scan_at_start(tmp_path, epoch=2, reserved_runs={directory.name}))
    assert not scan.candidates and not scan.record_ids
    assert directory.exists()
    no_worker_scan.assert_not_called()


def test_kept_explorer_startup_ownership_comes_from_durable_records(tmp_path):
    from looper.agents import keeper
    from looper.agents.ops.subagents import kept_explorer_records

    pool = SimpleNamespace(fetch=AsyncMock(return_value=[
        {"child_record_id": 3, "agent_id": "explorer-owned",
         "child_process": {"run_dir": str(tmp_path / keeper.RUNS_DIR / "explorer-owned")}},
        {"child_record_id": 4, "agent_id": "explorer-wrong",
         "child_process": {"run_dir": str(tmp_path / "outside" / "explorer-wrong")}},
    ]))
    assert asyncio.run(kept_explorer_records(tmp_path, pool)) == {"explorer-owned": 3}
