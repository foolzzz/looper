"""One fenced controller and independent, replayable file subscriptions."""

import asyncio
import base64
import logging
import os
import signal

from . import CHUNK, SOCKET, SOCKET_PATH_MAX, STDERR, STDIN, STDOUT, EndReason
from .protocol import (MAX_FRAME_BYTES, MAX_STDIN_BYTES, PROTOCOL_VERSIONS, ErrorCode,
                       KeeperError, KeeperSignal, Stream, decode_data, encode, negotiate, read_frame)

logger = logging.getLogger(__name__)


class KeeperServer:
    def __init__(self, keeper):
        self.keeper = keeper
        self.path = keeper.run_dir / SOCKET
        self.controller = None
        self.highest_epoch = -1
        self.connections = set()
        self.stdin_lock = asyncio.Lock()
        self.listener = None

    async def start(self):
        if len(os.fsencode(self.path)) > SOCKET_PATH_MAX:
            raise OSError("socket path too long")
        self.listener = await asyncio.start_unix_server(self._accept, path=self.path, limit=MAX_FRAME_BYTES)
        os.chmod(self.path, 0o600)
        self.keeper._write_state(socket=str(self.path))

    async def _accept(self, reader, writer):
        connection = _Connection(self, reader, writer)
        self.connections.add(connection)
        try:
            await connection.run()
        finally:
            self.connections.discard(connection)
            connection.closed.set()

    async def finish(self):
        async def deliver():
            while self.controller is not None:
                connection = self.controller
                try:
                    await connection.notify("child/exited", self.exit_status())
                    # Include a child/end reply and subscriptions accepted before shutdown.
                    await connection.idle.wait()
                    await asyncio.gather(*connection.subscriptions.values(), return_exceptions=True)
                except (ConnectionError, OSError):
                    if self.controller is connection:
                        break
                if self.controller is connection:
                    break
        try:
            await asyncio.wait_for(deliver(), 10)
        except (TimeoutError, ConnectionError, OSError):
            pass

    def exit_status(self):
        state = self.keeper.state
        return {"exit_code": state["exit_code"], "signal": state["signal"], "at": state["ended_at"]}

    async def close(self):
        if self.listener is not None:
            self.listener.close()
            await self.listener.wait_closed()
            self.path.unlink(missing_ok=True)
        for connection in tuple(self.connections):
            connection.writer.close()
            if not connection.task.cancelling():
                connection.task.cancel()
        await asyncio.gather(*(c.closed.wait() for c in tuple(self.connections)))


class _Connection:
    def __init__(self, server, reader, writer):
        self.server, self.reader, self.writer = server, reader, writer
        self.task = asyncio.current_task()
        self.initialized, self.epoch = False, None
        self.subscriptions = {}
        self.requests = set()
        self.closed, self.idle = asyncio.Event(), asyncio.Event()
        self.idle.set()

    async def send(self, message):
        self.writer.write(encode({"jsonrpc": "2.0", **message}))
        await self.writer.drain()

    async def notify(self, method, params):
        await self.send({"method": method, "params": params})

    def check_controller(self):
        if not self.initialized:
            raise KeeperError(ErrorCode.NOT_INITIALIZED, "initialize first")
        if self.server.controller is not self:
            raise KeeperError(ErrorCode.NOT_CONTROLLER, "connection is not the controller")

    async def run(self):
        try:
            while not self.writer.is_closing():
                try:
                    message = await read_frame(self.reader)
                except KeeperError as exc:
                    await self.reject(None, exc)
                    if exc.code == ErrorCode.FRAME_TOO_LARGE:
                        # Let the rejection reach a sender still flushing this frame.
                        try:
                            async with asyncio.timeout(1):
                                discarded = 0
                                while discarded < 2 * MAX_FRAME_BYTES:
                                    chunk = await self.reader.read(CHUNK)
                                    discarded += len(chunk)
                                    if not chunk or b"\n" in chunk:
                                        break
                        except TimeoutError:
                            pass
                        break
                    continue
                # Keep reading control requests and EOF while stdin is backpressured.
                self.idle.clear()
                self.requests.add(asyncio.create_task(self.respond(message)))
        except (EOFError, ConnectionError, OSError):
            pass
        except Exception:
            logger.exception("keeper control connection failed")
            try:
                await self.reject(None, KeeperError(ErrorCode.INTERNAL, "internal error"))
            except (ConnectionError, OSError):
                pass
        finally:
            if self.server.controller is self:
                self.server.controller = None
                if self.server.keeper.child is not None and self.server.keeper.child.returncode is None:
                    self.server.keeper.controller_detached()
            tasks = [*self.requests, *self.subscriptions.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.idle.set()
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), 1)
            except (TimeoutError, ConnectionError, OSError):
                self.writer.transport.abort()
            except asyncio.CancelledError:
                self.writer.transport.abort()
                raise

    async def reject(self, request_id, exc):
        error = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        await self.send({"id": request_id, "error": error})

    async def respond(self, message):
        request_id = None
        try:
            try:
                if self.writer.is_closing():
                    return
                if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                        or type(message.get("id")) is not int or not isinstance(message.get("method"), str)):
                    raise KeeperError(ErrorCode.INVALID_REQUEST, "invalid request")
                request_id = message["id"]
                params = message.get("params")
                if not isinstance(params, dict):
                    raise KeeperError(ErrorCode.INVALID_PARAMS, "params must be an object")
                result = await self.dispatch(message["method"], params)
                await self.send({"id": request_id, "result": result})
            except KeeperError as exc:
                await self.reject(request_id, exc)
                if exc.code in (ErrorCode.FRAME_TOO_LARGE, ErrorCode.VERSION_MISMATCH,
                                ErrorCode.STALE_CONTROLLER):
                    self.writer.close()
                    self.task.cancel()
        except (ConnectionError, OSError):
            self.task.cancel()
        except Exception:
            logger.exception("keeper control request failed")
            try:
                await self.reject(request_id, KeeperError(ErrorCode.INTERNAL, "internal error"))
            except (ConnectionError, OSError):
                pass
            finally:
                self.task.cancel()
        finally:
            self.requests.discard(asyncio.current_task())
            if not self.requests:
                self.idle.set()

    async def dispatch(self, method, params):
        keeper = self.server.keeper
        if method == "initialize":
            if self.initialized:
                raise KeeperError(ErrorCode.INVALID_REQUEST, "already initialized")
            controller, offered = params.get("controller"), params.get("protocol_versions")
            if (set(params) != {"controller", "protocol_versions"} or not isinstance(controller, dict)
                    or set(controller) != {"looper_version", "epoch"}
                    or not isinstance(controller["looper_version"], str)
                    or type(controller["epoch"]) is not int or controller["epoch"] < 0
                    or not isinstance(offered, list) or any(not isinstance(v, str) for v in offered)):
                raise KeeperError(ErrorCode.INVALID_PARAMS, "invalid initialize parameters")
            version = negotiate(offered)
            if version is None:
                raise KeeperError(ErrorCode.VERSION_MISMATCH, "no common protocol version",
                                  {"supported": PROTOCOL_VERSIONS})
            await keeper.ready.wait()
            if self.initialized:
                raise KeeperError(ErrorCode.INVALID_REQUEST, "already initialized")
            epoch = controller["epoch"]
            if epoch < self.server.highest_epoch:
                raise KeeperError(ErrorCode.STALE_CONTROLLER, "stale controller", {"epoch": self.server.highest_epoch})
            previous = self.server.controller
            self.initialized, self.epoch = True, epoch
            self.server.controller, self.server.highest_epoch = self, epoch
            keeper._write_state(controller_epoch=epoch)
            keeper.controller_attached()
            if previous is not None:
                previous.writer.write(encode({"jsonrpc": "2.0", "method": "controller/replaced", "params": {"epoch": epoch}}))
                previous.writer.close()
                previous.task.cancel()
            state = keeper.state
            return {"protocol_version": version,
                    "keeper": {"version": state["keeper_version"], "pid": state["keeper_pid"], "started": state["keeper_started"]},
                    "child": {"pid": state["child_pid"], "started": state["child_started"], "state": state["state"],
                              "exit_code": state["exit_code"], "signal": state["signal"]},
                    "offsets": {"stdout": keeper.offsets[STDOUT], "stderr": keeper.offsets[STDERR], "stdin": keeper.offsets[STDIN]},
                    "stdin_closed": keeper.stdin_closed}
        self.check_controller()
        try:
            if method == "stream/subscribe":
                self.keys(params, "stream", "from_offset")
                stream, offset = Stream(params["stream"]), self.offset(params["from_offset"])
                if stream in self.subscriptions:
                    raise KeeperError(ErrorCode.INVALID_REQUEST, "stream already subscribed")
                length = keeper.offsets[f"{stream}.log"]
                if offset > length:
                    raise KeeperError(ErrorCode.INVALID_PARAMS, "offset past stream length", {"length": length})
                # Task starts only after the response has been queued by respond().
                self.subscriptions[stream] = asyncio.create_task(self.tail(stream, offset))
            elif method == "stdin/write":
                self.keys(params, "data", "expect_offset")
                data, offset = decode_data(params["data"], MAX_STDIN_BYTES), self.offset(params["expect_offset"])
                async with self.server.stdin_lock:
                    self.check_controller()
                    if keeper.stdin_closed or keeper.child is None or keeper.child.returncode is not None:
                        raise KeeperError(ErrorCode.CHILD_EXITED, "child stdin closed")
                    actual = keeper.offsets[STDIN]
                    if offset != actual:
                        raise KeeperError(ErrorCode.OFFSET_MISMATCH, "stdin offset mismatch", {"actual": actual})
                    keeper.append_stdin(data)
                    try:
                        keeper.child.stdin.write(data)
                        await keeper.child.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        keeper.stdin_closed = True
                        raise KeeperError(ErrorCode.CHILD_EXITED, "child stdin closed") from None
                    return {"offset": keeper.offsets[STDIN]}
            elif method == "stdin/close":
                self.keys(params)
                async with self.server.stdin_lock:
                    self.check_controller()
                    keeper.stdin_closed = True
                    if keeper.child is not None:
                        keeper.child.stdin.close()
            elif method == "child/signal":
                self.keys(params, "signal")
                sig = KeeperSignal(params["signal"])
                if keeper.child is not None:
                    try:
                        os.killpg(keeper.child.pid, getattr(signal, f"SIG{sig}"))
                    except ProcessLookupError:
                        pass
            elif method == "child/end":
                self.keys(params)
                keeper.request_end(EndReason.ENDED)
                await keeper.finished.wait()
                return {k: keeper.state[k] for k in ("exit_code", "signal")}
            else:
                raise KeeperError(ErrorCode.METHOD_NOT_FOUND, "unknown method")
        except (ValueError, TypeError):
            raise KeeperError(ErrorCode.INVALID_PARAMS, "invalid parameters") from None
        return {}

    @staticmethod
    def keys(params, *names):
        if set(params) != set(names):
            raise KeeperError(ErrorCode.INVALID_PARAMS, "unexpected or missing parameters")

    @staticmethod
    def offset(value):
        if type(value) is not int or value < 0:
            raise KeeperError(ErrorCode.INVALID_PARAMS, "offset must be a nonnegative integer")
        return value

    async def tail(self, stream, offset):
        keeper = self.server.keeper
        name = f"{stream}.log"
        try:
            with (keeper.run_dir / name).open("rb", buffering=0) as source:
                source.seek(offset)
                while True:
                    async with keeper.conditions[name]:
                        await keeper.conditions[name].wait_for(lambda: keeper.offsets[name] > offset or name in keeper.eof)
                        data = source.read(CHUNK)
                        final = name in keeper.eof
                    if data:
                        await self.notify("stream/data", {"stream": stream, "offset": offset, "data": base64.b64encode(data).decode("ascii")})
                        offset += len(data)
                    elif final:
                        await self.notify("stream/eof", {"stream": stream, "offset": offset})
                        return
        except (ConnectionError, OSError):
            self.writer.close()
