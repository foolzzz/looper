"""Synthetic, loopback-only Responses HTTP/SSE scripts for real SDK tests."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import time
from typing import Any, Sequence


@dataclass
class ResponseScript:
    status: int = 200
    events: Sequence[dict[str, Any] | str] = ()
    error: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    disconnect_after: int | None = None
    gate: asyncio.Event | None = None
    gate_reached: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    closed: asyncio.Event = field(default_factory=asyncio.Event, init=False)


@dataclass(frozen=True)
class CapturedRequest:
    path: str
    headers: dict[str, str]
    body: dict[str, Any]
    received_at: float = field(default_factory=time.monotonic)


class FakeModelAPI:
    def __init__(self, scripts: Sequence[ResponseScript]):
        self.scripts = list(scripts)
        self.requests: list[CapturedRequest] = []
        self.closed: asyncio.Queue[int] = asyncio.Queue()
        self._handlers: set[asyncio.Task[Any]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._errors: list[str] = []

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.base_url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/v1"
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._server.close()
        await self._server.wait_closed()
        writers, handlers = tuple(self._writers), tuple(self._handlers)
        for writer in writers:
            writer.close()
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await asyncio.gather(*(writer.wait_closed() for writer in writers), return_exceptions=True)
        if exc_type is None and self._errors:
            raise AssertionError("; ".join(self._errors))

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._handlers.add(task)
        self._writers.add(writer)
        script, index = None, len(self.requests)
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            lines = head.split("\r\n")
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            headers = {key.lower(): value.strip() for key, value in headers.items()}
            body = json.loads(await reader.readexactly(int(headers.get("content-length", "0"))))
            index = len(self.requests)
            self.requests.append(CapturedRequest(lines[0].split()[1], headers, body))
            if index >= len(self.scripts):
                self._errors.append("Unexpected extra model request")
                writer.write(b"HTTP/1.1 500 Unexpected request\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            script = self.scripts[index]
            if headers.get("authorization") != "Bearer test-key":
                self._errors.append("Fake model requires the synthetic test key")
            response_headers = {"Connection": "close", **(script.headers or {})}
            if script.status != 200:
                payload = json.dumps(script.error or {"error": {"message": "scripted error"}}).encode()
                response_headers.update({"Content-Type": "application/json", "Content-Length": str(len(payload))})
            else:
                payload = b""
                response_headers.update({"Content-Type": "text/event-stream", "Transfer-Encoding": "chunked"})
            wire_head = f"HTTP/1.1 {script.status} Scripted\r\n" + "".join(f"{k}: {v}\r\n" for k, v in response_headers.items())
            writer.write(wire_head.encode() + b"\r\n" + payload)
            await writer.drain()
            if script.status != 200:
                return
            for count, envelope in enumerate(script.events):
                if script.disconnect_after is not None and count >= script.disconnect_after:
                    writer.transport.abort()
                    return
                if isinstance(envelope, dict) and envelope.get("type") in {"response.completed", "response.failed", "response.incomplete"} and script.gate is not None:
                    script.gate_reached.set()
                    opened, disconnected = asyncio.create_task(script.gate.wait()), asyncio.create_task(reader.read(1))
                    try:
                        done, _ = await asyncio.wait({opened, disconnected}, return_when=asyncio.FIRST_COMPLETED)
                        if disconnected in done:
                            return
                    finally:
                        opened.cancel()
                        disconnected.cancel()
                        await asyncio.gather(opened, disconnected, return_exceptions=True)
                data = envelope if isinstance(envelope, str) else json.dumps(envelope)
                chunk = f"data: {data}\n\n".encode()
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
            if script.disconnect_after is not None and len(script.events) >= script.disconnect_after:
                writer.transport.abort()
                return
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as error:
            self._errors.append(f"Fake model handler failed: {type(error).__name__}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError):
                pass
            self._writers.discard(writer)
            self._handlers.discard(task)
            if script is not None:
                script.closed.set()
                self.closed.put_nowait(index)


def event(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, **fields}


def text_item(text: str, id: str = "msg_1", *, refusal: bool = False):
    content = {"type": "refusal", "refusal": text} if refusal else {"type": "output_text", "text": text, "annotations": []}
    return {"type": "message", "id": id, "role": "assistant", "status": "completed", "content": [content]}


def call_item(arguments: str = '{}', name: str = "lookup", call_id: str = "call_1", id: str = "fc_1"):
    return {"type": "function_call", "id": id, "call_id": call_id, "name": name, "arguments": arguments, "status": "completed"}


def reasoning_item(summary: str = "Thinking", encrypted: str = "opaque", id: str = "rs_1"):
    return {"type": "reasoning", "id": id, "summary": [{"type": "summary_text", "text": summary}], "encrypted_content": encrypted}


def completed(output=(), usage=None, id: str = "resp_1"):
    return event("response.completed", response={"id": id, "object": "response", "status": "completed", "output": list(output), "usage": usage})


def failed(code: str = "server_error", message: str = "scripted failure"):
    return event("response.failed", response={"id": "resp_1", "status": "failed", "output": [], "error": {"code": code, "message": message}})


def text_events(text: str = "Hello", **completion):
    item = text_item(text)
    return [event("response.output_text.delta", output_index=0, item_id=item["id"], content_index=0, delta=text), completed([item], **completion)]


def call_events(arguments: str = '{}', **fields):
    item = call_item(arguments, **fields)
    return [event("response.output_item.added", output_index=0, item={**item, "arguments": ""}), *[event("response.function_call_arguments.delta", output_index=0, item_id=item["id"], delta=part) for part in (arguments[:1], arguments[1:])], event("response.output_item.done", output_index=0, item=item), completed([item])]
