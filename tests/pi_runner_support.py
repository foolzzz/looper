"""Isolated Pi runner processes and codec helpers shared by installed tests."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest

from looper.agents import proc_tree
from looper.agents.runtimes.runner import pi_command
from looper.agents.runtimes.runner.protocol import MAX_FRAME_BYTES
from looper.agents.runtimes.runner.supervisor import RunnerSupervisor, _ENV

_REQUIRED = "installed pi 1.3.x and Node 22.19+ are required; the Pi runner was not run"


def require_pi() -> tuple[str, str]:
    node, pi = shutil.which("node"), shutil.which("pi")
    try:
        if not node or not pi:
            pytest.skip(_REQUIRED)
        version = subprocess.run([node, "--version"], capture_output=True, text=True,
                                 check=True, timeout=5).stdout.strip().removeprefix("v")
        if tuple(map(int, version.split("."))) < (22, 19, 0):
            pytest.skip(_REQUIRED)
        parent = Path(pi).resolve().parent
        for _ in range(9):
            package = parent / "package.json"
            if package.is_file():
                metadata = json.loads(package.read_text())
                if metadata.get("name") == "@earendil-works/pi-coding-agent":
                    if re.fullmatch(r"1\.3\.\d+", metadata.get("version", "")):
                        return str(Path(node).absolute()), str(Path(pi).absolute())
                    break
            parent = parent.parent
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    pytest.skip(_REQUIRED)


@asynccontextmanager
async def pi_supervisor(tmp_path: Path, *, pi=None, env=None):
    node, installed_pi = require_pi()
    supervisor = RunnerSupervisor(tmp_path / "runtime", {
        "pi": pi_command.command(node, str(pi if pi is not None else installed_pi))},
        session_cap=10, env={"pi": {**pi_command.ENV, **(env or {})}}, connect=pi_command.connect)
    try:
        yield supervisor
    finally:
        await supervisor.stop()


@asynccontextmanager
async def raw_runner(tmp_path: Path, pi):
    node, _ = require_pi()
    root = Path(tempfile.mkdtemp(prefix="pi-runner-", dir=tmp_path))
    home, temporary = root / "home", root / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    environment = {name: value for name, value in os.environ.items() if name in _ENV}
    environment.update(pi_command.ENV)
    environment.update(HOME=str(home), TMPDIR=str(temporary))
    process = await asyncio.create_subprocess_exec(*pi_command.command(node, str(pi)), cwd=root,
        env=environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=MAX_FRAME_BYTES + 1)

    async def send(method, params=None, *, id="l-1"):
        message = method if isinstance(method, dict) else {
            "jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}
        process.stdin.write(json.dumps(message).encode() + b"\n")
        await process.stdin.drain()

    async def receive():
        async with asyncio.timeout(10):
            frame = await process.stdout.readline()
        assert frame.endswith(b"\n"), "runner did not return a complete frame"
        return json.loads(frame)

    try:
        yield process, send, receive
    finally:
        if not process.stdin.is_closing():
            process.stdin.close()
        try:
            async with asyncio.timeout(5):
                await process.wait()
        except TimeoutError:
            pass
        await proc_tree.end(process, label="test Pi runner")


def session_params(lease, *, base_url, key, role="manager", **override):
    return {"session_key": lease.session_key, "role": role, "cwd": str(lease.directory),
        "home": str(lease.home), "state_dir": str(lease.state_dir), "add_dirs": [], "env": {},
        "provider": {"protocol": "openai-responses", "base_url": base_url, "model": "test-model",
            "api_key": key, "account_id": "account", "reasoning": False, "effort": None, "context_window": None},
        "system_prompt": {"mode": "replace", "text": "Only the injected instructions."},
        "tools": {"builtin": [], "platform": [], "read_only": False},
        "limits": {"max_requests_per_turn": 30, "tool_timeout_s": 120, "tool_output_bytes": 102400},
        **override}


async def begin(bridge, sid, trace, *, cap=None, max_requests=30):
    from looper.agents.runtimes.pricing import ModelPrice
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def limits():
        return dict(run_cap=cap, task_cap=None, task_spent=0)

    async def cancel(_):
        await bridge.cancel_turn(sid, trace)

    turn = RunnerTurn(session_id=sid, trace_id=trace, role="manager", tools=None,
        provider_id="account", model="test-model", price=lambda _, usage: ModelPrice(1, 2, 3, 4).cost(usage),
        limits=limits, max_requests=max_requests, cancel=cancel)
    events = []
    await bridge.begin_turn(sid, trace, events.append, turn, text="Say hello.")
    return turn, events


def bind_pi_turn(session, tools):
    """Bind the same per-turn host contracts used by Manager and Planner."""
    from looper.agents.runtimes.pricing import ModelPrice

    async def limits():
        return dict(run_cap=None, task_cap=None, task_spent=0)

    session.set_turn_tools(tools)
    session.set_turn_budget(provider_id="account", model="test-model",
        price=lambda _, usage: ModelPrice(1, 2, 3, 4).cost(usage), limits=limits)


@asynccontextmanager
async def pi_session(tmp_path, api, tools):
    """Run the production resident-session path against a loopback model API."""
    from looper.agents.runtimes import ApiProtocol
    from looper.agents.runtimes.api.adapter import KEY_ENV
    from looper.agents.runtimes.runner.session import Descriptor, RunnerSession

    prompt = tmp_path / "system-prompt.md"
    prompt.write_text("Only the injected instructions.")
    async with pi_supervisor(tmp_path) as supervisor:
        session = RunnerSession("manager", supervisor, "pi")
        descriptor = Descriptor("pi", "manager", "account", ApiProtocol.OPENAI_RESPONSES,
            api.base_url, "test-model", None, None, str(prompt))
        try:
            await session.start(descriptor.to_argv(), cwd=str(tmp_path), signature=1,
                env={KEY_ENV: api.api_key})
            bind_pi_turn(session, tools)
            yield session
        finally:
            await session.stop()
