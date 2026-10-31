"""Hold a child, append its bytes, and bound the lifetime of an unattended run."""

import asyncio
import logging
import os
import signal
import stat
from datetime import datetime, timezone
from pathlib import Path

from looper import __version__
from looper.agents import proc_tree

from . import (CHUNK, DEFAULT_ORPHAN_TIMEOUT_S, DEFAULT_STDOUT_LIMIT_BYTES, MANIFEST,
               PROTOCOL_VERSIONS, STATE, STATE_FORMAT, STDERR, STDERR_LIMIT_BYTES,
               STDIN, STDOUT, EndReason, KeeperState, _write_all, read_json, write_json)

from .server import KeeperServer

logger = logging.getLogger(__name__)


class Keeper:
    def __init__(self, run_dir: Path, runtime_root: Path, cwd: str, argv: list[str]):
        self.run_dir, self.runtime_root, self.cwd, self.argv = run_dir, runtime_root, cwd, argv
        self.run_id = run_dir.name
        self.child: asyncio.subprocess.Process | None = None
        self.ready, self.finished = asyncio.Event(), asyncio.Event()
        self.stdin_closed = False
        self.eof = set()
        self.server = KeeperServer(self)
        self.end_reason: EndReason | None = None
        self._end_task: asyncio.Task | None = None
        self._orphan_task: asyncio.Task | None = None
        self.fds: dict[str, int] = {}
        self.offsets = {STDOUT: 0, STDERR: 0, STDIN: 0}
        self.conditions = {name: asyncio.Condition() for name in (STDOUT, STDERR)}
        self.state = {
            "format": STATE_FORMAT, "protocol_versions": PROTOCOL_VERSIONS, "keeper_version": __version__,
            "keeper_pid": os.getpid(), "keeper_started": None, "child_pid": None, "child_started": None,
            "state": KeeperState.STARTING, "exit_code": None, "signal": None, "ended_at": None,
            "end_reason": None, "error": None, "controller_epoch": None, "socket": None,
            "stderr_truncated": False,
        }

    def _write_state(self, **changes):
        self.state.update(changes)
        write_json(self.run_dir / STATE, self.state)

    def _manifest(self):
        defaults = (DEFAULT_ORPHAN_TIMEOUT_S, DEFAULT_STDOUT_LIMIT_BYTES)
        manifest = read_json(self.run_dir / MANIFEST)
        values = tuple((manifest or {}).get(key, default) for key, default in
                       zip(("orphan_timeout_s", "stdout_limit_bytes"), defaults))
        if manifest is None or any(type(value) is not int or value <= 0 for value in values):
            logger.warning("missing or invalid keeper manifest run=%s; using defaults", self.run_id)
            return defaults
        return values

    def controller_attached(self):
        if self._orphan_task is not None:
            self._orphan_task.cancel()
            self._orphan_task = None

    def controller_detached(self):
        self.controller_attached()
        self._orphan_task = asyncio.create_task(self._orphan())

    async def _orphan(self):
        await asyncio.sleep(self.orphan_timeout_s)
        logger.warning("keeper orphaned run=%s", self.run_id)
        self.request_end(EndReason.ORPHANED)

    def request_end(self, reason: EndReason):
        if self.end_reason is not None:
            return
        self.end_reason = reason
        if self.child is not None and self._end_task is None:
            self._end_task = asyncio.create_task(proc_tree.end(self.child, label=f"keeper {self.run_id}"))

    def append_stdin(self, data):
        try:
            _write_all(self.fds[STDIN], data)
        except OSError:
            os.ftruncate(self.fds[STDIN], self.offsets[STDIN])
            raise
        self.offsets[STDIN] += len(data)

    async def _mark_eof(self, name):
        async with self.conditions[name]:
            self.eof.add(name)
            self.conditions[name].notify_all()

    async def _pump(self, stream: asyncio.StreamReader, name: str, limit: int):
        while data := await stream.read(CHUNK):
            remaining = max(0, limit - self.offsets[name])
            if remaining:
                written = data[:remaining]
                _write_all(self.fds[name], written)
                async with self.conditions[name]:
                    self.offsets[name] += len(written)
                    self.conditions[name].notify_all()
            if name == STDOUT and len(data) > remaining:
                self.request_end(EndReason.LOG_LIMIT)
            elif name == STDERR and self.offsets[name] >= limit and not self.state["stderr_truncated"]:
                self._write_state(stderr_truncated=True)
        await self._mark_eof(name)

    def _close_pipes(self):
        if self.child is not None:
            # StreamReader has no public close; release inherited pipe readers after a bounded drain.
            for fd in (0, 1, 2):
                transport = self.child._transport.get_pipe_transport(fd)
                if transport is not None:
                    transport.close()

    async def _finish(self, pumps):
        # Process.wait() can wait for pipe EOF even after the child has been reaped.
        while self.child.returncode is None:
            for task in [*pumps, self._end_task]:
                if task is not None and task.done():
                    task.result()
            await asyncio.sleep(0.05)
        # A reaped child can leave group members behind even when both pipes have closed.
        if self._end_task is None:
            self._end_task = asyncio.create_task(proc_tree.end(self.child, label=f"keeper {self.run_id}"))
        await self._end_task
        await self._drain(pumps)
        await self.child.wait()

    async def _drain(self, pumps):
        try:
            results = await asyncio.wait_for(asyncio.gather(*pumps, return_exceptions=True), proc_tree.REAP_SEC)
        except asyncio.TimeoutError:
            self._close_pipes()
        else:
            for result in results:
                if isinstance(result, Exception):
                    raise result

    def _exited(self, reason, error=None):
        code = self.child.returncode if self.child is not None else None
        self._write_state(state=KeeperState.EXITED, end_reason=reason, error=error,
                          exit_code=code if code is not None and code >= 0 else None,
                          signal=-code if code is not None and code < 0 else None,
                          ended_at=datetime.now(timezone.utc).isoformat())

    async def run(self) -> int:
        try:
            info = self.run_dir.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("run directory must be owned by this uid with mode 0700")
        except (OSError, ValueError) as exc:
            logger.error("invalid keeper directory: %s", exc)
            return 2
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self.request_end, EndReason.ENDED)
        loop.add_signal_handler(signal.SIGINT, lambda: None)
        loop.add_signal_handler(signal.SIGHUP, lambda: None)
        pumps = []
        try:
            self.orphan_timeout_s, self.stdout_limit_bytes = self._manifest()
            for name in (STDOUT, STDERR, STDIN):
                self.fds[name] = os.open(self.run_dir / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                self.offsets[name] = os.fstat(self.fds[name]).st_size
            self._write_state(keeper_started=proc_tree._start_time(os.getpid()))
            try:
                await self.server.start()
                proc_tree.own_records(self.runtime_root, run_dir=self.run_dir)
                self.child = await proc_tree.spawn(*self.argv, cwd=self.cwd, env=dict(os.environ),
                                                  stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE)
            except (OSError, RuntimeError) as exc:
                for fd in self.fds.values():
                    os.fsync(fd)
                self._exited(EndReason.START_FAILED, str(exc)[:300])
                return 3
            self._write_state(state=KeeperState.RUNNING, child_pid=self.child.pid,
                              child_started=proc_tree._start_time(self.child.pid))
            pumps = [asyncio.create_task(self._pump(self.child.stdout, STDOUT, self.stdout_limit_bytes)),
                     asyncio.create_task(self._pump(self.child.stderr, STDERR, STDERR_LIMIT_BYTES))]
            self.ready.set()
            if self.server.controller is None:
                self.controller_detached()
            if self.end_reason is not None:  # SIGTERM may have arrived while spawn was awaiting.
                self._end_task = asyncio.create_task(proc_tree.end(self.child, label=f"keeper {self.run_id}"))
            await self._finish(pumps)
            for fd in self.fds.values():
                os.fsync(fd)
            for name in (STDOUT, STDERR):
                await self._mark_eof(name)
            self.stdin_closed = True
            self._exited(self.end_reason or EndReason.EXITED)
            self.finished.set()
            await self.server.finish()
            return 0
        except Exception as exc:
            logger.error("keeper failed run=%s: %s", self.run_id, type(exc).__name__)
            if self.child is not None:
                try:
                    await proc_tree.end(self.child, label=f"keeper {self.run_id}")
                except Exception:
                    logger.error("cannot end keeper child run=%s", self.run_id)
            try:
                await self._drain(pumps)
            except Exception:
                logger.error("cannot drain keeper logs run=%s", self.run_id)
            self._close_pipes()
            for fd in self.fds.values():
                try:
                    os.fsync(fd)
                except OSError:
                    logger.error("cannot sync keeper log run=%s", self.run_id)
            try:
                self._exited(EndReason.KEEPER_ERROR, str(exc)[:300])
            except OSError:
                logger.error("cannot write keeper error state run=%s", self.run_id)
            return 1
        finally:
            self.ready.set()
            self.finished.set()
            await self.server.close()
            self.controller_attached()
            for task in [*pumps, self._end_task]:
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*pumps, *([self._end_task] if self._end_task else []), return_exceptions=True)
            self._close_pipes()
            for fd in self.fds.values():
                os.close(fd)
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                loop.remove_signal_handler(sig)
