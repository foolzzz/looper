"""Process ownership proofs use actual private subprocesses and the completed Bridge."""
# ruff: noqa: E402 -- the subprocess entry point only imports the standard library.
import asyncio
from contextlib import asynccontextmanager
import errno
import json
import os
from pathlib import Path
import signal
import sys
import time


def fake_runner():
    """A standalone stdlib peer: no server import or PYTHONPATH is needed."""
    import subprocess
    options = json.loads(sys.argv[1])
    child = None
    if options.get("uncooperative") or options.get("pipe_child"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            start_new_session=not options.get("pipe_child", False))
    Path("observed.json").write_text(json.dumps({"cwd": os.getcwd(), "env": dict(os.environ),
        "child": child.pid if child else None}))
    if options.get("stderr"):
        sys.stderr.write("x" * 65536 + "\n" + "safe\u2028one-line\n" + "secret-sentinel\n" * 30)
        sys.stderr.flush()
    count, active = 0, {}
    for line in sys.stdin:
        request = json.loads(line)
        if "method" not in request:
            continue
        method, params = request["method"], request["params"]
        if method == "initialize":
            if options.get("slow_initialize"):
                time.sleep(0.25)
            result = {"protocol_version": options.get("version", "0.1"),
                "runner": {"kind": options.get("kind", "fake"), "version": "1"}, "sdk": {"name": "fake", "version": "1"},
                "capabilities": {"max_sessions": options.get("cap", 4),
                    "per_session_isolation": options.get("isolation", True),
                    "cancel": True, "usage": {"per_request": True}, "authorize_covers": ["turn"]}}
        elif method == "session/new":
            Path("opening").touch()
            Path("session-paths.json").write_text(json.dumps({key: params[key]
                for key in ("cwd", "add_dirs", "home", "state_dir", "session_key")}))
            if options.get("echo_key"):
                print(params["provider"]["api_key"], file=sys.stderr, flush=True)
            if options.get("slow_open"):
                time.sleep(0.25)
            if options.get("reject_open"):
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                    "error": {"code": -32602, "message": "invalid session"}}), flush=True)
                continue
            count += 1
            result = {"session_id": f"../native-{count}"}
        elif method == "session/prompt":
            active[params["session_id"]] = params["trace_id"]
            result = {"session_id": params["session_id"], "trace_id": params["trace_id"], "accepted": True}
        else:
            result = {key: params[key] for key in ("session_id", "trace_id") if key in params}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
        if method == "session/close" and params["session_id"] in active:
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
                "session_id": params["session_id"], "trace_id": active.pop(params["session_id"]),
                "kind": "settled", "outcome": "cancelled", "final_text": "",
                "requests": 0, "usage_complete": True}}), flush=True)
        if method == "session/prompt" and options.get("corrupt"):
            print("corrupt stdout", flush=True)
        if method == "session/prompt" and options.get("tool"):
            print(json.dumps({"jsonrpc": "2.0", "id": "r-1", "method": "tools/call", "params": {
                "session_id": params["session_id"], "trace_id": params["trace_id"],
                "call_id": "call", "name": "write", "arguments": {}}}), flush=True)
    if options.get("uncooperative") or options.get("corrupt"):
        time.sleep(60)


if __name__ == "__main__":
    fake_runner()
    sys.exit(0)

import pytest

from looper.agents import proc_tree
from looper.agents.log_redaction import add_secret
from looper.agents.run_usage import Usage
from looper.agents.runtimes.runner import protocol as p
from looper.agents.runtimes.runner import supervisor as s
from looper.agents.runtimes.runner.bridge import ConnectionLost, RpcFailure, RunnerBridge
from looper.agents.runtimes.runner.turn import RunnerTurn

NEW = next(c["message"]["params"] for c in json.loads(
    (Path(__file__).resolve().parents[1] / "runners/protocol/cases.json").read_text()) if c["name"] == "new")


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 8))


async def until(predicate, *, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class Turn:
    def __init__(self, gate=None):
        self.gate, self.entered = gate, asyncio.Event()
        self.stopped, self.finishes = False, 0

    def stop(self):
        self.stopped = True

    async def call_tool(self, params):
        self.entered.set()
        await self.gate.wait()
        return {"text": "written", "is_error": False, "ran": True}

    async def drain_tools(self):
        if self.gate:
            await self.gate.wait()

    async def finish(self, params):
        from types import SimpleNamespace
        await self.drain_tools()
        self.finishes += 1
        return SimpleNamespace(outcome=params["outcome"], usage=Usage(), totals=Usage(),
                               model_usage={}, cap_reason=None, usage_complete=False)


@pytest.fixture
def runners(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "BACKOFF_BASE", 0.02)
    monkeypatch.setattr(s, "SHUTDOWN_TIMEOUT", 0.1)
    monkeypatch.setattr(s, "STDERR_DRAIN_TIMEOUT", 0.1)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.1)
    monkeypatch.setattr(proc_tree, "REAP_SEC", 0.5)
    monkeypatch.setattr(proc_tree, "_records", None)
    monkeypatch.setattr(proc_tree, "_server", None)
    processes = []

    @asynccontextmanager
    async def make(*, session_cap=4, kinds=("fake",), control_timeouts=(2.0,), **options):
        async def connect(process, kind):
            processes.append(process)
            timeout = control_timeouts[min(len(processes) - 1, len(control_timeouts) - 1)]
            bridge = RunnerBridge(process.stdout, process.stdin, kind, control_timeout=timeout)
            try:
                await bridge.initialize()
            except BaseException:
                await bridge.close()
                raise
            return bridge
        supervisor = s.RunnerSupervisor(tmp_path, {kind: [sys.executable,
            Path("tests/test_runner_supervisor.py"), json.dumps({**options, "kind": kind})] for kind in kinds},
            session_cap=session_cap, connect=connect)
        try:
            yield supervisor, processes
        finally:
            await supervisor.stop()
            # Cleanup also runs when an assertion or an outer timeout cancels this test.
            for process in processes:
                await proc_tree.end(process, label="test runner cleanup")
            assert all(p.returncode is not None for p in processes)
            children = {child for path in tmp_path.glob("runners/*/observed.json")
                        if (child := json.loads(path.read_text())["child"]) is not None}
            if children:
                await until(lambda: not children & proc_tree._snapshot().keys())
    return make


def test_private_process_environment_and_session_paths(runners, tmp_path, monkeypatch):
    for key in ("OPENAI_API_KEY", "LOOPER_TOKEN", "PGPASSWORD", "PYTHONPATH", "CLAUDE_CONFIG_DIR", "LC_SECRET", "__CF_USER_TEXT_ENCODING"):
        monkeypatch.setenv(key, "must-not-inherit")
    for key, value in {"PATH": os.environ["PATH"], "LANG": "C", "LC_ALL": "C", "LANGUAGE": "C", "TZ": "UTC"}.items():
        monkeypatch.setenv(key, value)
    async def exercise():
        (tmp_path / "runners" / "fake-7").mkdir(parents=True)
        async with runners() as (owner, processes):
            assert not processes
            with pytest.raises(ValueError):
                await owner.acquire("../fake")
            await proc_tree.sweep(tmp_path)
            lease = await owner.acquire("fake")
            record = tmp_path / proc_tree.RECORDS_DIR / f"{processes[0].pid}.json"
            assert record.exists()
            observed = json.loads((lease._generation.directory / "observed.json").read_text())
            assert lease.runner_id == "fake-8" and observed["cwd"] == str(lease._generation.directory)
            env = observed["env"]
            assert env["HOME"] == observed["cwd"] and env["TMPDIR"] == str(lease._generation.directory / "tmp")
            assert {k: env[k] for k in ("PATH", "LANG", "LANGUAGE", "LC_ALL", "TZ")} == {
                k: os.environ[k] for k in ("PATH", "LANG", "LANGUAGE", "LC_ALL", "TZ")}
            # Darwin inserts CoreFoundation metadata after exec; it is not inherited.
            automatic = {"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()
            assert not (set(env) - s._ENV - {"HOME", "TMPDIR"} - automatic)
            assert "must-not-inherit" not in env.values()
            sid = await lease.open_session(NEW)
            paths = json.loads((lease._generation.directory / "session-paths.json").read_text())
            assert paths == {"cwd": NEW["cwd"], "add_dirs": NEW["add_dirs"],
                "home": str(lease.home), "state_dir": str(lease.state_dir), "session_key": lease.session_key}
            assert sid.startswith("../") and lease.session_key not in sid
            assert lease.home.parent == lease.state_dir.parent == lease.agent_dir.parent == lease.directory
            for path in (owner.root, lease._generation.directory, lease.directory, lease.home, lease.state_dir, lease.agent_dir, lease._generation.directory / "tmp"):
                assert path.stat().st_mode & 0o777 == 0o700
            second = await owner.acquire("fake")
            assert second.bridge is lease.bridge and second.directory != lease.directory
            await asyncio.gather(lease.release(), lease.release())
            assert not lease.directory.exists() and processes[0].returncode is None and second.valid
            await owner.stop()
            await owner.stop()
            assert not record.exists() and not second.directory.exists()
            with pytest.raises(s.RunnerUnavailable):
                await owner.acquire("fake")
    run(exercise())


@pytest.mark.parametrize("options,cap", [({}, 4), ({"session_cap": 1}, 1), ({"cap": 2}, 2), ({"isolation": False}, 1)])
def test_open_reservation_is_bounded_without_pool_expansion(runners, options, cap):
    async def exercise():
        async with runners(**options) as (owner, processes):
            results = await asyncio.gather(*(owner.acquire("fake") for _ in range(6)), return_exceptions=True)
            leases = [r for r in results if isinstance(r, s.RunnerLease)]
            assert len(leases) == cap and len(processes) == 1
            assert all(isinstance(r, p.ProtocolError) and r.code == p.ErrorCode.CAPACITY
                       for r in results if r not in leases)
            await leases[0].release()
            replacement = await owner.acquire("fake")
            assert replacement.bridge is leases[0].bridge and replacement.directory != leases[0].directory
    run(exercise())


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_or_cancelled_open_reclaims_reservation_and_directory(runners, cancelled):
    async def exercise():
        async with runners(session_cap=1, slow_open=cancelled, reject_open=not cancelled) as (owner, processes):
            lease = await owner.acquire("fake")
            opening = asyncio.create_task(lease.open_session(NEW))
            try:
                if cancelled:
                    await until(lambda: (lease._generation.directory / "opening").exists())
                    opening.cancel()
                with pytest.raises(asyncio.CancelledError if cancelled else RpcFailure):
                    await opening
                assert not lease.directory.exists() and not lease._generation.leases
                if not cancelled:
                    next_lease = await owner.acquire("fake")
                    assert next_lease.bridge is lease.bridge and len(processes) == 1
            finally:
                if not opening.done():
                    opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
    run(exercise())


@pytest.mark.parametrize("options", [{}, {"corrupt": True}, {"pipe_child": True}],
                         ids=["crash", "corrupt-live-stdout", "child-holds-pipes"])
def test_generation_loss_reaps_old_group_and_restarts_once(runners, options):
    async def exercise():
        async with runners(**options) as (owner, processes):
            old = await owner.acquire("fake")
            idle = await owner.acquire("fake")
            sid = await old.open_session(NEW)
            turn, events = Turn(), []
            await old.bridge.begin_turn(sid, "trace", events.append, turn, text="accepted prompt")
            if not options.get("corrupt"):
                processes[0].kill()
            assert (await old.bridge.wait_settled(sid, "trace")).outcome == "lost"
            await until(lambda: len(processes) == 2 and owner._kinds["fake"].generation is not None)
            current = await owner.acquire("fake")
            assert current.runner_id != old.runner_id and current.bridge is not old.bridge
            assert processes[0].returncode < 0 and not proc_tree.tree_of(processes[0])
            assert len(owner._kinds["fake"].failures) == 1 and turn.finishes == 1
            assert len([e for e in events if e["type"] == "result"]) == 1
            assert not old.valid and not idle.valid and not old.directory.exists()
            with pytest.raises(ConnectionLost):
                await idle.open_session(NEW)
            assert not current.bridge.sessions  # No replay and no native-id migration.
    run(exercise())


def test_version_mismatch_is_terminal_and_never_restarts(runners):
    async def exercise():
        async with runners(version="9.9") as (owner, processes):
            with pytest.raises(s.RunnerUnavailable):
                await owner.acquire("fake")
            with pytest.raises(s.RunnerUnavailable):
                await owner.acquire("fake")
            assert owner._kinds["fake"].fatal and owner._kinds["fake"].restart is None
            assert len(processes) == 1 and processes[0].returncode is not None
    run(exercise())


def test_connection_loss_during_initialize_restarts_runner(runners):
    async def exercise():
        async with runners() as (owner, processes):
            connect = owner.connect

            async def crash_once(process, kind):
                if not processes:
                    process.kill()
                    await process.wait()
                return await connect(process, kind)

            owner.connect = crash_once
            with pytest.raises(s.RunnerUnavailable, match="restart is bounded"):
                await owner.acquire("fake")
            assert not owner._kinds["fake"].fatal
            await until(lambda: len(processes) == 2 and owner._kinds["fake"].generation is not None)
            lease = await owner.acquire("fake")
            assert lease.valid and processes[0].returncode is not None
            assert len(owner._kinds["fake"].failures) == 1
    run(exercise())


def test_shutdown_reaps_uncooperative_parent_and_separate_child_group(runners):
    async def exercise():
        async with runners(uncooperative=True) as (owner, processes):
            lease = await owner.acquire("fake")
            observed = json.loads((lease._generation.directory / "observed.json").read_text())
            child = observed["child"]
            assert child in proc_tree.tree_of(processes[0])
            await asyncio.gather(owner.stop(), owner.stop())
            assert processes[0].returncode is not None
            assert child not in proc_tree._snapshot() and not proc_tree.tree_of(processes[0])
            assert not owner._tasks and len(processes) == 1
    run(exercise())


def test_stderr_is_lf_bounded_redacted_and_rate_limited(runners, caplog):
    add_secret("secret-sentinel")
    async def exercise():
        async with runners(stderr=True, echo_key=True) as (owner, _):
            lease = await owner.acquire("fake")
            await lease.open_session(dict(NEW, provider={**NEW["provider"], "api_key": "session-provider-sentinel"}))
            await owner.stop()
            records = [r for r in caplog.records if r.name == s.__name__]
            assert len(records) == 10
            assert "safe\u2028one-line" in caplog.text and "secret-sentinel" not in caplog.text
            assert "session-provider-sentinel" not in caplog.text
            retained = b"\n".join(lease._generation.diagnostics)
            assert b"session-provider-sentinel" not in retained and b"***" in retained
            assert sum(map(len, lease._generation.diagnostics)) <= s.DIAGNOSTIC_BYTES
            assert all(len(r.getMessage()) < s.DIAGNOSTIC_BYTES for r in records)
    run(exercise())


def test_shutdown_joins_started_tools_before_removing_session_directory(runners):
    async def exercise():
        async with runners(tool=True) as (owner, _):
            lease = await owner.acquire("fake")
            sid = await lease.open_session(NEW)
            gate = asyncio.Event()
            turn = Turn(gate)
            await lease.bridge.begin_turn(sid, "trace", lambda e: None, turn, text="write")
            await turn.entered.wait()
            stopping = asyncio.create_task(owner.stop())
            try:
                await until(lambda: lease._generation.process.returncode is not None)
                assert not stopping.done() and lease.directory.exists() and lease._generation.directory.exists()
                gate.set()
                await stopping
                assert not lease.directory.exists() and turn.finishes == 1
            finally:
                gate.set()
                await stopping
    run(exercise())



def test_release_closes_one_active_session_and_joins_its_tools(runners):
    async def exercise():
        async with runners(tool=True) as (owner, processes):
            lease, other = await owner.acquire("fake"), await owner.acquire("fake")
            sid = await lease.open_session(NEW)
            gate, events = asyncio.Event(), []
            turn = Turn(gate)
            await lease.bridge.begin_turn(sid, "trace", events.append, turn, text="write")
            await turn.entered.wait()
            releasing = asyncio.create_task(lease.release())
            try:
                await until(lambda: not lease.valid)
                assert lease.directory.exists() and not releasing.done()
                gate.set()
                await releasing
                assert not lease.directory.exists() and processes[0].returncode is None and other.valid
                assert events[-1]["runner"]["outcome"] == "cancelled" and not lease._generation.failed
            finally:
                gate.set()
                await releasing
    run(exercise())


def test_fifth_failure_stops_restart_loop_and_returns_unavailable(runners, monkeypatch):
    monkeypatch.setattr(s, "BACKOFF_MAX", 0.01)
    async def exercise():
        async with runners() as (owner, processes):
            await owner.acquire("fake")
            for number in range(1, 6):
                current = processes[-1]
                current.kill()
                state = owner._kinds["fake"]
                await until(lambda: len(state.failures) == number)
                assert current.returncode is not None and not proc_tree.tree_of(current)
                if number < 5:
                    await until(lambda: len(processes) == number + 1 and state.generation is not None)
            with pytest.raises(s.RunnerUnavailable, match="five failures"):
                await owner.acquire("fake")
            assert len(processes) == 5 and (state.restart is None or state.restart.done())
    run(exercise())


def test_stop_interrupts_backoff_without_spawning_a_replacement(runners, monkeypatch):
    monkeypatch.setattr(s, "BACKOFF_BASE", 60)
    async def exercise():
        async with runners() as (owner, processes):
            await owner.acquire("fake")
            await owner.invalidate("fake", p.ProtocolError(p.ErrorCode.PARSE_ERROR))
            state = owner._kinds["fake"]
            assert not state.fatal and state.restart is not None
            with pytest.raises(s.RunnerUnavailable):
                await owner.acquire("fake")
            await owner.stop()
            assert state.restart.done() and len(processes) == 1 and not owner._tasks
    run(exercise())


def test_directory_cleanup_failure_releases_cap_and_does_not_interrupt_shutdown(runners, monkeypatch):
    async def exercise():
        async with runners(session_cap=1, kinds=("fake", "other")) as (owner, processes):
            lease = await owner.acquire("fake")
            other = await owner.acquire("other")
            remove = s.shutil.rmtree
            def denied(path):
                if path in (lease.directory, other.directory):
                    raise PermissionError("test cleanup denied")
                remove(path)
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(s.shutil, "rmtree", denied)
                    with pytest.raises(PermissionError):
                        await lease.release()
                    replacement = await owner.acquire("fake")
                    assert replacement.bridge is lease.bridge
                    await owner.stop()
                    assert all(process.returncode is not None for process in processes)
                    assert not lease._generation.leases and not other._generation.leases
            finally:
                for path in (lease.directory, other.directory):
                    remove(path, ignore_errors=True)
    run(exercise())

def test_real_turn_reports_lost_after_runner_crash(runners):
    async def exercise():
        async with runners() as (owner, processes):
            lease = await owner.acquire("fake")
            sid = await lease.open_session(NEW)
            async def limits():
                return {"run_cap": None, "task_cap": None, "task_spent": 0}
            turn = RunnerTurn(session_id=sid, trace_id="trace", role="manager", tools=None,
                provider_id="fake", model="fake", price=None, limits=limits)
            await lease.bridge.begin_turn(sid, "trace", lambda e: None, turn, text="accepted")
            processes[0].kill()
            assert (await lease.bridge.wait_settled(sid, "trace")).outcome == "lost"
    run(exercise())


@pytest.mark.parametrize("flag", ["-c", "-e"])
def test_launch_preserves_long_inline_code_and_ordinary_file_arguments(tmp_path, monkeypatch, flag):
    monkeypatch.chdir(tmp_path)
    Path("payload").touch()
    code = "print('inline code');" + " " * 16384
    argv = [sys.executable, flag, code, "payload"]
    owner = s.RunnerSupervisor(tmp_path, {"fake": argv}, connect=None)
    assert owner._kinds["fake"].command == tuple(argv)


def test_launch_resolves_only_explicit_path_arguments(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("payload").touch()
    owner = s.RunnerSupervisor(tmp_path, {"fake": [sys.executable, Path("runner.py"), "payload"]},
                               connect=None)
    assert owner._kinds["fake"].command == (sys.executable, str(tmp_path / "runner.py"), "payload")


@pytest.mark.parametrize("binary", ["missing", "not-executable", "invalid-format"])
def test_launch_refusal_is_terminal(tmp_path, binary):
    executable = tmp_path / "runner"
    if binary != "missing":
        executable.write_text("invalid executable\n")
        executable.chmod(0o755 if binary == "invalid-format" else 0o644)

    async def exercise():
        owner = s.RunnerSupervisor(tmp_path, {"fake": [str(executable)]}, connect=None)
        try:
            for _ in range(2):
                with pytest.raises(s.RunnerUnavailable, match="configuration or protocol"):
                    await owner.acquire("fake")
            state = owner._kinds["fake"]
            assert state.fatal and state.restart is None and len(state.failures) == 1
        finally:
            await owner.stop()
    run(exercise())


def test_temporary_initialize_io_error_is_retryable(runners):
    async def exercise():
        async with runners() as (owner, processes):
            connect = owner.connect

            async def fail_once(process, kind):
                if not processes:
                    processes.append(process)
                    raise OSError(errno.EIO, "temporary initialization I/O failure")
                return await connect(process, kind)

            owner.connect = fail_once
            with pytest.raises(s.RunnerUnavailable, match="restart is bounded"):
                await owner.acquire("fake")
            state = owner._kinds["fake"]
            assert not state.fatal
            await until(lambda: state.generation is not None)
            assert (await owner.acquire("fake")).valid and len(processes) == 2
    run(exercise())


@pytest.mark.parametrize("shutdown", [False, True], ids=["recover", "shutdown"])
def test_reparented_child_in_separate_group_is_reaped(runners, shutdown):
    async def exercise():
        async with runners(uncooperative=True) as (owner, processes):
            lease = await owner.acquire("fake")
            generation = lease._generation
            child = json.loads((generation.directory / "observed.json").read_text())["child"]
            started = proc_tree._start_time(child)
            assert started and child in proc_tree.tree_of(processes[0])
            stopping = None
            try:
                # Synchronize on an observed identity before making the child unreachable from its parent.
                await until(lambda: generation.tree.seen.get(child) == started, timeout=4)
                processes[0].kill()
                await generation.exited.wait()
                assert child in proc_tree._snapshot() and child not in proc_tree.tree_of(processes[0])
                if shutdown:
                    stopping = asyncio.create_task(owner.stop())
                    done, _ = await asyncio.wait([stopping], timeout=4)
                    assert stopping in done
                    await stopping
                    assert not owner._tasks
                else:
                    await until(lambda: len(processes) == 2 and owner._kinds["fake"].generation is not None,
                                timeout=4)
                    assert (await owner.acquire("fake")).valid and not lease.valid
                assert child not in proc_tree._snapshot()
            finally:
                # A regression must not strand the real child or keep fixture shutdown on its pipe.
                if proc_tree._start_time(child) == started:
                    os.kill(child, signal.SIGKILL)
                if stopping is not None:
                    await stopping
    run(exercise())


@pytest.mark.parametrize("shutdown", [False, True], ids=["recover", "shutdown"])
def test_stalled_diagnostic_drain_does_not_block_cleanup(runners, monkeypatch, shutdown):
    async def exercise():
        gate, entered = asyncio.Event(), asyncio.Event()

        async def held_pipe(self, generation):
            entered.set()
            await gate.wait()

        monkeypatch.setattr(s.RunnerSupervisor, "_stderr", held_pipe)
        async with runners() as (owner, processes):
            lease = await owner.acquire("fake")
            await entered.wait()
            stopping = None
            try:
                if shutdown:
                    stopping = asyncio.create_task(owner.stop())
                    done, _ = await asyncio.wait([stopping], timeout=4)
                    assert stopping in done
                    await stopping
                    assert not owner._tasks
                else:
                    processes[0].kill()
                    await until(lambda: len(processes) == 2 and owner._kinds["fake"].generation is not None,
                                timeout=4)
                assert lease._generation.stderr.done()
            finally:
                gate.set()
                if stopping is not None:
                    await stopping
    run(exercise())


def test_reused_child_identity_is_not_signalled_or_allowed_to_hold_shutdown(runners, monkeypatch):
    async def exercise():
        async with runners(uncooperative=True) as (owner, processes):
            lease = await owner.acquire("fake")
            child = json.loads((lease._generation.directory / "observed.json").read_text())["child"]
            started = proc_tree._start_time(child)
            assert started
            stopping = None
            try:
                await until(lambda: lease._generation.tree.seen.get(child) == started, timeout=4)
                start_times = proc_tree._start_times

                def reused(pids):
                    starts = start_times(pids)
                    if starts is not None and child in starts:
                        starts[child] = "different process start time"
                    return starts

                monkeypatch.setattr(proc_tree, "_start_times", reused)
                processes[0].kill()
                await lease._generation.exited.wait()
                stopping = asyncio.create_task(owner.stop())
                done, _ = await asyncio.wait([stopping], timeout=4)
                assert stopping in done
                await stopping
                assert child in proc_tree._snapshot()  # Its inherited diagnostic pipe is still open.
                assert lease._generation.stderr.done() and not owner._tasks
            finally:
                if proc_tree._start_time(child) == started:
                    os.kill(child, signal.SIGKILL)
                if stopping is not None:
                    await stopping
    run(exercise())


@pytest.mark.parametrize("recovers", [True, False], ids=["retry-succeeds", "bounded-failures"])
def test_initialize_timeout_uses_bounded_restart(runners, monkeypatch, recovers):
    monkeypatch.setattr(s, "BACKOFF_MAX", 0.01)
    async def exercise():
        timeouts = (0.1, 2.0) if recovers else (0.1,)
        async with runners(slow_initialize=True, control_timeouts=timeouts) as (owner, processes):
            with pytest.raises(s.RunnerUnavailable, match="restart is bounded"):
                await owner.acquire("fake")
            state = owner._kinds["fake"]
            assert not state.fatal
            if recovers:
                await until(lambda: state.generation is not None)
                lease = await owner.acquire("fake")
                assert lease.valid and len(processes) == 2 and len(state.failures) == 1
            else:
                await until(lambda: len(state.failures) == s.MAX_FAILURES, timeout=5)
                with pytest.raises(s.RunnerUnavailable, match="five failures"):
                    await owner.acquire("fake")
                assert not state.fatal and len(processes) == s.MAX_FAILURES
                assert all(process.returncode is not None for process in processes)
    run(exercise())
