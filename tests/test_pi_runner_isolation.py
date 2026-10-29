"""Installed Pi isolation, disk resume and SDK-internal request attribution."""
import asyncio
import json
import os
import shlex
import signal

import pytest

from fake_model_api import FakeModelAPI, ResponseScript, call_events, event, text_events, text_item
from pi_runner_support import begin, pi_supervisor, session_params
from looper.agents.runtimes.runner.bridge import RpcFailure
from looper.agents.runtimes.runner.turn import RunnerTurn

pytestmark = pytest.mark.installed
KEY = "isolation-test-key"
USAGE = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
READ = ["read", "grep", "find", "ls"]
MANAGER = ["read", "bash", "edit", "write"]


def run(coroutine):
    asyncio.run(asyncio.wait_for(coroutine, 40))


def answer(text="done", *, usage=USAGE, **kwargs):
    item = text_item(text)
    return ResponseScript(events=[event("response.output_item.added", output_index=0, item={**item, "content": []}),
        event("response.content_part.added", output_index=0, content_index=0,
            part={"type": "output_text", "text": "", "annotations": []}),
        *text_events(text, usage=usage)], **kwargs)


def call(name, **arguments):
    events = call_events(json.dumps(arguments), name=name)
    events[-1]["response"]["usage"] = USAGE
    return ResponseScript(events=events)


async def until(predicate):
    async with asyncio.timeout(12):
        while not predicate():
            await asyncio.sleep(0.05)


def tool_results(events):
    return [block for e in events if e["type"] == "user"
        for block in e["message"]["content"] if block["type"] == "tool_result"]


def observe_requests(monkeypatch):
    authorized, ended = [], []
    authorize, end = RunnerTurn.authorize, RunnerTurn.request_end

    async def capture_authorize(self, params):
        authorized.append(dict(params))
        return await authorize(self, params)

    async def capture_end(self, params):
        ended.append(dict(params))
        return await end(self, params)

    monkeypatch.setattr(RunnerTurn, "authorize", capture_authorize)
    monkeypatch.setattr(RunnerTurn, "request_end", capture_end)
    return authorized, ended


def test_read_only_session_cannot_escape_or_write(tmp_path):
    async def exercise():
        outside, added = tmp_path / "outside", tmp_path / "added"
        outside.mkdir()
        added.mkdir()
        (outside / "secret.txt").write_text("SECRET-MUST-NEVER-REACH-MODEL")
        (added / "allowed.txt").write_text("added-content")
        async with pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            cwd = tmp_path / "task"
            cwd.mkdir()
            (cwd / "inside.txt").write_text("inside-content")
            (cwd / "link").symlink_to(outside / "secret.txt")
            calls = [call("read", path="inside.txt"), call("read", path="../outside/secret.txt"),
                call("read", path=str(outside / "secret.txt")), call("read", path="link"),
                call("read", path="~/x"), call("read", path=str(added / "allowed.txt")),
                call("grep", path="link", pattern="SECRET"), call("find", path="../outside", pattern="*"),
                call("ls", path=str(outside)), call("write", path="forbidden", content="no"), answer()]
            async with FakeModelAPI(calls, api_key=KEY) as api:
                sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY,
                    role="planner", cwd=str(cwd), add_dirs=[str(added)],
                    tools={"builtin": READ, "platform": [], "read_only": True}))
                _, events = await begin(lease.bridge, sid, "read-only")
                assert (await lease.bridge.wait_settled(sid, "read-only")).outcome == "completed"
                results = tool_results(events)
                assert len(results) == 10
                assert "inside-content" in results[0]["content"] and not results[0]["is_error"]
                assert all(results[i]["is_error"] and "Looper blocked" in results[i]["content"]
                    for i in [1, 2, 3, 4, 6, 7, 8])
                assert "added-content" in results[5]["content"] and not results[5]["is_error"]
                assert results[9]["is_error"] and "not found" in results[9]["content"].lower()
                assert not (cwd / "forbidden").exists()
                assert {t["name"] for t in api.requests[0].body["tools"]} == set(READ)
                assert "SECRET-MUST-NEVER-REACH-MODEL" not in json.dumps([r.body for r in api.requests])
                uses = [b for e in events if e["type"] == "assistant"
                    for b in e["message"]["content"] if b["type"] == "tool_use"]
                assert len(uses) == len(results)
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("name,alias,filename", [
    ("read", "linked\u00a0dir", "secret.txt"),
    ("write", "linked\u00a0dir", "secret.txt"),
    ("write", "linked\u00a0dir", "new/created.txt"),
    ("edit", "linked\u00a0dir", "secret.txt"),
    ("read", "quoted'dir", "secret.txt"),
])
def test_sdk_resolved_paths_cannot_escape(tmp_path, name, alias, filename):
    async def exercise():
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = "SECRET-MUST-NEVER-REACH-MODEL"
        (outside / "secret.txt").write_text(secret)
        async with pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            cwd = lease.directory
            link = "quoted\u2019dir" if alias == "quoted'dir" else "linked dir"
            (cwd / link).symlink_to(outside, target_is_directory=True)
            (cwd / "inside dir").mkdir()
            (cwd / "inside dir/allowed.txt").write_text("inside-content")
            arguments = {"path": f"{alias}/{filename}"}
            if name == "write":
                arguments["content"] = "overwritten"
            elif name == "edit":
                arguments["edits"] = [{"oldText": "SECRET", "newText": "overwritten"}]
            scripts = [call("read", path="inside\u00a0dir/allowed.txt"),
                call("write", path="inside\u00a0dir/new.txt", content="written"),
                call(name, **arguments), answer()]
            async with FakeModelAPI(scripts, api_key=KEY) as api:
                sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY,
                    tools={"builtin": MANAGER, "platform": [], "read_only": False}))
                _, events = await begin(lease.bridge, sid, "resolved-path")
                assert (await lease.bridge.wait_settled(sid, "resolved-path")).outcome == "completed"
                results = tool_results(events)
                assert len(results) == 3
                assert "inside-content" in results[0]["content"] and not results[0]["is_error"]
                assert not results[1]["is_error"]
                assert (cwd / "inside dir/new.txt").read_text() == "written"
                assert results[2]["is_error"] and "Looper blocked" in results[2]["content"]
                assert (outside / "secret.txt").read_text() == secret
                assert not (outside / "new").exists()
                assert secret not in json.dumps([r.body for r in api.requests])
            await lease.release()
    run(exercise())


def test_manager_bash_sees_only_its_session_environment(tmp_path):
    async def exercise():
        outside = tmp_path / "outside.txt"
        outside.write_text("original")
        command = 'printf "%s|%s|%s|%s|%s" "$HOME" "${LOOPER_TEST_PARENT_SENTINEL-unset}" "$CUSTOM" "$PWD" "${PI_SESSION_ID-unset}"'
        scripts = [call("bash", command=command), call("write", path="inside.txt", content="written"),
            call("write", path=str(outside), content="bad"),
            call("edit", path=str(outside), oldText="original", newText="bad"), answer()]
        async with FakeModelAPI(scripts, api_key=KEY) as api, pi_supervisor(tmp_path,
                env={"LOOPER_TEST_PARENT_SENTINEL": "parent-secret"}) as supervisor:
            lease = await supervisor.acquire("pi")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY,
                env={"CUSTOM": "x", "HOME": "must-be-overridden"},
                tools={"builtin": MANAGER, "platform": [], "read_only": False}))
            _, events = await begin(lease.bridge, sid, "manager")
            assert (await lease.bridge.wait_settled(sid, "manager")).outcome == "completed"
            results = tool_results(events)
            assert results[0]["content"] == f"{lease.home}|unset|x|{lease.directory}|unset"
            assert (lease.directory / "inside.txt").read_text() == "written"
            assert all(r["is_error"] and "Looper blocked" in r["content"] for r in results[2:])
            assert outside.read_text() == "original"
            assert (await lease.bridge.health())["tool_processes"] == 0
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("case,code", [("read-only-bash", -32008), ("unknown", -32602),
    ("relative-add", -32602), ("missing-add", -32602), ("state-link", -32602),
    ("home-link", -32602), ("relative-home", -32602), ("env-key", -32602), ("agent-missing", -32008)])
def test_session_new_refuses_unsafe_isolation(tmp_path, case, code):
    async def exercise():
        async with pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            params = session_params(lease, base_url="http://127.0.0.1:1/v1", key=KEY)
            if case == "read-only-bash":
                params["tools"].update(builtin=["bash"], read_only=True)
            elif case == "unknown":
                params["tools"]["builtin"] = ["not-a-tool"]
            elif case in {"relative-add", "missing-add"}:
                params["add_dirs"] = ["relative" if case == "relative-add" else str(tmp_path / "missing")]
            elif case in {"state-link", "home-link"}:
                link = tmp_path / "link"
                link.symlink_to(lease.state_dir if case == "state-link" else lease.home)
                params["state_dir" if case == "state-link" else "home"] = str(link)
            elif case == "relative-home":
                params["home"] = "relative"
            elif case == "env-key":
                params["env"] = {"BAD-KEY": "x"}
            else:
                lease.agent_dir.rmdir()
            # Raw bridge request preserves deliberately unsafe host-path parameters.
            with pytest.raises(RpcFailure) as error:
                await lease.bridge.request("session/new", params)
            assert error.value.code == code
            assert (await lease.bridge.health())["sessions"] == 0
            await lease.release()
    run(exercise())


def test_saved_session_resumes_on_another_live_pool_member(tmp_path):
    async def exercise():
        async with FakeModelAPI([answer("first-answer"), answer("second-answer")], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            supervisor.session_cap = 1
            lease = await supervisor.acquire("pi", owner="manager")
            params = session_params(lease, base_url=api.base_url, key=KEY, cwd=str(tmp_path))
            sid = await lease.open_session(params)
            await begin(lease.bridge, sid, "first")
            assert (await lease.bridge.wait_settled(sid, "first")).outcome == "completed"
            assert list(lease.state_dir.glob("*.jsonl")), "the first member must persist the owner session"

            second = await supervisor.acquire("pi")
            first_member, second_member = lease._generation, second._generation
            assert first_member is not second_member
            await lease.release()
            filler = await supervisor.acquire("pi")
            assert filler._generation is first_member
            await second.release()

            resumed = await supervisor.acquire("pi", owner="manager")
            assert resumed._generation is second_member
            assert resumed.state_dir == lease.state_dir
            assert first_member.process.returncode is second_member.process.returncode is None
            assert await resumed.load_session({**params, "session_id": sid}) == sid
            await begin(resumed.bridge, sid, "second")
            assert (await resumed.bridge.wait_settled(sid, "second")).outcome == "completed"
            assert "first-answer" in json.dumps(api.requests[1].body)
            await resumed.release()
            await filler.release()
    run(exercise())


def test_saved_session_resumes_once(tmp_path):
    async def exercise():
        async with FakeModelAPI([answer("first-answer"), answer("second-answer")], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi", owner="manager")
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY))
            await begin(lease.bridge, sid, "first")
            assert (await lease.bridge.wait_settled(sid, "first")).outcome == "completed"
            generation = lease._generation
            await lease.release()
            generation.process.kill()
            await until(lambda: generation.failure is not None)
            await asyncio.shield(generation.failure)
            await asyncio.shield(supervisor._kinds["pi"].restart)
            assert not supervisor._kinds["pi"].members
            resumed = await supervisor.acquire("pi", owner="manager")
            assert resumed._generation is not generation
            params = session_params(resumed, base_url=api.base_url, key=KEY, session_id=sid)
            assert await resumed.load_session(params) == sid
            await begin(resumed.bridge, sid, "second")
            assert (await resumed.bridge.wait_settled(sid, "second")).outcome == "completed"
            history = json.dumps(api.requests[1].body)
            assert "Say hello." in history and "first-answer" in history
            with pytest.raises(RpcFailure) as error:
                await resumed.bridge.request("session/load", params)
            assert error.value.code == -32002
            await resumed.bridge.close_session(sid)
            for file in list(resumed.state_dir.glob("*.jsonl")):
                (resumed.state_dir / "symlink.jsonl").symlink_to(file)
                (resumed.state_dir / "invalid.jsonl").write_text("not-json\n")
                (resumed.state_dir / "oversized.jsonl").write_text(" " * 65537 + json.dumps({"type": "session", "id": sid}) + "\n")
            with pytest.raises(RpcFailure) as error:
                await resumed.bridge.request("session/load", {**params, "session_id": "unknown"})
            assert error.value.code == -32001
            # Invalid headers and symlinks must not make the real saved id ambiguous.
            assert await resumed.bridge.load_session(params) == sid
            await resumed.bridge.close_session(sid)
            saved = next(p for p in resumed.state_dir.glob("*.jsonl") if p.name not in {"symlink.jsonl", "invalid.jsonl", "oversized.jsonl"})
            (resumed.state_dir / "duplicate.jsonl").write_bytes(saved.read_bytes())
            with pytest.raises(RpcFailure) as error:
                await resumed.bridge.request("session/load", params)
            assert error.value.code == -32001
            await resumed.release()
    run(exercise())


def test_two_sessions_keep_credentials_and_cancel_apart(tmp_path):
    async def exercise():
        gate_a, gate_b = asyncio.Event(), asyncio.Event()
        scripts = [answer("a", gate=gate_a), answer("b", gate=gate_b)]
        async with FakeModelAPI([scripts[0]], api_key="key-a") as a, FakeModelAPI([scripts[1]], api_key="key-b") as b, pi_supervisor(tmp_path) as supervisor:
            leases = [await supervisor.acquire("pi", owner=owner) for owner in ["a", "b"]]
            assert leases[0].bridge is leases[1].bridge
            ids = [await lease.open_session(session_params(lease, base_url=api.base_url, key=key))
                for lease, api, key in zip(leases, [a, b], ["key-a", "key-b"])]
            try:
                await asyncio.gather(*(begin(lease.bridge, sid, sid) for lease, sid in zip(leases, ids)))
                await asyncio.gather(*(script.gate_reached.wait() for script in scripts))
                await leases[0].bridge.cancel_turn(ids[0], ids[0])
                assert (await leases[0].bridge.wait_settled(ids[0], ids[0])).outcome == "cancelled"
                assert (await leases[1].bridge.health())["inflight_requests"] == 1
                gate_b.set()
                assert (await leases[1].bridge.wait_settled(ids[1], ids[1])).outcome == "completed"
                assert [r.headers["authorization"] for r in a.requests] == ["Bearer key-a"]
                assert [r.headers["authorization"] for r in b.requests] == ["Bearer key-b"]
                assert "key-b" not in json.dumps([r.body for r in a.requests])
                assert "key-a" not in json.dumps([r.body for r in b.requests])
            finally:
                gate_a.set()
                gate_b.set()
                await asyncio.gather(*(lease.release() for lease in leases))
    run(exercise())


@pytest.mark.parametrize("case", ["retry", "retry-denied", "compaction"])
def test_sdk_internal_requests_are_authorized_by_kind(tmp_path, monkeypatch, case):
    authorized, ended = observe_requests(monkeypatch)

    async def exercise():
        scripts = ([ResponseScript(status=500), answer()] if case == "retry" else
            [ResponseScript(status=500)] if case == "retry-denied" else
            [answer("A long answer. " * 7000, usage={"input_tokens": 30000, "output_tokens": 5000, "total_tokens": 35000}), answer("summary")])
        async with FakeModelAPI(scripts, api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            params = session_params(lease, base_url=api.base_url, key=KEY)
            if case == "compaction":
                params["provider"]["context_window"] = 32768
            sid = await lease.open_session(params)
            await begin(lease.bridge, sid, "internal", max_requests=1 if case == "retry-denied" else 30)
            summary = await lease.bridge.wait_settled(sid, "internal")
            if case.startswith("retry"):
                assert [a["kind"] for a in authorized] == ["turn", "retry"]
                assert summary.outcome == ("max_requests" if case == "retry-denied" else "completed")
                assert len(api.requests) == (1 if case == "retry-denied" else 2)
                assert ended[0]["status"] == "http_500" and ended[0]["complete"]
                assert all(ended[0][key] == 0 for key in ["input_uncached", "output", "cache_read", "cache_write"])
                assert summary.usage_complete
            else:
                assert summary.outcome == "completed"
                await until(lambda: len(api.requests) > 1 or any(b"usage_unattributed" in line for line in lease._generation.diagnostics))
                if len(api.requests) == 1:
                    assert [a["kind"] for a in authorized] == ["turn"]
                    assert any(b"usage_unattributed" in line for line in lease._generation.diagnostics)
                else:
                    await until(lambda: len(ended) == len(api.requests))
                    assert [a["kind"] for a in authorized][1:] == ["compaction"]
            assert len(ended) == len(api.requests)
            assert len({e["request_id"] for e in ended}) == len(ended)
            assert [e["request_kind"] for e in ended] == [a["kind"] for a in authorized[:len(ended)]]
            await lease.release()
    run(exercise())


@pytest.mark.parametrize("mode", ["kill", "stop"])
def test_runner_loss_leaves_no_tool_process(tmp_path, mode):
    async def exercise():
        pidfile = tmp_path / "bash.pid"
        command = f"echo $$ > {shlex.quote(str(pidfile))}; exec sleep 60"
        async with FakeModelAPI([call("bash", command=command)], api_key=KEY) as api, pi_supervisor(tmp_path) as supervisor:
            lease = await supervisor.acquire("pi")
            process = lease._generation.process
            sid = await lease.open_session(session_params(lease, base_url=api.base_url, key=KEY,
                tools={"builtin": MANAGER, "platform": [], "read_only": False}))
            await begin(lease.bridge, sid, "crash")
            await until(lambda: pidfile.exists() and pidfile.read_text().strip())
            pid = int(pidfile.read_text())
            os.kill(pid, 0)  # Positive control: the child really was alive.
            assert (await lease.bridge.health())["tool_processes"] == 1
            try:
                # Recovery owns observed detached children; wait for its normal ticker,
                # as in the supervisor tests. A pre-observation SIGKILL is a host gap.
                await until(lambda: lease._generation.tree.seen.get(pid) is not None)
                if mode == "kill":
                    process.send_signal(signal.SIGKILL)
                    summary = await lease.bridge.wait_settled(sid, "crash")
                    assert summary.outcome == "lost"
                    await until(lambda: lease._generation.failure is not None)
                    await asyncio.shield(lease._generation.failure)
                    await asyncio.shield(supervisor._kinds["pi"].restart)
                    assert not supervisor._kinds["pi"].members
                    replacement = await supervisor.acquire("pi")
                    assert replacement._generation is not lease._generation
                    await replacement.release()
                else:
                    await supervisor.stop()
                    assert process.returncode == 0
                def gone():
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return True
                    return False
                await until(gone)
            finally:
                # Failed crash assertions must not leak a synthetic sleep process.
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
    run(exercise())
