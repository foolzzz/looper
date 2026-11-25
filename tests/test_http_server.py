"""The HTTP server handles one request and closes the socket.

HTTP/1.1 keeps a connection whose response omits ``Connection: close``. A client
that writes the next request before it sees the close races the server and gets
``Server disconnected without sending a response``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from looper.agents.http_server import AgentHTTPServer

TOKEN = "operator_" + "a" * 32


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
