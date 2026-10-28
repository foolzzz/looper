"""Host tool deadlines through the real Pi runner and its bash process group."""
import asyncio
import json
import os
import shlex
import signal
import time
from types import SimpleNamespace

import pytest

from fake_model_api import FakeModelAPI, ResponseScript, call_events, text_events
from pi_runner_support import raw_runner, require_pi, session_params

pytestmark = pytest.mark.installed
KEY = "bash-timeout-test-key"


@pytest.mark.parametrize("model_timeout,expected", [(None, 2), (300, 2), (1, 1), (0, 2), (None, None)],
    ids=["host-default", "host-cap", "shorter-model-timeout", "zero-cannot-disable-limit", "quick-command"])
def test_bash_obeys_host_timeout(tmp_path, model_timeout, expected):
    async def exercise():
        _, pi = require_pi()
        pidfile = tmp_path / "bash.pids"
        command = (f"sleep 30 & echo $$ $! > {shlex.quote(str(pidfile))}; "
            "printf 'stdout before timeout\\n'; printf 'stderr before timeout\\n' >&2; wait"
            if expected else "printf 'quick output'")
        arguments = {"command": command}
        if model_timeout is not None:
            arguments["timeout"] = model_timeout
        scripts = [ResponseScript(events=call_events(json.dumps(arguments), name="bash")),
            ResponseScript(events=text_events("done"))]
        async with FakeModelAPI(scripts, api_key=KEY) as api, raw_runner(tmp_path, pi) as (_, send, receive):
            root, = tmp_path.glob("pi-runner-*")
            directory = root / "sessions/timeout"
            for name in ("agent", "native", "home"):
                (directory / name).mkdir(parents=True)
            lease = SimpleNamespace(session_key="timeout", directory=directory,
                home=directory / "home", state_dir=directory / "native")
            await send("initialize", {"protocol_versions": ["0.1"], "looper_version": "test",
                "host_capabilities": {"tools_call": True, "budget_authorize": True, "bus": False, "exec_slots": False}})
            assert "result" in await receive()
            # The host codec fixes the production limit at 120; use the real wire
            # directly to exercise the runner with a short session-owned deadline.
            await send("session/new", session_params(lease, base_url=api.base_url, key=KEY,
                tools={"builtin": ["bash"], "platform": [], "read_only": False},
                limits={"max_requests_per_turn": 30, "tool_timeout_s": 2, "tool_output_bytes": 102400}), id="l-2")
            sid = (await receive())["result"]["session_id"]
            trace = {"session_id": sid, "trace_id": "timeout"}
            pids, notices = [], []
            try:
                started = time.monotonic()
                await send("session/prompt", {**trace, "text": "Run the command", "tools": []}, id="l-3")
                async with asyncio.timeout((expected or 0) + 3):
                    while not notices or notices[-1]["kind"] != "settled":
                        frame = await receive()
                        if frame.get("method") == "budget/authorize":
                            await send({"jsonrpc": "2.0", "id": frame["id"], "result": {**trace, "allow": True}})
                        elif frame.get("method") == "session/update":
                            notice = frame["params"]
                            notices.append(notice)
                            if expected and notice["kind"] == "message" and any(
                                    block["type"] == "tool_use" for block in notice["content"]):
                                while not pidfile.exists() or len(pidfile.read_text().split()) != 2:
                                    await asyncio.sleep(0.01)
                                pids = list(map(int, pidfile.read_text().split()))
                                # Positive control: bash and its child are alive in bash's group.
                                assert all(os.getpgid(pid) == pids[0] for pid in pids)
                        else:
                            assert frame["result"]["accepted"]
                elapsed = time.monotonic() - started
                assert notices[-1]["outcome"] == "completed"
                result, = [notice for notice in notices if notice["kind"] == "tool_result"]
                if expected:
                    assert expected - 0.2 <= elapsed < expected + 0.8
                    assert result["is_error"]
                    assert f"Command timed out after {expected} seconds" in result["text"]
                    assert "stdout before timeout" in result["text"]
                    assert "stderr before timeout" in result["text"]
                    assert len(pids) == 2
                    # Check before runner teardown, which also cleans up processes.
                    async with asyncio.timeout(2):
                        for pid in pids:
                            while True:
                                try:
                                    os.kill(pid, 0)
                                except ProcessLookupError:
                                    break
                                await asyncio.sleep(0.01)
                else:
                    assert elapsed < 2
                    assert result["text"] == "quick output" and not result["is_error"]
                await send("runner/health", id="l-4")
                assert (await receive())["result"]["tool_processes"] == 0
            finally:
                if pidfile.exists() and (fields := pidfile.read_text().split()):
                    try:
                        os.killpg(int(fields[0]), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    asyncio.run(asyncio.wait_for(exercise(), 15))
