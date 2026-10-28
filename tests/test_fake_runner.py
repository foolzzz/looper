"""Close/settlement ordering against the real reference runner subprocess."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from looper.agents.runtimes.runner import protocol as p

ROOT = Path(__file__).resolve().parents[1]
CASES = {case["name"]: case for case in json.loads((ROOT / "runners/protocol/cases.json").read_text())}


@pytest.mark.parametrize("timing,outcome,requests,complete", [
    ("immediate", "cancelled", 0, True),
    ("running", "cancelled", 0, False),
    ("finished", "completed", 1, True),
])
def test_close_settles_turn_exactly_once(tmp_path, timing, outcome, requests, complete):
    async def exercise():
        env = dict(os.environ, HOME=str(tmp_path))
        env.pop("PYTHONPATH", None)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(ROOT / "runners/fake/main.py"), cwd=tmp_path, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        diagnostics = asyncio.create_task(process.stderr.read())
        messages = []

        async def send(*frames):
            # One write keeps prompt and close in the same input batch.
            process.stdin.write(b"".join(p.encode_frame(frame) for frame in frames))
            await process.stdin.drain()

        async def read():
            message = await asyncio.wait_for(p.read_frame(process.stdout), 2)
            messages.append(message)
            return message

        try:
            await send(p.Request("l-1", "initialize", CASES["initialize"]["message"]["params"]))
            init = await read()
            assert isinstance(init, p.Response) and init.id == "l-1" and init.error is None
            await send(p.Request("l-2", "session/new", CASES["new"]["message"]["params"]))
            opened = await read()
            assert isinstance(opened, p.Response) and opened.id == "l-2" and opened.error is None
            correlation = dict(session_id=opened.result["session_id"], trace_id="trace")
            prompt = p.Request("l-3", "session/prompt", dict(correlation, text="hello", tools=[
                dict(name="looper_tasks", description="read", inputSchema={"type": "object"})]))
            close = p.Request("l-4", "session/close", correlation)
            if timing == "immediate":
                await send(prompt, close)
            else:
                await send(prompt)
            accepted = await read()
            assert accepted == p.Response("l-3", result=dict(correlation, accepted=True))

            if timing != "immediate":
                while True:
                    message = await read()
                    if isinstance(message, p.Request):
                        if message.method == "budget/authorize":
                            await send(p.Response(message.id, result=dict(correlation, allow=True)))
                        else:
                            assert message.method == "tools/call"
                            if timing == "running":
                                break  # Close with a real tool request still in flight.
                            await send(p.Response(message.id, result=dict(
                                correlation, text="task-positive-fact", is_error=False, ran=True)))
                    elif isinstance(message, p.Notification) and message.params["kind"] == "settled":
                        assert timing == "finished"
                        break
                await send(close)

            while True:
                message = await read()
                if isinstance(message, p.Response) and message.id == "l-4":
                    assert message == p.Response("l-4", result=correlation)
                    break

            # EOF drains any late notifications too, so a duplicate cannot hide after the ACK.
            process.stdin.close()
            with pytest.raises(EOFError):
                while True:
                    await read()
            settled = [message for message in messages if isinstance(message, p.Notification)
                       and message.params.get("kind") == "settled"]
            assert len(settled) == 1
            assert settled[0].params == dict(correlation, kind="settled", outcome=outcome,
                final_text="Platform result: task-positive-fact" if timing == "finished" else "",
                requests=requests, usage_complete=complete)
            close_ack = p.Response("l-4", result=correlation)
            assert messages.index(accepted) < messages.index(settled[0]) < messages.index(close_ack)
            if timing == "immediate":
                assert messages[2:] == [accepted, settled[0], close_ack]
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
            evidence = await diagnostics
            assert process.returncode == 0, evidence.decode()

    asyncio.run(asyncio.wait_for(exercise(), 10))
