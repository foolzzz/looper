"""Scripted task tools over the production MCP bridge and isolated HTTP."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from evals.prompt_regression.task_manager_tools import run_case
from looper import child_env
from looper.agents import task_conversations_settings
from looper.agents.bus import MessageBus
from looper.agents.http_server import AgentHTTPServer
from looper.agents.library import EMPTY_LIBRARY
from looper.agents.ops.service import Platform
from looper.agents.ops.task_manager import TaskManagerOps
from looper.db import projects, tasks
from tests.db_support import run_db


def test_task_manager_tools_fake_eval(monkeypatch, tmp_path):
    async def check(pool):
        monkeypatch.setattr(task_conversations_settings, "_enabled", True)
        monkeypatch.setattr(task_conversations_settings, "_manager_answers", True)
        project = await projects.create_project(name="Synthetic task tools", work_dir=str(tmp_path), pool=pool)
        await tasks.create_task(task_id="task-tools-eval", requirement="Original synthetic requirement",
            title="Synthetic task", status="draft", project_id=project["id"], work_dir=str(tmp_path), pool=pool)
        platform = Platform(bus=MessageBus(runtime_root=tmp_path),
                            task_router=SimpleNamespace(has_planner=lambda _: False), runtime_root=tmp_path)
        ops = TaskManagerOps(platform, url="http://127.0.0.1:9")
        server = AgentHTTPServer(host="127.0.0.1", port=0, runtime_root=tmp_path,
            static_dir=tmp_path, web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
            task_router=platform.task_router, task_manager_ops=ops)
        await server.start()
        process = None
        token = ops.issue("task-tools-eval")
        try:
            url = f"http://127.0.0.1:{server._port}"
            assert urlparse(url).port != 8767
            ops._url = url
            directory = tmp_path / "ops"
            directory.mkdir(exist_ok=True, mode=0o700)
            (directory / "server.toml").write_text(f'[server]\nhost = "127.0.0.1"\nport = {server._port}\n')
            library = await ops.library_with_server(EMPTY_LIBRARY, tmp_path, token)
            config = json.loads(Path(library.mcp_config).read_text())["mcpServers"]["looper"]
            assert config["env"]["LOOPER_AGENT_URL"] == url
            process = await asyncio.create_subprocess_exec(config["command"], *config["args"],
                env={**os.environ, **config["env"], child_env.TASK_MANAGER_ENV: "task-tools-eval",
                     "HOME": str(tmp_path), "LOOPER_RUNTIME_ROOT": str(tmp_path)},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            sequence = 0

            async def rpc(method, params):
                nonlocal sequence
                sequence += 1
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": sequence,
                                                "method": method, "params": params}).encode() + b"\n")
                await process.stdin.drain()
                response = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
                assert response["id"] == sequence
                return response

            await rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "task-tools-fake-eval", "version": "1"}})
            ops.begin_turn(token)
            try:
                result = await run_case(rpc)
            finally:
                ops.end_turn(token)
            assert result["outcome"] == "pass", result
            assert result["task_id"] == "task-tools-eval"
            stored = await tasks.get_task("task-tools-eval", pool=pool)
            assert stored["requirement"] == result["requirement"]
        finally:
            ops.revoke(token)
            if process:
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 10)
                assert process.returncode == 0, (await process.stderr.read()).decode()
            await server.stop()

    run_db(check, monkeypatch)
