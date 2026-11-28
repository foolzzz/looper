"""No stop path may hang on a cancel its task lost, or cut a cleanup that is already running.

On Python 3.11 ``asyncio.wait_for`` returns the inner result and drops the cancel when the cancel arrives as the
inner call completes. A stop that cancels once and then waits without a bound then waits for as long as the task
runs on. Here every ``asyncio.wait_for`` drops the first cancel of its task that way, and every stop below must
still return, with its tasks ended.

A second ``cancel`` while cleanup is awaiting cuts that await. A task that is already cancelling is therefore
waited out for one interval before another cancel, and a cleanup longer than a service interval still finishes
on a process-class stop. The recording-proxy, channel-health and advisory checks are rows too: they call the
helpers ``serve`` uses.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import sys
import tempfile
from types import SimpleNamespace

import pytest

from looper.agents import (admission, admission_notices, attention, completion_events, completion_service, consult,
                           control_plane, failure_handling, gate_courier, gate_service, http_server, idle_checks,
                           intent_push, intent_recognition, intent_rule_engine, ledger_reconciler, loop_watch,
                           managers, memory_push, metrics_alerts, mid_turn_input, proc_tree, scheduler, serve,
                           source_service, supervisor, wait_service)
from looper.agents.channels import lark_stream, outbound, registry, status_line, telegram, web
from looper.agents.ops import subagents as ops_subagents
from looper.agents.runtimes.runner import supervisor as runner_supervisor
from looper.agents.tiers import manager_routing, task_manager, worker
from looper.agents.waits import CommandCheck

BOUND = 3.0
# Cap only a service-class wait. A process-class wait is RECANCEL_SEC; capping that too would let a stop that
# still re-cancels at 1s (and cuts a command inside its SIGTERM grace) pass.
AGAIN_CAP = 1.0
SHORT_SEC = 0.2
SLOW_SEC = 2.5
PROCESS_PATHS = frozenset({
    "consults",
    "wait service",
    "scheduler background job",
    "Manager fleet idle check",
    "worker run",
    "worker kill",
    "task Manager pool",
    "runner start",
})

_wait_for = asyncio.wait_for
_wait = asyncio.wait


@pytest.fixture(autouse=True)
def lost_cancel(monkeypatch):
    """Each task loses the first cancel that reaches it inside ``asyncio.wait_for``, as 3.11 loses one."""
    lost: set[int] = set()

    async def wait_for(awaitable, timeout):
        task = asyncio.current_task()
        try:
            return await _wait_for(awaitable, timeout)
        except asyncio.CancelledError:
            if task is None or id(task) in lost:
                raise
            lost.add(id(task))
            return None

    async def capped_wait(tasks, *, timeout=None, **kwargs):
        if timeout is not None and timeout + 1e-9 < supervisor.RECANCEL_SEC:
            timeout = min(timeout, AGAIN_CAP)
        return await _wait(tasks, timeout=timeout, **kwargs)

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    monkeypatch.setattr(supervisor, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait": capped_wait}))


async def _work(*_args, **_kwargs):
    """Waits in ``wait_for`` (where the first cancel is lost), then waits for what never comes."""
    await asyncio.wait_for(asyncio.Event().wait(), 3600)
    await asyncio.Event().wait()


class Tasks:
    def __init__(self, patch: pytest.MonkeyPatch, body=None) -> None:
        self.patch = patch
        self.body = _work if body is None else body
        self.started: list[asyncio.Task] = []

    def __call__(self, work=None) -> asyncio.Task:
        task = asyncio.create_task(self.body() if work is None else work)
        self.started.append(task)
        return task


def _cleanup_body(delay: float, counts: dict[str, int]):
    """Wait until cancelled, then spend ``delay`` in cleanup. A second cancel leaves ``finished`` short of ``entered``."""

    async def work():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            counts["entered"] += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            counts["finished"] += 1
            raise

    return work


def _bound(path: str) -> float:
    """How long a lost cancel may take. A process-class row waits out ``RECANCEL_SEC`` before the second cancel."""
    if path not in PROCESS_PATHS:
        return BOUND
    extra = supervisor.SERVICE_STOP_AGAIN_SEC if path == "task Manager pool" else 0.0
    return supervisor.RECANCEL_SEC + 2 + extra


async def _nothing(*_args, **_kwargs):
    return None


def _ns(**fields) -> SimpleNamespace:
    return SimpleNamespace(**fields)


def _stop(cls, method: str = "stop", **fields):
    """``cls.method`` run on an object that has only the fields the method reads."""
    return getattr(cls, method)(_ns(**fields))


def _completion(task, run_once):
    previous = completion_events._poke
    service = completion_service.CompletionService(bus=None, manager_running=lambda _: True, running_managers=list)
    service.run_once = run_once
    service._task = task(service._loop())

    async def stop():
        try:
            await service.stop()
        finally:
            completion_events.set_poke(previous)

    return stop()


def completion_waiting_for_a_poke(task):
    """The real loop, parked in its own ``wait_for``."""
    return _completion(task, _nothing if task.body is _work else task.body)


def completion_in_a_pass(task):
    """The real loop, with the cancel lost inside a pass."""
    return _completion(task, _work if task.body is _work else task.body)


def outbox(task):
    return _stop(outbound.Outbox, _recovery_task=task(), _task=task(), _workers={task()}, _waiters={})


def lark_stream_connection(task):
    """The real connection: its ping and receive loops wait in ``wait_for``; closing the events ends it.

    An instrumented body is one child of the same cleanup the card answers use, without opening a socket.
    """
    if task.body is not _work:
        return lark_stream._cancel_remaining([task()])

    async def endpoint():
        return "wss://example.invalid/ws?service_id=1", {}

    class Socket:
        async def recv(self):
            await asyncio.Event().wait()

        async def send(self, _data):
            return None

    class Connect:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *_exc):
            return False

    task.patch.setattr(lark_stream, "connect", Connect)

    async def stop():
        reading = task(lark_stream.stream_events(_ns(stream_endpoint=endpoint)).__anext__())
        await asyncio.sleep(0.05)  # connected: ping and receive are waiting
        reading.cancel()
        await _wait({reading})

    return stop()


def runner_start(task):
    return _stop(runner_supervisor.RunnerSupervisor, "_stop", _starts={task()}, _kinds={}, _tasks=set())


def task_manager_pool(task):
    lock = asyncio.Lock()
    return _stop(task_manager.TaskManagerPool, "shutdown", _lock=lock, closing=False, _pending=[],
                 agents={"t": _ns(running=task(), resident=_ns(stop=_nothing))}, _close_tasks={},
                 _available=asyncio.Condition(lock), _ticker=task())


def telegram_outbound(task):
    adapter = telegram.TelegramChannelAdapter.__new__(telegram.TelegramChannelAdapter)
    adapter.__dict__.update(_inbox=None, _app=None, _stop_boundary=None, _reactions=_ns(stop=_nothing),
                            _channel_outbox=None, _gateway=None, _requests=(), _channel_id="channel-telegram",
                            _outbound_task=task())
    return adapter.stop()


def http_summary(task):
    previous, previous_catchup = attention._status_checks, attention._catchup_checks
    summary = task()

    async def stop():
        try:
            await _stop(http_server.AgentHTTPServer, _summary_task=summary, _server=None, _handlers={},
                        _checks_now=None, _address_publication=None, _config_imports=None)
        finally:
            attention._status_checks, attention._catchup_checks = previous, previous_catchup

    return stop()


def _worker(**fields):
    pool = {"_expiry_task": None, "_candidate_locks": {}, "_listener_task": None, "_input_deliveries": set(),
            "_tasks": {}, "_active": {}, **fields}
    return worker.WorkerPool.stop(_ns(**pool))


PATHS = {
    "completion service, waiting for a poke": completion_waiting_for_a_poke,
    "completion service, in a pass": completion_in_a_pass,
    "consults": lambda t: _stop(consult.ConsultService, _running={t()}),
    "gate reminders": lambda t: _stop(gate_service.GateReminders, _task=t()),
    "gate courier": lambda t: _stop(gate_courier.GateCourier, _task=t()),
    "wait service": lambda t: _stop(wait_service.WaitService, _task=t()),
    "source loop": lambda t: _stop(source_service.SourceService, _task=t(), _collect_tasks={}, _alert_task=None,
                                   _alerts=None),
    "source collection": lambda t: _stop(source_service.SourceService, _task=None, _collect_tasks={"s": t()},
                                         _alert_task=None, _alerts=None),
    "source alerts": lambda t: _stop(source_service.SourceService, _task=None, _collect_tasks={}, _alert_task=t(),
                                     _alerts=None),
    "intent recognition": lambda t: _stop(intent_recognition.IntentRecognitions, _stopping=False, _tasks={t()},
                                          _running={}),
    "intent rules": lambda t: _stop(intent_rule_engine.IntentRuleService, _stop=asyncio.Event(), _task=t()),
    "intent push": lambda t: _stop(intent_push.IntentPushService, _task=t()),
    "memory push": lambda t: _stop(memory_push.MemoryPushService, _task=t()),
    "idle checks": lambda t: _stop(idle_checks.IdleCheckService, _task=t(), _stopping=asyncio.Event()),
    "metrics alerts": lambda t: _stop(metrics_alerts.MetricsAlertService, _task=t(), _stopping=asyncio.Event()),
    "ledger reconciler": lambda t: _stop(ledger_reconciler.LedgerReconciler, _task=t()),
    "scheduler loop": lambda t: _stop(scheduler.Scheduler, _loop_task=t(), _background=set()),
    "scheduler background job": lambda t: _stop(scheduler.Scheduler, _loop_task=None, _background={t()}),
    "HTTP summary": http_summary,
    "Manager fleet idle check": lambda t: _stop(managers.ManagerFleet, _idle_task=t(), _retries={}),
    "Manager fleet retry": lambda t: _stop(managers.ManagerFleet, _idle_task=None, _retries={"m": t()}),
    "supervisor health": lambda t: _stop(supervisor.AgentSupervisor, "stop_all", _health_task=t(), _agents={},
                                         _bus=_ns(shutdown=_nothing)),
    "supervisor restart": lambda t: _stop(supervisor.AgentSupervisor, "stop_all", _health_task=None,
                                          _agents={"a": _ns(restarting=t(), agent=_ns(stop=_nothing))},
                                          _bus=_ns(shutdown=_nothing)),
    "supervisor stops one agent": lambda t: supervisor.AgentSupervisor.stop_agent(
        _ns(_agents={"a": _ns(restarting=t(), agent=_ns(stop=_nothing))}), "a"),
    "worker candidate expiry": lambda t: _worker(_expiry_task=t()),
    "worker listener": lambda t: _worker(_listener_task=t()),
    "worker input delivery": lambda t: _worker(_input_deliveries={t()}),
    "worker run": lambda t: _worker(_tasks={"w": t()}),
    "worker kill": lambda t: worker.WorkerPool._kill_run(_ns(_candidates={}, _tasks={"w": t()}, _inputs={}), "w"),
    "task Manager pool": task_manager_pool,
    "runner start": runner_start,
    "explorer scanner": lambda t: ops_subagents.WorkerSubagents.stop(
        _ns(_scanner=t(), _jobs={}, service=_ns(detach=_nothing)), epoch=1),
    "admission": lambda t: _stop(admission.Controller, lock=asyncio.Lock(), probes={}, background_tasks={t()}),
    "failure timers": lambda t: _stop(failure_handling.FailureHandlingService, "close", _wake_waits=lambda: None,
                                      _timers={"f": t()}),
    "channel start retries": lambda t: _stop(registry.ChannelRegistry, "stop_all", _retries={"c": t()}, _channels={}),
    "channel start cancelled": lambda t: registry.ChannelRegistry.cancel_start(
        _ns(_retries={"c": t()}, _starts={}, unregister=_nothing), "c"),
    "Telegram outbound": telegram_outbound,
    "web outbound": lambda t: _stop(web.WebChannelAdapter, _outbound_task=t()),
    "channel outbox": outbox,
    "outbound gateway edit": lambda t: _stop(outbound.Gateway, _edits={"m": _ns(task=t())}),
    "status line calls": lambda t: _stop(status_line.Reactions, _pending={t()}),
    "Lark stream connection": lark_stream_connection,
    "process tree ticker": lambda t: _stop(proc_tree.TurnTree, "stop_ticker", _ticker=t()),
    "control plane health": lambda t: _stop(control_plane.ControlPlaneLease, "release", state=None, _health_task=t(),
                                            _conn=_ns(close=_nothing, terminate=lambda: None)),
    "event-loop watch": lambda t: _stop(loop_watch.LoopWatch, _stop=_ns(set=lambda: None), _task=t(), _thread=None),
    "mid-turn shutdown": lambda t: _stop(mid_turn_input.MidTurnInputs, "shutdown", _tasks={t()}, _admissions={}),
    "session router workers": lambda t: _stop(manager_routing.SessionRouter, _deadlines=[], _workers={"s": t()},
                                              _expiring=[], _reservations={}, _completion_ids={}),
    "serve recording-proxy check": lambda t: serve.stop_recording_and_channel_checks(t()),
    "serve channel-health check": lambda t: serve.stop_recording_and_channel_checks(t()),
    "serve advisory checks": lambda t: serve.stop_advisory_checks(t()),
    "source collect handshake": lambda t: source_service.cancel_collect_task(t()),
    "Lark card answer": lambda t: lark_stream._cancel_remaining([t()]),
}


@pytest.mark.parametrize("path", list(PATHS))
def test_stop_returns_although_its_task_lost_the_cancel(path, monkeypatch, caplog):
    caplog.set_level(logging.CRITICAL)

    async def run() -> None:
        before = asyncio.all_tasks()
        tasks = Tasks(monkeypatch)
        stop = PATHS[path](tasks)
        await asyncio.sleep(0.05)  # every planted task is inside its wait
        stopping = asyncio.ensure_future(stop)

        def running() -> set[asyncio.Task]:
            return asyncio.all_tasks() - before - {stopping}

        limit = _bound(path)
        try:
            done, _ = await _wait({stopping}, timeout=limit)
            assert done, f"{path}: the stop did not return within {limit:g} s"
            stopping.result()
            await asyncio.sleep(0)
            assert not running(), f"{path}: the stop returned with tasks still running: {running()}"
        finally:
            left = {task for task in {stopping, *running()} if not task.done()}
            while left:
                for task in left:
                    task.cancel()
                await _wait(left, timeout=0.1)
                left = {task for task in left if not task.done()}

    asyncio.run(run())


async def _await_stop(path: str, stop, *, limit: float, before: set[asyncio.Task]) -> float:
    stopping = asyncio.ensure_future(stop)
    began = asyncio.get_running_loop().time()

    def running() -> set[asyncio.Task]:
        return asyncio.all_tasks() - before - {stopping}

    try:
        done, _ = await _wait({stopping}, timeout=limit)
        elapsed = asyncio.get_running_loop().time() - began
        assert done, f"{path}: the stop did not return within {limit:g} s"
        stopping.result()
        await asyncio.sleep(0)
        assert not running(), f"{path}: the stop returned with tasks still running: {running()}"
        return elapsed
    finally:
        left = {task for task in {stopping, *running()} if not task.done()}
        while left:
            for task in left:
                task.cancel()
            await _wait(left, timeout=0.1)
            left = {task for task in left if not task.done()}


@pytest.mark.parametrize("path", list(PATHS))
def test_a_stop_waits_out_cleanup_that_was_already_cancelling(path, monkeypatch, caplog):
    """A task already in cleanup keeps that cleanup: the stop waits, and cancels again only after the interval."""
    caplog.set_level(logging.CRITICAL)
    counts = {"entered": 0, "finished": 0}

    async def run() -> None:
        before = asyncio.all_tasks()
        tasks = Tasks(monkeypatch, body=_cleanup_body(SHORT_SEC, counts))
        stop = PATHS[path](tasks)
        await asyncio.sleep(0.05)
        for task in tasks.started:
            task.cancel()
        await asyncio.sleep(0)
        assert counts["entered"] == len(tasks.started) > 0, path
        assert counts["finished"] == 0, path
        await _await_stop(path, stop, limit=SHORT_SEC + 2, before=before)
        assert counts["finished"] == counts["entered"], path

    asyncio.run(run())


@pytest.mark.parametrize("path", list(PATHS))
def test_cleanup_slower_than_a_service_interval_follows_the_path_class(path, monkeypatch, caplog):
    """Process-class cleanup of 2.5s finishes. Service-class cleanup is cut by the 1s re-cancel."""
    caplog.set_level(logging.CRITICAL)
    counts = {"entered": 0, "finished": 0}

    async def run() -> None:
        before = asyncio.all_tasks()
        tasks = Tasks(monkeypatch, body=_cleanup_body(SLOW_SEC, counts))
        stop = PATHS[path](tasks)
        await asyncio.sleep(0.05)
        elapsed = await _await_stop(path, stop, limit=_bound(path), before=before)
        assert counts["entered"] >= 1, path
        if path in PROCESS_PATHS:
            assert counts["finished"] >= 1, path
            assert elapsed >= SLOW_SEC - 0.15, path
            if path == "task Manager pool":
                # The ticker stays on the service interval, so its cleanup is cut while the run's finishes.
                assert counts["finished"] < counts["entered"], path
            else:
                assert counts["finished"] == counts["entered"], path
        else:
            assert counts["finished"] == 0, path
            assert 0.5 <= elapsed < SLOW_SEC, path

    asyncio.run(run())


def test_task_manager_shutdown_lets_a_turn_finish_its_cleanup(caplog):
    caplog.set_level(logging.CRITICAL)

    async def run() -> None:
        seen: dict[str, object] = {}

        async def turn() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                try:
                    await asyncio.sleep(SHORT_SEC)
                except asyncio.CancelledError:
                    seen["cancelling"] = asyncio.current_task().cancelling()
                    raise
                seen["cleaned"] = True

        async def ticker() -> None:
            await asyncio.Event().wait()

        lock = asyncio.Lock()
        pool = _ns(_lock=lock, closing=False, _pending=[], _close_tasks={}, _available=asyncio.Condition(lock),
                   _ticker=asyncio.create_task(ticker()),
                   agents={"t": _ns(running=asyncio.create_task(turn()), resident=_ns(stop=_nothing))})
        await asyncio.sleep(0.05)
        await asyncio.wait_for(task_manager.TaskManagerPool.shutdown(pool), SHORT_SEC + 2)
        assert seen.get("cleaned") is True, seen
        assert "cancelling" not in seen

    asyncio.run(run())


def test_a_runner_start_keeps_its_process_handle_when_stop_finds_it_cancelling(caplog):
    """The second shield in a start must receive the spawn. A cancel during that await drops the handle."""
    caplog.set_level(logging.CRITICAL)

    async def run() -> None:
        kept: dict[str, str] = {}
        release = asyncio.Event()
        waiting = asyncio.Event()

        async def spawn() -> str:
            waiting.set()
            await release.wait()
            return "handle"

        async def start() -> None:
            spawning = asyncio.create_task(spawn())
            try:
                try:
                    process = await asyncio.shield(spawning)
                except asyncio.CancelledError as error:
                    process = await asyncio.shield(spawning)
                    kept["process"] = process
                    raise error
                kept["process"] = process
            finally:
                if not spawning.done():
                    spawning.cancel()

        task = asyncio.create_task(start())
        await waiting.wait()
        task.cancel()
        await asyncio.sleep(0)  # the start is in the second shield, already cancelling
        stopping = asyncio.create_task(runner_supervisor.RunnerSupervisor._stop(
            _ns(_starts={task}, _kinds={}, _tasks=set())))
        await asyncio.sleep(0.05)  # a helper that cancels at once has already dropped the handle
        release.set()
        try:
            await asyncio.wait_for(stopping, 2)
        finally:
            if not task.done():
                task.cancel()
            if not stopping.done():
                stopping.cancel()
            await _wait({task, stopping}, timeout=1)
        assert kept.get("process") == "handle"
        assert task.cancelled()

    asyncio.run(run())


def test_an_admission_alert_releases_its_claim_when_stop_cancels_it(monkeypatch, caplog):
    caplog.set_level(logging.CRITICAL)

    async def run() -> None:
        released: list[dict] = []
        entered = asyncio.Event()

        async def sender(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()
            return True, None

        async def claim(_key, _value):
            return True

        async def put_settings(settings):
            await asyncio.sleep(0)
            released.append(dict(settings))

        async def dashboard_target():
            return _ns(channel_id="channel", target=_ns(id="chat", group=False))

        async def get_provider(_provider_id):
            return {"name": "account"}

        async def windows(_provider_id):
            return [{"window_name": "5h", "status": "rejected"}]

        monkeypatch.setattr(admission_notices.system_settings, "claim", claim)
        monkeypatch.setattr(admission_notices.system_settings, "put_settings", put_settings)
        monkeypatch.setattr(admission_notices.profile_registry, "get_provider", get_provider)
        monkeypatch.setattr(admission_notices.provider_limits, "windows", windows)
        monkeypatch.setattr(admission_notices, "_sender", sender)
        monkeypatch.setattr(gate_service, "dashboard_target", dashboard_target)
        controller = admission.Controller()
        controller._background(admission._freeze_alert("prov", 7, until=None, until_known=False, waiting=1))
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(controller.stop(), 3)
        assert released == [{"admission_alert_sent:7": None}]

    asyncio.run(run())


_COMMAND_CHILD = """\
import os
import signal
import sys
import time
marker, pidfile = sys.argv[1], sys.argv[2]
signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)

def handler(signum, frame):
    time.sleep(2)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("ok")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, handler)
with open(pidfile, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
time.sleep(30)
"""


def test_a_command_check_finishes_its_term_handler_when_the_service_stops(caplog):
    """A 1s re-cancel SIGKILLs the command inside its 3s SIGTERM grace. The process-class wait lets the handler finish."""
    caplog.set_level(logging.CRITICAL)

    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = os.path.join(directory, "child.py")
            marker = os.path.join(directory, "marker")
            pidfile = os.path.join(directory, "pid")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(_COMMAND_CHILD)
            command = " ".join(shlex.quote(part) for part in (sys.executable, script, marker, pidfile))
            service = _ns(_task=asyncio.create_task(
                wait_service.run_command(CommandCheck(command=command), directory, project_allows=True)))
            pid = None
            try:
                deadline = asyncio.get_running_loop().time() + 2
                while asyncio.get_running_loop().time() < deadline and not os.path.exists(pidfile):
                    await asyncio.sleep(0.02)
                assert os.path.exists(pidfile)
                pid = int(open(pidfile, encoding="utf-8").read())
                await asyncio.wait_for(wait_service.WaitService.stop(service), 8)
                assert os.path.exists(marker)
            finally:
                if pid is not None:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

    asyncio.run(run())


def test_an_idle_close_finishes_when_the_fleet_stops(monkeypatch, caplog):
    caplog.set_level(logging.CRITICAL)
    monkeypatch.setattr(managers, "IDLE_SWEEP_SECONDS", 0.05)

    async def run() -> None:
        closed: list[bool] = []
        started = asyncio.Event()

        async def try_suspend_idle(_clock) -> None:
            started.set()
            await asyncio.sleep(SLOW_SEC)
            closed.append(True)

        async def retry() -> None:
            await asyncio.Event().wait()

        fleet = _ns(agents=lambda: [_ns(agent_id="m", try_suspend_idle=try_suspend_idle)], _clock=lambda: None,
                    _retries={"m": asyncio.create_task(retry())})
        fleet._idle_task = asyncio.create_task(managers.ManagerFleet._idle_loop(fleet))
        current = asyncio.current_task()
        try:
            await asyncio.wait_for(started.wait(), 2)
            began = asyncio.get_running_loop().time()
            await asyncio.wait_for(managers.ManagerFleet.stop(fleet), supervisor.RECANCEL_SEC + 2)
            elapsed = asyncio.get_running_loop().time() - began
            assert closed == [True]
            assert SLOW_SEC - 0.15 <= elapsed < supervisor.RECANCEL_SEC
        finally:
            left = {task for task in asyncio.all_tasks() if task is not current and not task.done()}
            for task in left:
                task.cancel()
            if left:
                await _wait(left, timeout=1)

    asyncio.run(run())


def test_stop_cancels_a_collect_the_loop_starts_after_a_dropped_cancel(caplog):
    caplog.set_level(logging.CRITICAL)

    async def run() -> None:
        service = source_service.SourceService(interval=3600)
        started = asyncio.Event()

        async def run_once() -> None:
            await asyncio.wait_for(asyncio.Event().wait(), 3600)
            async def collect() -> None:
                started.set()
                await asyncio.Event().wait()
            service._collect_tasks["late"] = asyncio.create_task(collect())
            await asyncio.Event().wait()

        service.run_once = run_once  # type: ignore[method-assign]
        service._task = asyncio.create_task(service._loop())
        current = asyncio.current_task()
        try:
            await asyncio.sleep(0.05)
            stopping = asyncio.create_task(service.stop())
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(stopping, 3)
            await asyncio.sleep(0)
            left = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
            assert not left, left
        finally:
            left = {task for task in asyncio.all_tasks() if task is not current and not task.done()}
            for task in left:
                task.cancel()
            if left:
                await _wait(left, timeout=1)

    asyncio.run(run())


def test_lark_cleanup_stays_cancelled_and_keeps_the_uncancel_guard(caplog):
    """The parent stays cancelled after the children end. Dropping the uncancel guard leaves the child running."""
    caplog.set_level(logging.CRITICAL)

    async def run() -> None:
        async def child() -> None:
            await asyncio.wait_for(asyncio.Event().wait(), 3600)
            await asyncio.Event().wait()

        child_task = asyncio.create_task(child())
        parent_task = asyncio.create_task(lark_stream._cancel_remaining([child_task]))
        await asyncio.sleep(0.05)
        parent_task.cancel()
        parent_task.cancel()
        try:
            done, _ = await _wait({parent_task}, timeout=3)
            assert done
            assert child_task.done()
            assert parent_task.cancelled()
        finally:
            for task in (child_task, parent_task):
                if not task.done():
                    task.cancel()
            await _wait({child_task, parent_task}, timeout=1)

    asyncio.run(run())
