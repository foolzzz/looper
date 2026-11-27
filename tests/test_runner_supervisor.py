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
    import threading
    options = json.loads(sys.argv[1])
    stdout_lock = threading.Lock()

    def emit(payload):
        line = json.dumps(payload)
        with stdout_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def health_body():
        busy = 1 if active else 0
        return {"sessions": count, "running": busy, "inflight_requests": busy,
                "tool_processes": busy, "rss_bytes": 0,
                "event_loop_lag_ms": 5000 if busy else 0}

    def answer_health(rpc_id):
        body = health_body()
        with Path("health-sent.jsonl").open("a") as marker:
            marker.write(json.dumps({"id": rpc_id, **body}) + "\n")
        emit({"jsonrpc": "2.0", "id": rpc_id, "result": body})

    def start_child():
        return subprocess.Popen([sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            start_new_session=not options.get("pipe_child", False))

    child = None
    if options.get("uncooperative") or options.get("pipe_child"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if not options.get("initialize_child"):
            child = start_child()
    Path("observed.tmp").write_text(json.dumps({"cwd": os.getcwd(), "env": dict(os.environ),
        "child": child.pid if child else None}))
    Path("observed.tmp").replace("observed.json")
    if options.get("stderr"):
        sys.stderr.write("x" * 65536 + "\n" + "safe\u2028one-line\n" + "secret-sentinel\n" * 30)
        sys.stderr.flush()
    count, closes, active = 0, 0, {}
    pending_prompt = None
    for line in sys.stdin:
        request = json.loads(line)
        if "method" not in request:
            continue
        method, params = request["method"], request["params"]
        with Path("requests.jsonl").open("a") as log:
            log.write(json.dumps({"method": method, "params": params, "member": os.getpid()}) + "\n")
        if method == "initialize":
            if options.get("initialize_child"):
                child = start_child()
                observed = json.loads(Path("observed.json").read_text())
                Path("observed.tmp").write_text(json.dumps({**observed, "child": child.pid}))
                Path("observed.tmp").replace("observed.json")
            if options.get("slow_initialize"):
                time.sleep(0.25 if options["slow_initialize"] is True else options["slow_initialize"])
            result = {"protocol_version": options.get("version", "0.1"),
                "runner": {"kind": options.get("kind", "fake"), "version": "1"}, "sdk": {"name": "fake", "version": "1"},
                "capabilities": {"max_sessions": options.get("cap", 4),
                    "per_session_isolation": options.get("isolation", True),
                    "cancel": True, "usage": {"per_request": True},
                    "authorize_covers": ["turn", "retry", "compaction", "subagent", "tool", "title", "other"]}}
            if options.get("steer"):
                result["capabilities"]["steer"] = {"in_trace": True}
        elif method in {"session/new", "session/load"}:
            Path("opening").touch()
            Path("session-paths.json").write_text(json.dumps({key: params[key]
                for key in ("cwd", "add_dirs", "home", "state_dir", "session_key")}))
            if options.get("echo_key"):
                print(params["provider"]["api_key"], file=sys.stderr, flush=True)
            if options.get("slow_open"):
                time.sleep(0.25)
            if options.get("reject_open"):
                emit({"jsonrpc": "2.0", "id": request["id"],
                    "error": {"code": -32602, "message": "invalid session"}})
                continue
            count += 1
            result = ({"session_id": params["session_id"], "resumed": True} if method == "session/load"
                      else {"session_id": f"../native-{count}"})
        elif method == "session/steer":
            result = {"session_id": params["session_id"], "trace_id": params["trace_id"], "status": "queued"}
        elif method == "session/close":
            closes += 1
            if options.get("timeout_close_once") and closes == 1:
                # Keep reading the shared pipe while this one response is withheld.
                continue
            if options.get("reject_close") or (options.get("reject_close_once") and closes == 1):
                emit({"jsonrpc": "2.0", "id": request["id"],
                    "error": {"code": -32603, "message": "session persistence failed", "data": params}})
                continue
            result = {key: params[key] for key in ("session_id", "trace_id") if key in params}
        elif method == "runner/health":
            if options.get("health_eof"):
                sys.stdout.flush()
                os.close(1)
                time.sleep(60)
                return
            if options.get("health_error"):
                emit({"jsonrpc": "2.0", "id": request["id"],
                    "error": {"code": -32603, "message": "secret-health-body-sentinel"}})
                continue
            if options.get("health_invalid"):
                emit({"jsonrpc": "2.0", "id": request["id"],
                    "result": {"sessions": "secret-health-body-sentinel"}})
                continue
            if Path("hang-health").exists():
                if not Path("hang-noted").exists():
                    Path("hang-noted").write_text("1")
                    emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "session_id": "not-a-session", "trace_id": "not-a-trace",
                        "kind": "text_delta", "text": "still-busy"}})
                continue
            if Path("health-hold").exists():
                rpc_id = request["id"]

                def release(held=rpc_id):
                    while Path("health-hold").exists():
                        time.sleep(0.01)
                    answer_health(held)

                threading.Thread(target=release, daemon=True).start()
                continue
            answer_health(request["id"])
            continue
        elif method == "session/prompt":
            if options.get("close_before_prompt") and count == 1:
                pending_prompt = request
                continue
            active[params["session_id"]] = params["trace_id"]
            result = {"session_id": params["session_id"], "trace_id": params["trace_id"], "accepted": True}
        else:
            if method == "session/cancel" and Path("hold-cancel").exists():
                held_id = request["id"]
                held = {key: params[key] for key in ("session_id", "trace_id") if key in params}

                def release_cancel(rpc_id=held_id, body=held):
                    while Path("hold-cancel").exists():
                        time.sleep(0.01)
                    emit({"jsonrpc": "2.0", "id": rpc_id, "result": body})

                threading.Thread(target=release_cancel, daemon=True).start()
                continue
            if method == "session/cancel" and pending_prompt is not None:
                emit({"jsonrpc": "2.0", "id": pending_prompt["id"],
                    "error": {"code": -32603, "message": "session already closed",
                        "data": {key: pending_prompt["params"][key] for key in ("session_id", "trace_id")}}})
                pending_prompt = None
            result = {key: params[key] for key in ("session_id", "trace_id") if key in params}
        emit({"jsonrpc": "2.0", "id": request["id"], "result": result})
        if method == "session/close" and params["session_id"] in active:
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                "session_id": params["session_id"], "trace_id": active.pop(params["session_id"]),
                "kind": "settled", "outcome": "cancelled", "final_text": "",
                "requests": 0, "usage_complete": True}})
        if method == "session/prompt" and options.get("corrupt"):
            with stdout_lock:
                sys.stdout.write("corrupt stdout\n")
                sys.stdout.flush()
        if method == "session/prompt" and options.get("tool"):
            emit({"jsonrpc": "2.0", "id": "r-1", "method": "tools/call", "params": {
                "session_id": params["session_id"], "trace_id": params["trace_id"],
                "call_id": "call", "name": "write", "arguments": {}}})
        if method == "session/prompt" and options.get("auto_settle"):
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                "session_id": params["session_id"], "trace_id": params["trace_id"],
                "kind": "settled", "outcome": "completed", "final_text": "done",
                "requests": 0, "usage_complete": True}})
        if method == "session/prompt" and options.get("settle_file"):
            sid, tid = params["session_id"], params["trace_id"]

            def wait_settle(session_id=sid, trace_id=tid):
                while not Path("settle-now").exists():
                    time.sleep(0.01)
                emit({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "session_id": session_id, "trace_id": trace_id,
                    "kind": "settled", "outcome": "completed", "final_text": "done",
                    "requests": 0, "usage_complete": True}})

            threading.Thread(target=wait_settle, daemon=True).start()
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
from looper.agents.runtimes.runner.bridge import (
    MAX_ABANDONED_CONTROLS, ConnectionLost, RpcFailure, RunnerBridge)
from looper.agents.runtimes.runner.turn import RunnerTurn

NEW = next(c["message"]["params"] for c in json.loads(
    (Path(__file__).resolve().parents[1] / "runners/protocol/cases.json").read_text()) if c["name"] == "new")


def run(coro):
    # Real process identity queries each allow five seconds. This is a hang
    # guard for startup, recovery and cleanup, not a runner latency contract.
    asyncio.run(asyncio.wait_for(coro, 30))


async def until(predicate, *, timeout=10):
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
    async def make(*, session_cap=4, kinds=("fake",), control_timeouts=(10.0,), env=None,
                   startup_error=None, **options):
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
            session_cap=session_cap, connect=connect, env=env, startup_error=startup_error)
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


def test_kind_environment_reaches_only_its_runner(runners, monkeypatch):
    monkeypatch.setenv("PI_OFFLINE", "must-not-inherit")
    async def exercise():
        fixed = {"fake": {"PI_OFFLINE": "1"}}
        async with runners(kinds=("fake", "other"), env=fixed) as (owner, _):
            fixed["fake"]["PI_OFFLINE"] = "changed-after-construction"
            first, second = await owner.acquire("fake"), await owner.acquire("other")
            for lease, expected in ((first, "1"), (second, None)):
                observed = json.loads((lease._generation.directory / "observed.json").read_text())
                assert observed["env"].get("PI_OFFLINE") == expected
                assert observed["env"]["HOME"] == str(lease._generation.directory)
            await first.release()
            await second.release()
    run(exercise())


@pytest.mark.parametrize("env", [
    {"unknown": {"PI_OFFLINE": "1"}},
    {"fake": {"lowercase": "1"}},
    {"fake": {"1BAD": "1"}},
    *({"fake": {name: "1"}} for name in ("PATH", "HOME", "TMPDIR", "TZ", "LANG", "LANGUAGE", "LC_ALL")),
    {"fake": {"PI_OFFLINE": 1}},
    {"fake": {"PI_OFFLINE": "bad\0value"}},
    {"fake": []},
    [],
])
def test_invalid_kind_environment_is_rejected(tmp_path, env):
    with pytest.raises(ValueError):
        s.RunnerSupervisor(tmp_path, {"fake": [sys.executable]}, connect=None, env=env)


@pytest.mark.parametrize("load", [False, True])
def test_private_process_environment_and_session_paths(runners, tmp_path, monkeypatch, load):
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
            sid = (await lease.load_session({**NEW, "session_id": "../native-saved"}) if load
                   else await lease.open_session(NEW))
            with pytest.raises(RuntimeError, match="only one session"):
                await lease.load_session({**NEW, "session_id": "another"})
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
def test_open_reservation_is_bounded_by_pool_capacity(runners, options, cap):
    async def exercise():
        async with runners(**options) as (owner, processes):
            pool_cap = cap * s.MAX_RUNNERS_PER_KIND
            results = await asyncio.gather(*(owner.acquire("fake") for _ in range(pool_cap + 2)), return_exceptions=True)
            leases = [r for r in results if isinstance(r, s.RunnerLease)]
            assert len(leases) == pool_cap and len(processes) == s.MAX_RUNNERS_PER_KIND
            assert all(isinstance(r, p.ProtocolError) and r.code == p.ErrorCode.CAPACITY
                       for r in results if r not in leases)
            await leases[0].release()
            replacement = await owner.acquire("fake")
            assert replacement.bridge is leases[0].bridge and replacement.directory != leases[0].directory
    run(exercise())


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("load", [False, True])
def test_failed_or_cancelled_open_reclaims_reservation_and_directory(runners, cancelled, load):
    async def exercise():
        async with runners(session_cap=1, slow_open=cancelled, reject_open=not cancelled) as (owner, processes):
            lease = await owner.acquire("fake")
            opening = asyncio.create_task(lease.load_session({**NEW, "session_id": "saved"}) if load
                                          else lease.open_session(NEW))
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
def test_generation_loss_reaps_old_group_and_replaces_on_demand(runners, options):
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
            # Recovery reaps the old member; demand starts its replacement only
            # after the bounded backoff has completed.
            await asyncio.shield(old._generation.failure)
            await asyncio.shield(owner._kinds["fake"].restart)
            assert len(processes) == 1 and not owner._kinds["fake"].members
            current = await owner.acquire("fake")
            assert len(processes) == 2
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


def test_connection_loss_during_initialize_retries_on_demand(runners):
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
            state = owner._kinds["fake"]
            assert not state.fatal
            await asyncio.shield(state.restart)
            assert len(processes) == 1 and not state.members
            lease = await owner.acquire("fake")
            assert lease.valid and len(processes) == 2 and processes[0].returncode is not None
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


def test_stderr_is_lf_bounded_redacted_and_rate_limited(runners, caplog, monkeypatch):
    from types import SimpleNamespace

    # Keep this burst in one diagnostic window regardless of subprocess scheduling.
    # Replace only the supervisor's clock, leaving asyncio's timeout clock live.
    monkeypatch.setattr(s, "time", SimpleNamespace(monotonic=lambda: 0.0))
    add_secret("secret-sentinel")
    async def exercise():
        async with runners(stderr=True, echo_key=True) as (owner, _):
            lease = await owner.acquire("fake")
            await lease.open_session(dict(NEW, provider={**NEW["provider"], "api_key": "session-provider-sentinel"}))
            await until(lambda: len(lease._generation.diagnostics) == 33)
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


@pytest.mark.parametrize("kind", ["fake", "pi", "dsh"])
def test_fifth_failure_stops_demand_retries_and_returns_unavailable(runners, monkeypatch, kind):
    from looper.agents.runtimes import dsh_runner, pi_runner

    startup_error = {"pi": pi_runner._opt_out, "dsh": dsh_runner._opt_out}.get(kind)
    monkeypatch.setattr(s, "BACKOFF_MAX", 0.01)
    async def exercise():
        async with runners(kinds=(kind,), startup_error=startup_error) as (owner, processes):
            lease = await owner.acquire(kind)
            for number in range(1, 6):
                current = processes[-1]
                state = owner._kinds[kind]
                generation = lease._generation
                current.kill()
                await generation.exited.wait()
                await asyncio.shield(generation.failure)
                assert len(state.failures) == number
                assert current.returncode is not None and not proc_tree.tree_of(current)
                if number < 5:
                    await asyncio.shield(state.restart)
                    assert len(processes) == number and not state.members
                    lease = await owner.acquire(kind)
                    assert len(processes) == number + 1
            with pytest.raises(s.RunnerUnavailable, match="five failures") as caught:
                await owner.acquire(kind)
            message = str(caught.value)
            assert "runner-exited:" in message
            if startup_error is not None:
                assert f'[runtimes] {kind} = "cli"' in message
            assert len(processes) == 5 and (state.restart is None or state.restart.done())
    run(exercise())


def test_stop_interrupts_backoff_without_spawning_a_replacement(runners, monkeypatch):
    monkeypatch.setattr(s, "BACKOFF_BASE", 60)
    async def exercise():
        async with runners() as (owner, processes):
            lease = await owner.acquire("fake")
            await owner.invalidate(lease, p.ProtocolError(p.ErrorCode.PARSE_ERROR))
            state = owner._kinds["fake"]
            assert not state.fatal and state.restart is not None
            acquiring = asyncio.create_task(owner.acquire("fake"))
            await asyncio.sleep(0)
            assert not acquiring.done()
            await owner.stop()
            with pytest.raises(s.RunnerUnavailable, match="stopped"):
                await acquiring
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
            await asyncio.shield(state.restart)
            assert not state.members and len(processes) == 1
            assert (await owner.acquire("fake")).valid and len(processes) == 2
    run(exercise())


@pytest.mark.parametrize("kind", ["pi", "dsh"])
@pytest.mark.parametrize("failure", ["timeout", "io", "connection", "rpc"])
def test_retryable_startup_failure_names_cause_and_opt_out(runners, kind, failure):
    from looper.agents.runtimes import dsh_runner, pi_runner

    module = pi_runner if kind == "pi" else dsh_runner
    add_secret("startup-secret-sentinel")

    async def exercise():
        async with runners(startup_error=module._opt_out, slow_initialize=True,
                           control_timeouts=(0.1, 10)) as (owner, processes):
            connect = owner.connect

            async def fail_once(process, runner_kind):
                if not processes and failure != "timeout":
                    processes.append(process)
                    cause = "temporary initialization failure\nstartup-secret-sentinel"
                    if failure == "io":
                        raise OSError(errno.EIO, cause)
                    if failure == "connection":
                        raise ConnectionLost(cause)
                    raise RpcFailure({"code": p.ErrorCode.INTERNAL_ERROR, "message": cause})
                return await connect(process, runner_kind)

            owner.connect = fail_once
            with pytest.raises(s.RunnerUnavailable) as caught:
                await owner.acquire("fake")
            message = str(caught.value)
            assert ("timed out" if failure == "timeout" else "temporary initialization failure") in message
            assert f'[runtimes] {kind} = "cli" in the ops file' in message
            assert "restart is bounded" in message
            assert "startup-secret-sentinel" not in message and "\n" not in message
            state = owner._kinds["fake"]
            assert state.fatal is None and len(state.failures) == 1
            assert not state.members and processes[0].returncode is not None
            await asyncio.shield(state.restart)
            assert (await owner.acquire("fake")).valid and len(processes) == 2
    run(exercise())


@pytest.mark.parametrize("shutdown", [False, True], ids=["recover", "shutdown"])
def test_immediate_generation_loss_reaps_startup_child_in_separate_group(runners, monkeypatch, shutdown):
    # No periodic observation has run when a caller kills a just-started runner.
    monkeypatch.setattr(proc_tree.TurnTree, "start_ticker", lambda self: None)
    async def exercise():
        async with runners(uncooperative=True, initialize_child=True) as (owner, processes):
            lease = await owner.acquire("fake")
            generation = lease._generation
            child = json.loads((generation.directory / "observed.json").read_text())["child"]
            started = proc_tree._start_time(child)
            stopping = None
            try:
                processes[0].kill()
                await generation.exited.wait()
                assert started
                assert child in proc_tree._snapshot() and child not in proc_tree.tree_of(processes[0])
                if shutdown:
                    stopping = asyncio.create_task(owner.stop())
                    await stopping
                    assert not owner._tasks
                else:
                    await asyncio.shield(generation.failure)
                    await asyncio.shield(owner._kinds["fake"].restart)
                    assert len(processes) == 1 and not owner._kinds["fake"].members
                    assert (await owner.acquire("fake")).valid and not lease.valid
                    assert len(processes) == 2
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
                    await stopping
                    assert not owner._tasks
                else:
                    processes[0].kill()
                    await lease._generation.exited.wait()
                    await asyncio.shield(lease._generation.failure)
                    await asyncio.shield(owner._kinds["fake"].restart)
                    assert len(processes) == 1 and not owner._kinds["fake"].members
                    assert (await owner.acquire("fake")).valid and len(processes) == 2
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
                await until(lambda: lease._generation.tree.seen.get(child) == started)
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
                await stopping
                assert child in proc_tree._snapshot()  # Its inherited diagnostic pipe is still open.
                assert lease._generation.stderr.done() and not owner._tasks
            finally:
                if proc_tree._start_time(child) == started:
                    os.kill(child, signal.SIGKILL)
                if stopping is not None:
                    await stopping
    run(exercise())



@pytest.mark.parametrize("action", ["cancel", "abandon", "stop"])
def test_interrupted_initialize_reaps_uncooperative_process_tree(runners, monkeypatch, action):
    from looper.agents.runtimes import cancel

    monkeypatch.setattr(cancel, "ABANDON_SEC", 0.02)

    async def exercise():
        async with runners(uncooperative=True, initialize_child=True, slow_initialize=60) as (owner, processes):
            acquiring = asyncio.create_task(owner.acquire("fake"))
            stopping = None
            try:
                await until(lambda: bool(processes) and any(
                    path.exists() and json.loads(path.read_text())["child"] is not None
                    for path in owner.root.glob("*/observed.json")))
                child = next(json.loads(path.read_text())["child"]
                             for path in owner.root.glob("*/observed.json"))
                assert child in proc_tree._snapshot()
                if action == "stop":
                    stopping = asyncio.create_task(owner.stop())
                elif action == "abandon":
                    await cancel.abandon(acquiring)
                else:
                    acquiring.cancel()
                await until(lambda: processes[0].returncode is not None
                            and child not in proc_tree._snapshot(), timeout=s.SHUTDOWN_TIMEOUT + 5)
                if stopping is not None:
                    await stopping
                with pytest.raises((asyncio.CancelledError, s.RunnerUnavailable)):
                    await acquiring
                assert not owner._kinds["fake"].members
            finally:
                # Keep the regression itself from leaving a sleeping runner or child behind.
                for process in processes:
                    await proc_tree.end(process, label="interrupted startup test cleanup")
                await asyncio.gather(acquiring, *([stopping] if stopping else []), return_exceptions=True)
    run(exercise())


@pytest.mark.parametrize("action", ["cancel", "stop"])
def test_interrupted_spawn_reaps_process_when_spawn_returns(runners, monkeypatch, action):
    async def exercise():
        spawned, release = asyncio.Event(), asyncio.Event()
        original_spawn = proc_tree.spawn
        created = []

        async def held_spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            created.append(process)
            spawned.set()
            await release.wait()
            return process

        monkeypatch.setattr(proc_tree, "spawn", held_spawn)
        async with runners(uncooperative=True, slow_initialize=60) as (owner, _):
            acquiring = asyncio.create_task(owner.acquire("fake"))
            stopping = None
            try:
                await spawned.wait()
                await until(lambda: any(path.exists() for path in owner.root.glob("*/observed.json")))
                if action == "stop":
                    stopping = asyncio.create_task(owner.stop())
                    await asyncio.sleep(0)
                else:
                    acquiring.cancel()
                    await asyncio.sleep(0)
                release.set()
                await until(lambda: created[0].returncode is not None, timeout=s.SHUTDOWN_TIMEOUT + 5)
                assert created[0].returncode == -signal.SIGKILL
                assert not list(owner.root.glob("*/requests.jsonl"))
                if stopping is not None:
                    await stopping
                with pytest.raises((asyncio.CancelledError, s.RunnerUnavailable)):
                    await acquiring
            finally:
                release.set()
                for process in created:
                    await proc_tree.end(process, label="interrupted spawn test cleanup")
                await asyncio.gather(acquiring, *([stopping] if stopping else []), return_exceptions=True)
    run(exercise())

@pytest.mark.parametrize("recovers", [True, False], ids=["retry-succeeds", "bounded-failures"])
def test_initialize_timeout_uses_bounded_demand_retries(runners, monkeypatch, recovers):
    monkeypatch.setattr(s, "BACKOFF_MAX", 0.01)
    async def exercise():
        timeouts = (0.1, 10.0) if recovers else (0.1,)
        async with runners(slow_initialize=True, control_timeouts=timeouts) as (owner, processes):
            with pytest.raises(s.RunnerUnavailable, match="restart is bounded"):
                await owner.acquire("fake")
            state = owner._kinds["fake"]
            assert not state.fatal
            await asyncio.shield(state.restart)
            assert not state.members and len(processes) == 1
            if recovers:
                lease = await owner.acquire("fake")
                assert lease.valid and len(processes) == 2 and len(state.failures) == 1
            else:
                for number in range(2, s.MAX_FAILURES + 1):
                    with pytest.raises(s.RunnerUnavailable, match="restart is bounded"):
                        await owner.acquire("fake")
                    assert len(processes) == number and len(state.failures) == number
                    if number < s.MAX_FAILURES:
                        await asyncio.shield(state.restart)
                    assert not state.members
                assert len(state.failures) == s.MAX_FAILURES
                with pytest.raises(s.RunnerUnavailable, match="five failures"):
                    await owner.acquire("fake")
                assert not state.fatal and len(processes) == s.MAX_FAILURES
                assert all(process.returncode is not None for process in processes)
    run(exercise())


@pytest.mark.parametrize("active", [False, True], ids=["idle", "running"])
@pytest.mark.parametrize("failure", ["reject_close_once", "timeout_close_once"], ids=["rejected", "timeout"])
def test_failed_close_retains_lease_capacity_until_retry_succeeds(runners, monkeypatch, active, failure):
    monkeypatch.setattr(s, "MAX_RUNNERS_PER_KIND", 1)
    async def exercise():
        async with runners(**{failure: True}, session_cap=2) as (owner, processes):
            lease, other = await owner.acquire("fake"), await owner.acquire("fake")
            sid = await lease.open_session(NEW)
            other_sid = await other.open_session(NEW)
            entry = lease.bridge.sessions[sid]
            events = []
            if active:
                async def limits():
                    return {"run_cap": None, "task_cap": None, "task_spent": 0}
                turn = RunnerTurn(session_id=sid, trace_id="trace", role="manager", tools=None,
                    provider_id="fake", model="fake", price=None, limits=limits)
                await lease.bridge.begin_turn(sid, "trace", events.append, turn, text="running")
                assert (await turn.authorize({"session_id": sid, "trace_id": "trace", "request_id": "pending",
                    "kind": "turn", "provider": "fake", "model": "fake",
                    "estimated_input_tokens": 1, "max_output_tokens": 1}))["allow"]
                assert turn._requests
            other_events = []
            await other.bridge.begin_turn(other_sid, "other-trace", other_events.append, Turn(), text="still active")
            other_entry = other.bridge.sessions[other_sid]
            control_timeout = lease.bridge.control_timeout
            if failure == "timeout_close_once":
                lease.bridge.control_timeout = 0.1

            await lease.release()
            # The short deadline injects only the first withheld response. The
            # peer probe and successful retry use the normal control deadline.
            lease.bridge.control_timeout = control_timeout
            assert not lease.valid and lease.directory.exists()
            assert lease in lease._generation.leases and len(lease._generation.leases) == 2
            assert entry.close_state == "close_failed"
            assert other.valid and not lease._generation.failed and other_entry.summary is None
            assert other_entry.state == "running" and not any(event["type"] == "result" for event in other_events)
            assert processes[0].returncode is None and len(processes) == 1
            if active:
                assert (await lease.bridge.wait_settled(sid, "trace")).outcome == "failed"
                assert entry.worker.done() and not turn._requests
                assert [event["runner"]["outcome"] for event in events if event["type"] == "result"] == ["failed"]
            with pytest.raises(p.ProtocolError) as full:
                await owner.acquire("fake")
            assert full.value.code == p.ErrorCode.CAPACITY
            # A fresh request on the peer trace proves the shared reader still works.
            result = await other.bridge.request("session/cancel", {"session_id": other_sid, "trace_id": "other-trace"})
            assert result["session_id"] == other_sid and other_entry.summary is None

            await lease.release()
            assert not lease.directory.exists() and lease not in lease._generation.leases
            assert sid not in lease.bridge.sessions
            replacement = await owner.acquire("fake")
            assert replacement.bridge is other.bridge and len(lease._generation.leases) == 2
            await other.release()
            assert other_events[-1]["runner"]["outcome"] == "cancelled"
            assert processes[0].returncode is None and len(processes) == 1
    run(exercise())


def test_generation_end_cleans_lease_retained_after_rejected_close(runners):
    async def exercise():
        async with runners(reject_close=True, session_cap=2) as (owner, processes):
            lease = await owner.acquire("fake")
            sid = await lease.open_session(NEW)
            entry = lease.bridge.sessions[sid]
            generation = lease._generation
            await lease.release()
            assert not lease.valid and lease.directory.exists() and lease in generation.leases
            assert lease.bridge.sessions[sid].close_state == "close_failed"
            # Stop ends the generation even when every close attempt is rejected.
            await owner.stop()
            assert all(process.returncode is not None for process in processes)
            assert not lease.directory.exists() and not generation.leases
            assert entry.close_state == "poisoned"
    run(exercise())


@pytest.mark.parametrize("cancel_caller", [False, True], ids=["internal", "caller"])
def test_release_after_close_ack_and_prompt_rejection_settles_locally(runners, monkeypatch, cancel_caller):
    monkeypatch.setattr(s, "MAX_RUNNERS_PER_KIND", 1)
    async def exercise():
        async with runners(close_before_prompt=True, session_cap=1) as (owner, processes):
            lease = await owner.acquire("fake")
            sid = await lease.open_session(NEW)
            gate, events = asyncio.Event(), []
            turn = Turn(gate)
            beginning = asyncio.create_task(lease.bridge.begin_turn(sid, "trace", events.append, turn, text="running"))
            entry = lease.bridge.sessions[sid]
            await until(lambda: entry.prompt_sent)
            releasing = asyncio.create_task(lease.release())
            try:
                # request() has returned: close was acknowledged and its task is
                # now waiting on the still-pending prompt's settlement.
                await until(lambda: turn.stopped and entry.close_task is not None and not any(
                    pending.method == "session/close" for pending in lease.bridge._pending.values()))
                assert not releasing.done() and entry.close_state == "closing"
                if cancel_caller:
                    releasing.cancel()
                await lease.bridge.request("session/cancel", dict(session_id=sid, trace_id="trace"))
                with pytest.raises(RpcFailure):
                    await beginning
                await until(lambda: entry.close_state != "closing" or entry.close_task.done())
                assert entry.close_state == "close_failed"
                assert not releasing.done() and entry.ready.is_set()
                assert lease.directory.exists() and lease in lease._generation.leases
                with pytest.raises(p.ProtocolError) as full:
                    await owner.acquire("fake")
                assert full.value.code == p.ErrorCode.CAPACITY
            finally:
                gate.set()
                await asyncio.gather(beginning, releasing, return_exceptions=True)
            if cancel_caller:
                with pytest.raises(asyncio.CancelledError):
                    await releasing
            else:
                await releasing
            assert entry.summary.outcome == "failed" and turn.finishes == 1 and entry.worker.done()
            assert [event["runner"]["outcome"] for event in events if event["type"] == "result"] == ["failed"]
            assert lease.bridge.tombstones[sid].trace_id == "trace"
            assert lease.bridge.tombstones[sid].outcome == "failed"
            assert lease in lease._generation.leases and lease.directory.exists()
            await lease.release()
            assert sid not in lease.bridge.sessions and not lease.directory.exists()
            replacement = await owner.acquire("fake")
            assert replacement.bridge is lease.bridge and len(processes) == 1
            assert not replacement._generation.failed and processes[0].returncode is None
    run(exercise())


def test_owner_state_directory_survives_release_and_restart(runners, tmp_path):
    async def exercise():
        async with runners() as (owner, processes):
            lease = await owner.acquire("fake", owner="manager")
            await lease.open_session(NEW)
            state_dir = lease.state_dir
            assert state_dir == tmp_path / "runners/state/fake/manager"
            for directory in (state_dir, *list(state_dir.parents)[:3]):
                assert directory.stat().st_mode & 0o777 == 0o700
            (state_dir / "saved").write_text("native state")
            await lease.release()
            assert not lease.directory.exists() and (state_dir / "saved").read_text() == "native state"
            active = await owner.acquire("fake", owner="manager")
            assert active.state_dir == state_dir
            generation = lease._generation
            processes[0].kill()
            await generation.exited.wait()
            await asyncio.shield(generation.failure)
            await asyncio.shield(owner._kinds["fake"].restart)
            assert len(processes) == 1 and not owner._kinds["fake"].members
            again = await owner.acquire("fake", owner="manager")
            assert not active.directory.exists()
            assert again.runner_id != lease.runner_id and again.state_dir == state_dir
            assert (again.state_dir / "saved").read_text() == "native state"
            await again.release()
            assert not again.directory.exists() and state_dir.exists()
    run(exercise())


@pytest.mark.parametrize("name", ["..", ".x", ""])
def test_owner_name_cannot_escape(runners, name):
    async def exercise():
        async with runners() as (owner, processes):
            with pytest.raises(ValueError, match="owner"):
                await owner.acquire("fake", owner=name)
            assert not processes
    run(exercise())



def test_run_state_directory_survives_release(runners, tmp_path):
    async def exercise():
        async with runners() as (owner, processes):
            state_dir = tmp_path / "runs/t/r"
            lease = await owner.acquire("fake", state_dir=state_dir)
            assert lease.state_dir == state_dir
            for directory in (state_dir, state_dir.parent):
                assert directory.stat().st_mode & 0o777 == 0o700
            await lease.open_session(NEW)
            (state_dir / "saved").write_text("native state")
            await lease.release()
            assert not lease.directory.exists() and (state_dir / "saved").read_text() == "native state"
            active = await owner.acquire("fake", state_dir=state_dir)
            generation = active._generation
            processes[0].kill()
            await generation.exited.wait()
            await asyncio.shield(generation.failure)
            await asyncio.shield(owner._kinds["fake"].restart)
            assert len(processes) == 1 and not owner._kinds["fake"].members
            replacement = await owner.acquire("fake", state_dir=state_dir)
            assert replacement.state_dir == state_dir and replacement.runner_id != active.runner_id
            assert not active.directory.exists() and (state_dir / "saved").read_text() == "native state"
    run(exercise())


@pytest.mark.parametrize("rule", ["owner", "relative", "symlink"])
def test_run_state_directory_rules(runners, tmp_path, rule):
    async def exercise():
        async with runners() as (owner, processes):
            state_dir = tmp_path / "run"
            if rule == "relative":
                state_dir = Path("relative")
            elif rule == "symlink":
                state_dir.symlink_to(tmp_path, target_is_directory=True)
            with pytest.raises(ValueError):
                await owner.acquire("fake", owner="manager" if rule == "owner" else None, state_dir=state_dir)
            assert all(not member.leases for member in owner._kinds["fake"].members)
    run(exercise())


@pytest.mark.parametrize("kind", ["pi", "dsh"])
def test_five_initialize_failures_retain_last_cause_and_opt_out(runners, monkeypatch, kind):
    from looper.agents.runtimes import dsh_runner, pi_runner

    module = pi_runner if kind == "pi" else dsh_runner
    monkeypatch.setattr(s, "BACKOFF_BASE", 0)

    async def exercise():
        async with runners(startup_error=module._opt_out) as (owner, processes):
            async def fail(process, runner_kind):
                processes.append(process)
                raise ConnectionLost(f"initialize attempt {len(processes)} refused")

            owner.connect = fail
            for number in range(1, 6):
                with pytest.raises(s.RunnerUnavailable, match=f"initialize attempt {number} refused"):
                    await owner.acquire("fake")
            with pytest.raises(s.RunnerUnavailable) as caught:
                await owner.acquire("fake")
            message = str(caught.value)
            assert "five failures within ten minutes" in message
            assert "initialize attempt 5 refused" in message
            assert f'[runtimes] {kind} = "cli"' in message
            assert len(processes) == 5 and all(process.returncode is not None for process in processes)
    run(exercise())


@pytest.mark.parametrize("phase", ["spawn", "initialize", "ready"])
def test_five_cancelled_cold_starts_do_not_block_next_start(runners, monkeypatch, phase):
    async def exercise():
        entered = asyncio.Queue()
        original_spawn = proc_tree.spawn
        spawned = []

        async def held_spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            spawned.append(process)
            if phase == "spawn" and len(spawned) <= 5:
                release = asyncio.Event()
                await entered.put(release)
                await release.wait()
            return process

        monkeypatch.setattr(proc_tree, "spawn", held_spawn)
        async with runners() as (owner, processes):
            connect = owner.connect
            start = owner._start

            async def ready_start(kind, state):
                member = await start(kind, state)
                if len(spawned) <= 5:
                    entered.put_nowait(None)
                    acquiring.cancel()
                return member

            if phase == "ready":
                monkeypatch.setattr(owner, "_start", ready_start)

            async def held_connect(process, kind):
                if phase == "initialize" and len(spawned) <= 5:
                    processes.append(process)
                    await entered.put(None)
                    await asyncio.Event().wait()
                return await connect(process, kind)

            owner.connect = held_connect
            for _ in range(5):
                acquiring = asyncio.create_task(owner.acquire("fake"))
                try:
                    release = await entered.get()
                    if phase != "ready":
                        acquiring.cancel()
                    if release is not None:
                        release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await acquiring
                    assert spawned[-1].returncode is not None
                finally:
                    if not acquiring.done():
                        acquiring.cancel()
                    await asyncio.gather(acquiring, return_exceptions=True)
            state = owner._kinds["fake"]
            assert not state.failures and state.restart is None
            lease = await owner.acquire("fake")
            assert lease.valid and len(spawned) == 6
            await lease.release()
    run(exercise())


def _rows(directory):
    path = directory / "requests.jsonl"
    if not path.exists():
        return []
    # The peer can still be appending the last record. Its newline commits it.
    return [json.loads(line) for line in path.read_text().split("\n")[:-1] if line.strip()]


@pytest.mark.parametrize("tail", [
    '{"method": "session/cancel", "params": {"session_id": "../native-1", "',
    '{"method": "session/cancel"}',
])
def test_request_rows_wait_for_the_writer_to_finish_a_record(tmp_path, tail):
    path = tmp_path / "requests.jsonl"
    complete = '{"method": "session/new"}\n'
    path.write_text(complete + tail)
    assert _rows(tmp_path) == [{"method": "session/new"}]
    path.write_text(complete + '{"method": "session/cancel"}\n')
    assert _rows(tmp_path) == [{"method": "session/new"}, {"method": "session/cancel"}]
    path.write_text(complete + '{invalid}\n')
    with pytest.raises(json.JSONDecodeError):
        _rows(tmp_path)


def _health_count(directory):
    return sum(row["method"] == "runner/health" for row in _rows(directory))


def _answered(directory):
    path = directory / "health-sent.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


class CountingTurn(Turn):
    def __init__(self, gate=None):
        super().__init__(gate)
        self.writes = 0

    async def call_tool(self, params):
        self.writes += 1
        return await super().call_tool(params)


def test_health_monitor_polls_each_live_generation_without_overlap(runners, monkeypatch):
    assert s.HEALTH_INTERVAL_SECONDS == 10 and s.HEALTH_TIMEOUT_SECONDS == 30

    class Clock:
        def __init__(self):
            self.now = 0.0
            self.waiters = []

        def monotonic(self):
            return self.now

        def advance(self, dt):
            self.now += dt
            due = [fut for deadline, fut in self.waiters if deadline <= self.now + 1e-9]
            self.waiters = [(deadline, fut) for deadline, fut in self.waiters if deadline > self.now + 1e-9]
            for fut in due:
                if not fut.done():
                    fut.set_result(None)

    clock = Clock()
    monkeypatch.setattr(s, "time", clock)

    async def fake_sleep(seconds):
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        clock.waiters.append((clock.now + float(seconds), fut))
        await fut

    monkeypatch.setattr(s, "_sleep", fake_sleep)

    async def sleeping(n):
        async with asyncio.timeout(3):
            while len(clock.waiters) < n:
                await asyncio.sleep(0.01)

    async def probes(dirs, counts, sleepers):
        async with asyncio.timeout(3):
            while True:
                if ([_health_count(d) for d in dirs] == counts and len(clock.waiters) >= sleepers):
                    return
                await asyncio.sleep(0.01)

    async def exercise():
        async with runners(session_cap=1) as (owner, _):
            first, second = await owner.acquire("fake"), await owner.acquire("fake")
            dirs = [first._generation.directory, second._generation.directory]
            await sleeping(2)
            assert [_health_count(d) for d in dirs] == [0, 0]
            clock.advance(10)
            await probes(dirs, [1, 1], 2)
            clock.advance(10)
            await probes(dirs, [2, 2], 2)
            clock.advance(25)
            await probes(dirs, [3, 3], 2)
            assert clock.now == 45
            (dirs[0] / "health-hold").write_text("1")
            clock.advance(5)
            await until(lambda: _health_count(dirs[0]) == 4 and _health_count(dirs[1]) == 4
                        and _answered(dirs[0]) == 3 and len(clock.waiters) >= 1, timeout=3)
            clock.advance(25)
            await asyncio.sleep(0.05)
            assert _health_count(dirs[0]) == 4 and _answered(dirs[0]) == 3
            await probes([dirs[1]], [5], 1)
            assert _health_count(dirs[0]) == 4
            (dirs[0] / "health-hold").unlink()
            await until(lambda: _answered(dirs[0]) == 4 and len(clock.waiters) >= 2, timeout=3)
            assert _health_count(dirs[0]) == 4 and clock.now == 75
    run(exercise())


def test_busy_runner_answering_health_is_not_reaped(runners, monkeypatch):
    monkeypatch.setattr(s, "HEALTH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(s, "HEALTH_TIMEOUT_SECONDS", 0.4)

    async def exercise():
        async with runners(tool=True, settle_file=True) as (owner, processes):
            lease = await owner.acquire("fake")
            pid = processes[0].pid
            sid = await lease.open_session(NEW)
            gate, events = asyncio.Event(), []
            turn = CountingTurn(gate)
            await lease.bridge.begin_turn(sid, "trace", events.append, turn, text="write")
            await turn.entered.wait()
            directory = lease._generation.directory
            await until(lambda: _answered(directory) >= 1)
            sent = json.loads((directory / "health-sent.jsonl").read_text().splitlines()[-1])
            assert sent["event_loop_lag_ms"] == 5000 and sent["running"] == 1 and sent["tool_processes"] == 1
            await asyncio.sleep(0.12)
            assert processes[0].pid == pid and processes[0].returncode is None and lease.valid
            assert not lease._generation.failed and lease.session_id == sid and turn.writes == 1
            (directory / "settle-now").write_text("1")
            gate.set()
            assert (await lease.bridge.wait_settled(sid, "trace")).outcome == "completed"
            assert turn.writes == 1 and processes[0].returncode is None and lease.valid
            assert len([event for event in events if event["type"] == "result"]) == 1
    run(exercise())


def test_health_timeout_loses_only_the_hung_member_and_replaces_on_demand(runners, monkeypatch, caplog):
    monkeypatch.setattr(s, "HEALTH_INTERVAL_SECONDS", 0.05)
    # A loaded host can take longer than a few hundred milliseconds to answer
    # one health RPC. The hung member never answers; only this bound moves.
    monkeypatch.setattr(s, "HEALTH_TIMEOUT_SECONDS", 1.5)
    monkeypatch.setattr(s, "BACKOFF_BASE", 2.0)

    async def exercise():
        async with runners(session_cap=1, pipe_child=True, tool=True) as (owner, processes):
            hung, sibling = await owner.acquire("fake"), await owner.acquire("fake")
            (hung._generation.directory / "hang-health").write_text("1")
            sid = await hung.open_session(NEW)
            gate, events = asyncio.Event(), []
            turn = CountingTurn(gate)
            await hung.bridge.begin_turn(sid, "trace", events.append, turn, text="write")
            await turn.entered.wait()
            await until(lambda: hung._generation.failed, timeout=5)
            assert turn.writes == 1
            gate.set()
            assert (await hung.bridge.wait_settled(sid, "trace")).outcome == "lost"
            assert turn.finishes == 1 and len([e for e in events if e["type"] == "result"]) == 1
            await asyncio.shield(hung._generation.failure)
            state = owner._kinds["fake"]
            assert len(state.failures) == 1 and not hung.valid and not sibling._generation.failed
            assert processes[0].returncode is not None and not proc_tree.tree_of(processes[0])
            child = json.loads((hung._generation.directory / "observed.json").read_text())["child"]
            sibling_child = json.loads((sibling._generation.directory / "observed.json").read_text())["child"]
            assert child not in proc_tree._snapshot() and sibling_child in proc_tree._snapshot()
            assert processes[1].returncode is None and sibling.valid
            assert _health_count(sibling._generation.directory) >= 1
            failed = [r.getMessage() for r in caplog.records if "runner_health_failed" in r.getMessage()]
            assert len(failed) == 1 and "reason=timeout" in failed[0] and "still-busy" not in failed[0]
            await asyncio.sleep(0.1)
            assert len(processes) == 2 and (state.restart is None or not state.restart.done())
            await asyncio.shield(state.restart)
            replacement = await owner.acquire("fake")
            assert len(processes) == 3 and replacement.runner_id != hung.runner_id
            assert processes[-1].pid != processes[0].pid
            await replacement.load_session({**NEW, "session_id": sid})
            methods = [row["method"] for row in _rows(replacement._generation.directory)]
            assert "session/load" in methods and "session/prompt" not in methods and turn.writes == 1
    run(exercise())


@pytest.mark.parametrize(("mode", "reason"), [
    ("health_error", "rpc_error"),
    ("health_invalid", "protocol_error"),
    ("health_eof", "protocol_error"),
])
def test_health_rpc_failure_is_bounded_and_recovered(runners, monkeypatch, caplog, mode, reason):
    monkeypatch.setattr(s, "HEALTH_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(s, "HEALTH_TIMEOUT_SECONDS", 1.5)

    async def exercise():
        async with runners(**{mode: True}) as (owner, processes):
            lease = await owner.acquire("fake")
            await until(lambda: lease._generation.failed, timeout=5)
            await asyncio.shield(lease._generation.failure)
            state = owner._kinds["fake"]
            assert len(state.failures) == 1 and processes[0].returncode is not None
            failed = [r.getMessage() for r in caplog.records if "runner_health_failed" in r.getMessage()]
            assert len(failed) == 1 and f"reason={reason}" in failed[0]
            assert "secret-health-body-sentinel" not in caplog.text
            assert "secret-health-body-sentinel" not in (state.last_failure or "")
            await asyncio.shield(state.restart)
            replacement = await owner.acquire("fake")
            assert replacement.valid and replacement.runner_id != lease.runner_id
    run(exercise())


def test_health_eof_after_host_pauses_records_reason_before_recovery(runners, monkeypatch, caplog):
    monkeypatch.setattr(s, "HEALTH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(s, "HEALTH_TIMEOUT_SECONDS", 0.2)
    original = RunnerBridge._write
    pauses = []

    async def stalled_write(self, message, entry=None, *, before_write=None):
        await original(self, message, entry, before_write=before_write)
        if getattr(message, "method", None) == "runner/health":
            pauses.append(1)
            time.sleep(0.35)

    monkeypatch.setattr(RunnerBridge, "_write", stalled_write)

    async def exercise():
        async with runners(health_eof=True) as (owner, processes):
            lease = await owner.acquire("fake")
            generation = lease._generation
            await until(lambda: generation.failed, timeout=5)
            await asyncio.shield(generation.failure)
            assert pauses and not lease.valid and processes[0].returncode is not None
            assert len(owner._kinds["fake"].failures) == 1
            failed = [r for r in caplog.records if "runner_health_failed" in r.getMessage()]
            assert len(failed) == 1
            assert failed[0].levelname == "WARNING"
            assert "reason=protocol_error" in failed[0].getMessage()
            assert f"runner_id={lease.runner_id}" in failed[0].getMessage()
    run(exercise())


@pytest.mark.parametrize("how", ["stop", "idle"])
def test_dispose_joins_inflight_health_without_false_loss_or_restart(runners, monkeypatch, caplog, how):
    monkeypatch.setattr(s, "HEALTH_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(s, "HEALTH_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(s, "IDLE_STOP_SECONDS", 0.15)

    async def exercise():
        async with runners(session_cap=1, auto_settle=True) as (owner, processes):
            first = await owner.acquire("fake")
            sid = await first.open_session(NEW)
            events, turn = [], Turn()
            await first.bridge.begin_turn(sid, "trace", events.append, turn, text="done")
            summary = await first.bridge.wait_settled(sid, "trace")
            assert summary.outcome == "completed" and turn.finishes == 1
            directory = first._generation.directory
            (directory / "health-hold").write_text("1")
            await until(lambda: _health_count(directory) > _answered(directory), timeout=3)
            second = await owner.acquire("fake")
            failures = len(owner._kinds["fake"].failures)
            if how == "stop":
                await owner.stop()
                assert all(process.returncode is not None for process in processes) and not owner._tasks
            else:
                await first.release()
                await until(lambda: processes[0].returncode is not None, timeout=3)
                assert processes[1].returncode is None and second.valid
            assert len(owner._kinds["fake"].failures) == failures
            assert summary.outcome == "completed" and turn.finishes == 1
            assert len([e for e in events if e["type"] == "result"]) == 1
            assert events[-1]["runner"]["outcome"] == "completed"
            assert not any("runner_health_failed" in r.getMessage() for r in caplog.records)
            assert first._generation.health.done()
    run(exercise())


def test_health_timeout_and_process_exit_count_one_failure(runners, monkeypatch):
    monkeypatch.setattr(s, "HEALTH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(s, "HEALTH_TIMEOUT_SECONDS", 5)

    async def exercise():
        entered, go, ends = asyncio.Event(), asyncio.Event(), []
        async with runners() as (owner, processes):
            connect = owner.connect

            async def wrapped(process, kind):
                bridge = await connect(process, kind)

                async def held(*, timeout=None):
                    entered.set()
                    await go.wait()
                    raise TimeoutError()

                bridge.health = held
                return bridge

            owner.connect = wrapped
            lease = await owner.acquire("fake")
            await entered.wait()
            generation = lease._generation
            original = generation.tree.end

            async def end():
                ends.append(1)
                await original()

            generation.tree.end = end
            sid = await lease.open_session(NEW)
            turn = Turn()
            await lease.bridge.begin_turn(sid, "trace", lambda event: None, turn, text="accepted")
            go.set()
            processes[0].kill()
            assert (await lease.bridge.wait_settled(sid, "trace")).outcome == "lost"
            await asyncio.shield(generation.failure)
            assert len(owner._kinds["fake"].failures) == 1 and turn.finishes == 1 and ends == [1]
            assert owner._kinds["fake"].restart is not None
    run(exercise())


def test_a_host_stall_ending_while_the_sampler_sleeps_is_counted(monkeypatch):
    """The probe cancels its lag sampler right after the stall; the pending tick must already be counted."""
    monkeypatch.setattr(s, "HEALTH_TIMEOUT_SECONDS", 0.2)

    class Bridge:
        async def health(self, *, timeout):
            await asyncio.sleep(0.01)  # The sampler is now parked in its tick sleep.
            time.sleep(0.3)  # The host stalls, then the deadline passes without a yield.
            raise TimeoutError

    async def exercise():
        return await s.RunnerSupervisor._one_health(None, Bridge())

    reason, lag = asyncio.run(exercise())
    assert reason == "timeout"
    # Counted once: the 0.3 s stall less the 0.05 s tick, never twice.
    assert s._host_lag_limit(0.2) < lag < 0.4


def test_host_lag_settles_the_pending_tick_once():
    from looper.agents.runtimes.runner.bridge import HostLag

    async def exercise():
        lag = HostLag(0.05)
        task = asyncio.create_task(lag.run())
        await asyncio.sleep(0.01)
        time.sleep(0.25)
        settled = lag.settle()
        assert settled >= 0.15
        await asyncio.sleep(0.01)  # One loop turn: the already-due tick callback runs, a new tick cannot.
        assert lag.total < settled + 0.05
        task.cancel()
    run(exercise())


def test_host_stall_does_not_reclaim_a_runner_that_answered(runners, monkeypatch, caplog):
    """A child that answers inside the deadline stays up when the host then pauses."""
    monkeypatch.setattr(s, "HEALTH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(s, "HEALTH_TIMEOUT_SECONDS", 0.2)
    original = RunnerBridge._write
    stalled = {"done": False}

    async def stalled_write(self, message, entry=None, *, before_write=None):
        await original(self, message, entry, before_write=before_write)
        # Freeze after the frame is flushed. The peer answers during the pause;
        # the loop cannot read that reply until the pause ends, past the deadline.
        if not stalled["done"] and getattr(message, "method", None) == "runner/health":
            stalled["done"] = True
            time.sleep(0.35)

    monkeypatch.setattr(RunnerBridge, "_write", stalled_write)

    async def exercise():
        async with runners(tool=True, settle_file=True) as (owner, processes):
            lease = await owner.acquire("fake")
            sid = await lease.open_session(NEW)
            gate, events = asyncio.Event(), []
            turn = CountingTurn(gate)
            await lease.bridge.begin_turn(sid, "trace", events.append, turn, text="write")
            await turn.entered.wait()
            directory = lease._generation.directory
            try:
                await until(lambda: stalled["done"] and _answered(directory) >= 1, timeout=3)
                # The probe task must stay alive after the pause, not exit on the deadline's cancel.
                await until(lambda: _health_count(directory) >= 2 and not lease._generation.failed, timeout=3)
                assert lease.valid and not lease._generation.failed and lease.session_id == sid
                assert processes[0].returncode is None and turn.writes == 1
                assert not any("runner_health_failed" in record.getMessage() for record in caplog.records)
                (directory / "settle-now").write_text("1")
                gate.set()
                assert (await lease.bridge.wait_settled(sid, "trace")).outcome == "completed"
            finally:
                # Shutdown waits for this tool call. Open the gate if the probe failed.
                if not gate.is_set():
                    gate.set()
    run(exercise())


def test_inbound_health_reply_blocks_reclaim(runners, monkeypatch, caplog):
    monkeypatch.setattr(s, "HEALTH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(s, "HEALTH_TIMEOUT_SECONDS", 0.2)

    async def exercise():
        async with runners() as (owner, processes):
            lease = await owner.acquire("fake")
            bridge = lease.bridge

            async def buffered(*, timeout=None):
                bridge._abandoned_controls["l-buffer"] = ("runner/health", {})
                bridge._reader.reader.feed_data(
                    b'{"jsonrpc":"2.0","id":"l-buffer","result":{"sessions":0')
                raise TimeoutError()

            bridge.health = buffered
            await until(lambda: "reply_buffered" in caplog.text, timeout=3)
            await asyncio.sleep(0.3)
            assert lease.valid and not lease._generation.failed
            assert processes[0].returncode is None
            assert not any("runner_health_failed" in record.getMessage() for record in caplog.records)
    run(exercise())


def test_local_cancel_capacity_skips_health_without_reclaim(runners, monkeypatch, caplog):
    assert MAX_ABANDONED_CONTROLS == 128
    monkeypatch.setattr(s, "HEALTH_INTERVAL_SECONDS", 10)

    class Clock:
        def __init__(self):
            self.now = 0.0
            self.waiters = []

        def monotonic(self):
            return self.now

        def advance(self, dt):
            self.now += dt
            due = [fut for deadline, fut in self.waiters if deadline <= self.now + 1e-9]
            self.waiters = [(deadline, fut) for deadline, fut in self.waiters if deadline > self.now + 1e-9]
            for fut in due:
                if not fut.done():
                    fut.set_result(None)

    clock = Clock()
    monkeypatch.setattr(s, "time", clock)

    async def fake_sleep(seconds):
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        clock.waiters.append((clock.now + float(seconds), fut))
        await fut

    monkeypatch.setattr(s, "_sleep", fake_sleep)

    async def exercise():
        async with runners(tool=True) as (owner, processes):
            lease = await owner.acquire("fake")
            await until(lambda: len(clock.waiters) >= 1, timeout=3)
            sid = await lease.open_session(NEW)
            gate = asyncio.Event()
            gate.set()
            turn = CountingTurn(gate)
            await lease.bridge.begin_turn(sid, "trace", lambda event: None, turn, text="write")
            await turn.entered.wait()
            directory = lease._generation.directory
            (directory / "hold-cancel").write_text("1")
            tasks = [asyncio.create_task(lease.bridge.cancel_turn(sid, "trace"))
                     for _ in range(MAX_ABANDONED_CONTROLS)]
            await until(lambda: sum(item.method == "session/cancel"
                                    for item in lease.bridge._pending.values()) >= 128, timeout=3)
            sent = _health_count(directory)
            clock.advance(10)
            await until(lambda: "reason=local_capacity" in caplog.text, timeout=3)
            assert _health_count(directory) == sent
            assert lease.valid and not lease._generation.failed and processes[0].returncode is None
            assert not any("runner_health_failed" in record.getMessage() for record in caplog.records)
            (directory / "hold-cancel").unlink()
            # Join all replies under the bridge's control deadline before the next tick.
            await asyncio.gather(*tasks)
            clock.advance(10)
            await until(lambda: _health_count(directory) > sent, timeout=3)
            assert lease.valid and not lease._generation.failed
    run(exercise())


def test_health_reclaim_is_named_for_operators():
    text = Path("docs/architecture/rfcs/agent-sdk-runner-v0.zh-CN.md").read_text()
    health = text.split("### 5.4 生命周期", 1)[1].split("### 5.5", 1)[0]
    assert "runner_health_failed kind=<kind> runner_id=<id> reason=<timeout|rpc_error|protocol_error>" in health
    assert "runner protocol-error: health" in health
    assert "runner unavailable: five failures within ten minutes" in health
    assert '[runtimes] pi = "cli"' in health and '[runtimes] dsh = "cli"' in health
    assert "回滚到上一版本" in health
    assert "reason=local_capacity" in health


def test_no_health_monitor_without_published_generation(runners):
    import dataclasses
    import hashlib

    from looper.agents.claude_cli import (
        CONSULT_TOOLS, READ_ONLY_ALLOWED, READ_ONLY_MODE, WRITE_TOOLS, permission_args)
    from looper.agents.run_usage import Usage
    from looper.agents.runtimes import ApiProtocol
    from looper.agents.runtimes.api.adapter import RUNTIME_NOTE, Descriptor

    frozen = json.loads((Path(__file__).parent / "fixtures" / "runner_health_off.json").read_text())
    argv = Descriptor(ApiProtocol.OPENAI_RESPONSES, "fake-model", "https://example.invalid/v1",
                      "low", 100000, "prompt.md").to_argv()
    assert argv == frozen["api_argv"]
    assert hashlib.sha256(RUNTIME_NOTE.encode()).hexdigest() == frozen["api_runtime_note_sha256"]
    assert permission_args() == frozen["cli_permission_args"]
    assert list(READ_ONLY_MODE) == frozen["cli_read_only_mode"]
    assert list(WRITE_TOOLS) == frozen["cli_write_tools"]
    assert list(CONSULT_TOOLS) == frozen["cli_consult_tools"]
    assert list(READ_ONLY_ALLOWED) == frozen["cli_read_only_allowed"]
    assert [item.name for item in dataclasses.fields(Usage)] == frozen["usage_fields"]
    assert s.HEALTH_INTERVAL_SECONDS == 10 and s.HEALTH_TIMEOUT_SECONDS == 30

    def live_health(owner):
        return [task for task in owner._tasks if getattr(task.get_coro(), "__name__", "") == "_health"]

    async def exercise():
        async with runners() as (owner, processes):
            await owner.stop()
            with pytest.raises(s.RunnerUnavailable):
                await owner.acquire("fake")
            assert not processes and not live_health(owner)
            assert not list(owner.root.glob("*/requests.jsonl"))
        async with runners(version="9.9") as (owner, processes):
            with pytest.raises(s.RunnerUnavailable):
                await owner.acquire("fake")
            assert not owner._kinds["fake"].members and not live_health(owner)
            logged = list(owner.root.glob("*/requests.jsonl"))
            assert logged and all("runner/health" not in path.read_text() for path in logged)
        async with runners() as (owner, processes):
            before = len(processes)
            await owner.stop()
            with pytest.raises(s.RunnerUnavailable):
                await owner.acquire("fake")
            assert len(processes) == before and not live_health(owner) and not owner._tasks
    run(exercise())
