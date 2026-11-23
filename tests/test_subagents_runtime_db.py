"""Explorer process lifecycle and persistent run-tree records with a real CLI stand-in."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper import child_env
from looper.agents import proc_tree, spawn, subagents
from looper.agents.agent_settings import AgentSettings, AgentType
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.system_prompt import SystemPrompt
from looper.db import agent_spawns, llm_records
from tests.db_support import DSN, run_db
from tests.fake_claude import fake_claude

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# The stand-in reads the supplied working directory and speaks Claude's stream
# protocol. A release file controls completion without relying on fixed sleeps.
_EXPLORER_CLI = r'''
import json, os, sys, time
from pathlib import Path
turn = json.loads(sys.stdin.readline())["message"]["content"]
selected_model = sys.argv[sys.argv.index("--model") + 1]
model = {"opus":"claude-opus-5-5", "sonnet":"claude-sonnet-5-5"}.get(selected_model, selected_model)
source = Path("source.py").read_text()
flag = next(flag for flag in ("--system-prompt-file", "--append-system-prompt-file") if flag in sys.argv)
seen = {"pid": os.getpid(), "cwd": os.getcwd(), "argv": sys.argv[1:], "turn": turn,
        "source": source, "system": Path(sys.argv[sys.argv.index(flag) + 1]).read_text()}
Path(os.environ["EXPLORER_SEEN"]).write_text(json.dumps(seen))
def emit(value):
    print(json.dumps(value), flush=True)
emit({"type":"system", "subtype":"init", "session_id":"explorer-test", "model":model,
      "tools":["Read", "Glob", "Grep"], "mcp_servers":[]})
if os.environ.get("EXPLORER_MODE") == "gate":
    deadline = time.monotonic() + 20
    while not Path(os.environ["EXPLORER_RELEASE"]).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test did not release the explorer")
        time.sleep(.01)
report = {"summary":source.strip(), "findings":[{"claim":"Source was read", "evidence":{"path":"source.py"}}],
          "open_questions":[], "confidence":"high"}
text = json.dumps(report)
usage = {"input_tokens":40,"output_tokens":20,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}
emit({"type":"assistant", "session_id":"explorer-test", "message":{"id":"explorer-message", "role":"assistant",
      "model":model, "content":[{"type":"text", "text":text}], "usage":usage}})
emit({"type":"result", "subtype":"success", "is_error":False, "result":text,
      "session_id":"explorer-test", "stop_reason":"end_turn", "total_cost_usd":.02,
      "usage":usage, "modelUsage":{model:{"costUSD":.02}}})
'''


@pytest.fixture(autouse=True)
def _environment(monkeypatch, tmp_path):
    monkeypatch.setenv("EXPLORER_SEEN", str(tmp_path / "seen.json"))
    monkeypatch.setenv("EXPLORER_RELEASE", str(tmp_path / "release"))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed |
                        {"EXPLORER_SEEN", "EXPLORER_RELEASE", "EXPLORER_MODE"})


async def _parent(pool, root: Path):
    from looper.db import projects

    directory = root / "source"
    directory.mkdir()
    (directory / "source.py").write_text("local source evidence\n")
    project = await projects.create_project(name="Explorer lifecycle", pool=pool)
    await pool.execute("UPDATE projects SET subagents='on', subagent_task_budget_usd=.5 WHERE id=$1", project["id"])
    await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) "
                       "VALUES('explorer-task',$1,'Read local source','running')", project["id"])
    await pool.execute("INSERT INTO task_steps(id,task_id,step_index,instruction) "
                       "VALUES('explorer-step','explorer-task',0,'Locate source evidence')")
    record = await llm_records.start_record(agent_id="worker-explorer-parent", task_id="explorer-task", pool=pool)
    parent = spawn.SpawnParent("explorer-task", "explorer-step", record)
    return directory, parent, AgentSettings(profile=builtin_resolved_profile(), max_budget_usd=.5)


async def _wait_for_file(path: Path) -> None:
    async with asyncio.timeout(15):
        while not path.exists():
            await asyncio.sleep(.01)


def test_explorer_child_records_identity_parent_step_and_local_reads(tmp_path, monkeypatch):
    async def check(pool):
        directory, parent, settings = await _parent(pool, tmp_path)
        child = spawn.ChildRun(spawn.SpawnKind.EXPLORER, SystemPrompt("EXPLORER CONTRACT"),
                               "Find source evidence", timeout_s=15, max_turns=40, role=AgentType.EXPLORER)
        callbacks = []
        identity = {}

        async def recorded(record_id):
            callbacks.append("record")
            row = await llm_records.get_record(record_id, pool=pool)
            assert row["status"] == "running"

        async def started(value):
            callbacks.append("process")
            identity.update(value)
            assert await asyncio.to_thread(proc_tree._start_time, value["pid"]) == value["started"]

        result = await spawn.run_child(child, parent=parent, settings=settings, cap_usd=.5,
            runtime_root=tmp_path / "runtime", cwd=directory,
            claude_bin=fake_claude(tmp_path / "claude", _EXPLORER_CLI),
            on_record=recorded, on_process=started)
        assert callbacks == ["record", "process"]
        assert result.failure is None and result.cost_usd == pytest.approx(.02)
        assert json.loads(result.output)["summary"] == "local source evidence"
        record = await llm_records.get_record(result.record_id, pool=pool)
        tree = await pool.fetchrow("SELECT run_kind,parent_record_id FROM llm_records WHERE id=$1", result.record_id)
        assert tree["run_kind"] == "explorer" and tree["parent_record_id"] == parent.record_id
        assert record["task_id"] == parent.task_id and record["metadata"]["step_id"] == parent.step_id
        assert record["status"] == "completed" and record["cost_usd"] == pytest.approx(.02)
        seen = json.loads((tmp_path / "seen.json").read_text())
        assert Path(seen["cwd"]).resolve() == directory.resolve()
        assert seen["source"] == "local source evidence\n" and seen["system"] == "EXPLORER CONTRACT"
        argv = seen["argv"]
        assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
        assert "--strict-mcp-config" in argv
        assert "--mcp-config" not in argv and "--plugin-dir" not in argv
        assert await asyncio.to_thread(proc_tree._start_time, identity["pid"]) != identity["started"]
        assert (directory / "source.py").read_text() == "local source evidence\n"
        assert not (tmp_path / "runtime/.local/explorer-homes" / child.agent_id).exists()

    run_db(check, monkeypatch, timeout=45)


def test_timed_out_explorer_reaps_real_process_and_preserves_parent_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPLORER_MODE", "gate")

    async def check(pool):
        directory, parent, settings = await _parent(pool, tmp_path)
        child = spawn.ChildRun(spawn.SpawnKind.EXPLORER, SystemPrompt("Read only"),
                               "Read source", timeout_s=1, role=AgentType.EXPLORER)
        identity = {}

        async def started(value):
            identity.update(value)

        result = await spawn.run_child(child, parent=parent, settings=settings, cap_usd=.5,
            runtime_root=tmp_path / "runtime", cwd=directory,
            claude_bin=fake_claude(tmp_path / "claude", _EXPLORER_CLI), on_process=started)
        assert result.failure is spawn.ChildFailure.TIMEOUT and result.output is None
        assert identity and await asyncio.to_thread(proc_tree._start_time, identity["pid"]) != identity["started"]
        record = await llm_records.get_record(result.record_id, pool=pool)
        assert record["status"] != "running" and record["finished_at"] is not None
        assert directory.is_dir() and (directory / "source.py").read_text() == "local source evidence\n"
        assert not (tmp_path / "runtime/.local/explorer-homes" / child.agent_id).exists()

    run_db(check, monkeypatch, timeout=45)


def test_service_actual_process_start_failure_releases_entire_reservation(tmp_path, monkeypatch):
    async def check(pool):
        directory, parent, settings = await _parent(pool, tmp_path)
        monkeypatch.setattr(subagents, "load_agent_settings", AsyncMock(return_value=settings))
        service = subagents.SpawnService(tmp_path / "runtime", pool=pool,
                                         claude_bin=str(tmp_path / "missing-claude"))
        caller = subagents.Spawner(parent, "worker", directory)
        brief = {"goal":"Find source", "deliverable":"Evidence", "scope":["source.py"]}
        row = await service.admit(caller, brief, key="start-failure")
        result = await service.run(row["id"])
        assert result["state"] == "failed" and result["failure_cause"] == "start_failure"
        assert result["spent_usd"] == 0
        record = await llm_records.get_record(result["child_record_id"], pool=pool)
        assert record["status"] != "running"
        assert await pool.fetchval("SELECT parent_record_id FROM llm_records WHERE id=$1",
                                   result["child_record_id"]) == parent.record_id
        retry = await service.admit(caller, brief, key="after-failure")
        assert retry["budget_usd"] == .5
        assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE run_kind='explorer'") == 1
        assert not (tmp_path / "seen.json").exists()

    run_db(check, monkeypatch, timeout=45)


@pytest.mark.parametrize("winner", ["cancel", "finish"])
def test_service_cancel_and_finish_cas_race_with_real_child(tmp_path, monkeypatch, winner):
    monkeypatch.setenv("EXPLORER_MODE", "gate")

    async def check(pool):
        directory, parent, settings = await _parent(pool, tmp_path)
        monkeypatch.setattr(subagents, "load_agent_settings", AsyncMock(return_value=settings))
        service = subagents.SpawnService(tmp_path / "runtime", pool=pool,
            claude_bin=fake_claude(tmp_path / "claude", _EXPLORER_CLI))
        caller = subagents.Spawner(parent, "worker", directory)
        brief = {"goal":"Find source", "deliverable":"Evidence", "scope":["source.py"]}
        row = await service.admit(caller, brief, key="race")
        entered, proceed = asyncio.Event(), asyncio.Event()
        if winner == "finish":
            original = service.finish

            async def held_finish(spawn_id):
                entered.set()
                await proceed.wait()
                return await original(spawn_id)

            monkeypatch.setattr(service, "finish", held_finish)
        else:
            original = service._end_process

            async def held_end(value):
                entered.set()
                await proceed.wait()
                return await original(value)

            monkeypatch.setattr(service, "_end_process", held_end)
        runner = asyncio.create_task(service.run(row["id"]))
        cancellation = None
        try:
            await _wait_for_file(tmp_path / "seen.json")
            asking = await agent_spawns.get(row["id"], pool=pool)
            assert asking["state"] == "asking" and asking["child_process"]["pid"] > 0
            if winner == "cancel":
                cancellation = asyncio.create_task(service.cancel(row["id"]))
                await asyncio.wait_for(entered.wait(), 10)
                assert (await agent_spawns.get(row["id"], pool=pool))["state"] == "cancelling"
                (tmp_path / "release").touch()
                await asyncio.wait_for(runner, 15)
                proceed.set()
                await asyncio.wait_for(cancellation, 15)
            else:
                (tmp_path / "release").touch()
                await asyncio.wait_for(entered.wait(), 15)
                cancelled = await service.cancel(row["id"])
                assert cancelled["state"] == "finishing"
                proceed.set()
                await asyncio.wait_for(runner, 15)
            result = await agent_spawns.get(row["id"], pool=pool)
            assert result["state"] == ("cancelled" if winner == "cancel" else "answered")
            assert result["spent_usd"] == pytest.approx(.02)
            if winner == "cancel":
                assert result["cancel_reason"] == "requested" and "verified" not in result["result"]
            else:
                assert result["result"]["verified"] is True and result["cancel_reason"] is None
            assert result["finished_at"] is not None
            assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE run_kind='explorer'") == 1
            record = await llm_records.get_record(result["child_record_id"], pool=pool)
            assert record["status"] != "running"
            pid = asking["child_process"]["pid"]
            assert await asyncio.to_thread(proc_tree._start_time, pid) != asking["child_process"]["started"]
        finally:
            proceed.set()
            (tmp_path / "release").touch()
            for task in (runner, cancellation):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (runner, cancellation) if task is not None), return_exceptions=True)

    run_db(check, monkeypatch, timeout=60)


def test_missing_child_record_prevents_process_launch(tmp_path, monkeypatch):
    from looper.agents.claude_cli import ClaudeRunRecorder
    monkeypatch.setattr(ClaudeRunRecorder, 'start', AsyncMock())
    no_process = AsyncMock(side_effect=AssertionError('must persist a child record before launch'))
    monkeypatch.setattr(proc_tree, 'spawn', no_process)

    async def check(pool):
        directory, parent, settings = await _parent(pool, tmp_path)
        child = spawn.ChildRun(spawn.SpawnKind.EXPLORER, SystemPrompt('Read only'),
                               'Read source', timeout_s=1, role=AgentType.EXPLORER)
        result = await spawn.run_child(child, parent=parent, settings=settings, cap_usd=.5,
            runtime_root=tmp_path / 'runtime', cwd=directory, claude_bin='never-start')
        assert result.failure is spawn.ChildFailure.UNAVAILABLE and result.record_id is None
        no_process.assert_not_awaited()
    run_db(check, monkeypatch)


@pytest.mark.parametrize('operation', ['recover', 'cascade'])
def test_bulk_cleanup_preserves_unselected_running_explorer_home(tmp_path, monkeypatch, operation):
    monkeypatch.setenv('EXPLORER_MODE', 'gate')

    async def check(pool):
        directory, parent, settings = await _parent(pool, tmp_path)
        await pool.execute("UPDATE projects SET subagent_task_budget_usd=1")
        monkeypatch.setattr(subagents, 'load_agent_settings', AsyncMock(return_value=settings))
        service = subagents.SpawnService(tmp_path / 'runtime', pool=pool,
            claude_bin=fake_claude(tmp_path / 'claude', _EXPLORER_CLI))
        caller = subagents.Spawner(parent, 'worker', directory)
        brief = {'goal': 'Find source', 'deliverable': 'Evidence', 'scope': ['source.py']}
        active = await service.admit(caller, brief, key='live-home')
        other_record = await llm_records.start_record(agent_id='worker-other-parent', task_id=parent.task_id, pool=pool)
        other = subagents.Spawner(spawn.SpawnParent(parent.task_id, parent.step_id, other_record), 'worker', directory)
        queued = await service.admit(other, brief, key='other-parent')
        running = asyncio.create_task(service.run(active['id']))
        try:
            await _wait_for_file(tmp_path / 'seen.json')
            homes = list((service.runtime_root / '.local' / 'explorer-homes').iterdir())
            assert len(homes) == 1 and homes[0].is_dir()
            assert not service._handles  # the ordinary, non-keeper path owns a home too
            if operation == 'recover':
                await service.recover()
            else:
                await service.cascade(parent_record_id=other_record, reason=agent_spawns.CancelReason.PARENT_ENDED)
                assert (await agent_spawns.get(queued['id'], pool=pool))['state'] == 'cancelled'
            assert (await agent_spawns.get(active['id'], pool=pool))['state'] == 'asking'
            assert homes[0].is_dir()
            (tmp_path / 'release').touch()
            assert (await asyncio.wait_for(running, 15))['state'] == 'answered'
            assert not homes[0].exists()
        finally:
            (tmp_path / 'release').touch()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    run_db(check, monkeypatch, timeout=45)
