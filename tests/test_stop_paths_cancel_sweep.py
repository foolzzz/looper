"""No stop path may hang on a cancel its task lost.

On Python 3.11 ``asyncio.wait_for`` returns the inner result and drops the cancel when the cancel arrives as the
inner call completes. A stop that cancels once and then waits without a bound then waits for as long as the task
runs on. Here every ``asyncio.wait_for`` drops the first cancel of its task that way, and every stop below must
still return, with its tasks ended.

The server's own shutdown also cancels its recording-proxy, channel-health and advisory check tasks this way
(``serve``). Those are closures of ``serve``, so they are not rows here; they call the same helper.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from looper.agents import (admission, attention, completion_events, completion_service, consult, control_plane,
                           failure_handling, gate_courier, gate_service, http_server, idle_checks, intent_push,
                           intent_recognition, intent_rule_engine, ledger_reconciler, loop_watch, managers,
                           memory_push, metrics_alerts, mid_turn_input, proc_tree, scheduler, source_service,
                           supervisor, wait_service)
from looper.agents.channels import lark_stream, outbound, registry, status_line, telegram, web
from looper.agents.ops import subagents as ops_subagents
from looper.agents.runtimes.runner import supervisor as runner_supervisor
from looper.agents.tiers import manager_routing, task_manager, worker

BOUND = 3.0
# The product waits longer before a second cancel when a process is being reaped. Cap that wait here so one row
# stays inside BOUND; an unbounded wait still misses the bound.
AGAIN_CAP = 1.0

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
        return await _wait(tasks, timeout=None if timeout is None else min(timeout, AGAIN_CAP), **kwargs)

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    monkeypatch.setattr(supervisor, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait": capped_wait}))


async def _work(*_args, **_kwargs):
    """Waits in ``wait_for`` (where the first cancel is lost), then waits for what never comes."""
    await asyncio.wait_for(asyncio.Event().wait(), 3600)
    await asyncio.Event().wait()


class Tasks:
    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.patch = patch
        self.started: list[asyncio.Task] = []

    def __call__(self, work=None) -> asyncio.Task:
        task = asyncio.create_task(work if work is not None else _work())
        self.started.append(task)
        return task


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
    return _completion(task, _nothing)


def completion_in_a_pass(task):
    """The real loop, with the cancel lost inside a pass."""
    return _completion(task, _work)


def outbox(task):
    return _stop(outbound.Outbox, _recovery_task=task(), _task=task(), _workers={task()}, _waiters={})


def lark_stream_connection(task):
    """The real connection: its ping and receive loops wait in ``wait_for``; closing the events ends it."""

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

    async def stop():
        try:
            await _stop(http_server.AgentHTTPServer, _summary_task=task(), _server=None, _handlers={},
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

        try:
            done, _ = await _wait({stopping}, timeout=BOUND)
            assert done, f"{path}: the stop did not return within {BOUND:g} s"
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
