"""A running task survives actual serve process exits and service reinstallation.

Only systemctl is a stand-in: its stop sends a real signal to the MainPID.
The server, runner, reverse tools, records and PostgreSQL are real.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import asyncpg
import pytest

from evals.prompt_regression.runner import Api, free_port, fresh_database
from looper import ops_config
from looper.agents import execution_host, operator_auth, proc_tree, service
from tests.runner_support import ROOT, require_disposable_database


def eventually(read, accept=bool, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if accept(value):
            return value
        assert time.monotonic() < deadline, value
        time.sleep(0.05)


def query(url, sql, *args, execute=False):
    async def run():
        connection = await asyncpg.connect(url, timeout=5, command_timeout=10)
        try:
            return (await connection.execute(sql, *args) if execute else
                    [dict(row) for row in await connection.fetch(sql, *args)])
        finally:
            await connection.close()
    return asyncio.run(asyncio.wait_for(run(), 15))


@pytest.mark.parametrize("action", ["restart", "reinstall", "stop", "kill"])
def test_running_task_keeps_runner_trace_tools_and_charge_across_service_exit(tmp_path, action):
    admin = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("requires disposable LOOPER_TEST_DATABASE_URL")
    require_disposable_database(admin, served=False)
    with fresh_database(admin) as (url, _):
        root, home, work = (tmp_path / name for name in ("runtime", "home", "work"))
        for directory in (root, home, work):
            directory.mkdir()
        port = free_port()
        assert port != 8767
        ops = ops_config.OpsConfig(runtime_root=root, path=root / ops_config.FILE, database_url=url,
            port=port, operator_notes_root=str(home / "notes"))
        ops_config.write_private(ops.path, ops_config.render(ops))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(home / "codex"),
            "GROK_HOME": str(home / "grok"), "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": os.pathsep.join((str(ROOT / "tests/runtime_root_guard"), str(ROOT))),
            **{key: value for key, value in os.environ.items() if key.startswith("PYTEST_LOOPER_")}}
        processes = []
        log = (tmp_path / "frontend.log").open("w")

        def systemctl(argv):
            assert argv[:2] == ["systemctl", "--user"]
            args = argv[2:]
            process = processes[-1] if processes else None
            if args[0] == "stop":
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
                    assert process.returncode == 0
            elif args[:2] == ["enable", "--now"]:
                processes.append(subprocess.Popen([sys.executable, "-u",
                    str(ROOT / "tests/service_restart_support.py"), "frontend", str(root), str(work)],
                    cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True))
            elif args[0] == "show":
                active = process is not None and process.poll() is None
                return subprocess.CompletedProcess(argv, 0, "LoadState=loaded\n"
                    f"ActiveState={'active' if active else 'inactive'}\nSubState=running\n"
                    f"MainPID={process.pid if active else 0}\nExecMainCode=0\nExecMainStatus=0\n", "")
            else:
                assert args == ["daemon-reload"]
            return subprocess.CompletedProcess(argv, 0, "", "")

        host = service.Host(system="Linux", home=home, uid=os.getuid(), environ=env,
            executable=sys.executable, cwd=work, run=systemctl)
        owned = {}
        try:
            service.install(host, ops, timeout=40)
            state = execution_host.read_host(root)
            owned[state["pid"]] = state["started"]
            api = Api(f"http://127.0.0.1:{port}", timeout=10)
            api.token = operator_auth.read_token(root)
            query(url, """INSERT INTO agent_runtimes (id,name,type,version,capabilities)
                VALUES ('rt-restart','Restart runner','test-runner','0.1.0',ARRAY['resident_session','resume'])""",
                execute=True)
            provider = api.post("/api/v1/provider_accounts/create", {"name": "Synthetic account", "vendor_type": "custom",
                "api_key": "synthetic-restart-key", "endpoints": [{"protocol": "openai-responses",
                "base_url": "https://runner.example.invalid"}]})["result"]["provider"]
            model = api.post("/api/v1/ops/models/create", {"provider": provider["provider_id"], "name": "fake-model",
                "protocol": "openai-responses", "context_window": 100000, "input_usd_per_mtok": 1,
                "output_usd_per_mtok": 2, "cache_read_usd_per_mtok": 3,
                "cache_write_usd_per_mtok": 4})["result"]["model"]
            profile = api.post("/api/v1/profiles/create", {"name": "Restart profile", "runtime_id": "rt-restart",
                "model_id": model["model_id"]})["result"]["profile"]["profile_id"]
            api.post("/api/v1/agent_settings/update_role", {"role": "planner",
                "values": {"profile_id": profile, "run_budget_usd": 1}})
            task = api.post("/api/v1/tasks/create", {"requirement": "List the task profiles", "workspace_mode": "in_place",
                "project": {"name": "Restart fixture", "work_dir": str(work)}})["result"]["task_id"]
            api.post("/api/v1/ops/task_control/start", {"task_id": task})
            eventually(lambda: (root / "before-tools").exists())
            host_log = root / "logs/serve.log"
            runner = json.loads(next(line.removeprefix("test runner=") for line in host_log.read_text().splitlines()
                                     if line.startswith("test runner=")))
            owned[runner["pid"]] = runner["started"]
            record = json.loads((root / proc_tree.RECORDS_DIR / f"{runner['pid']}.json").read_text())
            assert record["server_pid"] == state["pid"] != processes[-1].pid
            (before,) = query(url, "SELECT * FROM llm_records WHERE task_id=$1", task)
            assert before["status"] == "running"
            events_before = query(url, "SELECT event FROM llm_record_events WHERE record_id=$1", before["id"])
            init = next(json.loads(row["event"]) for row in events_before
                        if json.loads(row["event"]).get("subtype") == "init")
            first_pid = processes[-1].pid
            if action == "restart":
                service.restart(host, timeout=30)
            elif action == "reinstall":
                service.install(host, ops, timeout=30)
            else:
                if action == "kill":
                    processes[-1].kill()
                    processes[-1].wait(timeout=5)
                else:
                    service.manager(host, service.DEFAULT_LABEL).stop()
                assert not service._port_in_use(port)
            assert proc_tree._start_time(runner["pid"]) == runner["started"]
            (root / "release-tools").touch()
            # For stop/kill, the reverse tool call and settlement happen while
            # there is no public serve process or port at all.
            rows = eventually(lambda: query(url, "SELECT * FROM llm_records WHERE task_id=$1", task),
                              lambda rows: rows and rows[0]["status"] != "running")
            if action in {"stop", "kill"}:
                service.restart(host, timeout=30)
            assert processes[-1].pid != first_pid
            assert execution_host.read_host(root)["pid"] == state["pid"]
            assert proc_tree._start_time(runner["pid"]) == runner["started"]
            assert len(rows) == 1 and rows[0]["id"] == before["id"]
            assert rows[0]["status"] == "completed", rows[0]["error"]
            assert str(rows[0]["cost_usd"]) == "0.0039"
            events = [json.loads(row["event"]) for row in query(url,
                "SELECT event FROM llm_record_events WHERE record_id=$1", before["id"])]
            (result,) = [event for event in events if event["type"] == "result"]
            assert result["trace_id"] == init["trace_id"] and result["session_id"] == init["session_id"]
            assert any(event["type"] == "user" for event in events)  # reverse tool result
            eventually(lambda: api.post("/api/v1/tasks/detail", {"task_id": task})["result"],
                       lambda detail: detail["status"] == "waiting")
            api.post("/api/v1/ops/task_control/stop", {"task_id": task})
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
            # The test explicitly owns the durable host as well as its frontend.
            state = execution_host.read_host(root)
            if state:
                owned[state["pid"]] = state["started"]
                os.kill(state["pid"], signal.SIGTERM)
            try:
                eventually(lambda: {pid for pid, started in owned.items()
                    if proc_tree._start_time(pid) == started}, lambda live: not live, timeout=20)
            finally:
                for pid, started in owned.items():
                    if proc_tree._start_time(pid) == started:
                        os.kill(pid, signal.SIGKILL)
                log.close()
            assert all(process.poll() is not None for process in processes)
