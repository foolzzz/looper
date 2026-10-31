"""Exercise the keeper's socket contract against real, disposable child processes."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from looper.agents import keeper, proc_tree
from looper.agents.keeper.client import KeeperClient, KeeperError, KeeperGone
from looper.agents.keeper.protocol import (MAX_FRAME_BYTES, ErrorCode, KeeperSignal, Stream,
                                          encode, negotiate, read_frame)


ROOT = Path(__file__).resolve().parents[1]
SLEEP = "import time; time.sleep(60)"


def exercise(coroutine) -> None:
    asyncio.run(asyncio.wait_for(coroutine, 35))


async def eventually(predicate: Callable[[], Any], timeout: float = 10) -> Any:
    async with asyncio.timeout(timeout):
        while not (result := predicate()):
            await asyncio.sleep(0.02)
        return result


def live(pid: int) -> bool:
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode in (0, 1), result.stderr
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@dataclass
class Run:
    directory: Path
    runtime_root: Path
    cwd: Path
    process: subprocess.Popen
    identities: dict[int, str] = field(default_factory=dict)

    @property
    def socket(self) -> Path:
        return self.directory / keeper.SOCKET

    def state(self) -> dict:
        return keeper.read_json(self.directory / keeper.STATE) or {}

    async def running(self) -> dict:
        return await eventually(lambda: state if (state := self.state()).get("state") == "running" else None)

    async def wait(self) -> dict:
        await eventually(lambda: self.process.poll() is not None, timeout=15)
        assert self.process.returncode == 0, (self.directory / keeper.LOG).read_text()
        return self.state()


@pytest.fixture
def launch() -> Iterator[Callable[..., Run]]:
    runs: list[Run] = []
    directories: list[tempfile.TemporaryDirectory] = []

    def start(script: str = SLEEP, *, manifest: dict | None = None,
              socket_overlong: bool = False, delay_spawn: bool = False) -> Run:
        # The checkout is long: using this directory itself keeps the socket at 103 bytes.
        temporary = tempfile.TemporaryDirectory(dir=ROOT, prefix=".k")
        directories.append(temporary)
        directory = Path(temporary.name)
        if socket_overlong:
            directory /= "x" * keeper.SOCKET_PATH_MAX
            directory.mkdir()
        else:
            assert len(os.fsencode(directory / keeper.SOCKET)) <= keeper.SOCKET_PATH_MAX
        directory.chmod(0o700)
        runtime_root, cwd = directory / "r", directory / "w"
        runtime_root.mkdir()
        cwd.mkdir()
        keeper.write_json(directory / keeper.MANIFEST, manifest or {})
        command = keeper.boot_argv(sys.executable, ROOT, directory, runtime_root, str(cwd),
                                   [sys.executable, "-c", script])
        if delay_spawn:
            setup = f"""\
import asyncio
from pathlib import Path
from looper.agents import proc_tree
original_spawn = proc_tree.spawn
async def delayed_spawn(*args, **kwargs):
    Path({str(cwd / 'spawn_waiting')!r}).touch()
    while not Path({str(cwd / 'spawn_allowed')!r}).exists():
        await asyncio.sleep(0.01)
    return await original_spawn(*args, **kwargs)
proc_tree.spawn = delayed_spawn
"""
            command[3] = command[3].replace("; sys.exit(", "\n" + setup + "\nsys.exit(")
        log_fd = os.open(directory / keeper.LOG, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=log_fd, start_new_session=True)
        finally:
            os.close(log_fd)
        run = Run(directory, runtime_root, cwd, process)
        runs.append(run)
        if started := proc_tree._start_time(process.pid):
            run.identities[process.pid] = started
        return run

    yield start

    for run in reversed(runs):
        state = run.state()
        if state.get("child_pid") and state.get("child_started"):
            run.identities[state["child_pid"]] = state["child_started"]
        for record_path in (run.runtime_root / proc_tree.RECORDS_DIR).glob("*.json"):
            record = keeper.read_json(record_path) or {}
            if record.get("pid") and record.get("started"):
                run.identities[record["pid"]] = record["started"]
        for pid in (keeper.read_json(run.cwd / "tree.json") or {}).values():
            if pid not in run.identities and (started := proc_tree._start_time(pid)):
                run.identities[pid] = started
        if run.process.poll() is None:
            run.process.terminate()
        try:
            run.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            run.process.kill()
            run.process.wait(timeout=5)
        for pid, started in run.identities.items():
            if live(pid) and proc_tree._start_time(pid) == started:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        while any(live(pid) and proc_tree._start_time(pid) == started
                  for pid, started in run.identities.items()):
            assert time.monotonic() < deadline, "test left a child process running"
            time.sleep(0.03)
    for directory in reversed(directories):
        directory.cleanup()


@dataclass
class Events:
    data: dict[Stream, bytearray] = field(default_factory=lambda: {stream: bytearray() for stream in Stream})
    offsets: dict[Stream, int] = field(default_factory=lambda: {stream: 0 for stream in Stream})
    eof: dict[Stream, int] = field(default_factory=dict)
    exited: list[tuple[int | None, int | None]] = field(default_factory=list)
    replaced: list[int] = field(default_factory=list)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    order: list[str] = field(default_factory=list)

    def attach(self, client: KeeperClient) -> None:
        def on_data(stream, offset, data):
            stream = Stream(stream)
            assert offset == self.offsets[stream]
            assert 0 < len(data) <= 65536
            self.data[stream].extend(data)
            self.offsets[stream] += len(data)

        def on_eof(stream, offset):
            stream = Stream(stream)
            assert offset == self.offsets[stream]
            assert stream not in self.eof
            self.eof[stream] = offset
            self.order.append(f"eof:{stream}")

        def on_exited(exit_code, sig):
            self.exited.append((exit_code, sig))
            self.order.append("exited")

        def on_closed():
            self.order.append("closed")
            self.closed.set()

        client.on_data = on_data
        client.on_eof = on_eof
        client.on_exited = on_exited
        client.on_replaced = self.replaced.append
        client.on_closed = on_closed


async def connected(run: Run, epoch: int = 0) -> tuple[KeeperClient, Events, dict]:
    await run.running()
    client, events = KeeperClient(), Events()
    events.attach(client)
    try:
        await client.connect(run.socket, timeout=5)
        result = await client.initialize(epoch=epoch, looper_version="test", timeout=5)
    except BaseException:
        await client.close()
        raise
    return client, events, result


async def raw_connect(run: Run):
    await run.running()
    return await asyncio.wait_for(asyncio.open_unix_connection(run.socket), 5)


async def request(reader, writer, method, params, request_id=1):
    writer.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                             "method": method, "params": params}).encode() + b"\n")
    await asyncio.wait_for(writer.drain(), 5)
    while True:
        response = json.loads(await asyncio.wait_for(reader.readline(), 5))
        if "id" in response:
            assert response["id"] == request_id
            return response


async def close_writer(writer) -> None:
    writer.close()
    await asyncio.wait_for(writer.wait_closed(), 5)


def test_versions_are_negotiated(launch) -> None:
    assert keeper.PROTOCOL_VERSIONS == ("1.0",)
    assert negotiate(["invalid", "1.0", "1.7", "2.0"]) == "1.0"
    assert negotiate(["1", "1.0.0", "-1.0", "1.-1", " 1.0", "1.0 "]) is None

    async def check():
        run = launch()
        reader, writer = await raw_connect(run)
        try:
            response = await request(reader, writer, "initialize", {
                "protocol_versions": ["2.0"], "controller": {"looper_version": "test", "epoch": 0}})
            assert response["error"]["code"] == ErrorCode.VERSION_MISMATCH
            assert response["error"]["data"] == {"supported": ["1.0"]}
            assert await asyncio.wait_for(reader.read(), 5) == b""
        finally:
            await close_writer(writer)
        reader, writer = await raw_connect(run)
        try:
            response = await request(reader, writer, "initialize", {
                "protocol_versions": ["1.0", "1.7"], "controller": {"looper_version": "test", "epoch": 0}})
            result = response["result"]
            assert result["protocol_version"] == "1.0"
            assert result["keeper"]["pid"] == run.process.pid
            assert result["child"]["pid"] == run.state()["child_pid"]
            assert result["child"]["state"] == "running"
            assert result["offsets"] == {"stdout": 0, "stderr": 0, "stdin": 0}
            assert result["stdin_closed"] is False
            assert stat.S_IMODE(run.socket.stat().st_mode) == 0o600
            assert run.state()["socket"] == str(run.socket)
        finally:
            await close_writer(writer)

    exercise(check())


def test_frame_size_includes_the_newline() -> None:
    async def check():
        payload = {"data": "x" * (MAX_FRAME_BYTES - len(encode({"data": ""})))}
        frame = encode(payload)
        assert len(frame) == MAX_FRAME_BYTES and frame.endswith(b"\n")
        reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
        reader.feed_data(frame)
        reader.feed_eof()
        assert await read_frame(reader) == payload
        payload["data"] += "x"
        with pytest.raises(KeeperError) as error:
            encode(payload)
        assert error.value.code == ErrorCode.FRAME_TOO_LARGE
        oversized = frame[:-3] + b"x" + frame[-3:]
        assert len(oversized) == MAX_FRAME_BYTES + 1
        reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
        reader.feed_data(oversized)
        reader.feed_eof()
        with pytest.raises(KeeperError) as error:
            await read_frame(reader)
        assert error.value.code == ErrorCode.FRAME_TOO_LARGE

    exercise(check())


def test_an_overlong_socket_path_fails_before_spawning_a_child(launch) -> None:
    async def check():
        run = launch("from pathlib import Path; Path('spawned').touch()", socket_overlong=True)
        assert len(os.fsencode(run.socket)) > keeper.SOCKET_PATH_MAX
        await eventually(lambda: run.process.poll() is not None)
        assert run.process.returncode == 3, (run.directory / keeper.LOG).read_text()
        state = run.state()
        assert state["state"] == "exited"
        assert state["end_reason"] == "start_failed"
        assert state["error"] == "socket path too long"
        assert state["child_pid"] is None and state["child_started"] is None
        assert not (run.cwd / "spawned").exists()
        assert not run.socket.exists()

    exercise(check())


def test_initialize_waits_for_the_child_to_be_ready(launch) -> None:
    async def check():
        run = launch(delay_spawn=True)
        await eventually(lambda: (run.cwd / "spawn_waiting").exists())
        assert run.state()["state"] == "starting" and run.state()["child_pid"] is None
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(run.socket), 5)
        initialization = asyncio.create_task(request(reader, writer, "initialize", {
            "protocol_versions": ["1.0"], "controller": {"looper_version": "test", "epoch": 0}}))
        try:
            await asyncio.sleep(0.1)
            assert not initialization.done(), "initialize returned before child spawning completed"
            (run.cwd / "spawn_allowed").touch()
            result = (await asyncio.wait_for(initialization, 5))["result"]
            assert result["child"]["pid"] is not None
            assert result["child"]["started"] is not None
            assert result["child"]["state"] == "running"
            assert result["child"]["pid"] == run.state()["child_pid"]
        finally:
            (run.cwd / "spawn_allowed").touch()
            initialization.cancel()
            await asyncio.gather(initialization, return_exceptions=True)
            await close_writer(writer)

    exercise(check())


def test_subscribe_replays_then_streams_without_a_gap(launch) -> None:
    async def check():
        run = launch("""\
import pathlib, sys, time
for number in range(200):
    print(f'before {number}', flush=True)
pathlib.Path('ready').touch()
while not pathlib.Path('continue').exists():
    time.sleep(0.01)
for number in range(200):
    print(f'after {number}', flush=True)
    time.sleep(0.002)
""")
        await eventually(lambda: (run.cwd / "ready").exists())
        client, events, result = await connected(run)
        try:
            assert result["offsets"]["stdout"] > 0
            await client.subscribe(Stream.STDOUT, 0)
            await client.subscribe(Stream.STDERR, 0)
            (run.cwd / "continue").touch()
            await asyncio.wait_for(events.closed.wait(), 10)
            assert events.exited == [(0, None)]
            assert set(events.eof) == set(Stream)
            expected = (run.directory / keeper.STDOUT).read_bytes()
            assert bytes(events.data[Stream.STDOUT]) == expected
            assert len(expected.splitlines()) == 400
            assert await run.wait()
        finally:
            await client.close()

    exercise(check())


def test_subscribe_from_any_offset(launch) -> None:
    async def check():
        run = launch("""\
import sys
sys.stdout.buffer.write(b'first line\\nsecond line\\n')
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
""")
        await eventually(lambda: (run.directory / keeper.STDOUT).exists()
                         and (run.directory / keeper.STDOUT).stat().st_size == 23)
        first, _, _ = await connected(run)
        await first.close()
        client, events, _ = await connected(run)
        try:
            with pytest.raises(KeeperError) as error:
                await client.subscribe(Stream.STDOUT, 24)
            assert error.value.code == ErrorCode.INVALID_PARAMS
            assert error.value.data == {"length": 23}
            events.offsets[Stream.STDOUT] = 4
            await client.subscribe(Stream.STDOUT, 4)
            with pytest.raises(KeeperError) as error:
                await client.subscribe(Stream.STDOUT, 0)
            assert error.value.code == ErrorCode.INVALID_REQUEST
            await client.stdin_close()
            await asyncio.wait_for(events.closed.wait(), 10)
            assert bytes(events.data[Stream.STDOUT]) == b'first line\nsecond line\n'[4:]
            assert events.eof[Stream.STDOUT] == 23
        finally:
            await client.close()

    exercise(check())


def test_a_killed_controller_loses_nothing(launch) -> None:
    async def check():
        run = launch("""\
import pathlib, time
for number in range(100):
    print(f'first {number}', flush=True)
while not pathlib.Path('continue').exists():
    time.sleep(0.01)
for number in range(200):
    print(f'next {number}', flush=True)
    time.sleep(0.002)
""")
        await run.running()
        script = """\
import asyncio, os, sys
from looper.agents.keeper.client import KeeperClient
from looper.agents.keeper.protocol import Stream
async def main():
    client = KeeperClient()
    def received(stream, offset, data):
        with open(sys.argv[2], 'ab', buffering=0) as output:
            output.write(data)
            os.fsync(output.fileno())
    client.on_data = received
    await client.connect(sys.argv[1], timeout=5)
    await client.initialize(epoch=7, looper_version='test')
    await client.subscribe(Stream.STDOUT, 0)
    await asyncio.sleep(20)
asyncio.run(main())
"""
        received = run.cwd / "received"
        controller = subprocess.Popen([sys.executable, "-c", script, str(run.socket), str(received)],
                                      cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            await eventually(lambda: received.exists() and received.stat().st_size > 0)
            controller.kill()
            controller.wait(timeout=5)
            prefix = received.read_bytes()
            assert run.state()["state"] == "running"
            assert live(run.state()["child_pid"])
            client, events, _ = await connected(run, epoch=7)
            try:
                events.offsets[Stream.STDOUT] = len(prefix)
                await client.subscribe(Stream.STDOUT, len(prefix))
                (run.cwd / "continue").touch()
                await asyncio.wait_for(events.closed.wait(), 10)
                assert prefix + events.data[Stream.STDOUT] == (run.directory / keeper.STDOUT).read_bytes()
                assert events.exited == [(0, None)]
                assert Stream.STDOUT in events.eof
            finally:
                await client.close()
        finally:
            if controller.poll() is None:
                controller.kill()
            controller.wait(timeout=5)

    exercise(check())


def test_a_stdin_write_applies_once(launch) -> None:
    async def check():
        run = launch("""\
import os, time
while data := os.read(0, 65536):
    os.write(1, data)
time.sleep(0.2)
""")
        client, _, _ = await connected(run)
        try:
            assert await client.stdin_write(b'A\x00\xff\n', 0) == 4
            with pytest.raises(KeeperError) as error:
                await client.stdin_write(b'B\n', 0)
            assert error.value.code == ErrorCode.OFFSET_MISMATCH
            assert error.value.data == {"actual": 4}
        finally:
            await client.close()
        client, events, result = await connected(run)
        try:
            assert result["offsets"]["stdin"] == 4
            assert await client.stdin_write(b'B\n', 4) == 6
            await client.subscribe(Stream.STDOUT, 0)
            await client.stdin_close()
            await client.stdin_close()
            await asyncio.wait_for(events.closed.wait(), 10)
            assert (run.directory / keeper.STDIN).read_bytes() == b'A\x00\xff\nB\n'
            assert bytes(events.data[Stream.STDOUT]) == b'A\x00\xff\nB\n'
            assert events.exited == [(0, None)]
        finally:
            await client.close()

    exercise(check())


def test_a_stdin_write_accepts_exactly_one_mib(launch) -> None:
    async def check():
        run = launch("""\
import sys
data = sys.stdin.buffer.read()
print(len(data), data == bytes(range(256)) * 4096, flush=True)
""")
        client, events, _ = await connected(run)
        payload = bytes(range(256)) * 4096
        try:
            await client.subscribe(Stream.STDOUT, 0)
            assert len(payload) == 1024 * 1024
            assert await client.stdin_write(payload, 0) == len(payload)
            await client.stdin_close()
            await asyncio.wait_for(events.closed.wait(), 10)
            assert (run.directory / keeper.STDIN).read_bytes() == payload
            assert bytes(events.data[Stream.STDOUT]) == b"1048576 True\n"
            assert events.exited == [(0, None)]
        finally:
            await client.close()

    exercise(check())


def test_closed_stdin_is_reported_and_rejects_writes(launch) -> None:
    async def check():
        run = launch()
        client, _, _ = await connected(run)
        try:
            await client.stdin_close()
            await client.stdin_close()
            with pytest.raises(KeeperError) as error:
                await client.stdin_write(b"closed", 0)
            assert error.value.code == ErrorCode.CHILD_EXITED
        finally:
            await client.close()
        client, _, result = await connected(run)
        try:
            assert result["stdin_closed"] is True
            assert result["offsets"]["stdin"] == 0
            assert (run.directory / keeper.STDIN).read_bytes() == b""
        finally:
            await client.close()

    exercise(check())


def test_broken_child_stdin_preserves_the_logged_write(launch) -> None:
    async def check():
        run = launch("""\
import os, pathlib, time
os.close(0)
pathlib.Path('stdin_closed').touch()
time.sleep(60)
""")
        await eventually(lambda: (run.cwd / "stdin_closed").exists())
        payload = bytes(range(256)) * 4096
        client, _, _ = await connected(run)
        try:
            with pytest.raises(KeeperError) as error:
                await client.stdin_write(payload, 0, timeout=5)
            assert error.value.code == ErrorCode.CHILD_EXITED
            assert (run.directory / keeper.STDIN).read_bytes() == payload
            assert run.state()["state"] == "running"
            assert live(run.state()["child_pid"])
        finally:
            await client.close()
        client, _, result = await connected(run)
        try:
            assert result["stdin_closed"] is True
            assert result["offsets"]["stdin"] == len(payload)
            with pytest.raises(KeeperError) as error:
                await client.stdin_write(b"later", len(payload), timeout=5)
            assert error.value.code == ErrorCode.CHILD_EXITED
            assert (run.directory / keeper.STDIN).read_bytes() == payload
            await client.end(timeout=5)
            await run.wait()
        finally:
            await client.close()

    exercise(check())


def test_replacing_a_controller_cancels_a_blocked_stdin_write(launch) -> None:
    async def check():
        run = launch()
        old, old_events, _ = await connected(run, 10)
        new, write_task = None, None
        payload = bytes(range(256)) * 4096
        try:
            write_task = asyncio.create_task(old.stdin_write(payload, 0, timeout=10))
            await eventually(lambda: (run.directory / keeper.STDIN).stat().st_size == len(payload))
            assert not write_task.done(), "the child must keep stdin drain blocked"
            assert run.state()["state"] == "running"
            new, new_events, result = await connected(run, 11)
            assert result["offsets"]["stdin"] == len(payload)
            await asyncio.wait_for(old_events.closed.wait(), 5)
            assert old_events.replaced == [11]
            with pytest.raises(KeeperGone):
                await asyncio.wait_for(write_task, 5)
            # A stale offset is checked under stdin_lock; returning proves the old handler released it.
            with pytest.raises(KeeperError) as error:
                await new.stdin_write(b"", 0, timeout=2)
            assert error.value.code == ErrorCode.OFFSET_MISMATCH
            assert error.value.data == {"actual": len(payload)}
            assert await new.end(timeout=5) == (None, signal.SIGTERM)
            await asyncio.wait_for(new_events.closed.wait(), 5)
            assert new_events.exited == [(None, signal.SIGTERM)]
            assert (await run.wait())["end_reason"] == "ended"
            assert (run.directory / keeper.STDIN).read_bytes() == payload
            assert not run.socket.exists()
        finally:
            if write_task is not None:
                if not write_task.done():
                    write_task.cancel()
                await asyncio.gather(write_task, return_exceptions=True)
            await old.close()
            if new is not None:
                await new.close()

    exercise(check())


def test_epochs_fence_old_controllers(launch) -> None:
    async def check():
        run = launch()
        first, first_events, _ = await connected(run, 5)
        stale, stale_events = KeeperClient(), Events()
        stale_events.attach(stale)
        newest = equal = None
        try:
            await stale.connect(run.socket, timeout=5)
            with pytest.raises(KeeperError) as error:
                await stale.initialize(epoch=4, looper_version="test")
            assert error.value.code == ErrorCode.STALE_CONTROLLER
            assert error.value.data == {"epoch": 5}
            await asyncio.wait_for(stale_events.closed.wait(), 5)
            newest, newest_events, _ = await connected(run, 6)
            await asyncio.wait_for(first_events.closed.wait(), 5)
            assert first_events.replaced == [6]
            with pytest.raises((KeeperGone, KeeperError)):
                await first.stdin_write(b"forbidden", 0)
            assert run.state()["controller_epoch"] == 6
            assert (run.directory / keeper.STDIN).read_bytes() == b""
            equal, _, _ = await connected(run, 6)
            await asyncio.wait_for(newest_events.closed.wait(), 5)
            assert newest_events.replaced == [6]
            assert run.state()["controller_epoch"] == 6
        finally:
            for client in (first, stale, newest, equal):
                if client is not None:
                    await client.close()

    exercise(check())


def test_signal_and_end(launch) -> None:
    async def check():
        run = launch()
        client, events, _ = await connected(run)
        try:
            await client.signal(KeeperSignal.TERM)
            await asyncio.wait_for(events.closed.wait(), 10)
            assert events.exited == [(None, signal.SIGTERM)]
        finally:
            await client.close()
        run = launch("""\
import json, os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
with open('tree.json', 'w') as output:
    json.dump({'child': os.getpid(), 'grandchild': grandchild.pid}, output)
while True:
    time.sleep(0.1)
""")
        tree = await eventually(lambda: keeper.read_json(run.cwd / "tree.json"))
        for pid in tree.values():
            started = proc_tree._start_time(pid)
            assert started is not None
            run.identities[pid] = started
        client, events, _ = await connected(run)
        try:
            assert await client.end() == (None, signal.SIGKILL)
            await asyncio.wait_for(events.closed.wait(), 10)
            assert events.exited == [(None, signal.SIGKILL)]
            assert (await run.wait())["end_reason"] == "ended"
            assert all(not live(pid) for pid in tree.values())
        finally:
            await client.close()

    exercise(check())


def test_the_orphan_timer_follows_the_controller(launch) -> None:
    async def check():
        run = launch(manifest={"orphan_timeout_s": 1})
        client, _, _ = await connected(run)
        try:
            await asyncio.sleep(3)
            assert run.state()["state"] == "running"
            assert live(run.state()["child_pid"])
        finally:
            await client.close()
        await asyncio.sleep(0.3)
        client, _, _ = await connected(run)
        try:
            await asyncio.sleep(1.2)
            assert run.state()["state"] == "running"
        finally:
            disconnected_at = time.monotonic()
            await client.close()
        state = await run.wait()
        elapsed = time.monotonic() - disconnected_at
        assert 0.8 <= elapsed < 5
        assert state["end_reason"] == "orphaned"

    exercise(check())


def test_an_oversized_frame_closes_only_that_connection(launch) -> None:
    async def check():
        run = launch()
        reader, writer = await raw_connect(run)
        try:
            writer.write(b'x' * (5 * 1024 * 1024) + b'\n')
            # The server can close while the transport still has oversized bytes queued.
            try:
                await asyncio.wait_for(writer.drain(), 5)
            except (BrokenPipeError, ConnectionResetError):
                pass
            response = json.loads(await asyncio.wait_for(reader.readline(), 5))
            assert response["error"]["code"] == ErrorCode.FRAME_TOO_LARGE
            assert response["id"] is None
            try:
                assert await asyncio.wait_for(reader.read(), 5) == b""
            except ConnectionResetError:
                pass
        finally:
            try:
                await close_writer(writer)
            except (BrokenPipeError, ConnectionResetError):
                pass
        client, _, result = await connected(run)
        try:
            assert result["child"]["state"] == "running"
            assert run.process.poll() is None
        finally:
            await client.close()

    exercise(check())


def test_the_keeper_delivers_everything_before_exiting(launch) -> None:
    async def check():
        run = launch("""\
import pathlib, sys, time
while not pathlib.Path('continue').exists():
    time.sleep(0.01)
sys.stdout.buffer.write(bytes(range(256)) * 4096)
sys.stderr.buffer.write(b'error\\x00\\xff' * 1000)
""")
        client, events, _ = await connected(run)
        try:
            await client.subscribe(Stream.STDOUT, 0)
            await client.subscribe(Stream.STDERR, 0)
            (run.cwd / "continue").touch()
            await asyncio.wait_for(events.closed.wait(), 15)
            assert bytes(events.data[Stream.STDOUT]) == bytes(range(256)) * 4096
            assert bytes(events.data[Stream.STDERR]) == b'error\x00\xff' * 1000
            assert events.exited == [(0, None)]
            assert set(events.eof) == set(Stream)
            assert events.order[-1] == "closed"
            assert events.order.index("exited") < events.order.index("closed")
            for stream in Stream:
                assert events.order.index(f"eof:{stream}") < events.order.index("closed")
            assert (await run.wait())["state"] == "exited"
            assert not run.socket.exists()
        finally:
            await client.close()

    exercise(check())


def test_bad_requests_do_not_break_the_controller(launch) -> None:
    async def check():
        run = launch()
        reader, writer = await raw_connect(run)
        try:
            writer.write(b'{bad json\n')
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), 5))
            assert response["id"] is None
            assert response["error"]["code"] == ErrorCode.PARSE
            response = await request(reader, writer, "stdin/close", {})
            assert response["error"]["code"] == ErrorCode.NOT_INITIALIZED
            response = await request(reader, writer, "initialize", {
                "protocol_versions": ["1.0"], "controller": {"looper_version": "test", "epoch": 0}})
            assert "result" in response
            response = await request(reader, writer, "initialize", {
                "protocol_versions": ["1.0"], "controller": {"looper_version": "test", "epoch": 0}})
            assert response["error"]["code"] == ErrorCode.INVALID_REQUEST
            response = await request(reader, writer, "not/a/method", {})
            assert response["error"]["code"] == ErrorCode.METHOD_NOT_FOUND
            bad_params = [
                ("stdin/write", {"data": "!not-base64!", "expect_offset": 0}),
                ("stdin/write", {"data": base64.b64encode(b'x' * (1024 * 1024 + 1)).decode(), "expect_offset": 0}),
                ("stdin/write", {"data": "", "expect_offset": -1}),
                ("stream/subscribe", {"stream": "stdout", "from_offset": -1}),
                ("stream/subscribe", {"stream": "unknown", "from_offset": 0}),
                ("child/signal", {"signal": "HUP"}),
                ("child/end", {"grace_seconds": 0}),
            ]
            for method, params in bad_params:
                response = await request(reader, writer, method, params)
                assert response["error"]["code"] == ErrorCode.INVALID_PARAMS, (method, response)
            assert (run.directory / keeper.STDIN).read_bytes() == b""
        finally:
            await close_writer(writer)

    exercise(check())


@asynccontextmanager
async def fake_server(handler):
    tasks, writers = [], []

    async def serve(reader, writer):
        writers.append(writer)
        message = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert message["method"] == "initialize"
        writer.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocol_version": "1.0", "keeper": {"version": "test", "pid": 1, "started": "test"},
            "child": {"pid": 2, "started": "test", "state": "running", "exit_code": None, "signal": None},
            "offsets": {"stdout": 0, "stderr": 0, "stdin": 0}, "stdin_closed": False,
        }}).encode() + b'\n')
        await writer.drain()
        await handler(reader, writer)

    def accept(reader, writer):
        tasks.append(asyncio.create_task(serve(reader, writer)))

    with tempfile.TemporaryDirectory(dir=ROOT, prefix=".k") as directory:
        path = Path(directory) / keeper.SOCKET
        server = await asyncio.start_unix_server(accept, path)
        try:
            yield path
        finally:
            server.close()
            await server.wait_closed()
            for writer in writers:
                await close_writer(writer)
            for task in tasks:
                if not task.done():
                    task.cancel()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                    raise outcome


def test_the_client_closes_on_a_stream_gap() -> None:
    async def check():
        async def gap(reader, writer):
            message = json.loads(await asyncio.wait_for(reader.readline(), 5))
            assert message["method"] == "stream/subscribe"
            writer.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}).encode() + b'\n')
            writer.write(json.dumps({"jsonrpc": "2.0", "method": "stream/data", "params": {
                "stream": "stdout", "offset": 1, "data": base64.b64encode(b'gap').decode(),
            }}).encode() + b'\n')
            await writer.drain()
            await asyncio.wait_for(reader.read(), 5)

        async with fake_server(gap) as path:
            client, events = KeeperClient(), Events()
            events.attach(client)
            try:
                await client.connect(path, timeout=5)
                await client.initialize(epoch=0, looper_version="test")
                await client.subscribe(Stream.STDOUT, 0)
                await asyncio.wait_for(events.closed.wait(), 5)
                assert events.data[Stream.STDOUT] == b""
                with pytest.raises(KeeperGone):
                    await client.stdin_close()
            finally:
                await client.close()

    exercise(check())


def test_the_client_fails_every_pending_request_when_the_keeper_disappears() -> None:
    async def check():
        async def disappear(reader, writer):
            requests = [json.loads(await asyncio.wait_for(reader.readline(), 5)) for _ in range(2)]
            assert {request["method"] for request in requests} == {"child/signal", "stdin/close"}
            await close_writer(writer)

        async with fake_server(disappear) as path:
            client, events = KeeperClient(), Events()
            events.attach(client)
            try:
                await client.connect(path, timeout=5)
                await client.initialize(epoch=0, looper_version="test")
                results = await asyncio.wait_for(asyncio.gather(
                    client.signal(KeeperSignal.TERM), client.stdin_close(), return_exceptions=True), 5)
                assert len(results) == 2
                assert all(isinstance(result, KeeperGone) for result in results)
                await asyncio.wait_for(events.closed.wait(), 5)
            finally:
                await client.close()

    exercise(check())
