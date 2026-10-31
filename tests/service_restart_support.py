"""Isolated serve entry point with the deterministic protocol runner."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looper.agents import execution_host, proc_tree, serve
from looper.agents.runtimes.runner.bridge import RunnerBridge
from looper.agents.runtimes.runner.supervisor import RunnerSupervisor
from tests.runner_support import _register


if sys.argv[1] == "peer":
    from runners.fake.main import Fake

    root = Path(sys.argv[2])
    reverse = Fake.reverse

    async def gated(self, method, params):
        if method == "tools/call":
            (root / "before-tools").touch()
            async with asyncio.timeout(40):
                while not (root / "release-tools").exists():
                    await asyncio.sleep(0.05)
        return await reverse(self, method, params)

    Fake.reverse = gated
    settle = Fake.settle

    async def planned(self, session, correlation, **kwargs):
        if session.params["role"] == "planner" and kwargs["outcome"] == "completed":
            kwargs["final_text"] = "```json\n" + json.dumps({"steps": [{"id": "wait", "kind": "wait",
                "topic": "Retain the task for the next operator message", "depends_on": [],
                "max_wait_seconds": 7200, "not_before_seconds": 3600}]}) + "\n```"
        return await settle(self, session, correlation, **kwargs)

    Fake.settle = planned
    asyncio.run(Fake().run())
elif sys.argv[1] == "host":
    original = execution_host.run_host

    async def host(**kwargs):
        async def connect(process, kind):
            print("test runner=" + json.dumps({"pid": process.pid,
                "started": proc_tree._start_time(process.pid)}), flush=True)
            bridge = RunnerBridge(process.stdout, process.stdin, kind)
            await bridge.initialize(permitted=("turn",))
            return bridge

        supervisor = RunnerSupervisor(kwargs["ops"].runtime_root,
            {"fake": [sys.executable, "-u", str(Path(__file__).resolve()), "peer",
                      str(kwargs["ops"].runtime_root)]}, connect=connect)
        _register(supervisor)

        async def no_bots():
            return []

        serve._enabled_bot_rows = no_bots
        await original(**kwargs, runner_supervisor=supervisor)

    execution_host.run_host = host
    del sys.argv[1]
    execution_host.main()
else:
    original = execution_host._host_command
    execution_host._host_command = lambda *args: [sys.executable, "-u", str(Path(__file__).resolve()), "host",
                                                 *original(*args)[3:]]
    # Exercise the Linux selection on macOS too, without changing sys.platform
    # (which would change asyncio's subprocess watcher).
    serve._persistent_runners = lambda ops: True
    serve.serve_agent(runtime_root=Path(sys.argv[2]), work_dir=Path(sys.argv[3]), skip_backup=True)
