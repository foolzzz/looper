"""The ManagerFleet: every enabled Manager runs, with its own ops and token; a change is applied, a disabled one is
retired without losing a message or a task's last report (``looper/agents/managers.py``), on a real PostgreSQL.

No runtime process starts: an agent starts its Claude lazily, and the tests stub what a message would run.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.message import (
    AgentAddress, MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address, manager_address,
)
from tests.db_support import DSN, run_db, until

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

WEB = channel_address("web")
TASK = AgentAddress(agent_type="task", agent_id="task-t1")
REVIEW = "manager-review"


async def _insert(pool: Any, key: str, *, enabled: bool = True) -> str:
    manager_id = f"manager-{key}"
    await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled) VALUES ($1, $2, 'Be.', $3)",
                       manager_id, key.title(), enabled)
    return manager_id


async def _set_enabled(pool: Any, manager_id: str, enabled: bool) -> None:
    await pool.execute("UPDATE managers SET enabled = $2, revision = revision + 1 WHERE id = $1", manager_id, enabled)


class Rig:
    def __init__(self, root: Path, **supervisor_args: Any) -> None:
        from looper.agents.bus import MessageBus
        from looper.agents.managers import ManagerFleet
        from looper.agents.ops.service import Platform
        from looper.agents.supervisor import AgentSupervisor
        from looper.agents.tiers.manager import ManagerConfig

        self.root = root
        self.bus = MessageBus(runtime_root=root)
        self.supervisor = AgentSupervisor(self.bus, root, **supervisor_args)
        platform = Platform(bus=self.bus, task_router=SimpleNamespace(has_planner=lambda task_id: False),
                            supervisor=self.supervisor, runtime_root=root)
        self.fleet = ManagerFleet(platform=platform, supervisor=self.supervisor, runtime_root=root,
                                  url="http://127.0.0.1:1", config=ManagerConfig(claude_bin="claude", work_dir=root),
                                  broadcaster=None)

    async def up(self) -> "Rig":
        await self.fleet.load()
        await self.supervisor.start_all()
        await self.fleet.sweep()
        return self

    async def down(self) -> None:
        await self.fleet.stop()
        await self.supervisor.stop_all()


@contextlib.asynccontextmanager
async def rig(root: Path, **supervisor_args: Any):
    r = await Rig(root, **supervisor_args).up()
    try:
        yield r
    finally:
        await r.down()


def _user(manager_id: str, text: str = "hi", n: int = 0) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=WEB, target=manager_address(manager_id), message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"session_id": "chat-1"}}, correlation_id=f"corr-{n}",
        metadata={"origin_channel_id": "channel-web"})


def _report(manager_id: str, message_type: MessageType, payload: dict[str, Any], corr: str) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=TASK,
        target=manager_address(manager_id), message_type=message_type, payload=payload, correlation_id=corr,
        metadata={"origin_channel_id": "channel-web"})


def _block_handle(monkeypatch: pytest.MonkeyPatch, manager_id: str) -> tuple[asyncio.Event, list[MessageEnvelope]]:
    """``_handle`` of ``manager_id`` waits for the event; the others run as they are. The envelopes it saw."""
    from looper.agents.tiers.manager import ManagerAgent

    gate, seen = asyncio.Event(), []
    original = ManagerAgent._handle

    async def handle(self: Any, envelope: MessageEnvelope) -> None:
        if self.agent_id == manager_id:
            seen.append(envelope)
            await gate.wait()
            return
        await original(self, envelope)

    monkeypatch.setattr(ManagerAgent, "_handle", handle)
    return gate, seen


def _journal_open(root: Path) -> set[str]:
    from looper.agents.bus import MessageJournal

    return {e.message_id for e in MessageJournal(root / ".local" / "agent-bus").undelivered()}


def test_load_builds_only_the_default_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        async with rig(tmp_path) as r:
            assert [a.agent_id for a in r.fleet.agents()] == ["manager"]
            assert r.fleet.default is r.fleet.get("manager")
            assert r.fleet.ops("manager")._turn_file.parts[-2:] == (".local", "manager-turn.json")
            assert set(r.supervisor.agent_status()) == {"manager"}

    run_db(body, monkeypatch)


def test_each_manager_has_its_own_ops_and_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        await _insert(pool, "aaa")
        await _insert(pool, "bbb")
        await _insert(pool, "off", enabled=False)
        async with rig(tmp_path) as r:
            assert [a.agent_id for a in r.fleet.agents()] == ["manager", "manager-aaa", "manager-bbb"]
            a, b = r.fleet.ops("manager-aaa"), r.fleet.ops("manager-bbb")
            assert a is not b and a._token != b._token
            assert r.fleet.ops_for("Bearer " + a._token) is a
            assert r.fleet.ops_for("Bearer " + b._token) is b
            assert r.fleet.ops_for("Bearer " + r.fleet.ops("manager")._token) is r.fleet.ops("manager")
            assert r.fleet.ops_for("Bearer nope") is None and r.fleet.ops_for(None) is None
            assert a._turn_file.name == "manager-aaa-turn.json"
            assert r.fleet.ops("manager-off") is None

    run_db(body, monkeypatch)


def test_at_most_eight_managers_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    caplog: pytest.LogCaptureFixture) -> None:
    async def body(pool: Any) -> None:
        for i in range(9):
            await _insert(pool, f"m{i}")
        with caplog.at_level(logging.ERROR):
            async with rig(tmp_path) as r:
                assert len(r.fleet.agents()) == 8
        skipped = [rec.getMessage() for rec in caplog.records if "not started" in rec.getMessage()]
        assert len(skipped) == 2 and "at most 8 Managers run at once" in skipped[0]

    run_db(body, monkeypatch)


def test_apply_stops_and_starts_a_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        review = await _insert(pool, "review")
        async with rig(tmp_path) as r:
            assert review in r.supervisor.agent_status()
            await _set_enabled(pool, review, False)
            await r.fleet.apply(review)
            assert review not in r.supervisor.agent_status() and r.fleet.get(review) is None
            assert r.fleet.state(review)["running"] is False and r.fleet.state(review)["sync"] == "ok"
            await _set_enabled(pool, review, True)
            await r.fleet.apply(review)
            assert r.supervisor.agent_status()[review]["healthy"] is True
            assert r.fleet.get(review).is_healthy()
            await r.fleet.apply("manager")
            assert r.fleet.default.is_healthy() and len(r.fleet.agents()) == 2

    run_db(body, monkeypatch)


def test_a_deleted_manager_is_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        from looper.db import managers

        review = await _insert(pool, "review")
        async with rig(tmp_path) as r:
            assert await managers.delete_manager(review) == "deleted"
            await r.fleet.apply(review)
            assert review not in r.supervisor.agent_status() and r.fleet.get(review) is None

    run_db(body, monkeypatch)


def test_disable_drops_queued_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        review = await _insert(pool, "review")
        gate, seen = _block_handle(monkeypatch, review)
        async with rig(tmp_path) as r:
            first, second, third = _user(review, n=1), _user(review, n=2), _user(review, n=3)
            await r.bus.publish(first)
            await until(lambda: len(seen) == 1, "the loop to block on the first", timeout=5)
            await r.bus.publish(second)
            await r.bus.publish(third)
            assert r.bus.pending_count(review) == 2
            await _set_enabled(pool, review, False)
            await r.fleet.apply(review)
            await _set_enabled(pool, review, True)
            await r.fleet.apply(review)
            gate.set()
            await asyncio.sleep(0.05)
            assert [e.message_id for e in seen] == [first.message_id]
            assert r.bus.pending_count(review) == 0
            open_ids = {e.message_id for e in r.bus.journal.undelivered()}
            assert not open_ids & {second.message_id, third.message_id}
            replies = []
            queue = r.bus.get_queue(WEB.agent_id)
            while not queue.empty():
                replies.append(queue.get_nowait())
            answers = [e for e in replies if e.message_type == MessageType.AGENT_REPLY]
            # The one in flight gets its stopped reply; the queued ones are told the Manager is off.
            (stopped,) = [e for e in answers if e.payload["outcome"] == "stopped"]
            assert stopped.reply_to == first.message_id
            turned_off = [e for e in answers if e is not stopped]
            assert sorted(e.reply_to for e in turned_off) == sorted([second.message_id, third.message_id])
            assert all("turned off" in e.payload["text"] for e in turned_off)
            assert all(e.metadata["origin_channel_id"] == "channel-web" for e in turned_off)

    run_db(body, monkeypatch)


def _stub_answer(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, list[MessageEnvelope]]:
    from looper.agents.tiers.manager import ManagerAgent

    gate, seen = asyncio.Event(), []

    async def answer(self: Any, envelope: MessageEnvelope, token: Any) -> None:
        seen.append(envelope)
        await gate.wait()

    monkeypatch.setattr(ManagerAgent, "_answer_user_message", answer)
    return gate, seen


def test_disable_settles_the_message_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        from looper.agents.bus import MessageBus
        from looper.agents.tiers import manager as manager_module

        review = await _insert(pool, "review")
        _, seen = _stub_answer(monkeypatch)
        recorded: list[int] = []
        real_cancel = manager_module.cancel_and_wait

        async def cancel(task: Any, **kwargs: Any) -> None:
            agent = r.fleet.get(review)
            if agent is not None:
                recorded.append(len(agent.stopped_in_flight))
            await real_cancel(task, **kwargs)

        monkeypatch.setattr(manager_module, "cancel_and_wait", cancel)
        async with rig(tmp_path) as r:
            message = _user(review)
            agent = r.fleet.get(review)
            published: list[MessageEnvelope] = []
            publish = r.bus.publish

            async def spy(envelope: MessageEnvelope) -> Any:
                published.append(envelope)
                return await publish(envelope)

            monkeypatch.setattr(r.bus, "publish", spy)
            await r.bus.publish(message)
            await until(lambda: len(seen) == 1, "the answer to block", timeout=5)
            await _set_enabled(pool, review, False)
            await r.fleet.apply(review)
            assert [e.message_id for e, _ in agent.stopped_in_flight] == [message.message_id]
            # The chat gets the stopped turn's final reply, not a silence until its stream times out.
            (reply,) = [e for e in published if e.message_type == MessageType.AGENT_REPLY]
            assert reply.reply_to == message.message_id and reply.payload["outcome"] == "stopped"
            assert reply.source == manager_address(review) and reply.target == WEB
            assert recorded and recorded[0] == 1  # read before the loop was cancelled
            await _set_enabled(pool, review, True)
            await r.fleet.apply(review)
            assert message.message_id not in _journal_open(tmp_path)
            again = r.fleet.get(review)
            assert again is not agent
        # The service restarts: nothing is replayed to the Manager.
        bus = MessageBus(runtime_root=tmp_path)
        await bus.start()
        assert bus.pending_count(review) == 0
        await bus.shutdown()

    run_db(body, monkeypatch)


def test_a_shutdown_still_replays_the_message_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        from looper.agents.bus import MessageBus

        review = await _insert(pool, "review")
        _, seen = _stub_answer(monkeypatch)
        r = await Rig(tmp_path).up()
        message = _user(review)
        await r.bus.publish(message)
        await until(lambda: len(seen) == 1, "the answer to block", timeout=5)
        await r.fleet.stop()
        await r.supervisor.stop_all()
        bus = MessageBus(runtime_root=tmp_path)
        await bus.start()
        assert bus.pending_count(review) == 1
        await bus.shutdown()

    run_db(body, monkeypatch)


def _watch_default(monkeypatch: pytest.MonkeyPatch, *, hold: bool) -> tuple[asyncio.Event, list[MessageEnvelope]]:
    """The default Manager's loop records the messages it is given (not lifecycle events); with ``hold`` it then
    waits on the event, so what it was given stays undelivered. Without it the real handler runs."""
    from looper.agents.tiers.manager import ManagerAgent

    gate, seen = asyncio.Event(), []
    original = ManagerAgent._handle

    async def handle(self: Any, envelope: MessageEnvelope) -> None:
        if self.agent_id == "manager" and envelope.message_type != MessageType.AGENT_LIFECYCLE:
            seen.append(envelope)
            if hold:
                await gate.wait()
                return
        await original(self, envelope)

    monkeypatch.setattr(ManagerAgent, "_handle", handle)
    return gate, seen


def _block_goal_status(monkeypatch: pytest.MonkeyPatch, manager_id: str) -> tuple[asyncio.Event, list[Any]]:
    from looper.agents.tiers.manager import ManagerAgent

    gate, seen = asyncio.Event(), []
    original = ManagerAgent._handle_goal_status

    async def goal_status(self: Any, envelope: MessageEnvelope) -> None:
        if self.agent_id == manager_id:
            seen.append(envelope)
            await gate.wait()
            return
        await original(self, envelope)

    monkeypatch.setattr(ManagerAgent, "_handle_goal_status", goal_status)
    return gate, seen


def _reports(manager_id: str) -> tuple[MessageEnvelope, MessageEnvelope, MessageEnvelope]:
    notice = {"channel": "telegram", "chat": "42", "kind": "chat", "mention": None, "text": "Task done."}
    return (_report(manager_id, MessageType.GOAL_STATUS, {"goal_id": "t1", "status": "running", "summary": "half"},
                    "c-1"),
            _report(manager_id, MessageType.GOAL_STATUS,
                    {"goal_id": "t1", "status": "completed", "summary": "done", "notice": notice}, "c-1"),
            _report(manager_id, MessageType.TASK_ACCEPTED, {"step_count": 2}, "c-1"))


def _same(handed: list[MessageEnvelope], originals: tuple[MessageEnvelope, ...]) -> None:
    assert len(handed) == len(originals)
    for sent, original in zip(sorted(handed, key=lambda e: e.payload.get("status", "z")),
                              sorted(originals, key=lambda e: e.payload.get("status", "z"))):
        assert sent.message_type == original.message_type and sent.payload == original.payload
        assert sent.correlation_id == original.correlation_id
        assert sent.message_id != original.message_id
        assert sent.metadata["handed_over_from"] == REVIEW and sent.target == MANAGER_ADDRESS


def test_disable_hands_task_reports_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        from looper.agents.bus import MessageBus

        review = await _insert(pool, "review")
        gate, seen = _block_goal_status(monkeypatch, review)
        _, default_seen = _watch_default(monkeypatch, hold=True)
        originals = _reports(review)
        async with rig(tmp_path) as r:
            for envelope in originals:
                await r.bus.publish(envelope)
                if envelope is originals[0]:
                    await until(lambda: len(seen) == 1, "the loop to block on a report", timeout=5)
            await _set_enabled(pool, review, False)
            await r.fleet.apply(review)
            await until(lambda: len(default_seen) == 1, "the default Manager to be given a report", timeout=5)
            handed = default_seen + [e for e in r.bus.get_queue("manager")._queue]  # the rest wait in its queue
            _same(handed, originals)
            open_ids = _journal_open(tmp_path)
            assert not open_ids & {e.message_id for e in originals}
            assert {e.message_id for e in handed} <= open_ids
        bus = MessageBus(runtime_root=tmp_path)
        await bus.start()
        assert bus.pending_count(review) == 0
        replayed = bus.take_queue("manager")
        _same([e for e in replayed if e.message_type != MessageType.AGENT_LIFECYCLE], originals)
        await bus.shutdown()
        gate.set()

    run_db(body, monkeypatch)


def test_the_default_manager_relays_a_handed_over_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        review = await _insert(pool, "review")
        gate, seen = _block_goal_status(monkeypatch, review)
        originals = _reports(review)
        async with rig(tmp_path) as r:
            for envelope in originals:
                await r.bus.publish(envelope)
                if envelope is originals[0]:
                    await until(lambda: len(seen) == 1, "the loop to block on a report", timeout=5)
            await _set_enabled(pool, review, False)
            await r.fleet.apply(review)
            notices = r.bus.get_queue(channel_address("telegram").agent_id)
            await until(lambda: any(e.message_type == MessageType.CHAT_NOTICE for e in list(notices._queue)),
                         "the notice", timeout=5)
            notice = next(e for e in notices._queue if e.message_type == MessageType.CHAT_NOTICE)
            assert notice.payload["text"] == "Task done." and notice.payload["chat"] == "42"
        gate.set()

    run_db(body, monkeypatch)


def test_a_crash_between_hand_over_and_settle_hands_over_again(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        from looper.agents import managers as managers_module
        from looper.agents.bus import MessageBus

        review = await _insert(pool, "review")
        gate, seen = _block_goal_status(monkeypatch, review)
        seen2 = _watch_default(monkeypatch, hold=True)[1]
        monkeypatch.setattr(managers_module, "RETRY_DELAYS", (3600,))
        in_flight = _reports(review)[0]
        r = await Rig(tmp_path).up()
        await r.bus.publish(in_flight)
        await until(lambda: len(seen) == 1, "the loop to block on a report", timeout=5)
        real_settle = r.bus.settle

        def settle(message_id: str) -> None:
            if message_id == in_flight.message_id:
                raise OSError("crash")
            real_settle(message_id)

        monkeypatch.setattr(r.bus, "settle", settle)
        await _set_enabled(pool, review, False)
        await r.fleet.apply(review)
        assert r.fleet.state(review)["sync"] == "pending"
        assert in_flight.message_id in _journal_open(tmp_path)
        await r.down()
        # The service restarts: the report is replayed to the Manager that no longer runs, and the sweep hands it over.
        seen2.clear()
        r2 = await Rig(tmp_path).up()
        try:
            await until(lambda: len(seen2) >= 1, "the default Manager to be given the report", timeout=5)
            assert seen2[0].payload == in_flight.payload and seen2[0].metadata["handed_over_from"] == review
            assert in_flight.message_id not in {e.message_id for e in r2.bus.journal.undelivered()}
        finally:
            await r2.down()

    run_db(body, monkeypatch)


def test_a_failed_apply_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        from looper.agents import managers as managers_module

        review = await _insert(pool, "review")
        monkeypatch.setattr(managers_module, "RETRY_DELAYS", (0,))
        real = managers_module.db_managers.get_manager
        failures = [1]

        async def get_manager(manager_id: str, **kwargs: Any) -> Any:
            if manager_id == review and failures[0]:
                failures[0] -= 1
                raise ConnectionError("database down")
            return await real(manager_id, **kwargs)

        async with rig(tmp_path) as r:
            await _set_enabled(pool, review, False)
            monkeypatch.setattr(managers_module.db_managers, "get_manager", get_manager)
            await r.fleet.apply(review)
            state = r.fleet.state(review)
            assert state["sync"] == "pending" and "database down" in state["sync_error"]
            assert r.fleet.get(review) is not None  # still in force until the retry
            await until(lambda: r.fleet.state(review)["sync"] == "ok", "the retry", timeout=5)
            assert r.fleet.get(review) is None and review not in r.supervisor.agent_status()
            assert not r.fleet._retries

    run_db(body, monkeypatch)


def test_a_manager_enabled_at_the_limit_is_started_when_a_place_frees(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        from looper.agents import managers as managers_module

        monkeypatch.setattr(managers_module, "RETRY_DELAYS", (0,))
        monkeypatch.setattr(managers_module, "RETRY_LATER_SECONDS", 0.05)
        for i in range(7):
            await _insert(pool, f"m{i}")
        late = await _insert(pool, "late", enabled=False)
        async with rig(tmp_path) as r:
            assert len(r.fleet.agents()) == 8
            await _set_enabled(pool, late, True)
            await r.fleet.apply(late)  # the place of m0 is not free yet
            state = r.fleet.state(late)
            assert state["sync"] == "pending" and "at most 8 Managers run at once" in state["sync_error"]
            assert r.fleet.get(late) is None
            await _set_enabled(pool, "manager-m0", False)
            await r.fleet.apply("manager-m0")
            await until(lambda: r.fleet.state(late)["sync"] == "ok", "the retry")
            assert r.fleet.get(late) is not None and r.fleet.get("manager-m0") is None
            assert not r.fleet._retries

    run_db(body, monkeypatch)


def test_a_newer_apply_supersedes_the_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        from looper.agents import managers as managers_module

        review = await _insert(pool, "review")
        monkeypatch.setattr(managers_module, "RETRY_DELAYS", (3600,))
        real = managers_module.db_managers.get_manager

        async def get_manager(manager_id: str, **kwargs: Any) -> Any:
            raise ConnectionError("database down")

        async with rig(tmp_path) as r:
            monkeypatch.setattr(managers_module.db_managers, "get_manager", get_manager)
            await r.fleet.apply(review)
            first = r.fleet._retries[review]
            await r.fleet.apply(review)
            second = r.fleet._retries[review]
            assert first is not second and first.cancelled() and len(r.fleet._retries) == 1
            monkeypatch.setattr(managers_module.db_managers, "get_manager", real)
            await r.fleet.apply(review)
            assert r.fleet.state(review)["sync"] == "ok" and not r.fleet._retries and second.cancelled()

    run_db(body, monkeypatch)


def test_is_running_is_false_from_the_first_step_of_a_retire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        review = await _insert(pool, "review")
        async with rig(tmp_path) as r:
            assert r.fleet.is_running(review)
            seen: list[bool] = []
            real = r.supervisor.stop_agent

            async def stop_agent(agent_id: str) -> None:
                seen.append(r.fleet.is_running(agent_id))
                await real(agent_id)

            monkeypatch.setattr(r.supervisor, "stop_agent", stop_agent)
            await _set_enabled(pool, review, False)
            await r.fleet.apply(review)
            assert seen == [False] and not r.fleet.is_running(review)

    run_db(body, monkeypatch)


def test_disable_cancels_a_pending_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        review = await _insert(pool, "review")
        release = asyncio.Event()

        async def sleep(delay: float) -> None:
            await release.wait()

        async with rig(tmp_path, sleep=sleep) as r:
            agent = r.fleet.get(review)
            agent._running = False  # unhealthy
            await r.supervisor.check_health()
            entry = r.supervisor._agents[review]
            restarting = entry.restarting
            assert restarting is not None
            await _set_enabled(pool, review, False)
            await r.fleet.apply(review)
            assert restarting.done() and restarting.cancelled()
            release.set()
            await asyncio.sleep(0.05)
            assert review not in r.supervisor.agent_status() and r.fleet.get(review) is None
            assert not agent.is_healthy()

    run_db(body, monkeypatch)


def test_start_agent_that_fails_is_restarted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        release = asyncio.Event()

        async def sleep(delay: float) -> None:
            await release.wait()

        class Broken:
            agent_id = "manager-broken"
            agent_address = manager_address("manager-broken")

            async def start(self, bus: Any) -> None:
                raise RuntimeError("no start")

            async def stop(self) -> None:
                pass

            def is_healthy(self) -> bool:
                return False

        async with rig(tmp_path, sleep=sleep) as r:
            assert await r.supervisor.start_agent(Broken()) is False
            assert r.supervisor._agents["manager-broken"].restarting is not None
            await r.supervisor.stop_agent("manager-broken")
            assert "manager-broken" not in r.supervisor.agent_status()
            release.set()

    run_db(body, monkeypatch)


def test_binding_report_sums_the_managers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        review = await _insert(pool, "review")
        async with rig(tmp_path) as r:
            r.fleet.ops("manager")._mismatch("not_this_turn", None, "looper_a")
            r.fleet.ops(review)._mismatch("not_this_turn", None, "looper_b")
            report = r.fleet.binding_report()
            assert set(report) == {"enforced", "mismatches", "by"} and report["mismatches"] == 2
            assert {(item["manager"], item["tool"]) for item in report["by"]} == {
                ("manager", "looper_a"), (review, "looper_b")}

    run_db(body, monkeypatch)


def test_a_message_reaches_its_own_manager_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        review = await _insert(pool, "review")
        gate, seen = _block_handle(monkeypatch, review)
        async with rig(tmp_path) as r:
            await r.bus.publish(_user(review, n=1))
            await until(lambda: len(seen) == 1, "the loop to take the first", timeout=5)
            await r.bus.publish(_user(review, n=2))
            assert r.bus.pending_count(review) == 1
            assert not [e for e in list(r.bus.get_queue("manager")._queue)
                        if e.message_type == MessageType.USER_MESSAGE]
            gate.set()

    run_db(body, monkeypatch)
