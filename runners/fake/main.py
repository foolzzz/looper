"""Deterministic protocol runner; invoke with Python -u and this absolute path."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import itertools
import json
import os
from pathlib import Path
import sys

# The supervisor strips PYTHONPATH and chooses a private cwd/HOME.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from looper.agents.runtimes.runner import protocol as p

RAW_USAGE = dict(input_uncached=1000, output=200, cache_read=300, cache_write=400)
SCRIPTS = {"ordinary", "pause-at-boundary", "tool-error", "exit-mid-turn"}


@dataclass(repr=False)
class Session:
    params: dict
    trace: str = ""
    task: asyncio.Task | None = None
    steers: list = field(default_factory=list)
    boundary: asyncio.Event = field(default_factory=asyncio.Event)
    closing: bool = False
    authorizing: bool = False
    cancel_requested: bool = False
    cancel_tasks: set = field(default_factory=set)
    settled: bool = True


class Fake:
    def __init__(self):
        self.initialized = False
        self.sessions = {}
        self.pending = {}
        self.ids = itertools.count(1)
        self.lock = asyncio.Lock()

    @staticmethod
    def evidence(event, params, **extra):
        safe = {key: params[key] for key in ("session_id", "trace_id", "request_id") if key in params}
        print(json.dumps(dict(event=event, **safe, **extra)), file=sys.stderr, flush=True)

    async def send(self, message):
        async with self.lock:
            sys.stdout.buffer.write(p.encode_frame(message))
            sys.stdout.buffer.flush()

    async def update(self, correlation, kind, **fields):
        params = dict(correlation, kind=kind, **fields)
        if kind in {"request_end", "settled"}:
            self.evidence(kind, params, **{k: fields[k] for k in
                ("outcome", "requests", "input_uncached", "output", "cache_read", "cache_write") if k in fields},
                **({"claimed_cost_usd": fields["total_cost_usd"]} if "total_cost_usd" in fields else {}))
        await self.send(p.Notification("session/update", params))

    async def reverse(self, method, params):
        rpc_id = f"r-{next(self.ids)}"
        future = asyncio.get_running_loop().create_future()
        self.pending[rpc_id] = (future, method, params)
        self.evidence(method, params)
        try:
            await self.send(p.Request(rpc_id, method, params))
            return await future
        finally:
            self.pending.pop(rpc_id, None)

    async def settle(self, session, correlation, *, outcome, final_text="", requests=0, usage_complete=True):
        if session.settled:
            return
        session.settled = True
        for message_id, _ in session.steers:
            await self.update(correlation, "steer_dropped", message_id=message_id,
                reason="session_closed" if session.closing else "cancelled" if outcome == "cancelled" else "turn_ended")
        session.steers.clear()
        await self.update(correlation, "settled", outcome=outcome, final_text=final_text,
                          requests=requests, usage_complete=usage_complete)
        session.task = None
        session.authorizing = False

    async def turn(self, session, params):
        correlation = {k: params[k] for k in ("session_id", "trace_id")}
        provider = session.params["provider"]
        request_id = f"request-{next(self.ids)}"
        requests, complete = 0, True
        outcome, final_text = "failed", ""
        try:
            if session.cancel_requested:
                outcome = "cancelled"
                return
            allowed = await self.reverse("budget/authorize", dict(correlation, request_id=request_id,
                kind="turn", provider=provider["account_id"],
                model=provider["model"], estimated_input_tokens=2000, max_output_tokens=1000))
            self.evidence("authorization", dict(correlation, request_id=request_id),
                          **{k: allowed[k] for k in ("allow", "reason") if k in allowed})
            if not allowed["allow"]:
                outcome, final_text = "capped", "authorization denied: " + allowed["reason"]
                return
            complete = False
            session.authorizing = False
            if session.cancel_requested:
                outcome = "cancelled"
                return
            script = session.params["env"].get("LOOPER_FAKE_SCRIPT", "ordinary")
            try:
                selection = json.loads(params["text"])
                if isinstance(selection, dict):
                    script = selection.get("fake_script", script)
            except ValueError:
                pass
            if script not in SCRIPTS:
                raise p.ProtocolError(p.ErrorCode.INVALID_PARAMS)
            if script == "exit-mid-turn":
                os._exit(23)
            tool = "looper_tasks" if session.params["role"] == "manager" else "looper_task_profiles"
            arguments = {"action": "list"} if tool == "looper_tasks" else {}
            if script == "tool-error":
                arguments = {"action": "fake-unsupported"}
            call_id = f"call-{next(self.ids)}"
            await self.update(correlation, "text_delta", text="Reading platform state. ")
            await self.update(correlation, "message", model=provider["model"], content=[
                {"type": "tool_use", "id": call_id, "name": tool, "input": arguments}])
            result = await self.reverse("tools/call", dict(correlation, call_id=call_id,
                                        name=tool, arguments=arguments))
            await self.update(correlation, "tool_result", call_id=call_id,
                              text=result["text"], is_error=result["is_error"])
            # An additive SDK dollar claim must never override host pricing.
            await self.update(correlation, "request_end", request_id=request_id, request_kind="turn",
                model=provider["model"], status="failed" if result["is_error"] else "completed",
                **RAW_USAGE, reasoning=None, complete=True, total_cost_usd=999)
            requests, complete = 1, True
            if script == "pause-at-boundary":
                await session.boundary.wait()
            applied = [text for _, text in session.steers]
            for message_id, _ in session.steers:
                await self.update(correlation, "steer_applied", message_id=message_id,
                    after_request_id=request_id, after_tool_batch=True, new_turn=False)
            session.steers.clear()
            outcome = "failed" if result["is_error"] else "completed"
            final_text = "Platform result: " + result["text"]
            if applied:
                final_text += "\nApplied steer: " + "\n".join(applied)
            if session.params["role"] == "planner":
                final_text = json.dumps({"reply": final_text, "action": "none"})
        except asyncio.CancelledError:
            outcome = "cancelled"
        except Exception:
            final_text = "fake scripted turn failed"
        finally:
            await self.settle(session, correlation, outcome=outcome, final_text=final_text,
                              requests=requests, usage_complete=complete)

    async def cancel_after_authorization(self, session, turn):
        # Only this trace gets 0.25 s for the host cap-denial reply after ACK.
        try:
            await asyncio.wait_for(asyncio.shield(turn), 0.25)
        except TimeoutError:
            if session.task is turn:
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)

    async def cancel(self, session):
        guards = tuple(session.cancel_tasks)
        for guard in guards:
            guard.cancel()
        await asyncio.gather(*guards, return_exceptions=True)
        if session.task:
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)

    async def control(self, message):
        params, method = message.params, message.method
        try:
            if method == "initialize":
                if self.initialized:
                    raise p.ProtocolError(p.ErrorCode.INVALID_REQUEST)
                version = p.select_version(params["protocol_versions"])
                self.initialized = True
                result = dict(protocol_version=version, runner={"kind": "fake", "version": "0.1.0"},
                    sdk={"name": "none", "version": "0"}, capabilities={"max_sessions": 4,
                        "per_session_isolation": True, "per_session_credentials": True, "cancel": True,
                        "steer": {"in_trace": True, "boundary": "after_tool_batch", "idle_start": False},
                        "usage": {"per_request": True, "buckets": True, "reasoning_in_output": False},
                        "authorize_covers": ["turn"]})
            elif not self.initialized:
                raise p.ProtocolError(p.ErrorCode.INVALID_REQUEST)
            elif method == "session/new":
                if len(self.sessions) >= 4:
                    raise p.ProtocolError(p.ErrorCode.CAPACITY)
                sid = f"session-{next(self.ids)}"
                self.sessions[sid] = Session(params)
                result = {"session_id": sid}
            else:
                session = self.sessions.get(params["session_id"])
                if session is None:
                    raise p.ProtocolError(p.ErrorCode.SESSION_NOT_FOUND)
                correlation = {k: params[k] for k in ("session_id", "trace_id") if k in params}
                if method == "session/prompt":
                    if session.task:
                        raise p.ProtocolError(p.ErrorCode.SESSION_BUSY)
                    tool = "looper_tasks" if session.params["role"] == "manager" else "looper_task_profiles"
                    if tool not in {t["name"] for t in params["tools"]}:
                        raise p.ProtocolError(p.ErrorCode.UNSUPPORTED)
                    session.trace, session.boundary = params["trace_id"], asyncio.Event()
                    session.authorizing, session.cancel_requested = True, False
                    session.settled = False
                    await self.send(p.Response(message.id, result=dict(correlation, accepted=True)))
                    session.task = asyncio.create_task(self.turn(session, params))
                    return
                if "trace_id" in params and params["trace_id"] != session.trace:
                    raise p.ProtocolError(p.ErrorCode.TRACE_MISMATCH)
                if method == "session/close":
                    session.closing = True
                    await self.cancel(session)
                    # A task cancelled before its first step never runs its finally block.
                    await self.settle(session, dict(session_id=params["session_id"], trace_id=session.trace),
                                      outcome="cancelled")
                    del self.sessions[params["session_id"]]
                    result = correlation if "trace_id" in params else {}
                elif method in {"session/cancel", "session/steer"}:
                    if not session.task:
                        raise p.ProtocolError(p.ErrorCode.NOT_RUNNING)
                    if method == "session/cancel":
                        # The host can cancel while its authorization callback is
                        # returning denial. Keep reading so that reply can settle
                        # capped with zero requests, instead of losing the evidence.
                        session.cancel_requested = True
                        if session.authorizing:
                            await self.send(p.Response(message.id, result=correlation))
                            guard = asyncio.create_task(self.cancel_after_authorization(session, session.task))
                            session.cancel_tasks.add(guard)
                            guard.add_done_callback(session.cancel_tasks.discard)
                            return
                        await self.cancel(session)
                        result = correlation
                    else:
                        session.steers.append((params["message_id"], params["text"]))
                        result = dict(correlation, status="queued")
                        await self.send(p.Response(message.id, result=result))
                        session.boundary.set()
                        return
                else:
                    raise p.ProtocolError(p.ErrorCode.METHOD_NOT_FOUND)
            await self.send(p.Response(message.id, result=result))
        except p.ProtocolError as error:
            data = {k: params[k] for k in ("session_id", "trace_id") if k in params}
            await self.send(p.Response(message.id, error=dict(code=int(error.code), message=error.code.name, data=data)))

    async def run(self):
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        try:
            while True:
                message = await p.read_frame(reader)
                if isinstance(message, p.Response):
                    pending = self.pending.get(message.id)
                    if pending and not pending[0].done():
                        future, method, params = pending
                        if message.error:
                            p.verify_error(message.error, params)
                            future.set_exception(p.ProtocolError(p.ErrorCode.INTERNAL_ERROR))
                        else:
                            future.set_result(p.decode_result(method, message.result, params))
                elif isinstance(message, p.Request):
                    await self.control(message)
                else:
                    raise p.ProtocolError(p.ErrorCode.INVALID_REQUEST)
        except EOFError:
            pass
        finally:
            await asyncio.gather(*(self.cancel(s) for s in self.sessions.values()))
            transport.close()


if __name__ == "__main__":
    try:
        asyncio.run(Fake().run())
    except (p.ProtocolError, BrokenPipeError):
        print("fake runner protocol connection closed", file=sys.stderr)
        sys.exit(1)
