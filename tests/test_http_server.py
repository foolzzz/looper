"""The HTTP server handles one request and closes the socket.

HTTP/1.1 keeps a connection whose response omits ``Connection: close``. A client
that writes the next request before it sees the close races the server and gets
``Server disconnected without sending a response``.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from looper.agents.http_server import AgentHTTPServer
from looper.agents import http_server

TOKEN = "operator_" + "a" * 32


def test_shutdown_aborts_a_stream_whose_peer_never_reads(tmp_path, monkeypatch, caplog):
    async def run():
        monkeypatch.setattr(http_server, "HTTP_SHUTDOWN_GRACE_SECONDS", .05, raising=False)
        monkeypatch.setattr(http_server, "HTTP_SHUTDOWN_CLEANUP_SECONDS", .05, raising=False)
        server = AgentHTTPServer(port=0, runtime_root=tmp_path,
            web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace())
        blocked = asyncio.Event()
        writers = []

        async def stream(path, writer, **kwargs):
            writers.append(writer)
            writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            # A real TCP peer never reads, so drain and then wait_closed cannot flush.
            while True:
                writer.write(b"data: " + b"x" * (1024 * 1024) + b"\n\n")
                if writer.transport.get_write_buffer_size():
                    blocked.set()
                await writer.drain()

        monkeypatch.setattr(server, "_serve_static", stream)
        await server.start()
        assert server._port != 8767
        peer = socket.socket()
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        peer.setblocking(False)
        stopping = None
        try:
            loop = asyncio.get_running_loop()
            await loop.sock_connect(peer, ("127.0.0.1", server._port))
            await loop.sock_sendall(peer, b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await asyncio.wait_for(blocked.wait(), 2)
            handlers = list(server._handlers)
            assert handlers and not handlers[0].done()
            assert writers[0].transport.get_write_buffer_size() > 0
            stopping = asyncio.create_task(server.stop())
            await asyncio.wait_for(asyncio.shield(stopping), .5)
            assert not server._handlers
            assert writers[0].transport.get_write_buffer_size() == 0
            assert "aborting 1 HTTP connection" in caplog.text
        finally:
            for writer in writers:
                writer.transport.abort()
            peer.close()
            if stopping is not None:
                await asyncio.wait_for(stopping, 1)
            else:
                await server.stop()

    asyncio.run(run())


@pytest.mark.parametrize("finish", [True, False])
def test_shutdown_gives_inflight_requests_a_grace_period(tmp_path, monkeypatch, finish):
    async def run():
        monkeypatch.setattr(http_server, "HTTP_SHUTDOWN_GRACE_SECONDS", .1, raising=False)
        server = AgentHTTPServer(port=0, runtime_root=tmp_path,
            web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace())
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def response(path, writer, **kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()

        monkeypatch.setattr(server, "_serve_static", response)
        await server.start()
        assert server._port != 8767
        reader, writer = await asyncio.open_connection("127.0.0.1", server._port)
        stopping = None
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 1)
            stopping = asyncio.create_task(server.stop())
            await asyncio.sleep(.02)
            assert not cancelled.is_set() and not stopping.done()
            if finish:
                release.set()
            reply = await asyncio.wait_for(reader.read(), 1)
            await asyncio.wait_for(stopping, 1)
            assert cancelled.is_set() is (not finish)
            assert reply.endswith(b"\r\n\r\nok") if finish else reply == b""
            assert not server._handlers
        finally:
            release.set()
            writer.close()
            await writer.wait_closed()
            if stopping is not None:
                await stopping
            await server.stop()

    asyncio.run(run())


def test_shutdown_aborts_a_connection_whose_handler_has_not_started(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(http_server, "HTTP_SHUTDOWN_GRACE_SECONDS", .05, raising=False)
        monkeypatch.setattr(http_server, "HTTP_SHUTDOWN_CLEANUP_SECONDS", .05, raising=False)
        server = AgentHTTPServer(port=0, runtime_root=tmp_path,
            web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace())
        release, started = asyncio.Event(), asyncio.Event()
        transports = []
        original = asyncio.start_server

        async def start_server(callback, *args, **kwargs):
            async def held(reader, writer):
                # Hold the accept callback before _handle_connection registers.
                transports.append(writer.transport)
                started.set()
                await release.wait()
                if writer.transport is not None and not writer.transport.is_closing():
                    await callback(reader, writer)

            return await original(held, *args, **kwargs)

        monkeypatch.setattr(asyncio, "start_server", start_server)
        await server.start()
        assert server._port != 8767
        reader, writer = await asyncio.open_connection("127.0.0.1", server._port)
        stopping = None
        try:
            await asyncio.wait_for(started.wait(), 1)
            assert transports and not server._handlers and not transports[0].is_closing()
            stopping = asyncio.create_task(server.stop())
            await asyncio.wait_for(asyncio.shield(stopping), .6)
            assert transports[0].is_closing()
            try:
                payload = await asyncio.wait_for(reader.read(), .5)
            except ConnectionResetError:
                payload = b""
            assert payload == b""
        finally:
            release.set()
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            if stopping is not None and not stopping.done():
                stopping.cancel()
            await server.stop()

    asyncio.run(run())


def test_keepalive_client_is_told_when_the_server_closes(tmp_path: Path) -> None:
    async def run() -> None:
        server = AgentHTTPServer(
            port=0, operator_token=TOKEN, runtime_root=tmp_path,
            web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace(),
        )
        accepts = 0
        delayed: list[asyncio.StreamWriter] = []
        release = asyncio.Event()
        original_close = asyncio.StreamWriter.close
        original_wait_closed = asyncio.StreamWriter.wait_closed

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal accepts
            accepts += 1
            await AgentHTTPServer._handle_connection(server, reader, writer)

        def close(self: asyncio.StreamWriter) -> None:
            # Hold the first close until the client has either reused the socket or opened another.
            if not delayed:
                delayed.append(self)
                return
            original_close(self)

        async def wait_closed(self: asyncio.StreamWriter) -> None:
            if delayed and delayed[0] is self and not release.is_set():
                await release.wait()
            await original_wait_closed(self)

        asyncio.StreamWriter.close = close  # type: ignore[method-assign]
        asyncio.StreamWriter.wait_closed = wait_closed  # type: ignore[method-assign]
        server._handle_connection = handle  # type: ignore[method-assign]
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        assert port != 8767
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False,
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as client:
                first = await client.post("/api/v1/whoami", json={})
                assert first.status_code == 405, first.text
                assert delayed, "the server did not close after the response"
                reader = delayed[0].transport.get_protocol()._stream_reader
                assert not reader._buffer

                second_task = asyncio.create_task(client.post("/api/v1/whoami", json={}))
                reused = False
                deadline = asyncio.get_running_loop().time() + 2
                while asyncio.get_running_loop().time() < deadline:
                    pending = bytes(reader._buffer)
                    if pending.startswith((b"POST", b"GET ")):
                        reused = True
                        break
                    if accepts >= 2 or second_task.done():
                        break
                    await asyncio.sleep(0.01)
                if reused:
                    # The server already finished the request. Dropping the socket now is the
                    # interleaving a keep-alive client loses: the next request has no response.
                    original_close(delayed[0])
                    release.set()
                    with pytest.raises(httpx.RemoteProtocolError, match="Server disconnected"):
                        await second_task
                    raise AssertionError(
                        "client reused a connection the server closed without Connection: close")
                second = await second_task
                assert second.status_code == 405, second.text
                assert accepts >= 2
                assert first.headers.get("connection", "").lower() == "close"
        finally:
            asyncio.StreamWriter.close = original_close  # type: ignore[method-assign]
            asyncio.StreamWriter.wait_closed = original_wait_closed  # type: ignore[method-assign]
            if delayed:
                original_close(delayed[0])
            release.set()
            await server.stop()

    asyncio.run(run())
