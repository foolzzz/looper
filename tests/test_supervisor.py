"""The supervisor restarts a resident agent that is no longer healthy for as long as the server runs: each restart in
a row waits twice as long as the last, from 1 s up to 5 minutes, an agent that stays up 5 minutes starts the count
again, and one restarted 3 times in a row shows as crash-looping until then. Its clock and sleep are the test's, so
nothing waits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from looper.agents.message import AgentAddress
from looper.agents.supervisor import (
    CRASH_LOOP_RESTARTS,
    STABLE_SECONDS,
    AgentState,
    AgentSupervisor,
    restart_delay,
)


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def publish(self, envelope: Any) -> None:
        self.events.append((envelope.payload["agent_id"], envelope.payload["event"]))


class _Agent:
    """An agent that is healthy until it crashes; its next ``failing_starts`` starts raise."""

    def __init__(self, agent_id: str = "worker-pool", *, failing_starts: int = 0) -> None:
        self.agent_id = agent_id
        self.agent_address = AgentAddress(agent_type="worker", agent_id=agent_id)
        self.healthy = False
        self.starts = 0
        self.stops = 0
        self.failing_starts = failing_starts

    async def start(self, bus: Any) -> None:
        self.starts += 1
        if self.failing_starts:
            self.failing_starts -= 1
            raise RuntimeError("cannot start")
        self.healthy = True

    async def stop(self) -> None:
        self.stops += 1
        self.healthy = False

    def is_healthy(self) -> bool:
        return self.healthy


class _Time:
    """The supervisor's clock and sleep: sleeping moves the clock, at once, and is recorded."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        self.slept: list[float] = []

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def _supervisor(agent: _Agent, time: _Time) -> tuple[AgentSupervisor, _Bus]:
    bus = _Bus()
    supervisor = AgentSupervisor(bus, Path("/nonexistent"), clock=time.clock, sleep=time.sleep)  # type: ignore[arg-type]
    supervisor.register(agent)
    return supervisor, bus


async def _settle(supervisor: AgentSupervisor) -> None:
    """Wait for every restart going on to end."""
    for entry in supervisor._agents.values():
        if entry.restarting is not None:
            await entry.restarting


async def _crash(supervisor: AgentSupervisor, agent: _Agent) -> None:
    agent.healthy = False
    await supervisor.check_health()
    await _settle(supervisor)


def test_the_delay_doubles_from_a_second_up_to_five_minutes() -> None:
    assert [restart_delay(n) for n in range(11)] == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300]


def test_an_agent_that_keeps_crashing_keeps_being_restarted_with_growing_delays() -> None:
    async def run() -> None:
        time, agent = _Time(), _Agent()
        supervisor, bus = _supervisor(agent, time)
        await supervisor.start_all()
        try:
            states = []
            for _ in range(12):
                await _crash(supervisor, agent)
                states.append(supervisor.agent_status()["worker-pool"]["state"])
            assert time.slept == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300]  # never given up on
            assert agent.starts == 13 and agent.healthy
            assert states[:2] == ["running", "running"] and set(states[2:]) == {"crash_looping"}
            events = [event for _, event in bus.events]
            assert events.count("crashed") == 12 and events.count("started") == 13
            assert events.count("crash_looping") == 1  # once, as it starts looping
            assert events.index("crash_looping") > [i for i, e in enumerate(events) if e == "crashed"][2]
        finally:
            await supervisor.stop_all()

    asyncio.run(run())


def test_staying_up_long_enough_makes_the_next_crash_wait_the_least_again() -> None:
    async def run() -> None:
        time, agent = _Time(), _Agent()
        supervisor, bus = _supervisor(agent, time)
        await supervisor.start_all()
        try:
            for _ in range(CRASH_LOOP_RESTARTS):
                await _crash(supervisor, agent)
            status = supervisor.agent_status()["worker-pool"]
            assert (status["state"], status["up"], status["healthy"], status["restart_count"]) == (
                "crash_looping", True, False, 3)
            assert status["last_crash_at"] is not None and status["next_restart_at"] is None

            time.now += timedelta(seconds=STABLE_SECONDS - 1)
            await supervisor.check_health()  # not yet stable
            assert supervisor.agent_status()["worker-pool"]["state"] == "crash_looping"
            time.now += timedelta(seconds=1)
            await supervisor.check_health()
            status = supervisor.agent_status()["worker-pool"]
            assert (status["state"], status["healthy"], status["restart_count"]) == ("running", True, 0)
            assert bus.events[-1] == ("worker-pool", "recovered")

            time.slept.clear()
            await _crash(supervisor, agent)
            assert time.slept == [1]
        finally:
            await supervisor.stop_all()

    asyncio.run(run())


def test_an_agent_that_cannot_start_is_tried_again_until_it_starts() -> None:
    async def run() -> None:
        time, agent = _Time(), _Agent(failing_starts=4)
        supervisor, bus = _supervisor(agent, time)
        await supervisor.start_all()  # its first start fails: it is restarted at once, not at the next check
        await _settle(supervisor)
        try:
            assert time.slept == [1, 2, 4, 8] and agent.starts == 5 and agent.healthy
            assert [e for _, e in bus.events].count("start_failed") == 4
            assert supervisor._agents["worker-pool"].state is AgentState.CRASH_LOOPING
        finally:
            await supervisor.stop_all()

    asyncio.run(run())


def test_a_component_waiting_to_restart_shows_when_and_stopping_the_server_ends_the_wait() -> None:
    async def run() -> None:
        time, agent = _Time(), _Agent()
        bus = _Bus()
        never = asyncio.Event()

        async def sleep(seconds: float) -> None:
            time.slept.append(seconds)
            await never.wait()

        supervisor = AgentSupervisor(bus, Path("/nonexistent"), clock=time.clock, sleep=sleep)  # type: ignore[arg-type]
        supervisor.register(agent)
        await supervisor.start_all()
        agent.healthy = False
        await supervisor.check_health()
        await asyncio.sleep(0)
        status = supervisor.agent_status()["worker-pool"]
        assert (status["state"], status["up"], status["healthy"]) == ("restarting", False, False)
        assert status["next_restart_at"] == (time.now + timedelta(seconds=1)).isoformat()
        await supervisor.check_health()  # a restart going on is not started twice
        assert time.slept == [1]
        await asyncio.wait_for(supervisor.stop_all(), 5)
        assert supervisor._agents["worker-pool"].restarting is None

    asyncio.run(run())
