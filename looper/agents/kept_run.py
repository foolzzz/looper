"""A runtime run whose process and byte streams are held by a keeper."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

import looper
from . import keeper, proc_tree
from .keeper.client import KeeperClient, KeeperGone
from .keeper.protocol import ErrorCode, KeeperError, MAX_STDIN_BYTES, Stream
from .runtimes.channel import RunStartError

logger = logging.getLogger(__name__)


class KeptRunState(StrEnum):
    STARTING = "starting"
    ATTACHED = "attached"
    DETACHED = "detached"
    ENDED = "ended"
    LOST = "lost"
    FENCED = "fenced"


class DetachKind(StrEnum):
    DETACHED = "detached"
    BUSY = "busy"
    FINISHING = "finishing"
    NOT_ATTACHED = "not_attached"
    LOST = "lost"


@dataclass(frozen=True)
class DetachOutcome:
    kind: DetachKind
    consumed: int | None = None


@dataclass(frozen=True)
class KeeperSettings:
    epoch: int


class KeeperLost(Exception):
    """The run lost its supervisor before its exit could be observed."""


class KeptStream:
    """Expose contiguous complete lines, keeping the parser's delivery offset."""

    def __init__(self):
        self.offset = self.delivered = 0
        self.idle = asyncio.Event()
        self._ready = asyncio.Event()
        self._pending, self._readable = bytearray(), bytearray()
        self._paused = self._eof = False
        self.eof_delivered = False

    def feed(self, offset, data):
        if offset != self.offset or self._eof:
            raise ValueError("noncontiguous keeper stdout")
        self.offset += len(data)
        self._pending.extend(data)
        self._publish()

    def feed_eof(self, offset):
        if offset != self.offset:
            raise ValueError("noncontiguous keeper stdout EOF")
        self._eof = True
        self._publish()

    def _publish(self):
        if not self._paused:
            end = len(self._pending) if self._eof else self._pending.rfind(b"\n") + 1
            self._readable.extend(self._pending[:end])
            del self._pending[:end]
            if self._readable or self._eof:
                self.idle.clear()
                self._ready.set()

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False
        self._publish()

    async def read(self, n=-1) -> bytes:
        if n == 0:
            return b""
        while not self._readable:
            if self._eof and not self._paused:
                self.eof_delivered = True
                return b""
            self._ready.clear()
            self.idle.set()
            await self._ready.wait()
        end = len(self._readable) if n < 0 else min(n, len(self._readable))
        data = bytes(self._readable[:end])
        del self._readable[:end]
        self.delivered += len(data)
        return data


class KeptStdin:
    def __init__(self, client, active: Callable[[], bool], on_stale: Callable[[], None]):
        self._client, self._active, self._on_stale = client, active, on_stale
        self.offset = 0
        self.inflight: asyncio.Future | None = None
        self._buffer = bytearray()
        self._lock = asyncio.Lock()
        self._closing = False
        self._close_task: asyncio.Task | None = None

    def write(self, data):
        if not self._active() or self._closing:
            raise BrokenPipeError("keeper stdin is closed")
        self._buffer.extend(data)

    async def drain(self):
        if self.inflight is None:
            self.inflight = asyncio.create_task(self._drain())
            # A channel's send timeout does not retract a queued write. Keep its ACK and
            # offset update alive even if the sender stops waiting.
            self.inflight.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        await asyncio.shield(self.inflight)

    async def _drain(self):
        async with self._lock:
            try:
                while self._buffer and self._active():
                    piece = bytes(self._buffer[:MAX_STDIN_BYTES])
                    self.offset = await self._client.stdin_write(piece, expect_offset=self.offset)
                    del self._buffer[:len(piece)]
            except KeeperError as exc:
                if exc.code == ErrorCode.OFFSET_MISMATCH:
                    raise RuntimeError("keeper stdin offset mismatch") from exc
                if exc.code == ErrorCode.CHILD_EXITED:
                    raise BrokenPipeError("keeper child exited") from exc
                if exc.code == ErrorCode.STALE_CONTROLLER:
                    self._on_stale()
                    raise ConnectionResetError("keeper controller replaced") from exc
                raise
            except KeeperGone as exc:
                raise ConnectionResetError(str(exc)) from exc
            finally:
                self.inflight = None

    def close(self):
        if not self._closing:
            self._closing = True
            if self._active():
                self._close_task = asyncio.create_task(self._close())

    async def _close(self):
        async with self._lock:
            if self._active():
                try:
                    await self._client.stdin_close()
                except (KeeperGone, KeeperError, TimeoutError):
                    pass  # output and child/exited still decide the run's outcome

    def is_closing(self):
        return self._closing


class KeptProcess:
    stderr = None

    def __init__(self, run):
        self.pid = None
        self.returncode = None
        self.stdout = KeptStream()
        self.stdin = KeptStdin(run._client, run._active, run._stale)
        self._run = run

    async def wait(self) -> int:
        return await self._run.wait()


class KeptRun:
    def __init__(self, runtime, command, *, launch, cwd, session_dir, label, run_dir,
                 runtime_root, epoch, on_stale: Callable[[str], None]):
        from looper import components

        self._runtime, self._command, self._launch = runtime, command, launch
        self._cwd, self._session_dir, self._label = cwd, session_dir, label
        self.run_dir, self._runtime_root = Path(run_dir), Path(runtime_root)
        self._epoch, self._on_stale = epoch, on_stale
        self.state = KeptRunState.STARTING
        self._client = KeeperClient()
        self._process = KeptProcess(self)
        self._keeper_process = self._channel = None
        self._finished = asyncio.Event()
        self._ending = False
        self._owns_dir = False
        self._client.on_data = self._data
        self._client.on_eof = self._eof
        self._client.on_exited = self._exited
        self._client.on_replaced = lambda epoch: self._stale()
        self._client.on_closing = self._closing
        self._client.on_closed = self._closed
        self.manifest = {"manifest_version": 1, "run_id": self.run_dir.name, "record_id": None,
                         "runtime_type": runtime.type, "command": command, "work_dir": str(cwd),
                         "orphan_timeout_s": keeper.DEFAULT_ORPHAN_TIMEOUT_S,
                         "stdout_limit_bytes": keeper.DEFAULT_STDOUT_LIMIT_BYTES,
                         "adoption": components.loaded_manifest().adoption}

    @property
    def command(self):
        return self._command

    @property
    def pid(self):
        return self._process.pid

    @property
    def channel(self):
        assert self._channel is not None
        return self._channel

    def set_record(self, record_id):
        self.manifest["record_id"] = record_id

    def _active(self):
        return self.state in (KeptRunState.STARTING, KeptRunState.ATTACHED)

    def _stale(self):
        if self._active():
            self.fence()
            self._on_stale("a keeper reported a newer control plane")

    def _check_launch(self):
        if proc_tree.spawns_fenced() or self.state is KeptRunState.FENCED:
            self.fence()
            raise RunStartError("keeper launch refused: control plane is fenced")
        if not self._active():
            raise RunStartError("keeper connection ended during startup")

    async def launch(self):
        self._check_launch()
        created = False
        try:
            self.run_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.run_dir.mkdir(mode=0o700)
            created = True
            self._owns_dir = True
            keeper.write_json(self.run_dir / keeper.MANIFEST, self.manifest)
            fd = os.open(self.run_dir / keeper.LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                self._keeper_process = await asyncio.create_subprocess_exec(
                    *keeper.boot_argv(sys.executable, Path(looper.__file__).resolve().parent.parent,
                                      self.run_dir, self._runtime_root, str(self._cwd), self.command),
                    env=self._launch.environment(), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=fd, cwd=self.run_dir, start_new_session=True)
            finally:
                os.close(fd)
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                self._check_launch()
                if self._keeper_process.returncode is not None:
                    state = keeper.read_json(self.run_dir / keeper.STATE) or {}
                    raise RunStartError(state.get("error") or "keeper exited before connecting")
                try:
                    await self._client.connect(self.run_dir / keeper.SOCKET, timeout=0.1)
                    break
                except OSError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RunStartError("keeper did not open its control socket within 10 s") from None
                    await asyncio.sleep(0.05)
            self._check_launch()
            result = await self._client.initialize(epoch=self._epoch, looper_version=looper.__version__)
            self._check_launch()
            self._process.pid = result["child"]["pid"]
            self._process.stdin.offset = result["offsets"]["stdin"]
            if result["child"]["state"] == keeper.KeeperState.EXITED:
                self._exited(result["child"]["exit_code"], result["child"]["signal"])
            await self._client.subscribe(Stream.STDOUT, 0)
            if not self._active():
                raise RunStartError("keeper connection ended during startup")
            self.state = KeptRunState.ATTACHED
            self._channel = self._runtime.run_channel(self._process, launch=self._launch, cwd=self._cwd,
                                                      session_dir=self._session_dir)
        except BaseException as exc:
            if isinstance(exc, KeeperError) and exc.code == ErrorCode.STALE_CONTROLLER:
                self._stale()
            await self.end()
            message = str(exc)
            if created and isinstance(exc, KeeperGone):
                # A failed spawn can close an already connected socket before initialize replies.
                state = keeper.read_json(self.run_dir / keeper.STATE) or {}
                message = state.get("error") or message
            if created:
                self.release()
            if isinstance(exc, (OSError, KeeperError, KeeperGone, TimeoutError)):
                raise RunStartError(message) from exc
            raise

    def _data(self, stream, offset, data):
        if self._active() and stream == Stream.STDOUT:
            self._process.stdout.feed(offset, data)

    def _eof(self, stream, offset):
        if self._active() and stream == Stream.STDOUT:
            self._process.stdout.feed_eof(offset)

    def _exited(self, exit_code, sig):
        if self._active():
            self._process.returncode = exit_code if exit_code is not None else -sig if sig else 0
            self._finished.set()

    def _closing(self, stdout_offset, stderr_offset):
        if self._active():
            self._finish_output(stdout_offset)

    def _finish_output(self, offset):
        stream = self._process.stdout
        if stream.offset < offset:
            with (self.run_dir / keeper.STDOUT).open("rb") as source:
                source.seek(stream.offset)
                while stream.offset < offset:
                    data = source.read(min(keeper.CHUNK, offset - stream.offset))
                    if not data:
                        raise KeeperLost("keeper stdout log is incomplete")
                    stream.feed(stream.offset, data)
        stream.feed_eof(stream.offset)

    async def _closed(self):
        if not self._active() or self._ending:
            return
        if self._process.returncode is not None:
            self._finish_output((self.run_dir / keeper.STDOUT).stat().st_size)
            return
        self.state = KeptRunState.LOST
        self._process.stdout.feed_eof(self._process.stdout.offset)
        try:
            await self._end_orphan()
        finally:
            self._finished.set()

    async def _end_orphan(self):
        if self.state in (KeptRunState.FENCED, KeptRunState.DETACHED):
            return
        state = keeper.read_json(self.run_dir / keeper.STATE) or {}
        if state.get("child_pid") and state.get("child_started"):
            await proc_tree.end_orphan(state["child_pid"], state["child_started"], label=self._label)
            path = self._runtime_root / proc_tree.RECORDS_DIR / f"{state['child_pid']}.json"
            record = keeper.read_json(path) or {}
            if record.get("started") == state["child_started"]:
                path.unlink(missing_ok=True)

    async def detach(self, *, epoch: int, timeout: float) -> DetachOutcome:
        if self.state is not KeptRunState.ATTACHED:
            return DetachOutcome(DetachKind.LOST if self.state is KeptRunState.LOST else DetachKind.NOT_ATTACHED)
        if self._ending:
            return DetachOutcome(DetachKind.BUSY)
        stream, stdin = self._process.stdout, self._process.stdin
        if stdin.inflight is not None:
            try:
                await asyncio.wait_for(asyncio.shield(stdin.inflight), timeout)
            except TimeoutError:
                return DetachOutcome(DetachKind.BUSY)
        if self.state is not KeptRunState.ATTACHED:
            return DetachOutcome(DetachKind.LOST if self.state is KeptRunState.LOST else DetachKind.NOT_ATTACHED)
        stream.pause()
        if stream.eof_delivered:
            stream.resume()
            return DetachOutcome(DetachKind.FINISHING)
        try:
            await asyncio.wait_for(stream.idle.wait(), timeout)
            if self.state is not KeptRunState.ATTACHED:
                stream.resume()
                return DetachOutcome(DetachKind.LOST if self.state is KeptRunState.LOST else DetachKind.NOT_ATTACHED)
            if stream._eof:
                stream.resume()
                return DetachOutcome(DetachKind.FINISHING)
            if self._ending or stdin.inflight is not None:
                stream.resume()
                return DetachOutcome(DetachKind.BUSY)
            consumed = stream.delivered
            keeper.write_json(self.run_dir / keeper.DETACH, {
                "format": 1, "epoch": epoch, "consumed": {"stdout": consumed},
                "stdin_offset": stdin.offset, "at": datetime.now(timezone.utc).isoformat(),
            })
        except (TimeoutError, OSError):
            stream.resume()
            return DetachOutcome(DetachKind.BUSY)
        except BaseException:
            stream.resume()
            raise
        self.state = KeptRunState.DETACHED
        await self._client.close()
        await self._client.wait_closed()
        return DetachOutcome(DetachKind.DETACHED, consumed)

    async def end(self):
        if self.state in (KeptRunState.DETACHED, KeptRunState.ENDED, KeptRunState.FENCED):
            return
        if self._ending:
            return
        self._ending = True
        try:
            if self.state == KeptRunState.STARTING:
                process = self._keeper_process
                if process is not None:
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 10)
                    except TimeoutError:
                        if self.state in (KeptRunState.FENCED, KeptRunState.DETACHED):
                            return
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
                        await self._end_orphan()
            elif self.state == KeptRunState.ATTACHED and self._process.returncode is None:
                try:
                    exit_code, sig = await self._client.end(timeout=15)
                    self._exited(exit_code, sig)
                except Exception:
                    if self._active() and self._process.returncode is None:
                        self.state = KeptRunState.LOST
                        self._finished.set()
                        await self._end_orphan()
            await self._client.close()
            await self._client.wait_closed()
            if self._keeper_process is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._keeper_process.wait(), 10)
        except Exception:
            logger.exception("could not end %s", self._label)
        finally:
            # Preserve LOST so wait() cannot mistake a missing supervisor for a normal exit.
            if self.state not in (KeptRunState.LOST, KeptRunState.FENCED, KeptRunState.DETACHED):
                self.state = KeptRunState.ENDED
            self._process.stdout.feed_eof(self._process.stdout.offset)
            self._ending = False

    async def wait(self) -> int:
        await self._finished.wait()
        if self.state == KeptRunState.LOST:
            raise KeeperLost("the run's keeper process ended unexpectedly")
        # The result publisher may release the logs as soon as wait returns.
        # Drain final callbacks and reap the keeper before allowing that removal.
        await self._client.wait_closed()
        if self._keeper_process is not None:
            await self._keeper_process.wait()
        return self._process.returncode or 0

    async def error_output(self) -> str:
        try:
            with (self.run_dir / keeper.STDERR).open("rb") as source:
                return source.read(65536).decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def dropped_input(self):
        return []

    def fence(self):
        self.state = KeptRunState.FENCED
        writer = self._client._writer
        if writer is not None:
            writer.transport.abort()
        self._process.stdout.feed_eof(self._process.stdout.offset)

    def release(self):
        if self._owns_dir and self.state not in (KeptRunState.DETACHED, KeptRunState.FENCED):
            shutil.rmtree(self.run_dir, ignore_errors=True)
            self._owns_dir = False
