"""Private, network-guarded DSH runner processes shared by installed tests."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from dsh_installed_stand_in import NETWORK_GUARD
from looper.agents import proc_tree
from looper.agents.runtimes.runner import dsh_command
from looper.agents.runtimes.runner.protocol import MAX_FRAME_BYTES
from looper.agents.runtimes.runner.supervisor import RunnerSupervisor, _ENV

_REQUIRED = "GAP: installed dsh 0.2.1-rc.2 and Node 22.19+ are required; the DSH runner was not run"
OFFER = {"protocol_versions": ["0.1"], "looper_version": "test", "host_capabilities": {
    "tools_call": True, "budget_authorize": True, "bus": False, "exec_slots": False}}


def require_node() -> str:
    node = shutil.which("node")
    try:
        if node:
            version = subprocess.run([node, "--version"], capture_output=True, text=True,
                                     check=True, timeout=10).stdout.strip().removeprefix("v")
            if tuple(map(int, version.split("."))) >= (22, 19, 0):
                return str(Path(node).absolute())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    pytest.skip(_REQUIRED)


def require_dsh() -> tuple[str, str]:
    node, executable = require_node(), shutil.which("dsh")
    try:
        if executable:
            parent = Path(executable).resolve().parent
            for _ in range(9):
                package = parent / "package.json"
                if package.is_file():
                    metadata = json.loads(package.read_text())
                    if metadata.get("name") == "@deepseek-ai/dsh":
                        if metadata.get("version") == "0.2.1-rc.2":
                            return node, str(Path(executable).absolute())
                        break
                parent = parent.parent
    except (OSError, ValueError):
        pass
    pytest.skip(_REQUIRED)


def guard_env(tmp_path: Path, port=0) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    guard = tmp_path / "network-guard.mjs"
    guard.write_text(NETWORK_GUARD)
    return {"NODE_OPTIONS": f"--import={guard}", "LOCAL_NETWORK_LOG": str(tmp_path / "network.jsonl"),
            "LOCAL_MESSAGES_PORT": str(port)}


def _guarded_env(tmp_path: Path, env) -> dict[str, str]:
    guard = guard_env(tmp_path)
    result = {**guard, **(env or {})}
    # Preserve the fixture endpoint/log and other preloads, but always install
    # the guard. guard_env(tmp_path, port) already names this same preload.
    options = result.get("NODE_OPTIONS", "")
    if guard["NODE_OPTIONS"] not in options.split():
        options = f"{options} {guard['NODE_OPTIONS']}".strip()
    result["NODE_OPTIONS"] = options
    return result


@asynccontextmanager
async def dsh_supervisor(tmp_path: Path, *, dsh=None, env=None):
    node = require_node()
    if dsh is None:
        _, dsh = require_dsh()
    runtime = tmp_path / "runtime"
    supervisor = RunnerSupervisor(runtime, {
        "dsh": dsh_command.command(node, str(dsh), dsh_command.state_root(runtime))},
        session_cap=20, env={"dsh": _guarded_env(tmp_path, env)},
        connect=dsh_command.connect)
    try:
        yield supervisor
    finally:
        await supervisor.stop()


@asynccontextmanager
async def raw_runner(tmp_path: Path, dsh, *, env=None):
    node = require_node()
    root = Path(tempfile.mkdtemp(prefix="dsh-runner-", dir=tmp_path))
    home, temporary = root / "home", root / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    environment = {name: value for name, value in os.environ.items() if name in _ENV}
    environment.update(_guarded_env(tmp_path, env))
    environment.update(HOME=str(home), TMPDIR=str(temporary))
    state = dsh_command.state_root(root)
    process = await asyncio.create_subprocess_exec(*dsh_command.command(node, str(dsh), state),
        cwd=root, env=environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=MAX_FRAME_BYTES + 1)

    async def send(method, params=None, *, id="l-1"):
        message = method if isinstance(method, dict) else {
            "jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}
        process.stdin.write(json.dumps(message).encode() + b"\n")
        await process.stdin.drain()

    async def receive():
        frame = await asyncio.wait_for(process.stdout.readline(), 35)
        assert frame.endswith(b"\n"), "runner did not return a complete frame"
        return json.loads(frame)

    try:
        yield process, send, receive
    finally:
        if not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            pass
        await proc_tree.end(process, label="test DSH runner")
