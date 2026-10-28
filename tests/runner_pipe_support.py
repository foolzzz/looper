"""Real runner pipes shared by transport, session and run-channel tests."""
import asyncio
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
from typing import get_args

from looper.agents.runtimes.runner import protocol as p
from looper.agents.runtimes.runner.bridge import RunnerBridge


async def pipe():
    read_fd, write_fd = os.pipe()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb"))
    protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
    transport, _ = await loop.connect_write_pipe(lambda: protocol, os.fdopen(write_fd, "wb"))
    return reader, asyncio.StreamWriter(transport, protocol, None, loop), read_transport


@asynccontextmanager
async def connection(handshake=None, **options):
    host_reader, runner_writer, rt = await pipe()
    runner_reader, host_writer, ht = await pipe()
    failures = []
    bridge = RunnerBridge(host_reader, host_writer, "fake", on_failure=failures.append, **options)
    try:
        init = asyncio.create_task(bridge.initialize())
        req = await p.read_frame(runner_reader)
        caps = {"cancel": True, "usage": {"per_request": True}, "authorize_covers": list(get_args(p.RequestKind)),
                "steer": {"in_trace": True, "boundary": "after_tool_batch", "idle_start": False}}
        result = {"protocol_version": "0.1",
            "runner": {"kind": "fake", "version": "1"}, "sdk": {"name": "fake", "version": "1"},
            "capabilities": caps, **(handshake or {})}
        await send(runner_writer, p.Response(req.id, result=result))
        await init
        yield bridge, runner_reader, runner_writer, failures
    finally:
        runner_writer.close()
        await bridge.close()
        rt.close()
        ht.close()
        await runner_writer.wait_closed()


async def send(writer, message):
    writer.write(p.encode_frame(message))
    await writer.drain()


class FakeSupervisor:
    """Leases exercise RunnerSession ownership over the production bridge."""

    def __init__(self, bridge, directory, *, failure=None):
        self.bridge, self.directory, self.failure = bridge, directory, failure
        self.leases = []

    async def acquire(self, kind, *, owner=None, state_dir=None):
        assert kind == "fake" and owner is None
        if self.failure is not None:
            raise self.failure
        lease = SimpleNamespace(bridge=self.bridge, runner_id="fake-1", valid=True,
                                session_id=None, state_dir=state_dir)

        async def open_session(params):
            lease.session_id = await self.bridge.open_session({**params,
                "session_key": f"run-{len(self.leases)}", "home": str(self.directory),
                "state_dir": str(state_dir or self.directory)})
            return lease.session_id

        async def release():
            lease.valid = False
            if not self.bridge._error and lease.session_id in self.bridge.sessions:
                await self.bridge.close_session(lease.session_id)

        lease.open_session, lease.release = open_session, release
        self.leases.append(lease)
        return lease
