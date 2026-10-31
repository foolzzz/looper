"""Linux serve listener handoff without interrupting the execution owner.

Runner stdio, reverse tool calls, budget authorizations and task coroutines
must have the same lifetime. They live together in a detached execution host;
the systemd MainPID owns only the public listener. Its replacement attaches
to the same authenticated loopback stream, without running startup recovery.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
from typing import TypedDict

from .. import ops_config
from ..ops_config import OpsConfig
from . import proc_tree

logger = logging.getLogger(__name__)
STATE = Path(".local/execution-host.json")
START_TIMEOUT = 120.0


class HostState(TypedDict):
    pid: int
    started: str
    port: int
    secret: str
    identity: str


def _identity(ops: OpsConfig, work_dir: Path, claude_bin: str) -> str:
    return hashlib.sha256((ops_config.render(ops) + str(work_dir.absolute()) + claude_bin).encode()).hexdigest()


def _private_directory(root: Path) -> Path:
    directory = root / STATE.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise ValueError("execution host state must be a private directory owned by this user")
    directory.chmod(0o700)
    return directory


def read_host(root: Path) -> HostState | None:
    """Read only our private, identity-checked host; never attach by port alone."""
    path = root / STATE
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
            raise ValueError("execution host state must be private and owned by this user")
        state: HostState = json.loads(path.read_text())
        if (type(state["pid"]) is not int or not isinstance(state["started"], str)
                or type(state["port"]) is not int or not 0 < state["port"] < 65536
                or not isinstance(state["secret"], str) or len(state["secret"]) != 64
                or not isinstance(state["identity"], str)):
            raise ValueError("invalid execution host state")
        return state if proc_tree._start_time(state["pid"]) == state["started"] else None
    except FileNotFoundError:
        return None


async def _connect(state: HostState, peer: str = "watch"):
    async with asyncio.timeout(5):
        reader, writer = await asyncio.open_connection("127.0.0.1", state["port"])
        try:
            writer.write(f"LOOPER/1 {state['secret']} {peer}\n".encode("ascii"))
            await writer.drain()
            if await reader.readline() != b"LOOPER/1 ready\n":
                raise ConnectionError("execution host rejected the listener")
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise
        return reader, writer


def _host_command(ops: OpsConfig, work_dir: Path, claude_bin: str, skip_backup: bool) -> list[str]:
    return [sys.executable, "-m", __name__, "--runtime-root", str(ops.runtime_root),
            "--config", str(ops.path), "--host", ops.host, "--port", str(ops.port),
            "--log-level", ops.log_level,
            "--work-dir", str(work_dir.absolute()), "--claude-bin", claude_bin,
            *(("--skip-backup",) if skip_backup else ())]


async def serve_frontend(*, ops: OpsConfig, work_dir: Path, claude_bin: str, skip_backup: bool) -> None:
    """Own the external port and streams, leaving the execution host alive on SIGTERM."""
    from .serve import StartupCheckFailed, stop_signal_handler

    root = ops.runtime_root
    _private_directory(root)
    identity = _identity(ops, work_dir, claude_bin)
    connections: set[asyncio.Task] = set()
    state = None

    async def relay(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        upstream = None
        try:
            peer = writer.get_extra_info("peername")[0]
            remote, upstream = await _connect(state, peer)

            async def copy(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()
                if destination.can_write_eof():
                    destination.write_eof()

            pumps = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(remote, writer))]
            try:
                # A caller may half-close its request and still await a response.
                done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                if pumps[0] in done and not pumps[0].cancelled() and pumps[0].exception() is None:
                    await pumps[1]
            finally:
                for pump in pumps:
                    pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
        except (OSError, TimeoutError):
            logger.warning("execution host connection lost")
        finally:
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()
            connections.discard(task)

    # Bind before starting/adopting an execution owner. A second listener cannot
    # launch another system against the same database while this one is serving.
    listener = await asyncio.start_server(relay, ops.host, ops.port, start_serving=False)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_signal_handler(stopped))
    child = watcher = stop_wait = None
    watch_writer = None
    try:
        state = read_host(root)
        if state is None:
            log = root / "logs/serve.log"
            log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "ab") as output:
                child = subprocess.Popen(_host_command(ops, work_dir, claude_bin, skip_backup),
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, cwd=work_dir)
        deadline = loop.time() + START_TIMEOUT
        while not stopped.is_set():
            state = read_host(root)
            if state is not None:
                if state["identity"] != identity:
                    raise StartupCheckFailed("a live execution host owns this runtime root with different settings; "
                        "finish its active work and stop that host before changing execution settings")
                try:
                    watch_reader, watch_writer = await _connect(state)
                    break
                except (OSError, TimeoutError):
                    pass
            if child is not None and child.poll() is not None:
                raise StartupCheckFailed("execution host exited during startup; see logs/serve.log")
            if loop.time() >= deadline:
                raise StartupCheckFailed("execution host startup timed out; see logs/serve.log")
            with suppress(TimeoutError):
                await asyncio.wait_for(stopped.wait(), 0.1)
        if stopped.is_set():
            return
        await listener.start_serving()
        logger.info("serve attached to execution host pid %d; runner sessions survive listener restarts", state["pid"])
        watcher = asyncio.create_task(watch_reader.read(1))
        stop_wait = asyncio.create_task(stopped.wait())
        await asyncio.wait((watcher, stop_wait), return_when=asyncio.FIRST_COMPLETED)
        if not stopped.is_set():
            raise StartupCheckFailed("execution host exited; restart serve to recover")
    finally:
        listener.close()
        await listener.wait_closed()
        for task in (*connections, watcher, stop_wait):
            if task is not None:
                task.cancel()
        await asyncio.gather(*connections, *(t for t in (watcher, stop_wait) if t is not None),
                             return_exceptions=True)
        if watch_writer is not None:
            watch_writer.close()
            with suppress(OSError):
                await watch_writer.wait_closed()
        if child is not None:
            if watch_writer is None and not stopped.is_set() and child.poll() is None:
                # A failed startup owns no live run to retain. Do not strand a
                # wedged initializing host across systemd's retry loop.
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait, timeout=5)
            child.poll()  # Reap a failed startup; a live host belongs to its next listener.
        logger.info("serve listener stopped; execution host retained")


async def run_host(*, ops: OpsConfig, work_dir: Path, claude_bin: str = "claude", skip_backup: bool = False,
                   runner_supervisor=None) -> None:
    from .serve import _run_agent_system_local

    directory = _private_directory(ops.runtime_root)
    # Retained for the host's lifetime, including startup before the state file
    # appears. Concurrent/replacement listeners cannot start duplicate owners.
    with (directory / "execution-host.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        started = proc_tree._start_time(os.getpid())
        if not started:
            raise RuntimeError("cannot establish execution host process identity")
        secret = secrets.token_hex(32)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            listener.setblocking(False)
            state: HostState = {"pid": os.getpid(), "started": started, "port": listener.getsockname()[1],
                     "secret": secret, "identity": _identity(ops, work_dir, claude_bin)}
            path = ops.runtime_root / STATE
            ops_config.write_private(path, json.dumps(state))
            try:
                await _run_agent_system_local(ops=ops, runtime_root=ops.runtime_root, work_dir=work_dir,
                    claude_bin=claude_bin, skip_backup=skip_backup, runner_supervisor=runner_supervisor,
                    http_socket=listener, proxy_secret=secret)
            finally:
                path.unlink(missing_ok=True)


def main() -> None:
    from .serve import StartupCheckFailed, _configure_logging

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime-root", "config", "host", "port", "log-level", "work-dir", "claude-bin"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--skip-backup", action="store_true")
    args = parser.parse_args()
    ops = ops_config.load(ops_config.Flags(runtime_root=args.runtime_root, config=args.config,
                                          host=args.host, port=int(args.port), log_level=args.log_level))
    _configure_logging(ops.log_level)
    try:
        asyncio.run(run_host(ops=ops, work_dir=Path(args.work_dir), claude_bin=args.claude_bin,
                             skip_backup=args.skip_backup))
    except StartupCheckFailed as error:
        logger.error("execution host cannot start: %s", error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
